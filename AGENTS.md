# Codex Project Guide

## Project Purpose

This repository contains a Python Azure Functions app for Talend alert noise suppression. It polls Talend task and plan executions, classifies transient failures versus valid failures, retries safe transient failures, stores alert rows, and sends a daily digest email.

The primary runtime file is `function_app.py`. Keep changes scoped and favor the current single-file structure unless a change clearly benefits from splitting modules.

## Runtime Shape

- Azure Functions v4 Python app using timer triggers.
- `poll_talend_alerts` runs every 10 minutes and scans latest Talend executions.
- `send_daily_digest` runs once daily and emails unsent valid alerts.
- Storage prefers Azure SQL when `AZURE_SQL_CONNECTION_STRING` is configured, otherwise falls back to SQLite at `LOCAL_ALERT_DB`.
- OpenAI summarization is optional and controlled by environment variables.

## Important Files

- `function_app.py`: application logic, Talend client, stores, retry/classification helpers, digest email.
- `requirements.txt`: Azure Functions and Python dependencies.
- `host.json`: Azure Functions host/runtime configuration.
- `local.settings.json`: local secrets/settings; do not commit secrets or copy values into docs.
- `alert_recipients.json`: local recipient config. At the time this guide was added, this file contained merge-conflict markers; resolve deliberately before relying on digest email locally.
- `.funcignore`: excludes local-only files from Azure Functions deployment.
- `.agents/skills/talend-ans-agent/SKILL.md`: repo-local Codex skill for future work on this app.

## Talend API Flow

All Talend API calls use:

- `Content-Type: application/json`
- `Authorization: Bearer <personal_access_token>`

The base URL is region-specific, currently `https://api.us.cloud.talend.com` by default through `Settings.talend_region`.

### Shared Entry Point

Start each poll with:

```text
GET /processing/executables/tasks/executions
```

Use `items` from the response. Group by `taskId` and keep only the most recent execution per task, scored by `finishTimestamp`, then `startTimestamp`, then `triggerTimestamp`.

Only process failed states:

- `EXECUTION_FAILED`
- `DEPLOY_FAILED`
- `EXECUTION_TERMINATED`
- `EXECUTION_REJECTED`

For every failed latest execution, pass its `executionId` to:

```text
GET /monitoring/observability/executions/{executionId}/component
```

Use the component response for stack traces, component names, `connector_type`, `connector_id`, `connector_label`, task/artifact name, and retry classification context.

### Manual Task Flow

For `executionType == "MANUAL"`:

1. Read the failed execution from `/processing/executables/tasks/executions`.
2. Extract `taskId`, `executionId`, `executionStatus`, `executionType`, `errorMessage`, and timestamps.
3. Call `/monitoring/observability/executions/{executionId}/component`.
4. Classify the failure as either `retryable_noise` or `valid_failure`.
5. If retryable, re-run the task with:

```text
POST /processing/executions
Payload: {"executable": "<taskId>", "logLevel": "WARN"}
Response: {"executionId": "<newExecutionId>"}
```

Retryable task failures should be transient platform, engine, network, timeout, or connectivity problems. Component exceptions, data issues, permissions, syntax problems, and ambiguous failures should remain valid human alerts.

### Master Job Flow

Master Jobs are still `MANUAL` executions, but the component payload identifies child jobs through `tRunJob` components.

1. Follow the Manual Task flow through the component metrics call.
2. Detect Master Jobs when any component has `connector_type == "tRunJob"`.
3. Use `connector_id` numeric suffixes such as `tRunJob_1`, `tRunJob_2`, and `tRunJob_3` to infer upstream-to-downstream order.
4. Use `connector_label` when available for human-readable child job names.
5. Treat the first `tRunJob` with a stack trace as the failed child job.
6. Include downstream child jobs in the alert as jobs that should not be expected to run after the upstream failure.

### Plan Flow

For `executionType == "PLAN"`, keep the normal latest-execution grouping from the shared entry point, then enrich plan context in this order:

1. `GET /monitoring/observability/executions/{executionId}/component`
   - Use the plan execution's component metrics and error context.
2. `GET /processing/executables/plans/executions`
   - Match rows by `planId`.
   - Extract the matching plan `executionId`; this is the `planExecutionId`.
3. `GET /processing/executions/plans/{planExecutionId}/steps`
   - Extract step execution statuses and failed `id`/step ID.
4. `GET /orchestration/executables/plans/{planId}`
   - Extract plan `name`.
   - Extract plan `executable`; this value is needed to retry the plan.
   - Extract `chart`, `nextStep`, and `flows` to map step IDs to step names and task names.

Use the joined plan data to report:

- plan name
- failed step ID and step name
- task names attached to the failed step
- downstream step names
- downstream task names that were not expected to run

If a plan failure is retryable, re-run the plan with:

```text
POST /processing/executions/plans
Payload: {"executable": "<plan executable from /orchestration/executables/plans/{planId}>"}
Response: {"executionId": "<newPlanExecutionId>"}
```

When adding support for a new Talend response shape, make parsing tolerant of missing keys and keep existing Manual, Master Job, and Plan flows working.

## Implementation Rules

- Keep all deployment/runtime knobs environment-driven through `Settings`.
- Do not hardcode PATs, SMTP credentials, OpenAI keys, workspace IDs, environment IDs, or recipient addresses in source.
- When adding alert fields, update both `SqliteAlertStore` and `AzureSqlAlertStore` schemas, inserts, and migrations together.
- Keep SQLite as a usable local fallback even when Azure SQL changes are made.
- Keep classification conservative: ambiguous failures should be treated as `valid_failure`.
- Only retry failures classified as `retryable_noise`; do not retry data, permission, syntax, or component exceptions.
- Preserve `STORE_RETRYABLE_NOISE` behavior: retryable noise is skipped from storage unless explicitly enabled.
- Preserve digest idempotency through `email_sent` and `email_sent_at_utc`.
- Keep email output simple HTML that works in common mail clients.
- Avoid broad refactors unless tests or a specific feature require them.

## Verification

There is no dedicated test suite yet. For small logic changes, run at least:

```powershell
python -m compileall function_app.py
```

For dependency/runtime changes, also run the Azure Functions host locally when possible:

```powershell
.\.venv\Scripts\python -m pip install -r requirements.txt
func host start
```

Networked Talend/OpenAI/SMTP behavior usually requires real environment variables. Prefer adding small isolated unit tests before changing classification, parsing, retry, or storage behavior in ways that are hard to verify manually.

## Future Codex Notes

- Start by reading this file and `function_app.py`.
- Check for unresolved merge markers before editing JSON or Python.
- Treat `local.settings.json` as sensitive.
- If Git commands fail with a safe-directory/dubious-ownership message, do not force Git config changes unless the user approves.
- Use `rg` for search and keep edits minimal.

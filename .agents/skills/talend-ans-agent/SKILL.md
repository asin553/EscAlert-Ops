---
name: talend-ans-agent
description: Work on this Talend Alert Noise Suppression Azure Functions app, including Talend task/plan parsing, retry classification, alert storage, digest email, and optional OpenAI summaries.
---

# Talend ANS Agent Skill

Use this skill when modifying the Talend Alert Noise Suppression source code in this repository.

## First Read

1. Read `AGENTS.md`.
2. Read `function_app.py`.
3. Check whether `alert_recipients.json` or any edited file contains unresolved merge markers.

## Code Map

- `Settings`: all environment-driven runtime configuration.
- `TalendClient`: Talend API calls.
- `SqliteAlertStore` and `AzureSqlAlertStore`: duplicate alert persistence contracts; keep schemas aligned.
- `EmailNotifier`: daily digest SMTP delivery.
- `OpenAIErrorSummarizer`: optional JSON-mode summary fallback.
- Decision helpers: retry/noise classification, rule-based summaries, master-job dependency parsing, plan dependency parsing.
- Timer triggers: `poll_talend_alerts` and `send_daily_digest`.

## Endpoint Flow To Preserve

Shared polling starts with `GET /processing/executables/tasks/executions`. Read response `items`, group by `taskId`, and keep the latest run using `finishTimestamp`, `startTimestamp`, then `triggerTimestamp`.

For every latest failed execution, call `GET /monitoring/observability/executions/{executionId}/component`.

For `MANUAL` task failures:

1. Use `taskId`, `executionId`, `executionStatus`, `executionType`, `errorMessage`, and component metrics to classify the failure.
2. Retry only transient network/engine/platform failures with `POST /processing/executions` and payload `{"executable": "<taskId>", "logLevel": "WARN"}`.
3. Treat data, component, permission, syntax, and ambiguous failures as valid alerts.

For `MANUAL` Master Jobs:

1. Detect child jobs from component metrics where `connector_type == "tRunJob"`.
2. Order children by numeric suffix in `connector_id`, such as `tRunJob_1` before `tRunJob_2`.
3. Use the first `tRunJob` with a stack trace as the failed child.
4. Report later `tRunJob` components as downstream jobs that should not be expected to run.

For `PLAN` failures, enrich context in this exact order:

1. `GET /monitoring/observability/executions/{executionId}/component`
2. `GET /processing/executables/plans/executions`, matching by `planId` to find `planExecutionId`
3. `GET /processing/executions/plans/{planExecutionId}/steps`, extracting failed step IDs/status
4. `GET /orchestration/executables/plans/{planId}`, extracting plan `name`, plan `executable`, step names, task names, `chart`, `nextStep`, and `flows`

For retryable plan failures, call `POST /processing/executions/plans` with payload `{"executable": "<plan executable>"}` where the executable comes from `/orchestration/executables/plans/{planId}`.

## Change Checklist

- Keep new settings in `Settings` with safe defaults.
- Keep Talend parsing tolerant of missing keys and alternate sample response shapes.
- Update both storage backends when alert row fields change.
- Classify unknown failures as valid failures.
- Retry only transient platform/network failures.
- Preserve digest filtering through `include_in_digest`.
- Do not expose secrets from `local.settings.json`.
- Run `python -m compileall function_app.py` after Python changes.

## Common Extension Points

- New transient failure: add a precise pattern to `TRANSIENT_PATTERNS`.
- New non-retryable failure: add a precise pattern to `NON_RETRIABLE_PATTERNS`.
- New human-readable summary: update `summarize_error_rule_based`.
- New Talend component relationship: extend `parse_master_job_dependency` or add a similarly tolerant helper.
- New plan response shape: adjust `flatten_plan_steps` or `enrich_plan_context` without breaking the linked `chart.nextStep` shape.
- New alert column: update SQLite DDL/migration, SQL Server DDL/migration, insert field lists, digest HTML if user-facing, and any test/sample data.

## Verification

Minimum syntax check:

```powershell
python -m compileall function_app.py
```

For runtime verification, install dependencies and start the Functions host:

```powershell
.\.venv\Scripts\python -m pip install -r requirements.txt
func host start
```

Use real Talend/OpenAI/SMTP calls only when the required environment variables are present and the user expects networked verification.

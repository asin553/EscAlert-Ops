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
- `.codex/skills/talend-ans-agent/SKILL.md`: optional repo-local Codex skill for future work on this app.

## Talend API Assumptions

Preserve support for the sample Talend response shapes already reflected in the code:

- Task executions come from `/processing/executables/tasks/executions` and are read from `items`.
- Latest execution grouping is by `taskId`, scored with `finishTimestamp`, `startTimestamp`, then `triggerTimestamp`.
- Failed states are `EXECUTION_FAILED`, `DEPLOY_FAILED`, `EXECUTION_TERMINATED`, and `EXECUTION_REJECTED`.
- Component metrics come from `/monitoring/observability/executions/{execution_id}/component`.
- Component payloads may include `metrics.items`, `stacktrace`, `connector_type`, `connector_id`, and `connector_label`.
- Master-job chains are inferred from `tRunJob` components ordered by numeric suffix in `connector_id`.
- Plan executions come from `/processing/executables/plans/executions`.
- Plan steps come from `/processing/executions/plans/{plan_execution_id}/steps`.
- Plan definitions come from `/orchestration/executables/plans/{plan_id}` and may include a linked-list-like `chart` with `nextStep` and `flows`.

When adding support for a new Talend response shape, make parsing tolerant of missing keys and keep existing response shapes working.

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

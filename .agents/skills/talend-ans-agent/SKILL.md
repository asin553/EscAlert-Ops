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

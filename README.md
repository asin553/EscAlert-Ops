# Talend Alert Noise Suppression Agent

This project implements an AI-assisted Alert Noise Suppression (ANS) and defect triage agent for Talend Cloud / Talend Management Console (TMC). Talend does not provide a public, native ANS workflow for automatically separating transient platform noise from valid job failures, so this agent adds that operational layer by polling Talend APIs, inspecting execution context, classifying failures, retrying safe transient failures, and summarizing actionable incidents for escalation.

The application is built as a Python Azure Functions app with timer triggers. It uses Talend APIs as external operational tools, optional OpenAI LLM summarization, local or Azure SQL alert persistence, and SMTP-based daily digest delivery.

## Serverless Architecture

<img width="3316" height="1952" alt="image" src="https://github.com/user-attachments/assets/1d7042eb-40af-4bae-987c-104cbeecdba1" />

The Azure resources are deployed under a Subscription and Resource Group. The Function App is the compute layer; the Storage Account supports Azure Functions runtime requirements; Azure SQL stores alert and digest state; Talend Cloud APIs provide execution context and retry operations; the OpenAI layer enriches valid failures with concise summaries; SMTP delivers the final digest to configured recipients.

## What This Agent Does

- Polls recent Talend task and plan executions on a schedule.
- Keeps only the latest execution for each task before evaluating failures.
- Calls Talend observability APIs to inspect failed execution component metrics.
- Distinguishes retryable infrastructure noise from valid job, data, permission, or component failures.
- Automatically retries safe transient task failures through Talend's execution API.
- Automatically retries safe transient plan failures through Talend's plan execution API.
- Detects Master Jobs that use `tRunJob` child-job components and identifies downstream child jobs that may not run after an upstream failure.
- Enriches failed TMC Plan alerts with plan name, failed step, step task names, and downstream impact.
- Stores alert records with idempotency so the same execution is not repeatedly emailed.
- Sends a daily HTML digest of valid alerts to configured recipients.
- Optionally uses an OpenAI model to convert raw Talend errors and stack traces into concise, plain-English summaries.

## Tech Stack

- **Runtime:** Python, Azure Functions v4
- **Scheduling:** Azure Functions timer triggers
- **Cloud APIs:** Talend Cloud / TMC REST APIs
- **AI summarization:** OpenAI Chat Completions-compatible API
- **Storage:** Azure SQL via `pyodbc`, with SQLite fallback for local/dev usage
- **Notifications:** SMTP email digest
- **Agent development context:** Codex with repo-local `AGENTS.md` and `.agents/skills/talend-ans-agent/SKILL.md`

## Repository Structure

```text
.
|-- function_app.py
|-- requirements.txt
|-- host.json
|-- alert_recipients.json
|-- AGENTS.md
|-- .agents/
|   |-- README.md
|   `-- skills/
|       `-- talend-ans-agent/
|           `-- SKILL.md
|-- .funcignore
`-- local.settings.json
```

### Key Files

- `function_app.py`: Main Azure Functions application, Talend API client, alert stores, retry logic, classification helpers, dependency parsing, and digest generation.
- `alert_recipients.json`: Email recipient list for digest delivery.
- `requirements.txt`: Python dependencies for Azure Functions, HTTP calls, and Azure SQL.
- `host.json`: Azure Functions host configuration.
- `local.settings.json`: Local development settings and secrets. Do not commit real secrets.
- `AGENTS.md`: Repo-wide Codex guidance, including Talend API flows and implementation guardrails.
- `.agents/skills/talend-ans-agent/SKILL.md`: Repo-local Codex skill that preserves the editing checklist and endpoint order for future agent-assisted changes.

## Runtime Flow

The application has two timer-triggered workflows:

1. **Polling and ingestion**
   - Runs every 10 minutes.
   - Polls Talend executions.
   - Classifies failures.
   - Retries safe transient failures.
   - Stores valid alert records for the digest.

2. **Daily digest**
   - Runs once per day.
   - Loads unsent alert rows for the target day.
   - Builds an HTML summary.
   - Sends the digest by SMTP.
   - Marks emailed rows as sent.

## Talend API Flow

All Talend API calls use:

```http
Content-Type: application/json
Authorization: Bearer <personal_access_token>
```

The default base URL is:

```text
https://api.us.cloud.talend.com
```

The region is configurable through `TALEND_REGION`.

### Shared Entry Point

Each polling run starts with:

```text
GET /processing/executables/tasks/executions
```

The agent reads `items`, groups executions by `taskId`, and keeps the most recent execution per task using:

1. `finishTimestamp`
2. `startTimestamp`
3. `triggerTimestamp`

Only failed latest executions are evaluated:

- `EXECUTION_FAILED`
- `DEPLOY_FAILED`
- `EXECUTION_TERMINATED`
- `EXECUTION_REJECTED`

For each failed latest execution, the agent calls:

```text
GET /monitoring/observability/executions/{executionId}/component
```

That component payload supplies stack traces, component metadata, connector IDs, connector labels, and task or artifact details used for classification and alert enrichment.

## Manual Task Handling

For `executionType == "MANUAL"`, the agent:

1. Reads the failed task execution from `/processing/executables/tasks/executions`.
2. Extracts `taskId`, `executionId`, `executionStatus`, `executionType`, `errorMessage`, and timestamps.
3. Calls `/monitoring/observability/executions/{executionId}/component`.
4. Classifies the failure as either `retryable_noise` or `valid_failure`.
5. Retries only safe transient failures.

Task retries use:

```text
POST /processing/executions
```

Payload:

```json
{
  "executable": "<taskId>",
  "logLevel": "WARN"
}
```

Expected response:

```json
{
  "executionId": "<newExecutionId>"
}
```

Failures are considered retryable only when they resemble temporary platform, engine, network, connection, or timeout issues. Data errors, Talend component exceptions, permission problems, syntax issues, and ambiguous failures are treated as valid alerts that require human review.

## Master Job Handling

Master Jobs are handled as a specialized form of `MANUAL` execution. The agent identifies them from the component metrics response when one or more components have:

```text
connector_type == "tRunJob"
```

The child-job order is inferred from connector IDs such as:

```text
tRunJob_1 -> tRunJob_2 -> tRunJob_3
```

The first `tRunJob` component with a stack trace is treated as the failed child job. Later `tRunJob` components are reported as downstream jobs that should not be expected to run after the upstream failure.

This allows the digest to explain not only that a Master Job failed, but also where the chain broke and which child jobs were likely skipped as a result.

## Plan Handling

For `executionType == "PLAN"`, the agent keeps the same latest-execution grouping but enriches the alert with plan and step context.

The endpoint order is:

1. Component metrics for the failed plan execution:

   ```text
   GET /monitoring/observability/executions/{executionId}/component
   ```

2. Plan execution lookup:

   ```text
   GET /processing/executables/plans/executions
   ```

   The agent matches by `planId` and extracts the matching plan execution `executionId`, used internally as the `planExecutionId`.

3. Plan step statuses:

   ```text
   GET /processing/executions/plans/{planExecutionId}/steps
   ```

   The agent extracts failed step IDs and execution status.

4. Plan definition:

   ```text
   GET /orchestration/executables/plans/{planId}
   ```

   The agent extracts the plan name, plan executable, step names, task names, and chart structure.

The plan definition's `chart`, `nextStep`, and `flows` fields are used to map step IDs to readable step names and task names. The final alert can include:

- plan name
- failed step ID and step name
- task names attached to the failed step
- downstream step names
- downstream task names that were not expected to run

Plan retries use:

```text
POST /processing/executions/plans
```

Payload:

```json
{
  "executable": "<plan executable>"
}
```

The `executable` value comes from:

```text
GET /orchestration/executables/plans/{planId}
```

## Failure Classification

The agent uses rule-based classification first. Optional OpenAI summarization is used to improve human-readable alert text, not to replace the conservative retry policy.

### Retryable Noise

Examples include:

- remote engine unavailable
- no available cloud engines
- connection reset
- connection timeout
- temporary network failure
- service unavailable
- deployment attempt exhaustion caused by platform/runtime instability

### Valid Failures

Examples include:

- exception in a Talend component
- `TDieException`
- output file already exists
- input file record limit exceeded
- permission denied
- syntax error
- null pointer exception
- unclear or ambiguous failures

The default posture is conservative: if the agent cannot confidently identify a transient platform issue, the failure is escalated as a valid alert.

## Alert Persistence

The app writes alert records through an `AlertStore` abstraction.

### Azure SQL

If `AZURE_SQL_CONNECTION_STRING` is set, the app uses Azure SQL through `pyodbc`.

### SQLite Fallback

If Azure SQL is not configured, or Azure SQL initialization fails at runtime, the app falls back to SQLite using `LOCAL_ALERT_DB`.

SQLite makes local development and lightweight testing possible without requiring cloud database access.

## Email Digest

Recipients are loaded from `alert_recipients.json`:

```json
{
  "emails": [
    "oncall@example.com"
  ]
}
```

SMTP settings are supplied through environment variables. If SMTP settings or recipients are missing, the digest is skipped safely and a warning is logged.

The digest includes alert metadata such as execution time, execution type, task ID, task name, status, summary, plan name, failed step, downstream impact, and Master Job impact.

## OpenAI Summarization

OpenAI summarization is optional. When enabled, the app sends bounded failure context to an OpenAI Chat Completions-compatible endpoint and asks for a concise JSON response containing a plain-English summary.

The implementation is intentionally defensive:

- AI summarization is disabled by default.
- Raw input is truncated by `AI_INPUT_CHAR_LIMIT`.
- If the OpenAI request fails, the app logs the issue and falls back to rule-based summaries.
- Rule-based summaries remain the baseline for known failure patterns.

## Configuration

Configuration is environment-driven through the `Settings` dataclass in `function_app.py`.

| Variable | Purpose | Default |
| --- | --- | --- |
| `TALEND_REGION` | Talend API region used in `https://api.{region}.cloud.talend.com` | `us` |
| `TALEND_PAT` | Talend personal access token | required |
| `TALEND_TASK_EXECUTIONS_LIMIT` | Number of task executions to request | `100` |
| `TALEND_PLAN_EXECUTIONS_LIMIT` | Number of plan executions to request | `100` |
| `REQUEST_TIMEOUT_SECONDS` | Timeout for Talend API calls | `20` |
| `TALEND_WORKSPACE_ID` | Optional workspace filter | empty |
| `TALEND_ENVIRONMENT_ID` | Optional environment filter | empty |
| `ALERT_RECIPIENTS_FILE` | Recipient JSON file path | `alert_recipients.json` |
| `LOCAL_ALERT_DB` | SQLite fallback database path | `/tmp/ans_alerts.db` |
| `AZURE_SQL_CONNECTION_STRING` | Optional Azure SQL connection string | empty |
| `RETRY_ENABLED` | Enable retry for retryable manual task failures | `true` |
| `RETRY_MAX_ATTEMPTS` | Maximum task retry attempts per poll | `1` |
| `PLAN_RETRY_ENABLED` | Enable retry for retryable plan failures | `true` |
| `PLAN_RETRY_MAX_ATTEMPTS` | Maximum plan retry attempts per poll | `1` |
| `STORE_RETRYABLE_NOISE` | Store retryable-noise rows in the database | `false` |
| `SMTP_HOST` | SMTP host | empty |
| `SMTP_PORT` | SMTP port | `587` |
| `SMTP_USERNAME` | SMTP username | empty |
| `SMTP_PASSWORD` | SMTP password | empty |
| `SMTP_SENDER` | Sender email address | empty |
| `SMTP_USE_TLS` | Use STARTTLS for SMTP | `true` |
| `AI_SUMMARIZATION_ENABLED` | Enable OpenAI summarization | `false` |
| `AI_SUMMARIZATION_MODE` | `fallback` or `always` | `fallback` |
| `OPENAI_API_KEY` | OpenAI API key | empty |
| `OPENAI_MODEL` | Model name | `gpt-4.1-mini` |
| `OPENAI_API_BASE_URL` | OpenAI-compatible API base URL | `https://api.openai.com/v1` |
| `OPENAI_CHAT_ENDPOINT` | Chat completions endpoint path | `/chat/completions` |
| `OPENAI_TIMEOUT_SECONDS` | OpenAI request timeout | `20` |
| `AI_INPUT_CHAR_LIMIT` | Max characters of error context sent to OpenAI | `3500` |
| `DIGEST_DAY_OFFSET_DAYS` | Offset for digest target date | `0` |

The Talend endpoint paths are also configurable through environment variables in `Settings`, but the defaults match the current implementation.

## Local Development

Create or activate the virtual environment, then install dependencies:

```powershell
.\.venv\Scripts\python -m pip install -r requirements.txt
```

Run a syntax check:

```powershell
python -m compileall function_app.py
```

Start the Azure Functions host:

```powershell
func host start
```

Local execution requires appropriate values in `local.settings.json` or the active shell environment. Do not commit real tokens, passwords, or connection strings.

## Deployment

The app is intended to be deployed as an Azure Function App. Deployment should include:

- `function_app.py`
- `host.json`
- `requirements.txt`
- any required non-secret runtime config files, such as `alert_recipients.json`

The `.funcignore` file excludes local-only files, virtual environments, editor settings, and Codex agent configuration from the deployment package.

Production settings should be configured in the Azure Function App configuration blade or through infrastructure-as-code, not committed to source.

## Agent Context Files

This repository includes Codex-specific guidance files:

- `AGENTS.md`
- `.agents/skills/talend-ans-agent/SKILL.md`

These files document the endpoint order, response fields, retry rules, parsing assumptions, and implementation guardrails used while developing the agent with Codex and GPT-5.5. They are not runtime dependencies. Their purpose is to make future agent-assisted changes consistent with the existing Talend flows instead of relying on one-off prompt context.

## Safety and Operational Notes

- Keep `TALEND_PAT`, SMTP credentials, OpenAI keys, and database connection strings out of source control.
- Resolve any merge-conflict markers in `alert_recipients.json` before using digest email.
- Keep task and plan retry limits low until production behavior is well understood.
- Prefer conservative classification. A questionable failure should alert a human rather than be repeatedly retried.
- When adding alert fields, update both SQLite and Azure SQL schemas.
- When changing Talend parsing, keep Manual Task, Master Job, and Plan flows compatible.

## Current Limitations

- There is no formal automated test suite yet.
- Talend API response parsing is based on known response shapes and should be extended carefully as new examples are discovered.
- Email delivery depends on external SMTP configuration.
- AI summarization is optional and should be treated as alert enrichment, not the source of truth for retry decisions.

## Project Impact

This agent creates an automated triage layer for Talend operations where a native public ANS/defect-triage workflow is not available. By combining Talend execution context, dependency-aware parsing, conservative retry logic, and LLM-assisted summaries, it reduces noisy alerts, improves failure explainability, and helps operations teams focus on incidents that actually require intervention.

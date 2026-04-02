import json
import logging
import os
import re
import smtplib
import sqlite3
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from email.mime.text import MIMEText
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

import azure.functions as func
import requests

app = func.FunctionApp()


# ----------------------------
# Configuration
# ----------------------------
def _env_bool(name: str, default: bool = False) -> bool:
    return os.getenv(name, str(default)).strip().lower() in {"1", "true", "yes", "on"}


@dataclass
class Settings:
    talend_region: str = os.getenv("TALEND_REGION", "us").lower()
    talend_pat: str = os.getenv("TALEND_PAT", "")
    lookback_limit: int = int(os.getenv("TALEND_TASK_EXECUTIONS_LIMIT", "100"))
    plan_lookback_limit: int = int(os.getenv("TALEND_PLAN_EXECUTIONS_LIMIT", "100"))
    request_timeout_seconds: int = int(os.getenv("REQUEST_TIMEOUT_SECONDS", "20"))

    alert_recipients_file: str = os.getenv("ALERT_RECIPIENTS_FILE", "alert_recipients.json")
    local_db_path: str = os.getenv("LOCAL_ALERT_DB", "/tmp/ans_alerts.db")
    azure_sql_connection_string: str = os.getenv("AZURE_SQL_CONNECTION_STRING", "")

    retry_enabled: bool = _env_bool("RETRY_ENABLED", True)
    retry_max_attempts: int = int(os.getenv("RETRY_MAX_ATTEMPTS", "1"))
    plan_retry_enabled: bool = _env_bool("PLAN_RETRY_ENABLED", True)
    plan_retry_max_attempts: int = int(os.getenv("PLAN_RETRY_MAX_ATTEMPTS", "1"))
    store_retryable_noise: bool = _env_bool("STORE_RETRYABLE_NOISE", False)

    smtp_host: str = os.getenv("SMTP_HOST", "")
    smtp_port: int = int(os.getenv("SMTP_PORT", "587"))
    smtp_username: str = os.getenv("SMTP_USERNAME", "")
    smtp_password: str = os.getenv("SMTP_PASSWORD", "")
    smtp_sender: str = os.getenv("SMTP_SENDER", "")
    smtp_use_tls: bool = _env_bool("SMTP_USE_TLS", True)

    ai_summarization_enabled: bool = _env_bool("AI_SUMMARIZATION_ENABLED", False)
    ai_summarization_mode: str = os.getenv("AI_SUMMARIZATION_MODE", "fallback").lower()  # fallback|always
    openai_api_key: str = os.getenv("OPENAI_API_KEY", "")
    openai_model: str = os.getenv("OPENAI_MODEL", "gpt-4.1-mini")
    openai_api_base_url: str = os.getenv("OPENAI_API_BASE_URL", "https://api.openai.com/v1")
    openai_chat_endpoint: str = os.getenv("OPENAI_CHAT_ENDPOINT", "/chat/completions")
    openai_timeout_seconds: int = int(os.getenv("OPENAI_TIMEOUT_SECONDS", "20"))
    ai_input_char_limit: int = int(os.getenv("AI_INPUT_CHAR_LIMIT", "3500"))

    task_executions_endpoint: str = os.getenv(
        "TASK_EXECUTIONS_ENDPOINT",
        "/processing/executables/tasks/executions",
    )
    execution_component_endpoint_template: str = os.getenv(
        "EXECUTION_COMPONENT_ENDPOINT_TEMPLATE",
        "/monitoring/observability/executions/{execution_id}/component",
    )
    retry_execution_endpoint: str = os.getenv(
        "RETRY_EXECUTION_ENDPOINT",
        "/processing/executions",
    )
    plan_retry_execution_endpoint: str = os.getenv(
        "PLAN_RETRY_EXECUTION_ENDPOINT",
        "/processing/executions/plans",
    )
    plan_executions_endpoint: str = os.getenv(
        "PLAN_EXECUTIONS_ENDPOINT",
        "/processing/executables/plans/executions",
    )
    plan_steps_endpoint_template: str = os.getenv(
        "PLAN_STEPS_ENDPOINT_TEMPLATE",
        "/processing/executions/plans/{plan_execution_id}/steps",
    )
    plan_definition_endpoint_template: str = os.getenv(
        "PLAN_DEFINITION_ENDPOINT_TEMPLATE",
        "/orchestration/executables/plans/{plan_id}",
    )

    @property
    def api_base_url(self) -> str:
        return f"https://api.{self.talend_region}.cloud.talend.com"


# ----------------------------
# Talend API client
# ----------------------------
class TalendClient:
    def __init__(self, settings: Settings) -> None:
        if not settings.talend_pat:
            raise ValueError("Missing TALEND_PAT environment variable.")
        self.settings = settings
        self.session = requests.Session()
        self.session.headers.update(
            {
                "Authorization": f"Bearer {settings.talend_pat}",
                "Content-Type": "application/json",
            }
        )

    def _url(self, endpoint: str) -> str:
        return f"{self.settings.api_base_url}{endpoint}"

    def get_task_executions(self) -> List[Dict[str, Any]]:
        params = {"limit": self.settings.lookback_limit, "offset": 0}
        resp = self.session.get(
            self._url(self.settings.task_executions_endpoint),
            params=params,
            timeout=self.settings.request_timeout_seconds,
        )
        resp.raise_for_status()
        return resp.json().get("items", [])

    def get_component_metrics(self, execution_id: str) -> Dict[str, Any]:
        endpoint = self.settings.execution_component_endpoint_template.format(execution_id=execution_id)
        resp = self.session.get(self._url(endpoint), timeout=self.settings.request_timeout_seconds)
        resp.raise_for_status()
        return resp.json()

    def retry_task(self, task_id: str) -> Optional[str]:
        payload = {"executable": task_id, "logLevel": "WARN"}
        resp = self.session.post(
            self._url(self.settings.retry_execution_endpoint),
            data=json.dumps(payload),
            timeout=self.settings.request_timeout_seconds,
        )
        resp.raise_for_status()
        return resp.json().get("executionId")

    def retry_plan(self, plan_executable_id: str) -> Optional[str]:
        payload = {"executable": plan_executable_id}
        resp = self.session.post(
            self._url(self.settings.plan_retry_execution_endpoint),
            data=json.dumps(payload),
            timeout=self.settings.request_timeout_seconds,
        )
        resp.raise_for_status()
        return resp.json().get("executionId")

    def get_plan_executions(self) -> List[Dict[str, Any]]:
        params = {"limit": self.settings.plan_lookback_limit, "offset": 0}
        resp = self.session.get(
            self._url(self.settings.plan_executions_endpoint),
            params=params,
            timeout=self.settings.request_timeout_seconds,
        )
        resp.raise_for_status()
        return resp.json().get("items", [])

    def get_plan_steps(self, plan_execution_id: str) -> List[Dict[str, Any]]:
        endpoint = self.settings.plan_steps_endpoint_template.format(plan_execution_id=plan_execution_id)
        resp = self.session.get(self._url(endpoint), timeout=self.settings.request_timeout_seconds)
        resp.raise_for_status()
        return resp.json() if isinstance(resp.json(), list) else []

    def get_plan_definition(self, plan_id: str) -> Dict[str, Any]:
        endpoint = self.settings.plan_definition_endpoint_template.format(plan_id=plan_id)
        resp = self.session.get(self._url(endpoint), timeout=self.settings.request_timeout_seconds)
        resp.raise_for_status()
        return resp.json()


# ----------------------------
# Storage: Azure SQL (optional) + SQLite fallback
# ----------------------------
class AlertStore:
    def init(self) -> None:
        raise NotImplementedError

    def exists_alert_key(self, alert_key: str) -> bool:
        raise NotImplementedError

    def insert_alert(self, row: Dict[str, Any]) -> None:
        raise NotImplementedError

    def get_digest_rows(self, digest_day: date) -> List[Dict[str, Any]]:
        raise NotImplementedError

    def mark_emailed(self, ids: List[int]) -> None:
        raise NotImplementedError


class SqliteAlertStore(AlertStore):
    def __init__(self, db_path: str) -> None:
        self.db_path = db_path

    def _conn(self) -> sqlite3.Connection:
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def init(self) -> None:
        with self._conn() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS alerts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    alert_key TEXT UNIQUE NOT NULL,
                    observed_at_utc TEXT NOT NULL,
                    day_utc TEXT NOT NULL,
                    execution_id TEXT NOT NULL,
                    task_id TEXT,
                    task_name TEXT,
                    execution_type TEXT,
                    execution_status TEXT,
                    plan_id TEXT,
                    plan_execution_id TEXT,
                    plan_name TEXT,
                    failed_step_id TEXT,
                    failed_step_name TEXT,
                    downstream_summary TEXT,
                    master_summary TEXT,
                    decision TEXT,
                    include_in_digest INTEGER DEFAULT 1,
                    human_error TEXT,
                    raw_error TEXT,
                    email_sent INTEGER DEFAULT 0,
                    email_sent_at_utc TEXT
                )
                """
            )
            # Lightweight schema migration for existing DB files.
            for ddl in [
                "ALTER TABLE alerts ADD COLUMN task_name TEXT",
                "ALTER TABLE alerts ADD COLUMN include_in_digest INTEGER DEFAULT 1",
            ]:
                try:
                    conn.execute(ddl)
                except sqlite3.OperationalError:
                    pass

    def exists_alert_key(self, alert_key: str) -> bool:
        with self._conn() as conn:
            row = conn.execute("SELECT 1 FROM alerts WHERE alert_key = ?", (alert_key,)).fetchone()
            return row is not None

    def insert_alert(self, row: Dict[str, Any]) -> None:
        with self._conn() as conn:
            conn.execute(
                """
                INSERT INTO alerts (
                    alert_key, observed_at_utc, day_utc, execution_id, task_id, task_name, execution_type, execution_status,
                    plan_id, plan_execution_id, plan_name, failed_step_id, failed_step_name,
                    downstream_summary, master_summary, decision, include_in_digest, human_error, raw_error
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    row["alert_key"],
                    row["observed_at_utc"],
                    row["day_utc"],
                    row["execution_id"],
                    row.get("task_id"),
                    row.get("task_name"),
                    row.get("execution_type"),
                    row.get("execution_status"),
                    row.get("plan_id"),
                    row.get("plan_execution_id"),
                    row.get("plan_name"),
                    row.get("failed_step_id"),
                    row.get("failed_step_name"),
                    row.get("downstream_summary"),
                    row.get("master_summary"),
                    row.get("decision"),
                    row.get("include_in_digest", 1),
                    row.get("human_error"),
                    row.get("raw_error"),
                ),
            )

    def get_digest_rows(self, digest_day: date) -> List[Dict[str, Any]]:
        with self._conn() as conn:
            rows = conn.execute(
                """
                SELECT *
                FROM alerts
                WHERE day_utc = ? AND email_sent = 0 AND include_in_digest = 1
                ORDER BY observed_at_utc ASC
                """,
                (digest_day.isoformat(),),
            ).fetchall()
            return [dict(r) for r in rows]

    def mark_emailed(self, ids: List[int]) -> None:
        if not ids:
            return
        now = datetime.now(timezone.utc).isoformat()
        placeholders = ",".join("?" for _ in ids)
        with self._conn() as conn:
            conn.execute(
                f"UPDATE alerts SET email_sent = 1, email_sent_at_utc = ? WHERE id IN ({placeholders})",
                (now, *ids),
            )


class AzureSqlAlertStore(AlertStore):
    """Optional Azure SQL backend. Activated when AZURE_SQL_CONNECTION_STRING is set."""

    def __init__(self, conn_str: str) -> None:
        self.conn_str = conn_str

    def _conn(self):
        import pyodbc  # Lazy import so local dev works without pyodbc.

        return pyodbc.connect(self.conn_str)

    def init(self) -> None:
        # Keep schema in sync with SqliteAlertStore; using SQL Server syntax.
        ddl = """
        IF NOT EXISTS (SELECT * FROM sysobjects WHERE name='alerts' AND xtype='U')
        CREATE TABLE alerts (
            id INT IDENTITY(1,1) PRIMARY KEY,
            alert_key NVARCHAR(255) UNIQUE NOT NULL,
            observed_at_utc NVARCHAR(64) NOT NULL,
            day_utc NVARCHAR(32) NOT NULL,
            execution_id NVARCHAR(128) NOT NULL,
            task_id NVARCHAR(128),
            task_name NVARCHAR(256),
            execution_type NVARCHAR(64),
            execution_status NVARCHAR(64),
            plan_id NVARCHAR(128),
            plan_execution_id NVARCHAR(128),
            plan_name NVARCHAR(256),
            failed_step_id NVARCHAR(128),
            failed_step_name NVARCHAR(256),
            downstream_summary NVARCHAR(MAX),
            master_summary NVARCHAR(MAX),
            decision NVARCHAR(64),
            include_in_digest BIT DEFAULT 1,
            human_error NVARCHAR(512),
            raw_error NVARCHAR(MAX),
            email_sent BIT DEFAULT 0,
            email_sent_at_utc NVARCHAR(64)
        )
        """
        with self._conn() as conn:
            cur = conn.cursor()
            cur.execute(ddl)
            cur.execute("IF COL_LENGTH('alerts','task_name') IS NULL ALTER TABLE alerts ADD task_name NVARCHAR(256)")
            cur.execute(
                "IF COL_LENGTH('alerts','include_in_digest') IS NULL ALTER TABLE alerts ADD include_in_digest BIT DEFAULT 1"
            )
            conn.commit()

    def exists_alert_key(self, alert_key: str) -> bool:
        with self._conn() as conn:
            row = conn.cursor().execute("SELECT TOP 1 1 FROM alerts WHERE alert_key = ?", alert_key).fetchone()
            return row is not None

    def insert_alert(self, row: Dict[str, Any]) -> None:
        fields = [
            "alert_key", "observed_at_utc", "day_utc", "execution_id", "task_id", "task_name",
            "execution_type", "execution_status", "plan_id", "plan_execution_id", "plan_name",
            "failed_step_id", "failed_step_name", "downstream_summary", "master_summary", "decision",
            "include_in_digest", "human_error", "raw_error",
        ]
        values = [row.get(f) for f in fields]
        placeholders = ",".join("?" for _ in values)
        sql = f"INSERT INTO alerts ({','.join(fields)}) VALUES ({placeholders})"
        with self._conn() as conn:
            conn.cursor().execute(sql, values)
            conn.commit()

    def get_digest_rows(self, digest_day: date) -> List[Dict[str, Any]]:
        with self._conn() as conn:
            cur = conn.cursor()
            rows = cur.execute(
                "SELECT * FROM alerts WHERE day_utc = ? AND email_sent = 0 AND include_in_digest = 1 ORDER BY observed_at_utc ASC",
                digest_day.isoformat(),
            ).fetchall()
            columns = [c[0] for c in cur.description]
            return [dict(zip(columns, row)) for row in rows]

    def mark_emailed(self, ids: List[int]) -> None:
        if not ids:
            return
        now = datetime.now(timezone.utc).isoformat()
        with self._conn() as conn:
            cur = conn.cursor()
            for id_ in ids:
                cur.execute("UPDATE alerts SET email_sent = 1, email_sent_at_utc = ? WHERE id = ?", now, id_)
            conn.commit()


# ----------------------------
# Email notifier
# ----------------------------
class EmailNotifier:
    def __init__(self, settings: Settings, recipients: List[str]) -> None:
        self.settings = settings
        self.recipients = recipients

    def is_enabled(self) -> bool:
        needed = [
            self.settings.smtp_host,
            self.settings.smtp_username,
            self.settings.smtp_password,
            self.settings.smtp_sender,
        ]
        return bool(self.recipients and all(needed))

    def send(self, subject: str, body: str) -> None:
        if not self.is_enabled():
            logging.warning("SMTP settings or recipients are missing; skipping email notification.")
            return

        msg = MIMEText(body, "html")
        msg["Subject"] = subject
        msg["From"] = self.settings.smtp_sender
        msg["To"] = ", ".join(self.recipients)

        with smtplib.SMTP(self.settings.smtp_host, self.settings.smtp_port) as server:
            if self.settings.smtp_use_tls:
                server.starttls()
            server.login(self.settings.smtp_username, self.settings.smtp_password)
            server.sendmail(self.settings.smtp_sender, self.recipients, msg.as_string())


class OpenAIErrorSummarizer:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    def is_enabled(self) -> bool:
        return self.settings.ai_summarization_enabled and bool(self.settings.openai_api_key)

    def summarize(
        self,
        error_message: str,
        component_payload: Dict[str, Any],
        execution: Optional[Dict[str, Any]] = None,
    ) -> Optional[str]:
        if not self.is_enabled():
            return None

        stacktraces = [
            m.get("stacktrace", "") for m in component_payload.get("metrics", {}).get("items", []) if m.get("stacktrace")
        ]
        context = {
            "execution_status": (execution or {}).get("executionStatus", ""),
            "execution_type": (execution or {}).get("executionType", ""),
            "error_message": (error_message or "")[: self.settings.ai_input_char_limit],
            "stacktrace_excerpt": "\n".join(stacktraces)[: self.settings.ai_input_char_limit],
        }

        system_prompt = (
            "You are an incident assistant for Talend operations. "
            "Return concise, plain-English summaries for monitoring teams. "
            "Avoid internal IDs, Java traces, and Talend component jargon unless unavoidable."
        )
        user_prompt = (
            "Summarize this failure in one short sentence, understandable by non-developers. "
            "Return strict JSON with key: summary.\n\n"
            f"Context:\n{json.dumps(context)}"
        )

        try:
            resp = requests.post(
                f"{self.settings.openai_api_base_url}{self.settings.openai_chat_endpoint}",
                headers={
                    "Authorization": f"Bearer {self.settings.openai_api_key}",
                    "Content-Type": "application/json",
                },
                json={
                    "model": self.settings.openai_model,
                    "temperature": 0.1,
                    "response_format": {"type": "json_object"},
                    "messages": [
                        {"role": "system", "content": system_prompt},
                        {"role": "user", "content": user_prompt},
                    ],
                },
                timeout=self.settings.openai_timeout_seconds,
            )
            resp.raise_for_status()
            content = resp.json()["choices"][0]["message"]["content"]
            parsed = json.loads(content) if isinstance(content, str) else content
            summary = str(parsed.get("summary", "")).strip()
            return summary[:300] if summary else None
        except Exception:
            logging.exception("OpenAI summarization failed; using rule-based fallback.")
            return None


# ----------------------------
# Decisioning helpers
# ----------------------------
FAILED_STATES = {"EXECUTION_FAILED", "DEPLOY_FAILED", "EXECUTION_TERMINATED", "EXECUTION_REJECTED"}
TRANSIENT_PATTERNS = [
    r"remote engine is not available",
    r"no available cloud engines",
    r"connection reset",
    r"connection timed out",
    r"timeout",
    r"temporary failure",
    r"network",
    r"service unavailable",
    r"max_deployment_attempts_reached",
    r"remote_engine_unavailable",
    r"execution_terminated"
]
NON_RETRIABLE_PATTERNS = [
    r"exception in component",
    r"tdieexception",
    r"already exist",
    r"file has more records",
    r"permission denied",
    r"syntax",
    r"nullpointerexception",
]


def _parse_dt(value: Optional[str]) -> datetime:
    if not value:
        return datetime.fromtimestamp(0, tz=timezone.utc)
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def get_latest_execution_by_task(items: Iterable[Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
    grouped: Dict[str, Dict[str, Any]] = {}
    for item in items:
        task_id = item.get("taskId")
        if not task_id:
            continue
        score = max(
            _parse_dt(item.get("finishTimestamp")),
            _parse_dt(item.get("startTimestamp")),
            _parse_dt(item.get("triggerTimestamp")),
        )
        current = grouped.get(task_id)
        if not current:
            grouped[task_id] = item
            continue
        current_score = max(
            _parse_dt(current.get("finishTimestamp")),
            _parse_dt(current.get("startTimestamp")),
            _parse_dt(current.get("triggerTimestamp")),
        )
        if score > current_score:
            grouped[task_id] = item
    return grouped


def _contains_pattern(text: str, patterns: List[str]) -> bool:
    lowered = (text or "").lower()
    return any(re.search(pattern, lowered) for pattern in patterns)


GENERIC_SUMMARY = "Job failed due to processing error; manual review recommended."


def summarize_error_rule_based(error_message: str, component_payload: Dict[str, Any]) -> str:
    combined = (error_message or "") + "\n" + "\n".join(
        [m.get("stacktrace", "") for m in component_payload.get("metrics", {}).get("items", [])]
    )
    text = combined.lower()

    if "file has more records" in text:
        return "Input file exceeds expected record limit."
    if "already exist" in text:
        return "Output file already exists and overwrite is disabled."
    if "remote engine" in text or "cloud engines" in text:
        return "Talend runtime engine is unavailable."
    if "connection" in text or "network" in text or "timeout" in text:
        return "Temporary connectivity issue occurred."
    if "child job running failed" in text:
        return "A child job failed inside the master job chain."
    if "permission denied" in text:
        return "Permission issue while accessing resource."
    return GENERIC_SUMMARY


def summarize_error(
    error_message: str,
    component_payload: Dict[str, Any],
    execution: Optional[Dict[str, Any]] = None,
    ai_summarizer: Optional[OpenAIErrorSummarizer] = None,
    ai_mode: str = "fallback",
) -> str:
    baseline = summarize_error_rule_based(error_message, component_payload)
    if not ai_summarizer or not ai_summarizer.is_enabled():
        return baseline

    if ai_mode != "always" and baseline != GENERIC_SUMMARY:
        return baseline

    ai_summary = ai_summarizer.summarize(error_message, component_payload, execution)
    return ai_summary or baseline


def classify_failure(execution: Dict[str, Any], component_payload: Dict[str, Any]) -> Tuple[str, str]:
    err = execution.get("errorMessage", "") or ""
    status = execution.get("executionStatus", "") or ""
    stacktraces = [m.get("stacktrace", "") for m in component_payload.get("metrics", {}).get("items", [])]
    combined = "\n".join([err, *stacktraces])

    if _contains_pattern(combined, NON_RETRIABLE_PATTERNS):
        return "valid_failure", "Likely job/data issue; human intervention needed."
    if status in {"DEPLOY_FAILED", "EXECUTION_REJECTED"} and _contains_pattern(combined, TRANSIENT_PATTERNS):
        return "retryable_noise", "Likely transient platform issue; retry allowed."
    if _contains_pattern(combined, TRANSIENT_PATTERNS):
        return "retryable_noise", "Likely temporary infrastructure/network issue."
    return "valid_failure", "Failure reason is ambiguous; escalate to humans."


def parse_master_job_dependency(component_payload: Dict[str, Any]) -> str:
    run_jobs = [
        m for m in component_payload.get("metrics", {}).get("items", []) if m.get("connector_type") == "tRunJob"
    ]
    if not run_jobs:
        return ""

    def runjob_index(item: Dict[str, Any]) -> int:
        connector_id = item.get("connector_id", "")
        match = re.search(r"_(\d+)$", connector_id)
        return int(match.group(1)) if match else 9999

    ordered = sorted(run_jobs, key=runjob_index)
    failed = next((j for j in ordered if j.get("stacktrace")), None)
    if not failed:
        names = [j.get("connector_label", j.get("connector_id", "unknown")) for j in ordered]
        return f"Master job chain detected: {' -> '.join(names)}"

    failed_idx = runjob_index(failed)
    failed_name = failed.get("connector_label", failed.get("connector_id", "unknown"))
    downstream = [
        j.get("connector_label", j.get("connector_id", "unknown"))
        for j in ordered
        if runjob_index(j) > failed_idx
    ]
    if downstream:
        return f"Master chain failure at '{failed_name}'; downstream not expected: {', '.join(downstream)}"
    return f"Master chain failure at '{failed_name}'."


def flatten_plan_steps(chart_node: Dict[str, Any]) -> List[Dict[str, Any]]:
    steps: List[Dict[str, Any]] = []
    current = chart_node
    while isinstance(current, dict) and current:
        step_id = current.get("id")
        if step_id:
            flows = current.get("flows", []) if isinstance(current.get("flows"), list) else []
            flow_names = [f.get("name") for f in flows if isinstance(f, dict) and f.get("name")]
            steps.append(
                {
                    "id": step_id,
                    "name": current.get("name", step_id),
                    "flows": flow_names,
                    "flow_ids": [
                        f.get("id") for f in flows if isinstance(f, dict) and f.get("id")
                    ],
                }
            )
        current = current.get("nextStep") if isinstance(current.get("nextStep"), dict) else None
    return steps


def enrich_plan_context(
    client: TalendClient,
    execution: Dict[str, Any],
) -> Dict[str, str]:
    plan_id = execution.get("planId")
    if not plan_id:
        return {}

    plan_executions = client.get_plan_executions()
    matching = [p for p in plan_executions if p.get("planId") == plan_id]
    if not matching:
        return {"plan_id": plan_id}

    matching.sort(key=lambda x: _parse_dt(x.get("startTimestamp")), reverse=True)
    plan_exec = matching[0]
    plan_execution_id = plan_exec.get("executionId")

    step_rows = client.get_plan_steps(plan_execution_id) if plan_execution_id else []
    failed_step = next((s for s in step_rows if str(s.get("executionStatus", "")).upper() in {"FAIL", "FAILED"}), None)

    plan_definition = client.get_plan_definition(plan_id)
    plan_name = plan_definition.get("name", plan_id)
    plan_executable = plan_definition.get("executable", plan_id)
    chart = plan_definition.get("chart", {})
    ordered_steps = flatten_plan_steps(chart)
    step_name_map = {s["id"]: s.get("name", s["id"]) for s in ordered_steps}

    failed_step_id = failed_step.get("id") if failed_step else ""
    failed_step_name = step_name_map.get(failed_step_id, failed_step_id) if failed_step_id else ""
    step_tasks_map: Dict[str, str] = {}
    for s in ordered_steps:
        task_list = [name for name in s.get("flows", []) if name]
        step_tasks_map[s["id"]] = ", ".join(task_list)

    downstream: List[str] = []
    downstream_tasks: List[str] = []
    if failed_step_id:
        ids = [s["id"] for s in ordered_steps]
        if failed_step_id in ids:
            idx = ids.index(failed_step_id)
            downstream = [step_name_map.get(step_id, step_id) for step_id in ids[idx + 1 :]]
            downstream_tasks = [step_tasks_map.get(step_id, "") for step_id in ids[idx + 1 :]]

    return {
        "plan_id": plan_id,
        "plan_execution_id": plan_execution_id or "",
        "plan_name": plan_name,
        "plan_executable": plan_executable,
        "failed_step_id": failed_step_id,
        "failed_step_name": failed_step_name,
        "failed_step_tasks": step_tasks_map.get(failed_step_id, ""),
        "downstream_summary": ", ".join(downstream) if downstream else "",
        "downstream_tasks": " | ".join([x for x in downstream_tasks if x]),
    }


def load_recipients(path: str) -> List[str]:
    cfg = Path(path)
    if not cfg.exists():
        logging.warning("Recipients file %s not found.", path)
        return []
    data = json.loads(cfg.read_text(encoding="utf-8"))
    return [r for r in data.get("emails", []) if isinstance(r, str) and r.strip()]


def plan_dependency_summary(plan_context: Dict[str, str]) -> str:
    failed_step = plan_context.get("failed_step_name", "")
    failed_tasks = plan_context.get("failed_step_tasks", "")
    downstream_steps = plan_context.get("downstream_summary", "")
    downstream_tasks = plan_context.get("downstream_tasks", "")
    if not failed_step and not downstream_steps:
        return ""

    chunks = []
    if failed_step:
        if failed_tasks:
            chunks.append(f"Plan failure at step '{failed_step}' (task(s): {failed_tasks})")
        else:
            chunks.append(f"Plan failure at step '{failed_step}'")
    if downstream_steps:
        chunks.append(f"Downstream steps not completed: {downstream_steps}")
    if downstream_tasks:
        chunks.append(f"Downstream task(s) impacted: {downstream_tasks}")
    return "; ".join(chunks)


def pick_store(settings: Settings) -> AlertStore:
    if settings.azure_sql_connection_string:
        logging.info("Using Azure SQL alert store.")
        return AzureSqlAlertStore(settings.azure_sql_connection_string)
    logging.info("Using SQLite fallback alert store at %s", settings.local_db_path)
    return SqliteAlertStore(settings.local_db_path)


def init_store_with_fallback(settings: Settings) -> AlertStore:
    """
    Initializes the preferred store and falls back to SQLite when Azure SQL
    driver/connection settings are unavailable at runtime.
    """
    preferred = pick_store(settings)
    try:
        preferred.init()
        return preferred
    except Exception:
        if isinstance(preferred, AzureSqlAlertStore):
            logging.exception(
                "Azure SQL initialization failed (driver/connection issue). "
                "Falling back to SQLite at %s",
                settings.local_db_path,
            )
            fallback = SqliteAlertStore(settings.local_db_path)
            fallback.init()
            return fallback
        raise


def build_digest_html(digest_day: date, rows: List[Dict[str, Any]]) -> str:
    header = (
        "<h3>Talend Alert Noise Suppression - Daily Summary</h3>"
        f"<p><b>Date (UTC):</b> {digest_day.isoformat()}<br/>"
        f"<b>Total alerts:</b> {len(rows)}</p>"
    )
    if not rows:
        return header + "<p>No alerts recorded for this day.</p>"

    table_head = (
        "<table border='1' cellpadding='6' cellspacing='0' style='border-collapse: collapse;'>"
        "<tr>"
        "<th>Time (UTC)</th><th>Type</th><th>Task ID</th><th>Task Name</th><th>Status</th><th>Summary</th>"
        "<th>Plan</th><th>Failed step</th><th>Downstream impact</th><th>Master impact</th>"
        "</tr>"
    )
    rows_html = ""
    for r in rows:
        rows_html += (
            "<tr>"
            f"<td>{r.get('observed_at_utc','')}</td>"
            f"<td>{r.get('execution_type','')}</td>"
            f"<td>{r.get('task_id','')}</td>"
            f"<td>{r.get('task_name','')}</td>"
            f"<td>{r.get('execution_status','')}</td>"
            f"<td>{r.get('human_error','')}</td>"
            f"<td>{r.get('plan_name','')}</td>"
            f"<td>{r.get('failed_step_name','')}</td>"
            f"<td>{r.get('downstream_summary','')}</td>"
            f"<td>{r.get('master_summary','')}</td>"
            "</tr>"
        )
    return header + table_head + rows_html + "</table>"


# ----------------------------
# Trigger 1: Frequent polling/ingestion
# ----------------------------
@app.timer_trigger(schedule="0 */10 * * * *", arg_name="pollTimer", run_on_startup=False, use_monitor=True)
def poll_talend_alerts(pollTimer: func.TimerRequest) -> None:
    if pollTimer.past_due:
        logging.info("Poll timer is past due.")

    settings = Settings()
    store = init_store_with_fallback(settings)
    ai_summarizer = OpenAIErrorSummarizer(settings)

    client = TalendClient(settings)
    all_execs = client.get_task_executions()
    latest = get_latest_execution_by_task(all_execs)

    now = datetime.now(timezone.utc)
    for task_id, execution in latest.items():
        execution_status = execution.get("executionStatus", "")
        if execution_status not in FAILED_STATES:
            continue

        execution_id = execution.get("executionId")
        if not execution_id:
            continue

        alert_key = execution_id  # unique per Talend run; avoids duplicate inserts across poll cycles
        if store.exists_alert_key(alert_key):
            continue

        plan_context: Dict[str, str] = {}
        if execution.get("executionType") == "PLAN" and execution.get("planId"):
            try:
                plan_context = enrich_plan_context(client, execution)
            except Exception:
                logging.exception("Failed to enrich plan context for execution %s", execution_id)

        component_payload = client.get_component_metrics(execution_id)
        decision, reason = classify_failure(execution, component_payload)

        if decision == "retryable_noise":
            # Retry transient MANUAL task runs.
            if execution.get("executionType") == "MANUAL" and settings.retry_enabled:
                retry_count = 0
                while retry_count < settings.retry_max_attempts:
                    retry_count += 1
                    try:
                        new_execution_id = client.retry_task(task_id)
                        logging.info(
                            "Retried task %s due to transient issue. prior=%s new=%s",
                            task_id,
                            execution_id,
                            new_execution_id,
                        )
                        break
                    except Exception:
                        logging.exception("Retry attempt %d failed for task %s", retry_count, task_id)

            # Retry transient PLAN runs via /processing/executions/plans.
            if execution.get("executionType") == "PLAN" and settings.plan_retry_enabled:
                plan_exec_id = (
                    plan_context.get("plan_executable")
                    or execution.get("planId")
                    or plan_context.get("plan_id")
                )
                if plan_exec_id:
                    retry_count = 0
                    while retry_count < settings.plan_retry_max_attempts:
                        retry_count += 1
                        try:
                            new_plan_execution_id = client.retry_plan(plan_exec_id)
                            logging.info(
                                "Retried plan executable %s due to transient issue. prior=%s new=%s",
                                plan_exec_id,
                                execution_id,
                                new_plan_execution_id,
                            )
                            break
                        except Exception:
                            logging.exception(
                                "Plan retry attempt %d failed for plan executable %s",
                                retry_count,
                                plan_exec_id,
                            )

        master_summary = parse_master_job_dependency(component_payload)
        if execution.get("executionType") == "PLAN":
            master_summary = plan_dependency_summary(plan_context) or master_summary
        human_error = summarize_error(
            execution.get("errorMessage", ""),
            component_payload,
            execution=execution,
            ai_summarizer=ai_summarizer,
            ai_mode=settings.ai_summarization_mode,
        )

        include_in_digest = 1 if decision == "valid_failure" else 0
        if decision == "retryable_noise" and not settings.store_retryable_noise:
            continue

        row = {
            "alert_key": alert_key,
            "observed_at_utc": now.isoformat(),
            "day_utc": now.date().isoformat(),
            "execution_id": execution_id,
            "task_id": task_id,
            "task_name": component_payload.get("artifact_name", ""),
            "execution_type": execution.get("executionType", ""),
            "execution_status": execution_status,
            "plan_id": plan_context.get("plan_id", execution.get("planId", "")),
            "plan_execution_id": plan_context.get("plan_execution_id", ""),
            "plan_name": plan_context.get("plan_name", ""),
            "failed_step_id": plan_context.get("failed_step_id", ""),
            "failed_step_name": plan_context.get("failed_step_name", ""),
            "downstream_summary": plan_context.get("downstream_summary", ""),
            "master_summary": master_summary,
            "decision": decision,
            "include_in_digest": include_in_digest,
            "human_error": human_error,
            "raw_error": execution.get("errorMessage", "") + " | " + reason,
        }
        store.insert_alert(row)

    logging.info("Polling completed. Scanned latest executions for %d tasks.", len(latest))


# ----------------------------
# Trigger 2: Daily summary email
# ----------------------------
@app.timer_trigger(schedule="0 5 18 * * *", arg_name="digestTimer", run_on_startup=False, use_monitor=True)
def send_daily_digest(digestTimer: func.TimerRequest) -> None:
    if digestTimer.past_due:
        logging.info("Digest timer is past due.")

    settings = Settings()
    store = init_store_with_fallback(settings)

    recipients = load_recipients(settings.alert_recipients_file)
    notifier = EmailNotifier(settings, recipients)
    if not notifier.is_enabled():
        logging.warning("Email notifier not configured. Digest skipped.")
        return

    digest_day = datetime.now(timezone.utc).date() - timedelta(days=1)
    rows = store.get_digest_rows(digest_day)
    body = build_digest_html(digest_day, rows)
    subject = f"Talend Daily Alert Summary - {digest_day.isoformat()}"
    notifier.send(subject, body)

    ids = [r["id"] for r in rows if r.get("id") is not None]
    store.mark_emailed(ids)
    logging.info("Digest sent for %s with %d row(s).", digest_day.isoformat(), len(rows))

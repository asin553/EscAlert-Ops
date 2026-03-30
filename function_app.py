import json
import logging
import os
import re
import smtplib
from dataclasses import dataclass
from datetime import datetime, timezone
from email.mime.text import MIMEText
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

import azure.functions as func
import requests

app = func.FunctionApp()


def _env_bool(name: str, default: bool = False) -> bool:
    return os.getenv(name, str(default)).strip().lower() in {"1", "true", "yes", "on"}


@dataclass
class Settings:
    talend_region: str = os.getenv("TALEND_REGION", "us").lower()
    talend_pat: str = os.getenv("TALEND_PAT", "KeJ_eYY2RsKX4Df-hNhgOi97eBQ8TfOsIABiCZz2Ci5kUMbzz1SKruUkHjtdkhhl")
    lookback_limit: int = int(os.getenv("TALEND_TASK_EXECUTIONS_LIMIT", "100"))
    alert_recipients_file: str = os.getenv("ALERT_RECIPIENTS_FILE", "alert_recipients.json")
    state_file: str = os.getenv("ANS_STATE_FILE", "/tmp/ans_state.json")
    duplicate_suppress_minutes: int = int(os.getenv("DUPLICATE_SUPPRESS_MINUTES", "120"))
    retry_enabled: bool = _env_bool("RETRY_ENABLED", True)
    retry_max_attempts: int = int(os.getenv("RETRY_MAX_ATTEMPTS", "1"))
    request_timeout_seconds: int = int(os.getenv("REQUEST_TIMEOUT_SECONDS", "20"))
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

    smtp_host: str = os.getenv("SMTP_HOST", "smtp.outlook.com")
    smtp_port: int = int(os.getenv("SMTP_PORT", "587"))
    smtp_username: str = os.getenv("SMTP_USERNAME", "applications@thinkartha.com")
    smtp_password: str = os.getenv("SMTP_PASSWORD", "ARTHA@2022")
    smtp_sender: str = os.getenv("SMTP_SENDER", "applications@thinkartha.com")
    smtp_use_tls: bool = _env_bool("SMTP_USE_TLS", True)

    @property
    def api_base_url(self) -> str:
        return f"https://api.{self.talend_region}.cloud.talend.com"


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


class StateStore:
    def __init__(self, path: str) -> None:
        self.path = Path(path)

    def load(self) -> Dict[str, Any]:
        if not self.path.exists():
            return {"tasks": {}}
        try:
            return json.loads(self.path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            logging.warning("State file is invalid JSON. Reinitializing state.")
            return {"tasks": {}}

    def save(self, state: Dict[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(state, indent=2), encoding="utf-8")


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

        msg = MIMEText(body)
        msg["Subject"] = subject
        msg["From"] = self.settings.smtp_sender
        msg["To"] = ", ".join(self.recipients)

        with smtplib.SMTP(self.settings.smtp_host, self.settings.smtp_port) as server:
            if self.settings.smtp_use_tls:
                server.starttls()
            server.login(self.settings.smtp_username, self.settings.smtp_password)
            server.sendmail(self.settings.smtp_sender, self.recipients, msg.as_string())


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
]

NON_RETRIABLE_PATTERNS = [
    r"exception in component",
    r"tdieexception",
    r"already exist",
    r"file has more records",
    r"syntax",
    r"permission denied",
    r"nullpointerexception",
]

FAILED_STATES = {
    "EXECUTION_FAILED",
    "DEPLOY_FAILED",
    "EXECUTION_TERMINATED",
    "EXECUTION_REJECTED",
}


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
        candidate = grouped.get(task_id)
        if not candidate:
            grouped[task_id] = item
            continue
        existing_score = max(
            _parse_dt(candidate.get("finishTimestamp")),
            _parse_dt(candidate.get("startTimestamp")),
            _parse_dt(candidate.get("triggerTimestamp")),
        )
        if score > existing_score:
            grouped[task_id] = item
    return grouped


def _contains_pattern(text: str, patterns: List[str]) -> bool:
    lowered = text.lower()
    return any(re.search(pattern, lowered) for pattern in patterns)


def classify_failure(execution: Dict[str, Any], component_payload: Dict[str, Any]) -> Tuple[str, str]:
    err = execution.get("errorMessage", "") or ""
    status = execution.get("executionStatus", "") or ""

    stacktraces: List[str] = []
    for item in component_payload.get("metrics", {}).get("items", []):
        st = item.get("stacktrace")
        if st:
            stacktraces.append(st)
    combined = "\n".join([err, *stacktraces])

    if _contains_pattern(combined, NON_RETRIABLE_PATTERNS):
        return "valid_failure", "Error pattern indicates code/data issue requiring human intervention."

    if status in {"DEPLOY_FAILED", "EXECUTION_REJECTED"} and _contains_pattern(combined, TRANSIENT_PATTERNS):
        return "retryable_noise", "Deployment/execution failure appears transient; retry is safe."

    if _contains_pattern(combined, TRANSIENT_PATTERNS):
        return "retryable_noise", "Transient infrastructure/network issue detected from logs."

    return "valid_failure", "Failure reason is ambiguous and should be escalated to humans."


def load_recipients(path: str) -> List[str]:
    cfg = Path(path)
    if not cfg.exists():
        logging.warning("Recipients file %s not found.", path)
        return []
    data = json.loads(cfg.read_text(encoding="utf-8"))
    return [r for r in data.get("emails", []) if isinstance(r, str) and r.strip()]


def compose_alert(execution: Dict[str, Any], reason: str, component_payload: Dict[str, Any]) -> Tuple[str, str]:
    task_id = execution.get("taskId")
    execution_id = execution.get("executionId")
    subject = f"[Talend Alert] Valid failure for task {task_id}"

    failed_components = []
    for m in component_payload.get("metrics", {}).get("items", []):
        if m.get("stacktrace"):
            failed_components.append(f"- {m.get('connector_label', m.get('connector_id', 'unknown'))}")

    body = (
        f"Task ID: {task_id}\n"
        f"Execution ID: {execution_id}\n"
        f"Status: {execution.get('executionStatus')}\n"
        f"Reason: {reason}\n"
        f"Error: {execution.get('errorMessage', 'N/A')}\n"
        f"Failed components:\n{chr(10).join(failed_components) if failed_components else '- none captured'}\n"
        f"Start: {execution.get('startTimestamp')}\n"
        f"Finish: {execution.get('finishTimestamp')}\n"
    )
    return subject, body


def should_suppress_duplicate(task_state: Dict[str, Any], signature: str, now: datetime, suppress_min: int) -> bool:
    if not task_state:
        return False
    if task_state.get("last_signature") != signature:
        return False
    alerted_at = _parse_dt(task_state.get("last_alerted_at"))
    age_minutes = (now - alerted_at).total_seconds() / 60
    return age_minutes < suppress_min


@app.timer_trigger(schedule="0 */5 * * * *", arg_name="myTimer", run_on_startup=False, use_monitor=False)
def timer_trigger(myTimer: func.TimerRequest) -> None:
    if myTimer.past_due:
        logging.info("The timer is past due.")

    settings = Settings()
    recipients = load_recipients(settings.alert_recipients_file)
    notifier = EmailNotifier(settings, recipients)
    state_repo = StateStore(settings.state_file)
    state = state_repo.load()
    state.setdefault("tasks", {})

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
            logging.warning("Task %s has failed state without executionId; skipping.", task_id)
            continue

        component_payload = client.get_component_metrics(execution_id)
        decision, reason = classify_failure(execution, component_payload)
        signature = f"{execution_status}|{execution.get('errorMessage', '')[:180]}"
        task_state = state["tasks"].get(task_id, {})

        if should_suppress_duplicate(task_state, signature, now, settings.duplicate_suppress_minutes):
            logging.info("Suppressed duplicate alert for task %s.", task_id)
            continue

        if decision == "retryable_noise" and settings.retry_enabled:
            attempts = int(task_state.get("retry_attempts", 0))
            if attempts < settings.retry_max_attempts:
                new_execution_id = client.retry_task(task_id)
                logging.info(
                    "Retried task %s for execution %s, new executionId=%s",
                    task_id,
                    execution_id,
                    new_execution_id,
                )
                state["tasks"][task_id] = {
                    "last_signature": signature,
                    "last_decision": decision,
                    "last_retry_at": now.isoformat(),
                    "retry_attempts": attempts + 1,
                    "last_execution_id": execution_id,
                }
                continue

            logging.info("Retry skipped for task %s because retry_max_attempts reached.", task_id)

        subject, body = compose_alert(execution, reason, component_payload)
        notifier.send(subject, body)
        state["tasks"][task_id] = {
            "last_signature": signature,
            "last_decision": decision,
            "last_alerted_at": now.isoformat(),
            "retry_attempts": 0,
            "last_execution_id": execution_id,
        }

    state_repo.save(state)
    logging.info("Alert Noise Suppression run finished. Checked %d latest task executions.", len(latest))

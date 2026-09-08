"""ntfy.sh push-notification client -- the primary daily-report channel
(Daniel's ruling, 2026-09-08, superseding the email-report design).

Deliberately modeled on app/notify_email.py and app/healthcheck.py: never
raises (every failure mode comes back as a NotificationResult), bounded
retries, credentials/topic never logged in full, and completely unable to
affect backup SUCCESS/FAILED -- see app.runner.Runner
._send_transaction_notifications, which isolates this client in its own
try/except exactly like email and healthcheck already are.

ntfy vs Healthchecks.io (do not conflate these -- see HANDOFF/README):
    ntfy answers "the backup system reached a point where it could tell a
        human what happened" -- it requires the code to run far enough to
        build a report and make an HTTP call.
    Healthchecks answers "the expected check-in never arrived" -- it fires
        from Healthchecks' own servers on a MISSING signal, so it still
        works when the pod is dead, backupd never woke, networking is
        down, or this very module has a bug. That is a fundamentally
        different failure mode ntfy cannot cover, which is why both exist.

ntfy.sh topic naming: the public hosted service (https://ntfy.sh) has no
per-topic access control unless the operator sets one up -- the topic name
IS the secret. BACKUP_NTFY_URL must therefore be a long, random,
unguessable topic (see bin/generate_ntfy_topic.py), never a descriptive
name like "osf-backup". Topics need no server-side creation step: the
first publish or subscribe to a name creates it implicitly.
"""

from __future__ import annotations

import json
import logging
import os
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from app.healthcheck import NotificationResult, _format_bytes, format_duration

logger = logging.getLogger(__name__)

CREDENTIALS_ENV_PATH = Path(__file__).resolve().parent.parent / "config" / "credentials.env"
DEFAULT_MAX_ATTEMPTS = 3
DEFAULT_RETRY_BACKOFF_SECONDS = 2.0
DEFAULT_TIMEOUT_SECONDS = 15.0

# ntfy priority names -> its documented 1-5 integer scale (1=min, 5=max).
# https://docs.ntfy.sh/publish/#message-priority
_PRIORITY_LEVELS = {"min": 1, "low": 2, "default": 3, "high": 4, "urgent": 5, "max": 5}

# How much detail the phone push shows. This is a lock-screen notification,
# not the daily email -- it must answer "does this still look sane" in the
# few seconds someone actually looks at it. The full per-transaction detail
# stays available via `backupctl report`/`status`; nothing here is lost,
# only not pushed to a phone.
NTFY_TOP_ROOTS = 3
NTFY_SAMPLE_PATHS = 3


def _read_dotenv(path: Path) -> dict[str, str]:
    """Parse simple KEY=value lines from a dotenv file. Duplicates the
    tiny parser already in app.storage/app.notify_email rather than
    importing a private method cross-module. {} if unreadable/missing."""
    values: dict[str, str] = {}
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, val = line.split("=", 1)
            key = key.strip()
            val = val.strip()
            if len(val) >= 2 and val[0] == val[-1] and val[0] in ("'", '"'):
                val = val[1:-1]
            if key:
                values[key] = val
    except OSError:
        return {}
    return values


def _env_or_file(name: str, file_values: dict[str, str]) -> str | None:
    value = os.environ.get(name)
    if value is not None and value.strip():
        return value.strip()
    file_value = file_values.get(name)
    return file_value.strip() if file_value and file_value.strip() else None


def _mask_topic_url(url: str) -> str:
    """Mask the topic name for logging -- the topic IS the credential on
    the public ntfy.sh service, so it must never appear in full in logs,
    the same rule this project already applies to the Healthchecks UUID
    and S3 keys."""
    try:
        parts = urlsplit(url)
        topic = parts.path.lstrip("/")
        if not topic:
            return url
        visible = topic[:4]
        return f"{parts.scheme}://{parts.netloc}/{visible}***"
    except Exception:
        return "***"


class NtfyClient:
    """Client for ntfy.sh push notifications.

    Configuration (env var, falling back to config/credentials.env):
      - BACKUP_NTFY_ENABLED: "1"/"true"/"yes" to enable; anything else
        (including unset) is a documented no-op.
      - BACKUP_NTFY_URL: full topic URL, e.g. "https://ntfy.sh/<topic>".
        Treated as a secret on the public hosted service -- never logged
        in full (see _mask_topic_url).
    """

    def __init__(
        self,
        *,
        max_attempts: int = DEFAULT_MAX_ATTEMPTS,
        retry_backoff_seconds: float = DEFAULT_RETRY_BACKOFF_SECONDS,
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
    ) -> None:
        self.max_attempts = max_attempts
        self.retry_backoff_seconds = retry_backoff_seconds
        self.timeout = timeout

        file_values = _read_dotenv(CREDENTIALS_ENV_PATH)

        raw_enabled = _env_or_file("BACKUP_NTFY_ENABLED", file_values)
        self.enabled = (raw_enabled or "").strip().lower() in ("1", "true", "yes")

        raw_url = _env_or_file("BACKUP_NTFY_URL", file_values)
        self.topic_url = raw_url.rstrip("/") if raw_url else None

        if not self.enabled:
            logger.info("BACKUP_NTFY_ENABLED is not set; ntfy notifications are no-ops")
        elif self.topic_url:
            logger.debug("ntfy topic configured: %s", _mask_topic_url(self.topic_url))

    def _misconfiguration(self) -> str | None:
        if not self.topic_url:
            return "Missing required config: BACKUP_NTFY_URL"
        parts = urlsplit(self.topic_url)
        if not parts.scheme or not parts.netloc or not parts.path.lstrip("/"):
            return f"BACKUP_NTFY_URL is not a valid topic URL: {_mask_topic_url(self.topic_url)}"
        return None

    def send(
        self,
        title: str,
        message: str,
        *,
        priority: str = "default",
        tags: list[str] | None = None,
    ) -> NotificationResult:
        """Publish one ntfy notification. Never raises -- disabled,
        misconfigured, or a transport error after bounded retries all come
        back as a NotificationResult instead, exactly like EmailClient.send
        and HealthcheckClient's ping methods.

        Publishes via ntfy's JSON API (POST to the server root with the
        topic as a body field) rather than the header-based form -- the
        header form requires ASCII-safe values and silent truncation rules
        for anything else; JSON has neither pitfall for a title/message
        this code does not fully control the character set of.
        """
        if not self.enabled:
            return NotificationResult(status="disabled", attempts=0)

        problem = self._misconfiguration()
        if problem:
            logger.warning("ntfy misconfigured: %s", problem)
            return NotificationResult(status="misconfigured", attempts=0, error=problem)

        parts = urlsplit(self.topic_url)
        server_root = f"{parts.scheme}://{parts.netloc}/"
        topic = parts.path.lstrip("/")

        payload: dict[str, Any] = {"topic": topic, "title": title, "message": message}
        priority_level = _PRIORITY_LEVELS.get(priority, _PRIORITY_LEVELS["default"])
        if priority_level != _PRIORITY_LEVELS["default"]:
            payload["priority"] = priority_level
        if tags:
            payload["tags"] = list(tags)

        data = json.dumps(payload).encode("utf-8")
        masked = _mask_topic_url(self.topic_url)

        last_error: str | None = None
        for attempt in range(1, self.max_attempts + 1):
            req = urllib.request.Request(
                server_root, data=data, method="POST",
                headers={"Content-Type": "application/json; charset=utf-8"},
            )
            try:
                with urllib.request.urlopen(req, timeout=self.timeout) as response:
                    response.read()
                    status = getattr(response, "status", 200)
                    if 200 <= status < 300:
                        return NotificationResult(status="sent", attempts=attempt)
                    last_error = f"HTTP {status}"
            except urllib.error.HTTPError as exc:
                last_error = f"HTTP {exc.code}"
            except Exception as exc:
                last_error = f"{type(exc).__name__}: {exc}"

            logger.warning(
                "ntfy publish attempt %d/%d to %s failed: %s",
                attempt, self.max_attempts, masked, last_error,
            )
            if attempt < self.max_attempts:
                time.sleep(self.retry_backoff_seconds * attempt)

        logger.error(
            "ntfy publish to %s failed after %d attempts: %s",
            masked, self.max_attempts, last_error,
        )
        return NotificationResult(status="failed", attempts=self.max_attempts, error=last_error)

    def success(self, report: dict[str, Any]) -> NotificationResult:
        title, message = format_success_notification(report)
        return self.send(
            title, message, priority="default", tags=["white_check_mark", "floppy_disk"]
        )

    def failure(self, report: dict[str, Any]) -> NotificationResult:
        title, message = format_failure_notification(report)
        return self.send(title, message, priority="urgent", tags=["rotating_light", "warning"])

    def interrupted(self, txn_id: str, phase: str, reason: str) -> NotificationResult:
        title, message = format_interrupted_notification(txn_id, phase, reason)
        # "high", not "urgent" -- a deliberate operator/shutdown stop is
        # not an emergency the way a genuine FAILED transaction is (see
        # module docstring and Runner._send_interrupted_notification).
        return self.send(title, message, priority="high", tags=["pause_button"])

    def test_notification(self) -> NotificationResult:
        return self.send(
            "OSF Backup TEST",
            "ntfy notifications are working from the RunPod backup system.",
            priority="default",
            tags=["test_tube"],
        )


def _txn_date(report: dict[str, Any]) -> str:
    return str(report.get("txn_date") or report.get("txn_id") or "unknown")


def format_success_notification(report: dict[str, Any]) -> tuple[str, str]:
    """Compact phone-push body for a genuine SUCCESS. Every figure here is
    also in the full report (`backupctl report`) -- this just picks the
    handful that answer "does this still look sane" at a glance."""
    title = f"OSF Backup OK — {_txn_date(report)}"

    duration = format_duration(report.get("created_at"), report.get("updated_at"))
    lines = [
        f"{report['file_count']:,} files · {_format_bytes(report['protected_bytes'])}"
        + (f" · {duration}" if duration else "")
    ]

    error_count = report.get("error_count", 0)
    deferred_count = report.get("deferred_active", {}).get("count", 0)
    lines.append(f"{error_count} upload errors · {deferred_count} deferred")

    top_classes = sorted(
        report.get("by_classification", {}).items(),
        key=lambda kv: kv[1]["bytes"],
        reverse=True,
    )[:NTFY_TOP_ROOTS]
    if top_classes:
        lines.append(
            " · ".join(f"{name} {_format_bytes(stats['bytes'])}" for name, stats in top_classes)
        )

    skipped = report.get("scan_skipped")
    if skipped:
        unclassified = skipped.get("UNCLASSIFIED", 0)
        if unclassified:
            lines.append(f"{unclassified:,} unclassified skipped")

    staging_bytes = report.get("local_staging_bytes")
    if staging_bytes is not None:
        lines.append(f"Staging {_format_bytes(staging_bytes)}")

    return title, "\n".join(lines)


def format_failure_notification(report: dict[str, Any]) -> tuple[str, str]:
    """Compact phone-push body for a genuine terminal FAILED. Deliberately
    no traceback -- full diagnostic detail stays in logs/HANDOFF/state;
    the push exists to get a human's attention, not to be read on a
    lock screen."""
    title = f"OSF BACKUP FAILED — {_txn_date(report)}"

    progress = report.get("progress", {})
    total_bytes = report.get("total_selected_bytes")
    protected_bytes = report.get("protected_bytes", 0)

    lines = [f"Phase: {report.get('phase', 'unknown')}"]
    lines.append(
        f"{progress.get('uploaded', 0):,} / {progress.get('total_scan_items', 0):,} files"
    )
    if total_bytes:
        lines.append(f"{_format_bytes(protected_bytes)} / {_format_bytes(total_bytes)}")

    error = report.get("error") or "(no error message recorded)"
    if len(error) > 300:
        error = error[:300] + "…"
    lines.append(f"Error: {error}")
    lines.append(f"Resume phase: {report.get('resume_phase') or 'unknown'}")

    return title, "\n".join(lines)


def format_interrupted_notification(txn_id: str, phase: str, reason: str) -> tuple[str, str]:
    """A deliberate cooperative stop (SIGTERM mid-transaction, an operator
    restart) is neither SUCCESS nor FAILED and must never be described as
    either -- see app.runner.TransactionCancelled's own docstring."""
    title = f"OSF Backup Interrupted — {txn_id}"
    message = f"Stopped at {phase}, not a failure -- will resume.\n{reason}"
    return title, message

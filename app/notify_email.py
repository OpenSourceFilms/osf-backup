"""Daily email notification client for backup transactions.

Deliberately modeled on the existing app/healthcheck.py HealthcheckClient:
best-effort, never allowed to affect backup SUCCESS/FAILED (see
app/runner.py's Runner._send_transaction_email), bounded retries,
credentials never logged. Email failure is notification failure, not
backup failure -- persisted separately by the caller
(Runner._record_notification_result) and surfaced by
`backupctl status`/`backupctl doctor`.
"""

from __future__ import annotations

import logging
import os
import smtplib
import time
from datetime import datetime
from email.message import EmailMessage
from pathlib import Path
from typing import Any

from app.healthcheck import NotificationResult, _format_bytes, format_duration

logger = logging.getLogger(__name__)

# Same designated location app/storage.py's RunPodStorage uses for S3 credentials,
# see config/policy.yaml hard_exclude -- SMTP credentials belong beside them,
# never in policy/runtime YAML.
CREDENTIALS_ENV_PATH = Path(__file__).resolve().parent.parent / "config" / "credentials.env"
DEFAULT_SMTP_PORT = 587
DEFAULT_MAX_ATTEMPTS = 3
DEFAULT_RETRY_BACKOFF_SECONDS = 2.0


def _read_dotenv(path: Path) -> dict[str, str]:
    """Parse simple KEY=value lines from a dotenv file.

    Deliberately duplicates app.storage.RunPodStorage._read_dotenv's tiny parser
    rather than importing a private method cross-module. Returns {} if the file
    cannot be read (OSError), including when it is missing.
    """
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
    """Return env var value if set, otherwise file value, stripped or None."""
    env_value = os.environ.get(name)
    if env_value and env_value.strip():
        return env_value.strip()
    file_value = file_values.get(name)
    if file_value and file_value.strip():
        return file_value.strip()
    return None


class EmailClient:
    """Send OSF backup transaction email notifications.

    Reads these environment variables, with config/credentials.env fallback via
    ``_read_dotenv`` and ``_env_or_file`` for whichever aren't already in the
    environment:

    - BACKUP_EMAIL_ENABLED: only "1"/"true"/"yes" case-insensitive actually sends;
      anything else including unset makes every send() a documented no-op, never an error.
    - BACKUP_EMAIL_TO: recipient, required when enabled.
    - BACKUP_SMTP_HOST: SMTP host, required when enabled.
    - BACKUP_SMTP_PORT: optional, default 587.
    - BACKUP_SMTP_USERNAME: SMTP username, required when enabled.
    - BACKUP_SMTP_PASSWORD: an app password, never the account password; required when enabled.
    - BACKUP_EMAIL_FROM: optional, defaults to BACKUP_SMTP_USERNAME.
    """

    def __init__(
        self,
        *,
        max_attempts: int = DEFAULT_MAX_ATTEMPTS,
        retry_backoff_seconds: float = DEFAULT_RETRY_BACKOFF_SECONDS,
    ) -> None:
        self.max_attempts = max_attempts
        self.retry_backoff_seconds = retry_backoff_seconds

        file_values = _read_dotenv(CREDENTIALS_ENV_PATH)

        raw_enabled = _env_or_file("BACKUP_EMAIL_ENABLED", file_values)
        self.enabled = (raw_enabled or "").strip().lower() in ("1", "true", "yes")

        self.to_addr = _env_or_file("BACKUP_EMAIL_TO", file_values)
        self.smtp_host = _env_or_file("BACKUP_SMTP_HOST", file_values)

        raw_port = _env_or_file("BACKUP_SMTP_PORT", file_values)
        self.smtp_port = int(raw_port) if raw_port else DEFAULT_SMTP_PORT

        self.smtp_username = _env_or_file("BACKUP_SMTP_USERNAME", file_values)
        self.smtp_password = _env_or_file("BACKUP_SMTP_PASSWORD", file_values)
        self.from_addr = _env_or_file("BACKUP_EMAIL_FROM", file_values) or self.smtp_username

        if not self.enabled:
            logger.info("BACKUP_EMAIL_ENABLED is not set; email notifications are no-ops")

    def _misconfiguration(self) -> str | None:
        """Verify required SMTP settings are present, returning a diagnostic if not."""
        missing: list[str] = []
        if not self.to_addr:
            missing.append("BACKUP_EMAIL_TO")
        if not self.smtp_host:
            missing.append("BACKUP_SMTP_HOST")
        if not self.smtp_username:
            missing.append("BACKUP_SMTP_USERNAME")
        if not self.smtp_password:
            missing.append("BACKUP_SMTP_PASSWORD")
        if missing:
            return f"Missing required config: {', '.join(missing)}"
        return None

    def send(self, subject: str, body: str) -> NotificationResult:
        """Send an email notification.

        Never raises -- every failure mode (disabled, misconfigured, SMTP error
        after bounded retries) comes back as a NotificationResult instead.
        """
        if not self.enabled:
            return NotificationResult(status="disabled", attempts=0)

        problem = self._misconfiguration()
        if problem:
            logger.warning("Email notifications misconfigured: %s", problem)
            return NotificationResult(status="misconfigured", attempts=0, error=problem)

        message = EmailMessage()
        message["Subject"] = subject
        message["From"] = self.from_addr
        message["To"] = self.to_addr
        message.set_content(body)

        last_error: str | None = None
        for attempt in range(1, self.max_attempts + 1):
            try:
                with smtplib.SMTP(self.smtp_host, self.smtp_port, timeout=30) as smtp:
                    smtp.starttls()
                    smtp.login(self.smtp_username, self.smtp_password)
                    smtp.send_message(message)
                return NotificationResult(status="sent", attempts=attempt)
            except Exception as exc:
                last_error = f"{type(exc).__name__}: {exc}"
                logger.warning(
                    "SMTP attempt %d/%d failed for host %s: %s",
                    attempt,
                    self.max_attempts,
                    self.smtp_host,
                    type(exc).__name__,
                )
                if attempt < self.max_attempts:
                    time.sleep(self.retry_backoff_seconds)

        logger.error(
            "Email send failed after %d attempts: %s",
            self.max_attempts,
            last_error,
        )
        return NotificationResult(
            status="failed",
            attempts=self.max_attempts,
            error=last_error,
        )

    def success(self, report: dict[str, Any]) -> NotificationResult:
        return self.send(*format_success_email(report))

    def failure(self, report: dict[str, Any]) -> NotificationResult:
        return self.send(*format_failure_email(report))

    def test_email(self) -> NotificationResult:
        return self.send(
            "[OSF Backup TEST] Email notifications working",
            "This is a test email from `backupctl test-email` -- if you are reading this, "
            "SMTP notification delivery is configured correctly. "
            "No backup was run to produce this message.",
        )


def _fmt_dt(iso_str: str | None) -> str:
    """Format an ISO datetime string, falling back to the raw value on parse errors."""
    if not iso_str:
        return "unknown"
    try:
        dt = datetime.fromisoformat(iso_str)
        return dt.strftime("%Y-%m-%d %H:%M %Z")
    except ValueError:
        return iso_str


# How much detail the daily email shows. The counts are always exact; only
# the illustrative path samples are trimmed. This is a report someone reads
# over coffee every morning -- at 20 samples per diff category it ran to
# 102 lines, most of it path lists, which is how a daily email stops being
# read at all. `backupctl report` still holds the full detail.
EMAIL_SAMPLE_PATHS = 5
EMAIL_TOP_ROOTS = 8


def format_success_email(report: dict[str, Any]) -> tuple[str, str]:
    subject = (
        f"[OSF Backup OK] {report['txn_date']} — "
        f"{report['file_count']:,} files / {report['protected_bytes'] / (1024**3):.0f} GiB"
    )

    lines: list[str] = []
    lines.append(f"Transaction: {report['txn_id']}")
    lines.append(f"Started: {_fmt_dt(report['created_at'])}")
    lines.append(f"Finished: {_fmt_dt(report['updated_at'])}")
    duration = format_duration(report.get("created_at"), report.get("updated_at"))
    if duration:
        lines.append(f"Duration: {duration}")
    lines.append(f"File count: {report['file_count']:,}")
    lines.append(f"Protected: {_format_bytes(report['protected_bytes'])}")

    # What was NOT backed up, immediately under what was. A daily report
    # listing only successes gives the reader no way to notice a policy
    # gap: the 2026-09-07 run protected 67,678 files and silently skipped
    # 46,748 as UNCLASSIFIED. UNCLASSIFIED is called out on its own line
    # because every other skipped class is a rule somebody wrote on
    # purpose, while UNCLASSIFIED means policy has no opinion at all.
    skipped = report.get("scan_skipped")
    lines.append("")
    if skipped is None:
        lines.append("Not backed up: not measured for this transaction")
    elif not skipped:
        lines.append("Not backed up: nothing — every scanned file was selected")
    else:
        unclassified = skipped.get("UNCLASSIFIED", 0)
        total_skipped = sum(skipped.values())
        lines.append(f"Not backed up: {total_skipped:,} files skipped by policy")
        for name, count in sorted(skipped.items(), key=lambda kv: -kv[1]):
            lines.append(f"  {name}: {count:,}")
        if unclassified:
            lines.append(
                f"  ^ {unclassified:,} UNCLASSIFIED means policy has no rule covering "
                f"them. If any of it matters, adjudicate in config/policy.yaml."
            )

    lines.append("")
    lines.append("Classification summary:")

    classification_items = sorted(
        report["by_classification"].items(),
        key=lambda kv: kv[1]["bytes"],
        reverse=True,
    )
    for classification, stats in classification_items:
        lines.append(
            f"  {classification}: {stats['count']:,} files, {_format_bytes(stats['bytes'])}"
        )

    lines.append("")
    lines.append("Top source roots:")
    for entry in report["top_roots"][:EMAIL_TOP_ROOTS]:
        lines.append(
            f"  {entry['root']}: {entry['count']:,} files, {_format_bytes(entry['bytes'])}"
        )

    lines.append("")
    diff = report["diff_from_previous_success"]
    if diff is None:
        lines.append("Diff from previous SUCCESS: none (first successful snapshot)")
    else:
        lines.append(f"Diff from previous SUCCESS ({diff['previous_txn_id']}):")
        for label, marker, count_key, paths_key in (
            ("added", "+", "added_count", "added_paths"),
            ("changed", "~", "changed_count", "changed_paths"),
            ("disappeared", "-", "disappeared_count", "disappeared_paths"),
        ):
            count = diff[count_key]
            lines.append(f"  {label}: {count:,}")
            shown = diff[paths_key][:EMAIL_SAMPLE_PATHS]
            for path in shown:
                lines.append(f"    {marker} {path}")
            if count > len(shown):
                lines.append(f"    ... and {count - len(shown):,} more")
        lines.append(f"  new unique CAS bytes: {_format_bytes(diff['new_unique_bytes'])}")
        if diff["disappeared_count"]:
            # Deletions are the one diff line worth a nudge: they are
            # usually legitimate (the 2026-09-07 run saw 30,464 after a
            # comfy/webroot clearout) but an accidental rm shows up here
            # first, and only while the older snapshots are still retained.
            lines.append(
                "  (disappeared = gone from /workspace, still held in retained "
                "snapshots — check if unexpected)"
            )

    lines.append("")
    deferred_active = report["deferred_active"]
    lines.append(f"Deferred-active: {deferred_active['count']}")
    for path in deferred_active["paths"]:
        lines.append(f"  {path}")
    if deferred_active["truncated"] is True:
        lines.append(
            f"  ... and {deferred_active['count'] - len(deferred_active['paths'])} more"
        )

    lines.append("")
    lines.append(f"Result: {report['phase']}")

    staging_bytes = report.get("local_staging_bytes")
    if staging_bytes is not None:
        lines.append(f"Local backup staging: {_format_bytes(staging_bytes)}")

    return subject, "\n".join(lines)


def format_failure_email(report: dict[str, Any]) -> tuple[str, str]:
    subject = f"[OSF BACKUP FAILED] {report['txn_date']} — {report['phase']}"

    # Runner already stores only str(exc) in transactions.error, never a full
    # traceback, so this is a defensive cap, not routine truncation.
    error = report.get("error") or "(no error message recorded)"
    if len(error) > 2000:
        error = error[:2000] + " ... (truncated)"

    lines: list[str] = []
    lines.append(f"Transaction: {report['txn_id']}")
    lines.append(f"Failed phase: {report['phase']}")
    lines.append(f"Resume phase: {report.get('resume_phase') or 'unknown'}")
    lines.append(
        f"Progress: {report['progress']['uploaded']:,} / "
        f"{report['progress']['total_scan_items']:,} scan items uploaded"
    )
    lines.append(f"Error: {error}")

    if report.get("last_known_path"):
        lines.append(f"Last known path (best-effort): {report['last_known_path']}")

    staging_bytes = report.get("local_staging_bytes")
    if staging_bytes is not None:
        lines.append(f"Local backup staging: {_format_bytes(staging_bytes)}")

    return subject, "\n".join(lines)

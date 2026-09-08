"""Healthchecks.io dead-man monitoring client using only urllib from the stdlib.

Also home to the small primitives every notification transport shares
(``NotificationResult``, byte/duration formatting) -- this module has no
dependency on any other notifier, so it is the one place notify_email.py
and notify_ntfy.py can both import from without a cycle.
"""

from __future__ import annotations

import logging
import os
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

logger = logging.getLogger(__name__)

_MAX_BODY_BYTES = 10 * 1024

__all__ = [
    "HealthcheckClient",
    "NotificationResult",
    "format_success_body",
    "format_failure_body",
    "format_duration",
    "start",
    "success",
    "fail",
]


@dataclass(frozen=True)
class NotificationResult:
    """One outcome from one notification attempt, shape shared by every
    transport (email, ntfy, Healthchecks) so `backupctl status`/`doctor`
    render all three the same way instead of drifting into three slightly
    different result shapes."""

    status: str  # "sent" | "failed" | "disabled" | "misconfigured"
    attempts: int
    error: str | None = None


def format_duration(start_iso: str | None, end_iso: str | None) -> str | None:
    """Wall-clock duration between two ISO stamps, or None if either is
    missing/unparseable -- a report line must never be the reason a
    notification fails to render."""
    if not start_iso or not end_iso:
        return None
    try:
        start = datetime.fromisoformat(str(start_iso))
        end = datetime.fromisoformat(str(end_iso))
    except (TypeError, ValueError):
        return None
    seconds = int((end - start).total_seconds())
    if seconds < 0:
        return None
    hours, rem = divmod(seconds, 3600)
    minutes, secs = divmod(rem, 60)
    if hours:
        return f"{hours}h {minutes}m"
    return f"{minutes}m {secs}s" if minutes else f"{secs}s"


CREDENTIALS_ENV_PATH = Path(__file__).resolve().parent.parent / "config" / "credentials.env"


def _url_from_credentials_file() -> str | None:
    """Read BACKUP_HEALTHCHECKS_URL from the project's designated
    credentials file when the environment doesn't already carry it.

    app/storage.py and app/notify_email.py have always fallen back to that
    file, and config/credentials.env ships a commented
    ``BACKUP_HEALTHCHECKS_URL=`` line inviting the operator to fill it in.
    The value is a secret (the Healthchecks UUID is the whole credential),
    so it is never logged here -- see _mask_url.

    Renamed from the singular ``BACKUP_HEALTHCHECK_URL`` on 2026-09-08 to
    match Healthchecks.io's own product name and this session's ntfy
    integration spec -- done before any real URL was ever configured (the
    shipped line was still commented/empty), so this is a clean rename,
    not a migration.
    """
    try:
        raw = CREDENTIALS_ENV_PATH.read_text(encoding="utf-8")
    except OSError:
        return None
    for line in raw.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        if key.strip() == "BACKUP_HEALTHCHECKS_URL":
            return value.strip().strip('"').strip("'") or None
    return None


class HealthcheckClient:
    """Client for Healthchecks.io dead-man pings.

    Parameters
    ----------
    timeout:
        Optional per-request timeout in seconds. ``None`` disables the timeout.
        Defaults to 15 seconds.
    """

    def __init__(self, timeout: float | None = 15.0) -> None:
        if timeout is not None and timeout <= 0:
            raise ValueError("timeout must be positive or None")

        self.timeout = timeout

        raw_url = os.environ.get("BACKUP_HEALTHCHECKS_URL") or _url_from_credentials_file()
        self.url = raw_url.strip() if raw_url and raw_url.strip() else None

        if self.url is None:
            logger.info(
                "BACKUP_HEALTHCHECKS_URL is not set; healthcheck pings will be no-ops"
            )
        else:
            logger.debug("Healthcheck URL configured: %s", self._mask_url(self.url))

    def start(self) -> NotificationResult:
        """POST to ``<url>/start``."""
        return self._post("/start")

    def success(self, body: str) -> NotificationResult:
        """POST to ``<url>``."""
        return self._post("", body=body)

    def fail(self, body: str) -> NotificationResult:
        """POST to ``<url>/fail``."""
        return self._post("/fail", body=body)

    def _post(self, suffix: str, body: str | None = None) -> NotificationResult:
        """POST one ping, never raising -- every outcome (disabled,
        transport failure, non-2xx response) comes back as a
        NotificationResult so the caller can persist it for
        `backupctl status`/`doctor` (see Runner._record_healthchecks_result).
        A dead Healthchecks endpoint must never affect backup truth, which
        is why this returns rather than raises even on outright failure.
        """
        if self.url is None:
            label = suffix.lstrip("/") or "success"
            logger.info(
                "BACKUP_HEALTHCHECKS_URL is not set; skipping healthcheck %s ping",
                label,
            )
            return NotificationResult(status="disabled", attempts=0)

        url = self._endpoint_url(suffix)
        masked_url = self._mask_url(url)
        data = self._truncate_body(body) if body is not None else b""

        logger.debug("POST %s (%d bytes)", masked_url, len(data))

        req = urllib.request.Request(url, data=data, method="POST")
        req.add_header("Content-Type", "text/plain; charset=utf-8")

        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as response:
                # Read the small response body so the connection closes cleanly.
                response.read()
                status = getattr(response, "status", None)
                if status is not None and status >= 400:
                    logger.warning(
                        "Healthcheck ping to %s returned HTTP %s",
                        masked_url,
                        status,
                    )
                    return NotificationResult(
                        status="failed", attempts=1, error=f"HTTP {status}"
                    )
                return NotificationResult(status="sent", attempts=1)
        except Exception as exc:
            # Never fail the backup because the external monitoring ping failed.
            logger.warning(
                "Healthcheck ping to %s failed (%s)",
                masked_url,
                type(exc).__name__,
            )
            return NotificationResult(
                status="failed", attempts=1, error=f"{type(exc).__name__}: {exc}"
            )

    def _endpoint_url(self, suffix: str) -> str:
        base = self.url.rstrip("/")
        if not suffix:
            return base
        return f"{base}/{suffix.lstrip('/')}"

    @staticmethod
    def _truncate_body(body: str) -> bytes:
        """Encode and truncate diagnostic body text to 10KB of UTF-8 bytes."""
        encoded = body.encode("utf-8", errors="replace")
        if len(encoded) <= _MAX_BODY_BYTES:
            return encoded

        truncated = encoded[:_MAX_BODY_BYTES]
        # Avoid sending a broken multi-byte character at the truncation border.
        return truncated.decode("utf-8", errors="ignore").encode("utf-8")

    @staticmethod
    def _mask_url(url: str) -> str:
        """Return a safe, masked URL for logging.

        The Healthchecks.io ping URL contains a secret in the path.  Only the
        scheme, host, port, and a non-secret endpoint suffix such as ``/start``
        or ``/fail`` are included in the returned value.
        """
        parsed = urllib.parse.urlsplit(url)

        netloc = parsed.netloc
        if "@" in netloc:
            netloc = netloc.rsplit("@", 1)[-1]

        endpoint = ""
        if parsed.path.endswith("/start"):
            endpoint = "/start"
        elif parsed.path.endswith("/fail"):
            endpoint = "/fail"

        if parsed.scheme and netloc:
            return f"{parsed.scheme}://{netloc}/***{endpoint}"
        return f"***{endpoint}"


_default_client: HealthcheckClient | None = None


def _get_client() -> HealthcheckClient:
    global _default_client
    if _default_client is None:
        _default_client = HealthcheckClient()
    return _default_client


def start() -> None:
    """Convenience wrapper for the default client's :meth:`HealthcheckClient.start`."""
    _get_client().start()


def success(body: str) -> None:
    """Convenience wrapper for the default client's :meth:`HealthcheckClient.success`."""
    _get_client().success(body)


def fail(body: str) -> None:
    """Convenience wrapper for the default client's :meth:`HealthcheckClient.fail`."""
    _get_client().fail(body)


def format_success_body(
    *,
    selected_bytes: int,
    selected_files: int,
    new_blobs: int,
    deduplicated: int,
    deferred: int,
    skipped: int,
    repo_usage_bytes: int,
    repo_capacity_bytes: int,
    duration_seconds: float,
    snapshot_date: str,
    verification_result: str,
    log_path: str | None = None,
    last_completed_item: str | None = None,
) -> str:
    """Format an operator-facing success body for Healthchecks.io."""
    return _format_backup_body(
        status="Backup succeeded",
        selected_bytes=selected_bytes,
        selected_files=selected_files,
        new_blobs=new_blobs,
        deduplicated=deduplicated,
        deferred=deferred,
        skipped=skipped,
        repo_usage_bytes=repo_usage_bytes,
        repo_capacity_bytes=repo_capacity_bytes,
        duration_seconds=duration_seconds,
        snapshot_date=snapshot_date,
        verification_result=verification_result,
        phase=None,
        error=None,
        last_completed_item=last_completed_item,
        log_path=log_path,
    )


def format_failure_body(
    *,
    selected_bytes: int,
    selected_files: int,
    new_blobs: int,
    deduplicated: int,
    deferred: int,
    skipped: int,
    repo_usage_bytes: int,
    repo_capacity_bytes: int,
    duration_seconds: float,
    snapshot_date: str,
    error: str,
    log_path: str | None = None,
    verification_result: str | None = None,
    phase: str | None = None,
    last_completed_item: str | None = None,
) -> str:
    """Format an operator-facing failure body for Healthchecks.io."""
    return _format_backup_body(
        status="Backup FAILED",
        selected_bytes=selected_bytes,
        selected_files=selected_files,
        new_blobs=new_blobs,
        deduplicated=deduplicated,
        deferred=deferred,
        skipped=skipped,
        repo_usage_bytes=repo_usage_bytes,
        repo_capacity_bytes=repo_capacity_bytes,
        duration_seconds=duration_seconds,
        snapshot_date=snapshot_date,
        verification_result=verification_result,
        phase=phase,
        error=error,
        last_completed_item=last_completed_item,
        log_path=log_path,
    )


def _format_backup_body(
    *,
    status: str,
    selected_bytes: int,
    selected_files: int,
    new_blobs: int,
    deduplicated: int,
    deferred: int,
    skipped: int,
    repo_usage_bytes: int,
    repo_capacity_bytes: int,
    duration_seconds: float,
    snapshot_date: str,
    verification_result: str | None,
    phase: str | None,
    error: str | None,
    last_completed_item: str | None,
    log_path: str | None,
) -> str:
    lines = [
        status,
        f"- selected: {_format_bytes(selected_bytes)} in {selected_files} files",
        f"- new blobs: {new_blobs}",
        f"- deduplicated: {_format_bytes(deduplicated)}",
        f"- deferred: {deferred}",
        f"- skipped: {skipped}",
    ]

    if repo_capacity_bytes > 0:
        usage_pct = (repo_usage_bytes / repo_capacity_bytes) * 100
        lines.append(
            f"- repository: {_format_bytes(repo_usage_bytes)} / "
            f"{_format_bytes(repo_capacity_bytes)} ({usage_pct:.1f}%)"
        )
    else:
        lines.append(
            f"- repository: {_format_bytes(repo_usage_bytes)} / "
            f"{_format_bytes(repo_capacity_bytes)}"
        )

    lines.append(f"- duration: {_format_duration(duration_seconds)}")
    lines.append(f"- snapshot date: {snapshot_date or 'unknown'}")
    lines.append(f"- verification: {verification_result or 'not run'}")

    if phase:
        lines.append(f"- phase: {phase}")
    if error:
        lines.append(f"- error: {error}")
    if last_completed_item:
        lines.append(f"- last completed item: {last_completed_item}")
    if log_path:
        lines.append(f"- log: {log_path}")

    return "\n".join(lines)


def _format_bytes(num_bytes: int | float) -> str:
    value = float(num_bytes)
    if value == 0:
        return "0 B"

    units = ("B", "KiB", "MiB", "GiB", "TiB", "PiB")
    for unit in units:
        if abs(value) < 1024.0 or unit == units[-1]:
            if unit == "B":
                return f"{int(value)} B"
            return f"{value:.2f} {unit}"
        value /= 1024.0

    return f"{num_bytes} B"


def _format_duration(seconds: float) -> str:
    total_seconds = max(0, int(seconds))
    hours, remainder = divmod(total_seconds, 3600)
    minutes, secs = divmod(remainder, 60)

    if hours:
        return f"{hours}h {minutes:02d}m {secs:02d}s"
    if minutes:
        return f"{minutes}m {secs:02d}s"
    return f"{secs}s"

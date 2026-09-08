"""Shared utilities for the selective backup system at /workspace/_BACKUP."""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import sys
import tempfile
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import urlsplit, urlunsplit
from zoneinfo import ZoneInfo

VERSION = "1.0.0"

# ---------------------------------------------------------------------------
# Path constants
# ---------------------------------------------------------------------------
# Derived from this file's location rather than hardcoded, so the install can
# be relocated or restored to a different root without editing code.
INSTALL_ROOT = Path(__file__).resolve().parent.parent

APP_DIR = INSTALL_ROOT / "app"
BIN_DIR = INSTALL_ROOT / "bin"
CONFIG_DIR = INSTALL_ROOT / "config"
STATE_DIR = INSTALL_ROOT / "state"
REPORTS_DIR = INSTALL_ROOT / "reports"
LOGS_DIR = INSTALL_ROOT / "logs"
TMP_DIR = INSTALL_ROOT / "tmp"

DEFAULT_POLICY_PATH = CONFIG_DIR / "policy.yaml"
STATE_DB_PATH = STATE_DIR / "state.sqlite"
LOCK_PATH = STATE_DIR / "backupd.lock"
LOG_PATH = LOGS_DIR / "backupd.log"

HARD_EXCLUDED_INSTALL_SUBDIRS = (
    "state",
    "logs",
    "tmp",
    ".venv",
    "_inventory",
    "_gen",
)

# ---------------------------------------------------------------------------
# Time helpers
# ---------------------------------------------------------------------------

def _coerce_zone(tz: str | ZoneInfo) -> ZoneInfo:
    """Return a ZoneInfo instance from either a zone name or a ZoneInfo."""
    if isinstance(tz, ZoneInfo):
        return tz
    return ZoneInfo(tz)


def now_utc() -> datetime:
    """Return current UTC time as a timezone-aware datetime."""
    return datetime.now(timezone.utc)


def now_local(tz: str | ZoneInfo) -> datetime:
    """Return current time in the supplied IANA zone."""
    return datetime.now(_coerce_zone(tz))


def run_date_for(tz: str | ZoneInfo, at: datetime | None = None) -> str:
    """Return the local calendar date that owns *at* (default: now).

    This is the identity of a logical backup transaction. Taking an explicit
    instant matters on resume: a run that began at 23:59 and is resumed at
    00:02 must still be attributed to the date it started, which the caller
    supplies from persisted state rather than recomputing from the clock.
    """
    if at is None:
        return now_local(tz).date().isoformat()
    return at.astimezone(_coerce_zone(tz)).date().isoformat()


def next_midnight(tz: str | ZoneInfo, after: datetime | None = None) -> datetime:
    """Return the next local midnight strictly after *after* (default: now).

    DST correctness is achieved by advancing the calendar date in the local
    timezone and re-localising, rather than adding a fixed 86400 seconds --
    a Melbourne DST day is 23 or 25 hours long, not 24.
    """
    zone = _coerce_zone(tz)
    ref = (after or datetime.now(timezone.utc)).astimezone(zone)
    tomorrow = ref.date() + timedelta(days=1)
    return datetime(tomorrow.year, tomorrow.month, tomorrow.day, tzinfo=zone)


def iso(value: datetime | date | None = None) -> str:
    """Return an ISO-8601 string.

    Datetimes are formatted with second precision and retain their offset.
    Dates are formatted as YYYY-MM-DD. With no argument, returns "now" in UTC.
    """
    if value is None:
        value = now_utc()
    if isinstance(value, datetime):
        return value.isoformat(timespec="seconds")
    return value.isoformat()


# ---------------------------------------------------------------------------
# Human formatting helpers
# ---------------------------------------------------------------------------

def _format_decimal(value: float) -> str:
    """Format a float without useless trailing zeros."""
    text = f"{value:.2f}"
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text


def human_bytes(num: int | float) -> str:
    """Return a human-readable byte count using binary units."""
    value = float(num)
    if value < 0:
        return f"-{human_bytes(-value)}"
    if value == 0:
        return "0 B"

    units = ("B", "KiB", "MiB", "GiB", "TiB", "PiB", "EiB", "ZiB", "YiB")
    for unit in units:
        if value < 1024.0 or unit == units[-1]:
            if unit == "B":
                return f"{int(value)} B"
            return f"{_format_decimal(value)} {unit}"
        value /= 1024.0
    return f"{_format_decimal(value)} {units[-1]}"  # pragma: no cover - unreachable


def human_duration(seconds: int | float) -> str:
    """Return a human-readable duration string such as '2d 3h 4m 5s'."""
    total = int(seconds)
    if total < 0:
        return f"-{human_duration(-total)}"
    if total < 60:
        return f"{total}s"

    days, rem = divmod(total, 86400)
    hours, rem = divmod(rem, 3600)
    minutes, secs = divmod(rem, 60)

    parts: list[str] = []
    if days:
        parts.append(f"{days}d")
    if hours:
        parts.append(f"{hours}h")
    if minutes:
        parts.append(f"{minutes}m")
    if secs:
        parts.append(f"{secs}s")
    return " ".join(parts)


# ---------------------------------------------------------------------------
# Hashing
# ---------------------------------------------------------------------------

_CHUNK_SIZE = 4 * 1024 * 1024  # 4 MiB


def sha256_file(path: str | Path) -> str:
    """Return the SHA-256 hex digest of a file, streamed in 4 MiB chunks."""
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        while chunk := fh.read(_CHUNK_SIZE):
            digest.update(chunk)
    return digest.hexdigest()


def sha256_bytes(data: bytes) -> str:
    """Return the SHA-256 hex digest of a bytes object."""
    return hashlib.sha256(data).hexdigest()


# ---------------------------------------------------------------------------
# Blob key construction
# ---------------------------------------------------------------------------

def blob_key(prefix: str, digest: str) -> str:
    """Return a content-addressed blob key.

    Format: <prefix>/blobs/sha256/<first2>/<digest>
    """
    digest = digest.lower()
    if len(digest) < 2:
        raise ValueError("digest must contain at least two hex characters")
    clean_prefix = prefix.rstrip("/")
    return f"{clean_prefix}/blobs/sha256/{digest[:2]}/{digest}"


# ---------------------------------------------------------------------------
# Atomic file writes
# ---------------------------------------------------------------------------

def _fsync_dir(path: Path) -> None:
    """Best-effort fsync of a directory for atomic rename durability."""
    flags = os.O_RDONLY
    if hasattr(os, "O_DIRECTORY"):
        flags |= os.O_DIRECTORY
    try:
        fd = os.open(path, flags)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


def atomic_write_bytes(path: str | Path, data: bytes) -> None:
    """Atomically write bytes to *path* using a temp file in the same dir."""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)

    tmp_name: str | None = None
    try:
        fd, tmp_name = tempfile.mkstemp(
            prefix=f".{target.name}.",
            suffix=".tmp",
            dir=str(target.parent),
        )
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())

        os.replace(tmp_name, target)
        tmp_name = None
        _fsync_dir(target.parent)
    except BaseException:
        if tmp_name is not None:
            try:
                os.unlink(tmp_name)
            except FileNotFoundError:
                pass
        raise


def atomic_write_json(
    path: str | Path,
    obj: Any,
    *,
    indent: int = 2,
    sort_keys: bool = True,
) -> None:
    """Atomically serialize *obj* as UTF-8 JSON to *path*."""
    payload = json.dumps(obj, indent=indent, sort_keys=sort_keys, ensure_ascii=False)
    payload += "\n"
    atomic_write_bytes(path, payload.encode("utf-8"))


# ---------------------------------------------------------------------------
# Secret redaction
# ---------------------------------------------------------------------------

_SECRET_ENV_MARKERS = (
    "SECRET",
    "TOKEN",
    "PASSWORD",
    "PASSWD",
    "PASS",
    "PASSPHRASE",
    "API_KEY",
    "ACCESS_KEY",
    "SECRET_KEY",
    "PRIVATE_KEY",
    "CREDENTIAL",
    "CREDENTIALS",
    "AUTH",
    "KEY",
    # The Healthchecks ping URL is a bearer secret in URL form: anyone holding
    # it can forge a success ping and silence the dead-man alarm. It contains
    # none of the markers above, so it must be named explicitly.
    "HEALTHCHECK_URL",
    "HEALTHCHECK",
    "PAT",
    "SESSION",
    "SIGNATURE",
    "WEBHOOK",
)


def is_secret_env_name(name: str) -> bool:
    """Return True if an environment variable name looks secret."""
    normalised = re.sub(r"[^A-Z0-9]+", "_", name.upper()).strip("_")
    parts = normalised.split("_")

    for marker in _SECRET_ENV_MARKERS:
        marker_parts = tuple(marker.split("_"))
        for idx in range(len(parts) - len(marker_parts) + 1):
            if tuple(parts[idx : idx + len(marker_parts)]) == marker_parts:
                return True
    return False


def redact_env(env: Mapping[str, str] | None = None) -> dict[str, str]:
    """Return a copy of *env* (default: os.environ) with secret values masked.

    Used to build the machine reconstruction inventory that ships inside every
    snapshot, so the default must be the safe one.
    """
    if env is None:
        env = os.environ
    return {
        key: ("***" if is_secret_env_name(key) else value)
        for key, value in env.items()
    }


def redact_url(url: str) -> str:
    """Mask the final path segment of *url* (e.g. a Healthchecks ping UUID)."""
    if not url:
        return url

    try:
        parsed = urlsplit(url)
    except ValueError:
        return "***"

    path = parsed.path
    if not path:
        new_path = ""
    elif path.endswith("/"):
        trailing = "/"
        stripped = path.rstrip("/")
        if not stripped:
            new_path = path
        else:
            parts = stripped.rsplit("/", 1)
            if len(parts) == 2:
                new_path = f"{parts[0]}/***{trailing}"
            else:
                new_path = f"***{trailing}"
    else:
        parts = path.rsplit("/", 1)
        if len(parts) == 2:
            new_path = f"{parts[0]}/***"
        else:
            new_path = "***"

    return urlunsplit(
        (parsed.scheme, parsed.netloc, new_path, parsed.query, parsed.fragment)
    )


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

_LOG_FORMAT = "%(asctime)s %(levelname)s %(name)s: %(message)s"


def log_path_for(run_date: str | None = None) -> Path:
    """Path of the log file for a run, or the daemon log when run_date is None."""
    return LOGS_DIR / (f"backup-{run_date}.log" if run_date else "backupd.log")


def get_logger(name: str = "backup", run_date: str | None = None) -> logging.Logger:
    """Return a logger writing to stderr and to logs/.

    With *run_date*, the run gets its own log file, so a failure notification
    can point the operator at exactly one file for that day.

    The function is idempotent: repeated calls return an already-configured
    logger without adding duplicate handlers.
    """
    logger = logging.getLogger(name)
    if getattr(logger, "_backup_handlers_installed", False):
        return logger

    logger.setLevel(logging.INFO)
    logger.propagate = False

    formatter = logging.Formatter(_LOG_FORMAT)

    stderr_handler = logging.StreamHandler(sys.stderr)
    stderr_handler.setLevel(logging.INFO)
    stderr_handler.setFormatter(formatter)
    logger.addHandler(stderr_handler)

    LOGS_DIR.mkdir(parents=True, exist_ok=True)
    file_handler = logging.FileHandler(log_path_for(run_date), encoding="utf-8")
    file_handler.setLevel(logging.INFO)
    file_handler.setFormatter(formatter)
    logger.addHandler(file_handler)

    logger._backup_handlers_installed = True  # type: ignore[attr-defined]
    return logger


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------

class BackupError(Exception):
    """Base class for backup-related failures."""


class CapacityError(BackupError):
    """Raised when the destination does not have enough capacity."""


# ---------------------------------------------------------------------------
# Path helpers
# ---------------------------------------------------------------------------

def relpath(path: str | Path, start: str | Path = INSTALL_ROOT) -> str:
    """Return a relative path from *start*, defaulting to INSTALL_ROOT."""
    return os.path.relpath(path, start)



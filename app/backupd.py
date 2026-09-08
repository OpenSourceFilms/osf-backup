#!/workspace/_BACKUP/.venv/bin/python
"""app/backupd.py - long-running backup daemon with DST-correct scheduling.

The daemon coordinates a single logical backup transaction per local day.
Phase decisions are read directly from app.runner.Runner's own
``transactions`` table in state/state.sqlite -- the same database backupctl
and app.runner use, and the same table Runner exclusively writes to
(app.statedb.StateDB's ``runs`` table is a distinct, older table that
Runner never touches; backupd must not read it as if it reflected real
backup state). state/backupd.lock is a pure fcntl mutex; it holds no state
of its own (previously it doubled as a private JSON state file -- that was
a dual-authority bug: resume/catch-up/hold decisions must come from one
source of truth, not a daemon-private copy of it).

_run_transaction calls the real app.runner.Runner.run_for_date, and the
transactions table (written exclusively by Runner) is the sole source of
truth for SUCCESS/FAILED; backupd's own log is visibility only, never a
competing writer.
"""

from __future__ import annotations

import argparse
import fcntl
import json
import logging
import logging.handlers
import os
import signal
import sqlite3
import sys
import threading
import time
from datetime import datetime, timedelta, time as dt_time, timezone
from pathlib import Path
from typing import Any, Dict, Optional
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

BACKUP_HOME = Path(
    os.environ.get("BACKUP_HOME", Path(__file__).resolve().parent.parent)
).expanduser()
if str(BACKUP_HOME) not in sys.path:
    sys.path.insert(0, str(BACKUP_HOME))

logger = logging.getLogger("backupd")

DEFAULT_TIMEZONE = "Australia/Melbourne"
DEFAULT_RUN_HOUR = 4
DEFAULT_RUN_MINUTE = 0
DEFAULT_LOG_DIR = "logs"
DEFAULT_LOCK_FILE = "state/backupd.lock"
DEFAULT_STATE_DB = "state/state.sqlite"

# Phase vocabulary matches app.statedb's documented contract and
# app.runner.Phase exactly.
PHASE_PENDING = "PENDING"
PHASE_SCANNING = "SCANNING"
PHASE_UPLOADING = "UPLOADING"
PHASE_VERIFYING = "VERIFYING"
PHASE_COMMITTING = "COMMITTING"
PHASE_POST_VERIFY = "POST_VERIFY"
PHASE_SUCCESS = "SUCCESS"
PHASE_FAILED = "FAILED"
PHASE_ABANDONED = "ABANDONED"


class DaemonShutdown(Exception):
    """Raised when a shutdown signal is received during transaction work."""


class LockError(RuntimeError):
    """Raised when the exclusive instance lock cannot be acquired."""


def utc_now_iso() -> str:
    """Return a UTC ISO-8601 timestamp for state update fields."""
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _default_run_time_from_runtime_config() -> tuple[int, int]:
    """Read config/runtime.yaml's schedule.time ("HH:MM") as the default
    run_hour/run_minute -- the same field bin/backupctl's
    _next_melbourne_run() already treats as the single source of truth for
    "when does the nightly backup run" (used by backupctl status/doctor),
    both going through app.wiring._load_runtime_yaml. backupd must not
    invent its own separate default here; doing so would make its actual
    schedule silently drift from what status/doctor report, the same
    duplicate-authority failure mode app/wiring.py already exists to
    prevent for Runner construction.

    Deliberately reads config/runtime.yaml, not config/policy.yaml:
    scheduling is operational, not part of what Policy.policy_hash must
    fingerprint (see config/runtime.yaml's own header comment).

    Falls back to DEFAULT_RUN_HOUR/DEFAULT_RUN_MINUTE if runtime.yaml is
    missing or malformed -- validating runtime.yaml is backupctl doctor's
    job, not backupd startup's; the daemon should still be able to come up
    (e.g. in test/supervisor scenarios) rather than crash on a config file
    it doesn't own.
    """
    try:
        from app.wiring import _load_runtime_yaml

        raw_runtime = _load_runtime_yaml()
        hh, mm = (int(part) for part in str(raw_runtime["schedule"]["time"]).split(":"))
        return hh, mm
    except Exception as exc:
        logger.warning(
            "Could not read schedule.time from config/runtime.yaml (%s); "
            "using default run time %02d:%02d",
            exc,
            DEFAULT_RUN_HOUR,
            DEFAULT_RUN_MINUTE,
        )
        return DEFAULT_RUN_HOUR, DEFAULT_RUN_MINUTE


def load_config(config_path: Optional[str]) -> Dict[str, Any]:
    """Load optional JSON config, falling back to safe defaults.

    Config keys accepted: timezone, run_hour, run_minute, log_dir,
    log_level, lock_file, state_db. run_hour/run_minute default from
    config/runtime.yaml's schedule.time (see
    _default_run_time_from_runtime_config), not a hardcoded constant, so
    an explicit JSON config key is only ever an override of that shared
    source, never a competing default.
    """
    default_run_hour, default_run_minute = _default_run_time_from_runtime_config()
    config: Dict[str, Any] = {
        "timezone": DEFAULT_TIMEZONE,
        "run_hour": default_run_hour,
        "run_minute": default_run_minute,
        "log_dir": DEFAULT_LOG_DIR,
        "log_level": "INFO",
        "lock_file": DEFAULT_LOCK_FILE,
        "state_db": DEFAULT_STATE_DB,
    }

    if config_path is None:
        return config

    try:
        raw = Path(config_path).read_text(encoding="utf-8")
        data = json.loads(raw)
        if not isinstance(data, dict):
            logger.warning("Config file %s must contain a JSON object; ignoring", config_path)
            return config

        for key in config:
            if key in data:
                config[key] = data[key]
        for unknown in data.keys() - config.keys():
            logger.warning("Ignoring unknown config key %r from %s", unknown, config_path)
    except FileNotFoundError:
        logger.warning("Config file %s not found; using defaults", config_path)
    except json.JSONDecodeError as exc:
        logger.warning("Config file %s invalid JSON (%s); using defaults", config_path, exc)
    except OSError as exc:
        logger.warning("Cannot read config file %s (%s); using defaults", config_path, exc)

    return config


class BackupDaemon:
    """Single-instance backup daemon.

    The daemon never forks. It holds an exclusive fcntl lock on
    ``state/backupd.lock`` for its entire lifetime, purely as a mutex -- the
    lock file has no content. All run state lives in app.statedb.StateDB
    (``state/state.sqlite``), the same database backupctl and app.runner
    use, so resume/catch-up/hold decisions come from one source of truth.
    """

    def __init__(self, config: Dict[str, Any], once: bool = False, foreground: bool = False) -> None:
        self.config = config
        self.once = once
        self.foreground = foreground

        self._lock_file = None
        self._lock_held = False

        self._shutdown_event = threading.Event()
        self._retry_event = threading.Event()

    # ------------------------------------------------------------------
    # Path / logging / lock / statedb helpers
    # ------------------------------------------------------------------
    def _resolve(self, key: str, default: str) -> Path:
        raw = Path(self.config.get(key) or default)
        return raw if raw.is_absolute() else BACKUP_HOME / raw

    def _setup_logging(self) -> None:
        """Configure file logging under logs/ and optional console logging.

        The handlers are attached to BOTH the daemon's own ``backupd``
        logger and the ``app`` logger namespace. Until 2026-09-07 only
        ``backupd`` was wired up, and it is configured propagate=False, so
        every record from app.runner/app.scanner/app.storage/app.policy
        fell through to a root logger with no handlers at all: INFO
        vanished outright and WARNING/ERROR went to logging's lastResort
        stderr, i.e. into supervisor.out rather than the log this system
        tells operators to read. The visible symptom is in the archive --
        the whole 17-hour 2026-08-27 canary produced a 194-byte
        backupd.log holding three scheduler lines and not one word about
        what the backup actually did.

        ``app`` deliberately keeps propagate=True: nothing is attached to
        the root logger in production, so there is no duplicate emission,
        while pytest's caplog (which captures at the root) still sees
        app.* records.
        """
        log_dir = self._resolve("log_dir", DEFAULT_LOG_DIR)
        try:
            log_dir.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            logger.warning("Cannot create log directory %s: %s", log_dir, exc)
            return

        log_file = log_dir / "backupd.log"
        level_name = str(self.config.get("log_level", "INFO")).upper()
        level = getattr(logging, level_name, logging.INFO)

        root = logging.getLogger("backupd")
        root.setLevel(level)
        root.propagate = False

        formatter = logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s")

        # Everything the backup itself logs lives under the "app" package
        # logger; see this method's docstring for why it must share these
        # handlers rather than silently falling through to a bare root.
        app_logger = logging.getLogger("app")
        app_logger.setLevel(level)

        for target in (root, app_logger):
            # Matched on the resolved filename, not merely on handler type:
            # a handler already pointing somewhere ELSE must not count as
            # "already configured", or reconfiguring the log directory
            # silently keeps writing to the old file.
            if not any(
                isinstance(h, logging.handlers.TimedRotatingFileHandler)
                and Path(getattr(h, "baseFilename", "")) == log_file.resolve()
                for h in target.handlers
            ):
                fh = logging.handlers.TimedRotatingFileHandler(
                    log_file, when="midnight", backupCount=7, encoding="utf-8"
                )
                fh.setFormatter(formatter)
                target.addHandler(fh)

            if self.foreground:
                if not any(
                    isinstance(h, logging.StreamHandler) and h.stream is sys.stdout
                    for h in target.handlers
                ):
                    sh = logging.StreamHandler(sys.stdout)
                    sh.setFormatter(formatter)
                    target.addHandler(sh)

    def _acquire_lock(self) -> None:
        """Open state/backupd.lock and acquire an exclusive non-blocking flock.

        Pure mutex: nothing is ever read from or written to this file's
        content. Run state lives in StateDB instead.
        """
        lock_path = self._resolve("lock_file", DEFAULT_LOCK_FILE)
        lock_path.parent.mkdir(parents=True, exist_ok=True)

        try:
            fd = os.open(str(lock_path), os.O_RDWR | os.O_CREAT, 0o644)
            self._lock_file = os.fdopen(fd, "r+")
            fcntl.flock(self._lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            if self._lock_file:
                self._lock_file.close()
                self._lock_file = None
            raise LockError(f"Another backupd instance holds {lock_path}: {exc}") from exc

        self._lock_held = True
        logger.debug("Acquired exclusive lock on %s", lock_path)

    def _release_lock(self) -> None:
        """Release the lock and close the lock file."""
        if self._lock_file is not None:
            try:
                if self._lock_held:
                    fcntl.flock(self._lock_file.fileno(), fcntl.LOCK_UN)
                    self._lock_held = False
            except OSError as exc:
                logger.warning("Error releasing lock: %s", exc)
            finally:
                self._lock_file.close()
                self._lock_file = None

    def _get_transaction_phase(self, run_date: str) -> Optional[str]:
        """Read-only peek at app.runner.Runner's ``transactions`` table --
        the real, sole source of truth for backup phase (see module
        docstring). Returns None if no transaction has ever run for
        run_date, including when the table/file itself does not exist yet
        (a fresh install before the first real run) -- never raises for
        that case, since "nothing has run" is a legitimate, common state.
        """
        db_path = self._resolve("state_db", DEFAULT_STATE_DB)
        if not db_path.exists():
            return None
        conn = sqlite3.connect(str(db_path))
        try:
            try:
                row = conn.execute(
                    "SELECT phase FROM transactions WHERE txn_id = ?",
                    (f"backup-{run_date}",),
                ).fetchone()
            except sqlite3.OperationalError:
                return None
            return row[0] if row else None
        finally:
            conn.close()

    # ------------------------------------------------------------------
    # Time helpers
    # ------------------------------------------------------------------
    def _get_zoneinfo(self, tz_name: str) -> ZoneInfo:
        try:
            return ZoneInfo(tz_name)
        except ZoneInfoNotFoundError:
            logger.error(
                "Configured timezone %r not found; falling back to %s",
                tz_name,
                DEFAULT_TIMEZONE,
            )
            return ZoneInfo(DEFAULT_TIMEZONE)

    def _today_str(self, tz: ZoneInfo) -> str:
        return datetime.now(tz).strftime("%Y-%m-%d")

    def _run_datetime_on(self, tz: ZoneInfo, date_: Any) -> datetime:
        """Return an aware datetime at the configured run time on ``date_`` in ``tz``."""
        hour = self.config.get("run_hour", DEFAULT_RUN_HOUR)
        minute = self.config.get("run_minute", DEFAULT_RUN_MINUTE)
        return datetime.combine(date_, dt_time(hour, minute), tzinfo=tz)

    def _today_run_datetime(self, tz: ZoneInfo) -> datetime:
        """Return today's scheduled run time as an aware datetime in ``tz``."""
        return self._run_datetime_on(tz, datetime.now(tz).date())

    def _seconds_until_next_run_time(self, tz: ZoneInfo) -> float:
        """Return seconds until the next scheduled run time in ``tz``.

        The target is either today's configured run time or tomorrow's,
        depending on whether today's occurrence is still in the future.
        This deliberately uses the same DST-safe timestamp arithmetic as
        the previous midnight scheduler: shared ZoneInfo instances can make
        datetime subtraction silently lose the DST offset, while
        .timestamp() resolves each side to a true POSIX instant.
        """
        now = datetime.now(tz)

        try:
            today_target = self._today_run_datetime(tz)
            if today_target.timestamp() > now.timestamp():
                target = today_target
            else:
                target = self._run_datetime_on(tz, now.date() + timedelta(days=1))

            seconds = target.timestamp() - now.timestamp()
            if seconds > 0:
                return seconds
        except Exception:
            logger.warning(
                "Run time calculation failed for %s; using UTC probe fallback",
                tz,
            )

        probe = now
        for _ in range(48):
            probe = probe + timedelta(hours=1)
            try:
                candidate = self._run_datetime_on(tz, probe.date())
                if candidate.timestamp() > now.timestamp():
                    return max(candidate.timestamp() - now.timestamp(), 1.0)
            except Exception:
                continue

        logger.warning("Unable to calculate next run time; sleeping 3600s and recomputing")
        return 3600.0

    def _sleep_interruptible(self, seconds: float) -> None:
        """Sleep for ``seconds``, waking early on SIGTERM/SIGINT or SIGUSR1.

        SIGTERM/SIGINT raise DaemonShutdown. SIGUSR1 simply returns so the
        caller can handle a manual retry.
        """
        deadline = time.monotonic() + seconds
        while True:
            if self._shutdown_event.is_set():
                raise DaemonShutdown()
            if self._retry_event.is_set():
                return

            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return

            self._shutdown_event.wait(min(remaining, 0.5))

    # ------------------------------------------------------------------
    # Transaction state machine -- backed entirely by StateDB
    # ------------------------------------------------------------------
    def _run_transaction(self, run_date: str) -> None:
        """Run ``run_date``'s logical transaction via app.runner.Runner.

        Runner.run_for_date is idempotent and phase-keyed: it upserts the
        transactions row, no-ops on SUCCESS, resets-or-resumes on FAILED per
        its own policy-hash guard, and resumes from whatever phase is
        already persisted otherwise. Runner is the sole writer to
        transactions; this only logs failures for the daemon's own
        operational visibility, never a competing opinion about the result.

        The Runner is built with this daemon's own shutdown Event as its
        cooperative-cancellation predicate, so a SIGTERM arriving *during*
        a transaction stops it at the next safe boundary. Before
        2026-09-07 the call below was uninterruptible: the signal handler
        set the event, the main loop only checked it between transactions,
        and a multi-hour SCANNING/UPLOADING phase had to be SIGKILLed
        (confirmed live on 2026-08-29). A cancelled transaction is left at
        whatever phase it reached -- never marked FAILED -- so the next
        start resumes it.
        """
        from app import wiring
        from app.runner import TransactionCancelled

        runner = wiring.build_runner(should_stop=self._shutdown_event.is_set)
        try:
            runner.run_for_date(run_date)
        except TransactionCancelled as exc:
            logger.info(
                "Transaction for %s stopped for shutdown (%s); phase preserved for resume",
                run_date,
                exc,
            )
        except Exception:
            logger.exception("Transaction for %s failed", run_date)

    # ------------------------------------------------------------------
    # Signal handlers
    # ------------------------------------------------------------------
    def _install_signal_handlers(self) -> None:
        signal.signal(signal.SIGTERM, self._handle_shutdown_signal)
        signal.signal(signal.SIGINT, self._handle_shutdown_signal)

        if hasattr(signal, "SIGUSR1"):
            signal.signal(signal.SIGUSR1, self._handle_retry_signal)
        else:
            logger.warning("SIGUSR1 is not available on this platform; manual retry is disabled")

    def _handle_shutdown_signal(self, signum: int, frame: Any) -> None:
        sig_name = signal.Signals(signum).name
        logger.info("Received %s; requesting clean shutdown", sig_name)
        self._shutdown_event.set()

    def _handle_retry_signal(self, signum: int, frame: Any) -> None:
        logger.info("Received manual retry signal")
        self._retry_event.set()

    # ------------------------------------------------------------------
    # Main daemon loop
    # ------------------------------------------------------------------
    def run(self) -> int:
        self._setup_logging()
        try:
            self._acquire_lock()
        except LockError as exc:
            logger.error("%s", exc)
            return 1

        self._install_signal_handlers()

        try:
            while True:
                if self._shutdown_event.is_set():
                    logger.info(
                        "Shutdown requested; exiting main loop without starting "
                        "or resuming further work"
                    )
                    break

                tz = self._get_zoneinfo(str(self.config.get("timezone", DEFAULT_TIMEZONE)))
                today = self._today_str(tz)

                phase = self._get_transaction_phase(today)

                if phase == PHASE_SUCCESS:
                    logger.info(
                        "Today's transaction (%s) already SUCCESS; sleeping until next scheduled run time (04:00 local by default)",
                        today,
                    )
                    if self.once:
                        break
                    self._sleep_interruptible(self._seconds_until_next_run_time(tz))
                    continue

                if phase == PHASE_ABANDONED:
                    # Terminal by operator decision -- Runner.run_for_date
                    # would refuse it, and without this branch the
                    # "phase is not None" arm below would call it on every
                    # pass and spin the loop with no sleep.
                    logger.info(
                        "Today's transaction (%s) was deliberately ABANDONED; "
                        "waiting for the next scheduled run time",
                        today,
                    )
                    if self.once:
                        break
                    self._sleep_interruptible(self._seconds_until_next_run_time(tz))
                    continue

                if phase == PHASE_FAILED:
                    logger.warning(
                        "Today's transaction (%s) is FAILED; waiting for manual retry or next scheduled run time",
                        today,
                    )
                    if self.once:
                        logger.info("--once specified; not retrying FAILED transaction")
                        break
                    self._wait_for_manual_retry_or_next_run(tz, today)
                    continue

                if phase is not None:
                    logger.info(
                        "Transaction for %s is %s; running to resume/catch-up", today, phase
                    )
                    self._run_transaction(today)
                    if self.once:
                        break
                else:
                    now = datetime.now(tz)
                    run_time = self._today_run_datetime(tz)

                    if self.once or now >= run_time:
                        logger.info("No transaction for %s; running catch-up immediately", today)
                        self._run_transaction(today)
                        if self.once:
                            break
                    else:
                        seconds = run_time.timestamp() - now.timestamp()
                        logger.info(
                            "No transaction for %s; sleeping until today's scheduled run time (%.1fs)",
                            today,
                            seconds,
                        )
                        self._sleep_interruptible(seconds)
                        self._retry_event.clear()
                        continue

        except DaemonShutdown:
            logger.info("Shutdown requested during transaction; leaving current phase for resume")
        finally:
            self._release_lock()

        logger.info("Backup daemon stopped")
        return 0

    def _wait_for_manual_retry_or_next_run(self, tz: ZoneInfo, today: str) -> None:
        """Wait until SIGUSR1 or the next scheduled run time.

        If SIGUSR1 arrives, re-run today's previously FAILED transaction from
        the beginning. If the next scheduled run time arrives naturally,
        control returns to the main loop and a fresh daily transaction will
        be started.
        """
        self._retry_event.clear()
        seconds = self._seconds_until_next_run_time(tz)
        logger.info(
            "Waiting %.1f seconds for manual retry (SIGUSR1) or next scheduled run time",
            seconds,
        )

        self._sleep_interruptible(seconds)

        if self._retry_event.is_set():
            self._retry_event.clear()
            logger.info("Manual retry requested for failed transaction")

            now_today = self._today_str(tz)
            if now_today == today:
                self._run_transaction(now_today)
            else:
                logger.info("Date changed while waiting for retry; deferring to daily decision")


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Backup daemon scheduler")
    parser.add_argument("--once", action="store_true", help="Run one schedule decision and exit")
    parser.add_argument(
        "--foreground",
        action="store_true",
        help="Also log to stdout in addition to logs/",
    )
    parser.add_argument("--config", help="Path to JSON config file")
    args = parser.parse_args(argv)

    config = load_config(args.config)
    daemon = BackupDaemon(config, once=args.once, foreground=args.foreground)
    return daemon.run()


if __name__ == "__main__":
    sys.exit(main())

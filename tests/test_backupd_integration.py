"""Non-live integration tests for the backupd <-> Runner boundary.

Before this fix, app.backupd.BackupDaemon._run_transaction never called a
real Runner at all (app.runner exposed no run_today()); every cycle
recorded a programming-error PHASE_NOT_CONFIGURED. It now calls the real
app.wiring.build_runner().run_for_date(...), and polls phase directly from
Runner's own transactions table (app.statedb.StateDB's separate, older
'runs' table is never written by Runner and must not be read as if it
reflected real backup state -- see the module docstring in app/backupd.py).

These monkeypatch app.wiring.build_runner with a fake Runner double and
drive BackupDaemon.run(once=True) for one decision cycle at a time,
asserting only on what backupd itself decided to do. Real Runner behavior
(resume/reset semantics, policy-hash guard, etc.) is covered by
test_runner_failed_resume.py / test_runner_integration.py, not here.
"""

import sqlite3
import sys
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import backupd as app_backupd
from app import wiring as app_wiring

MELBOURNE = ZoneInfo("Australia/Melbourne")


def _today() -> str:
    return datetime.now(MELBOURNE).date().isoformat()


def _make_transactions_db(db_path, rows):
    conn = sqlite3.connect(str(db_path))
    conn.execute(
        "CREATE TABLE transactions (txn_id TEXT PRIMARY KEY, "
        "txn_date TEXT NOT NULL, phase TEXT NOT NULL)"
    )
    for txn_date, phase in rows:
        conn.execute(
            "INSERT INTO transactions (txn_id, txn_date, phase) VALUES (?, ?, ?)",
            (f"backup-{txn_date}", txn_date, phase),
        )
    conn.commit()
    conn.close()


class _FakeRunner:
    def __init__(self, calls, *, raises=None):
        self._calls = calls
        self._raises = raises

    def run_for_date(self, txn_date):
        self._calls.append(txn_date)
        if self._raises is not None:
            raise self._raises


def _make_daemon(tmp_path, once=True):
    config = {
        "timezone": "Australia/Melbourne",
        "log_dir": str(tmp_path / "logs"),
        "log_level": "INFO",
        "lock_file": str(tmp_path / "backupd.lock"),
        "state_db": str(tmp_path / "state.sqlite"),
    }
    return app_backupd.BackupDaemon(config, once=once, foreground=False)


def test_today_missing_runner_called_once_with_today(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(app_wiring, "build_runner", lambda **_kw: _FakeRunner(calls))
    _make_transactions_db(tmp_path / "state.sqlite", [])
    daemon = _make_daemon(tmp_path)
    daemon.run()
    assert calls == [_today()]


def test_today_success_runner_not_called(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(app_wiring, "build_runner", lambda **_kw: _FakeRunner(calls))
    _make_transactions_db(tmp_path / "state.sqlite", [(_today(), "SUCCESS")])
    daemon = _make_daemon(tmp_path)
    daemon.run()
    assert calls == []


def test_old_failed_later_success_today_missing_runs_current_date(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(app_wiring, "build_runner", lambda **_kw: _FakeRunner(calls))
    _make_transactions_db(
        tmp_path / "state.sqlite",
        [("2026-08-19", "FAILED"), ("2026-08-23", "SUCCESS")],
    )
    daemon = _make_daemon(tmp_path)
    daemon.run()
    assert calls == [_today()]


def test_old_abandoned_scanning_later_success_runs_current_date(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(app_wiring, "build_runner", lambda **_kw: _FakeRunner(calls))
    _make_transactions_db(
        tmp_path / "state.sqlite",
        [("2026-08-20", "SCANNING"), ("2026-08-23", "SUCCESS")],
    )
    daemon = _make_daemon(tmp_path)
    daemon.run()
    assert calls == [_today()]


def test_current_failed_does_not_auto_retry_in_once_mode(tmp_path, monkeypatch):
    """Today FAILED -> backupd waits for manual retry or midnight rather
    than immediately re-invoking Runner; once=True must not retry."""
    calls = []
    monkeypatch.setattr(app_wiring, "build_runner", lambda **_kw: _FakeRunner(calls))
    _make_transactions_db(tmp_path / "state.sqlite", [(_today(), "FAILED")])
    daemon = _make_daemon(tmp_path, once=True)
    daemon.run()
    assert calls == []


def test_runner_exception_is_recorded_visibly_not_masqueraded(tmp_path, monkeypatch, caplog):
    calls = []
    monkeypatch.setattr(
        app_wiring,
        "build_runner",
        lambda **_kw: _FakeRunner(calls, raises=RuntimeError("boom")),
    )
    _make_transactions_db(tmp_path / "state.sqlite", [])
    daemon = _make_daemon(tmp_path)
    with caplog.at_level("ERROR", logger="backupd"):
        daemon.run()
    assert calls == [_today()]
    assert any("failed" in rec.message.lower() for rec in caplog.records)


def test_daemon_restart_after_success_does_not_call_runner_again(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(app_wiring, "build_runner", lambda **_kw: _FakeRunner(calls))
    _make_transactions_db(tmp_path / "state.sqlite", [(_today(), "SUCCESS")])
    _make_daemon(tmp_path).run()
    _make_daemon(tmp_path).run()
    assert calls == []


@pytest.mark.parametrize(
    "phase",
    ["PENDING", "SCANNING", "UPLOADING", "VERIFYING", "COMMITTING", "POST_VERIFY"],
)
def test_in_progress_phase_resumes_via_runner_not_special_cased(tmp_path, monkeypatch, phase):
    calls = []
    monkeypatch.setattr(app_wiring, "build_runner", lambda **_kw: _FakeRunner(calls))
    _make_transactions_db(tmp_path / "state.sqlite", [(_today(), phase)])
    daemon = _make_daemon(tmp_path)
    daemon.run()
    assert calls == [_today()]


def test_transactions_table_missing_is_treated_as_no_transaction(tmp_path, monkeypatch):
    """Fresh install: state.sqlite doesn't exist yet at all."""
    calls = []
    monkeypatch.setattr(app_wiring, "build_runner", lambda **_kw: _FakeRunner(calls))
    daemon = _make_daemon(tmp_path)
    daemon.run()
    assert calls == [_today()]


def _freeze_now(monkeypatch, fixed_now):
    """Patch app.backupd's datetime.now to a fixed aware instant, the same
    technique the pre-existing DST regression test used."""

    class _FrozenDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            return fixed_now if tz is None else fixed_now.astimezone(tz)

    monkeypatch.setattr(app_backupd, "datetime", _FrozenDatetime)
    return fixed_now


@pytest.mark.parametrize(
    "now_iso,expected_seconds",
    [
        # Ordinary AEST (winter) day, well before today's 04:00 -- steady
        # +10:00 offset on both sides, no transition involved.
        ("2026-06-15T01:00:00", 3 * 3600.0),
        # Ordinary AEDT (summer) day, well before today's 04:00 -- steady
        # +11:00 offset on both sides, no transition involved.
        ("2026-12-15T01:00:00", 3 * 3600.0),
        # DST starts (2026-10-04, clocks skip 02:00->03:00 AEDT): 01:00 AEST
        # to 04:00 AEDT the same wall-clock day is only 2 real hours, not 3.
        ("2026-10-04T01:00:00", 2 * 3600.0),
        # DST ends (2026-04-05, clocks fall back 03:00 AEDT->02:00 AEST):
        # 01:00 AEDT to 04:00 AEST the same wall-clock day is 4 real hours,
        # not 3 -- the extra hour is genuinely re-lived.
        ("2026-04-05T01:00:00", 4 * 3600.0),
        # Ordinary day, already past today's 04:00 -- next run time rolls
        # over to tomorrow's 04:00 (5 hours away from 23:00).
        ("2026-06-15T23:00:00", 5 * 3600.0),
    ],
)
def test_next_run_time_is_correct_across_dst_transitions(
    tmp_path, monkeypatch, now_iso, expected_seconds
):
    """Locks in the same DST-correctness property the old midnight scheduler
    had, now retargeted at the 04:00 Australia/Melbourne production
    schedule: next_run_datetime and now previously would have shared the
    identical ZoneInfo object under naive subtraction, silently losing the
    1-hour DST shift on the two Melbourne transition nights each year
    (2026-10-04 spring-forward, 2026-04-05 fall-back). .timestamp()
    resolves true POSIX instants on both sides and is correct across the
    boundary regardless of which local hour is targeted.
    """
    fixed_now = datetime.fromisoformat(now_iso).replace(tzinfo=MELBOURNE)
    _freeze_now(monkeypatch, fixed_now)
    daemon = _make_daemon(tmp_path)
    seconds = daemon._seconds_until_next_run_time(MELBOURNE)
    assert seconds == pytest.approx(expected_seconds)


@pytest.mark.parametrize(
    "hour,minute,expect_due",
    [(3, 59, False), (4, 0, True), (4, 1, True)],
)
def test_today_run_time_due_boundary(tmp_path, monkeypatch, hour, minute, expect_due):
    """Exercises the exact 'now >= today_run_datetime' comparison run() uses
    to decide whether a still-PENDING/never-started today's transaction
    should launch immediately or wait for the scheduled slot."""
    fixed_now = datetime(2026, 6, 15, hour, minute, tzinfo=MELBOURNE)
    _freeze_now(monkeypatch, fixed_now)
    daemon = _make_daemon(tmp_path)
    run_time = daemon._today_run_datetime(MELBOURNE)
    assert (fixed_now >= run_time) is expect_due


def test_success_today_after_run_time_sleeps_until_tomorrows_run_time(tmp_path, monkeypatch):
    """A daemon that starts (or wakes) well after today's already-SUCCESS
    04:00 run -- e.g. a supervisor/pod restart later the same day -- must
    not re-run today, and must compute tomorrow's 04:00 as the next sleep
    target, not some remainder of today's already-passed slot."""
    fixed_now = datetime(2026, 6, 15, 10, 0, tzinfo=MELBOURNE)
    _freeze_now(monkeypatch, fixed_now)
    today_str = fixed_now.date().isoformat()
    _make_transactions_db(tmp_path / "state.sqlite", [(today_str, "SUCCESS")])

    calls = []
    monkeypatch.setattr(app_wiring, "build_runner", lambda **_kw: _FakeRunner(calls))

    daemon = _make_daemon(tmp_path, once=False)
    sleeps = []

    def fake_sleep(seconds):
        sleeps.append(seconds)
        raise app_backupd.DaemonShutdown()

    monkeypatch.setattr(daemon, "_sleep_interruptible", fake_sleep)
    daemon.run()

    assert calls == []
    assert sleeps == [pytest.approx(18 * 3600.0)]


def _write_runtime_yaml(tmp_path, schedule_time):
    runtime_path = tmp_path / "runtime.yaml"
    runtime_path.write_text(
        f"""
schedule:
  timezone: Australia/Melbourne
  time: "{schedule_time}"
  catch_up: true
  resume_interrupted: true
""",
        encoding="utf-8",
    )
    return runtime_path


def test_load_config_defaults_run_time_from_runtime_yaml_schedule_time(tmp_path, monkeypatch):
    """backupd must not invent its own schedule default independently of
    config/runtime.yaml's schedule.time -- the same field bin/backupctl's
    status/doctor already treat as authoritative -- or the two can
    silently disagree about when the nightly backup actually runs."""
    monkeypatch.setattr(app_wiring, "RUNTIME_PATH", _write_runtime_yaml(tmp_path, "05:30"))
    config = app_backupd.load_config(None)
    assert (config["run_hour"], config["run_minute"]) == (5, 30)


def test_load_config_json_override_wins_over_runtime_yaml(tmp_path, monkeypatch):
    """An explicit --config JSON file is a deliberate operator override and
    must still take precedence over the runtime.yaml-derived default."""
    monkeypatch.setattr(app_wiring, "RUNTIME_PATH", _write_runtime_yaml(tmp_path, "05:30"))
    config_path = tmp_path / "backupd_config.json"
    config_path.write_text('{"run_hour": 9, "run_minute": 15}', encoding="utf-8")
    config = app_backupd.load_config(str(config_path))
    assert (config["run_hour"], config["run_minute"]) == (9, 15)


def test_load_config_falls_back_to_default_when_runtime_yaml_missing(tmp_path, monkeypatch):
    """A missing/unreadable runtime.yaml must not prevent backupd from
    starting -- validating runtime.yaml is backupctl doctor's job."""
    monkeypatch.setattr(app_wiring, "RUNTIME_PATH", tmp_path / "does-not-exist.yaml")
    config = app_backupd.load_config(None)
    assert (config["run_hour"], config["run_minute"]) == (
        app_backupd.DEFAULT_RUN_HOUR,
        app_backupd.DEFAULT_RUN_MINUTE,
    )


def test_load_runtime_yaml_raises_on_missing_file(tmp_path, monkeypatch):
    """Unlike backupd's load_config above (which falls back to safe
    defaults), app.wiring._load_runtime_yaml itself must raise on a
    missing file -- this is exactly what makes `backupctl doctor`'s
    "runtime config parses and validates" check fail explicitly instead
    of silently passing."""
    monkeypatch.setattr(app_wiring, "RUNTIME_PATH", tmp_path / "does-not-exist.yaml")
    with pytest.raises(FileNotFoundError):
        app_wiring._load_runtime_yaml()


def test_load_runtime_yaml_raises_on_malformed_non_mapping_content(tmp_path, monkeypatch):
    """A runtime.yaml that parses as YAML but isn't a top-level mapping
    (e.g. a bare list) must also raise, not silently produce a dict-like
    KeyError further down the line."""
    bad_path = tmp_path / "runtime.yaml"
    bad_path.write_text("- not\n- a\n- mapping\n", encoding="utf-8")
    monkeypatch.setattr(app_wiring, "RUNTIME_PATH", bad_path)
    with pytest.raises(ValueError):
        app_wiring._load_runtime_yaml()


def test_daemon_log_file_captures_the_backups_own_records(tmp_path):
    """logs/backupd.log must contain what the backup DID, not only what the
    scheduler decided.

    Until 2026-09-07 _setup_logging attached its handlers to the ``backupd``
    logger alone, and that logger is configured propagate=False. Every
    app.runner/app.scanner/app.storage record therefore fell through to a
    root logger with no handlers: INFO was discarded outright, and
    WARNING/ERROR went to logging's lastResort stderr -- into
    supervisor.out, not the log operators are told to read. The archived
    evidence is unambiguous: the entire 17-hour 2026-08-27 canary produced
    a 194-byte backupd.log holding three scheduler lines and nothing about
    the backup itself.
    """
    import logging

    daemon = _make_daemon(tmp_path)
    app_logger = logging.getLogger("app")
    previous = list(app_logger.handlers)
    try:
        daemon._setup_logging()
        logging.getLogger("app.runner").info("scan summary line")
        logging.getLogger("app.runner").warning("unclassified warning line")
        for handler in app_logger.handlers:
            handler.flush()

        written = (tmp_path / "logs" / "backupd.log").read_text(encoding="utf-8")
    finally:
        for handler in list(app_logger.handlers):
            if handler not in previous:
                app_logger.removeHandler(handler)
                handler.close()

    assert "scan summary line" in written
    assert "unclassified warning line" in written
    assert "app.runner" in written

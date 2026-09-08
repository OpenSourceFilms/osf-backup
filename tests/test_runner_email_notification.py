"""Regression tests for Runner's email-notification integration
(_send_transaction_email/_record_notification_result) -- added alongside
the daily email report feature (app/notify_email.py). Mirrors
tests/test_runner_failed_resume.py's _make_runner helper (a real Runner
built via object.__new__ + a real sqlite3 state DB, config as a
SimpleNamespace) rather than a full wiring.build_runner() Config, since
this only needs to exercise the email/notification wiring in run_for_date,
not the rest of the pipeline.
"""

import sqlite3
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import runner as app_runner
from app.notify_email import NotificationResult

CURRENT_POLICY_HASH = "test-hash"


class _FakeEmailClient:
    def __init__(self, *, success_result=None, failure_result=None, raise_on_success=False):
        self.success_calls = []
        self.failure_calls = []
        self._success_result = success_result or NotificationResult(status="sent", attempts=1)
        self._failure_result = failure_result or NotificationResult(status="sent", attempts=1)
        self._raise_on_success = raise_on_success

    def success(self, report):
        if self._raise_on_success:
            raise RuntimeError("simulated SMTP client crash")
        self.success_calls.append(report)
        return self._success_result

    def failure(self, report):
        self.failure_calls.append(report)
        return self._failure_result


def _make_runner(tmp_path, *, email=None, source_root=None):
    conn = sqlite3.connect(tmp_path / "state.sqlite")
    conn.row_factory = sqlite3.Row
    r = object.__new__(app_runner.Runner)
    r.state_db = conn
    r.staging_dir = tmp_path / "staging"
    r._init_schema()
    r.config = SimpleNamespace(
        healthcheck=SimpleNamespace(
            start=lambda: None, success=lambda s: None, fail=lambda e: None
        ),
        policy=SimpleNamespace(policy_hash=CURRENT_POLICY_HASH),
        email=email,
        source_root=source_root or (tmp_path / "source"),
    )
    return r


def _seed_scan_item(r, txn_id, path="/workspace/erf/a.txt"):
    r.state_db.execute(
        "INSERT INTO scan_items (txn_id, path, size, mtime_ns, classification, upload_status) "
        "VALUES (?, ?, 10, 0, 'CRITICAL', 'UPLOADED')",
        (txn_id, path),
    )
    r.state_db.commit()


def test_success_sends_email_and_records_notification_result(tmp_path, monkeypatch):
    email = _FakeEmailClient()
    r = _make_runner(tmp_path, email=email, source_root=Path("/workspace"))
    txn_id, txn_date = "backup-2026-08-28", "2026-08-28"

    def fake_run_phases(txn_id_arg, txn_date_arg):
        r._set_phase(txn_id_arg, app_runner.Phase.SUCCESS)
        _seed_scan_item(r, txn_id_arg)

    monkeypatch.setattr(r, "_run_phases", fake_run_phases)
    r.run_for_date(txn_date)

    assert len(email.success_calls) == 1
    assert email.success_calls[0]["txn_id"] == txn_id
    assert len(email.failure_calls) == 0

    row = dict(r.state_db.execute(
        "SELECT notification_status, notification_attempts, notification_error "
        "FROM transactions WHERE txn_id = ?", (txn_id,),
    ).fetchone())
    assert row["notification_status"] == "sent"
    assert row["notification_attempts"] == 1
    assert row["notification_error"] is None


def test_failure_sends_failure_email_and_still_reraises(tmp_path, monkeypatch):
    email = _FakeEmailClient()
    r = _make_runner(tmp_path, email=email, source_root=Path("/workspace"))
    txn_date = "2026-08-29"

    def fake_run_phases(txn_id_arg, txn_date_arg):
        raise RuntimeError("simulated backup failure")

    monkeypatch.setattr(r, "_run_phases", fake_run_phases)

    try:
        r.run_for_date(txn_date)
        assert False, "run_for_date should have re-raised"
    except RuntimeError as exc:
        assert "simulated backup failure" in str(exc)

    assert len(email.failure_calls) == 1
    assert email.failure_calls[0]["phase"] == "FAILED"
    assert len(email.success_calls) == 0


def test_email_client_crash_never_prevents_success_or_propagates(tmp_path, monkeypatch):
    """Email must never control backup truth (Daniel's explicit
    requirement): a broken email client crashing must not turn a SUCCESS
    into an exception or a FAILED transaction."""
    email = _FakeEmailClient(raise_on_success=True)
    r = _make_runner(tmp_path, email=email, source_root=Path("/workspace"))
    txn_id, txn_date = "backup-2026-08-28", "2026-08-28"

    def fake_run_phases(txn_id_arg, txn_date_arg):
        r._set_phase(txn_id_arg, app_runner.Phase.SUCCESS)
        _seed_scan_item(r, txn_id_arg)

    monkeypatch.setattr(r, "_run_phases", fake_run_phases)

    r.run_for_date(txn_date)  # must not raise

    phase = r.state_db.execute(
        "SELECT phase FROM transactions WHERE txn_id = ?", (txn_id,)
    ).fetchone()["phase"]
    assert phase == "SUCCESS"


def test_no_email_client_is_a_silent_noop(tmp_path, monkeypatch):
    r = _make_runner(tmp_path, email=None, source_root=Path("/workspace"))
    txn_id, txn_date = "backup-2026-08-28", "2026-08-28"

    def fake_run_phases(txn_id_arg, txn_date_arg):
        r._set_phase(txn_id_arg, app_runner.Phase.SUCCESS)

    monkeypatch.setattr(r, "_run_phases", fake_run_phases)
    r.run_for_date(txn_date)  # must not raise

    row = dict(r.state_db.execute(
        "SELECT notification_status FROM transactions WHERE txn_id = ?", (txn_id,),
    ).fetchone())
    assert row["notification_status"] is None

"""Cooperative-cancellation tests for the mid-transaction stop path added
2026-09-07.

Why this exists: until this fix, app/backupd.py's SIGTERM handler only took
effect *between* transactions -- Runner.run_for_date ran to completion no
matter what, so stopping a multi-hour SCANNING/UPLOADING phase required
SIGKILL (confirmed live on 2026-08-29, where CPU kept climbing after the
SIGTERM was logged as received). Controlled daemon/pod restart is a gate on
this system going into production, so "can we stop cleanly, mid-flight,
without lying about the result" is a first-class behaviour, not a nicety.

The two properties that matter, and that these tests pin:
  * a stop is NOT a failure -- the transaction keeps whatever phase it
    reached, is never marked FAILED, and sends no failure notification, so
    the next start resumes rather than resetting; and
  * a stop only ever lands between whole units of work, so every durable
    row is either fully done or untouched.

Built as bare Runner instances (schema + sqlite only) in the same
lightweight style as tests/test_runner_failed_resume.py -- no live S3.
"""

import sqlite3
import sys
import inspect
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import runner as app_runner
from app import wiring
from app.backupd import BackupDaemon


def _make_runner(tmp_path):
    conn = sqlite3.connect(tmp_path / "state.sqlite")
    conn.row_factory = sqlite3.Row
    r = object.__new__(app_runner.Runner)
    r.state_db = conn
    r._init_schema()
    r.config = SimpleNamespace(
        healthcheck=SimpleNamespace(
            start=lambda: None,
            success=lambda s: None,
            fail=lambda e: None,
        ),
        policy=SimpleNamespace(policy_hash="test-policy-hash-v1"),
        should_stop=None,
        blob_store=None,
        capacity=SimpleNamespace(
            hard_threshold_bytes=10**12,
            soft_threshold_bytes=10**12,
            current_usage_bytes=lambda: 0,
        ),
        key_prefix="",
        capacity_soft_ratio=0.8,
        verify_download_cap_bytes=0,
        email=None,
        gc_weekday=None,
    )
    return r


def test_should_stop_none_never_cancels(tmp_path):
    """A missing stop hook must not block a good backup."""
    r = _make_runner(tmp_path)
    r.config.should_stop = None

    assert r._raise_if_cancelled("UPLOADING") is None


def test_should_stop_true_raises_with_where(tmp_path):
    """A cancel signal must surface as the dedicated cancellation exception and include the phase."""
    r = _make_runner(tmp_path)
    r.config.should_stop = lambda: True

    with pytest.raises(app_runner.TransactionCancelled) as exc:
        r._raise_if_cancelled("VERIFYING")

    assert "VERIFYING" in str(exc.value)


def test_broken_should_stop_hook_does_not_cancel(tmp_path):
    """A broken external hook should never take down an otherwise good backup."""
    r = _make_runner(tmp_path)

    def broken_hook():
        raise RuntimeError("broken external hook")

    r.config.should_stop = broken_hook

    assert r._raise_if_cancelled("COMMITTING") is None


def test_run_phases_cancels_at_pending_boundary(tmp_path):
    """Cancellation at the phase boundary keeps the transaction out of SUCCESS."""
    r = _make_runner(tmp_path)
    r.config.should_stop = lambda: True

    txn_id = "txn-boundary"
    r._upsert_transaction(txn_id, "2024-01-01")

    with pytest.raises(app_runner.TransactionCancelled):
        r._run_phases(txn_id, "2024-01-01")

    phase = r._get_phase(txn_id)
    assert phase in (app_runner.Phase.PENDING, app_runner.Phase.SCANNING)
    assert phase != app_runner.Phase.SUCCESS


def test_run_for_date_cancellation_does_not_mark_failed_or_email(tmp_path, monkeypatch):
    """Cancellation is not a backup failure: it must not mark FAILED or send failure email."""
    r = _make_runner(tmp_path)
    txn_date = "2024-01-01"
    txn_id = f"backup-{txn_date}"

    healthcheck_fail_calls = []
    r.config.healthcheck.fail = lambda e: healthcheck_fail_calls.append(e)

    email_calls = []
    r._send_transaction_notifications = lambda txn_id, success: email_calls.append(
        (txn_id, success)
    )

    def cancelled_phases(txn_id, txn_date):
        raise app_runner.TransactionCancelled("cancel requested")

    monkeypatch.setattr(r, "_run_phases", cancelled_phases)

    with pytest.raises(app_runner.TransactionCancelled):
        r.run_for_date(txn_date)

    row = r.state_db.execute(
        "SELECT phase, resume_phase FROM transactions WHERE txn_id = ?",
        (txn_id,),
    ).fetchone()

    assert row["phase"] != app_runner.Phase.FAILED.value
    assert row["phase"] == app_runner.Phase.PENDING.value
    assert row["resume_phase"] is None
    assert healthcheck_fail_calls == []
    assert email_calls == []


def test_run_for_date_generic_exception_marks_failed(tmp_path, monkeypatch):
    """Generic errors still follow the failure path, proving the cancellation test above is specific."""
    r = _make_runner(tmp_path)
    txn_date = "2024-01-01"
    txn_id = f"backup-{txn_date}"

    healthcheck_fail_calls = []
    r.config.healthcheck.fail = lambda e: healthcheck_fail_calls.append(e)

    email_calls = []
    r._send_transaction_notifications = lambda txn_id, success: email_calls.append(
        (txn_id, success)
    )

    def broken_phases(txn_id, txn_date):
        raise RuntimeError("boom")

    monkeypatch.setattr(r, "_run_phases", broken_phases)

    with pytest.raises(RuntimeError, match="boom"):
        r.run_for_date(txn_date)

    row = r.state_db.execute(
        "SELECT phase FROM transactions WHERE txn_id = ?", (txn_id,)
    ).fetchone()

    assert row["phase"] == app_runner.Phase.FAILED.value
    assert len(healthcheck_fail_calls) == 1
    assert email_calls == [(txn_id, False)]


def test_upload_stops_between_files(tmp_path):
    """Cancellation during upload leaves exactly the completed row UPLOADED, so a resume can reprocess the rest."""
    r = _make_runner(tmp_path)
    txn_id = "txn-upload"

    r._upsert_transaction(txn_id, "2024-01-02")

    files = []
    for name in ("a.txt", "b.txt", "c.txt"):
        p = tmp_path / name
        p.write_bytes(f"content-{name}".encode())
        files.append(p)
        st = p.stat()
        r.state_db.execute(
            "INSERT INTO scan_items "
            "(txn_id, path, size, mtime_ns, classification, upload_status, verify_status) "
            "VALUES (?, ?, ?, ?, ?, 'PENDING_UPLOAD', 'PENDING')",
            (txn_id, str(p), st.st_size, st.st_mtime_ns, "CRITICAL"),
        )
    r.state_db.commit()

    put_keys = []

    class FakeBlobStore:
        def head_object(self, key):
            return None

        def put_object_from_file(self, key, path):
            put_keys.append(key)

    r.config.blob_store = FakeBlobStore()

    calls = 0

    def should_stop():
        nonlocal calls
        calls += 1
        return calls > 1

    r.config.should_stop = should_stop

    with pytest.raises(app_runner.TransactionCancelled):
        r._upload(txn_id)

    rows = r.state_db.execute(
        "SELECT path, upload_status FROM scan_items WHERE txn_id = ? ORDER BY path",
        (txn_id,),
    ).fetchall()

    status_by_path = {row["path"]: row["upload_status"] for row in rows}
    assert status_by_path[str(files[0])] == app_runner.UploadStatus.UPLOADED.value
    assert (
        status_by_path[str(files[1])] == app_runner.UploadStatus.PENDING_UPLOAD.value
    )
    assert (
        status_by_path[str(files[2])] == app_runner.UploadStatus.PENDING_UPLOAD.value
    )
    assert len(put_keys) == 1


def test_verify_stops_between_files(tmp_path, monkeypatch):
    """Cancellation during verification leaves only already-verified items verified, keeping resume safe."""
    r = _make_runner(tmp_path)
    txn_id = "txn-verify"

    r._upsert_transaction(txn_id, "2024-01-02")

    r.state_db.execute(
        "INSERT INTO scan_items "
        "(txn_id, path, size, mtime_ns, classification, sha256, blob_key, upload_status, verify_status) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, 'UPLOADED', 'PENDING')",
        (txn_id, "a", 10, 0, "CRITICAL", "dummy-sha-a", "key-a"),
    )
    r.state_db.execute(
        "INSERT INTO scan_items "
        "(txn_id, path, size, mtime_ns, classification, sha256, blob_key, upload_status, verify_status) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, 'UPLOADED', 'PENDING')",
        (txn_id, "b", 10, 0, "CRITICAL", "dummy-sha-b", "key-b"),
    )
    r.state_db.commit()

    monkeypatch.setattr(r, "_head_object_after_upload", lambda key: 10)

    calls = 0

    def should_stop():
        nonlocal calls
        calls += 1
        return calls > 1

    r.config.should_stop = should_stop
    r.config.verify_download_cap_bytes = 0

    with pytest.raises(app_runner.TransactionCancelled):
        r._verify(txn_id)

    rows = r.state_db.execute(
        "SELECT path, verify_status FROM scan_items WHERE txn_id = ? ORDER BY path",
        (txn_id,),
    ).fetchall()

    assert rows[0]["verify_status"] == app_runner.VerifyStatus.VERIFIED.value
    assert rows[1]["verify_status"] == app_runner.VerifyStatus.PENDING.value


def test_wiring_build_runner_accepts_should_stop():
    """The wiring layer must expose should_stop so daemons can cancel cleanly."""
    build_sig = inspect.signature(wiring.build_runner)
    assert "should_stop" in build_sig.parameters

    config_sig = inspect.signature(app_runner.Config)
    assert "should_stop" in config_sig.parameters
    assert config_sig.parameters["should_stop"].default is None


def test_backup_daemon_passes_should_stop_and_swallows_cancellation(monkeypatch):
    """The daemon must hand its shutdown event to the runner and treat cancellation as a quiet stop."""
    captured_kwargs = {}

    class FakeRunner:
        def run_for_date(self, run_date):
            raise app_runner.TransactionCancelled("cancelled")

    def fake_build_runner(**kwargs):
        captured_kwargs.update(kwargs)
        return FakeRunner()

    monkeypatch.setattr(wiring, "build_runner", fake_build_runner)

    daemon = BackupDaemon({})

    assert daemon._run_transaction("2024-01-02") is None

    assert "should_stop" in captured_kwargs
    should_stop = captured_kwargs["should_stop"]

    assert should_stop() is False
    daemon._shutdown_event.set()
    assert should_stop() is True

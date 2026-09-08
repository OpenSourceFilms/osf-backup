"""Regression tests for Runner notification wiring: ntfy, email, Healthchecks."""
import sqlite3
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import healthcheck as hc
from app import runner as app_runner
from app.healthcheck import NotificationResult

CURRENT_POLICY_HASH = "test-hash"


class _FakeHealthcheck:
    def __init__(self):
        self.calls = []
        self.start_result = NotificationResult(status="sent", attempts=1)
        self.success_result = NotificationResult(status="sent", attempts=1)
        self.fail_result = NotificationResult(status="sent", attempts=1)

    def start(self):
        self.calls.append(("start", None))
        return self.start_result

    def success(self, body):
        self.calls.append(("success", body))
        return self.success_result

    def fail(self, body):
        self.calls.append(("fail", body))
        return self.fail_result


class _RaisingHealthcheck:
    """A HealthcheckClient double whose start() raises outright, to prove
    Runner._ping_healthcheck's own try/except (not just HealthcheckClient's
    internal one) isolates a misbehaving client."""

    def start(self):
        raise RuntimeError("simulated healthcheck client crash")

    def success(self, body):
        raise RuntimeError("simulated healthcheck client crash")

    def fail(self, body):
        raise RuntimeError("simulated healthcheck client crash")


class _FakeNtfyClient:
    def __init__(
        self,
        success_result=None,
        failure_result=None,
        interrupted_result=None,
        raise_on_success=False,
    ):
        self.success_calls = []
        self.failure_calls = []
        self.interrupted_calls = []
        self._success_result = success_result or NotificationResult(status="sent", attempts=1)
        self._failure_result = failure_result or NotificationResult(status="sent", attempts=1)
        self._interrupted_result = interrupted_result or NotificationResult(status="sent", attempts=1)
        self._raise_on_success = raise_on_success

    def success(self, report):
        if self._raise_on_success:
            raise RuntimeError("simulated ntfy client crash")
        self.success_calls.append(report)
        return self._success_result

    def failure(self, report):
        self.failure_calls.append(report)
        return self._failure_result

    def interrupted(self, txn_id, phase, reason):
        self.interrupted_calls.append((txn_id, phase, reason))
        return self._interrupted_result


class _FakeEmailClient:
    def __init__(self, success_result=None, failure_result=None):
        self.success_calls = []
        self.failure_calls = []
        self._success_result = success_result or NotificationResult(status="sent", attempts=1)
        self._failure_result = failure_result or NotificationResult(status="sent", attempts=1)

    def success(self, report):
        self.success_calls.append(report)
        return self._success_result

    def failure(self, report):
        self.failure_calls.append(report)
        return self._failure_result


def _make_runner(tmp_path, *, ntfy=None, email=None, healthcheck=None, source_root=None):
    conn = sqlite3.connect(tmp_path / "state.sqlite")
    conn.row_factory = sqlite3.Row
    r = object.__new__(app_runner.Runner)
    r.state_db = conn
    r.staging_dir = tmp_path / "staging"
    r._init_schema()
    r.config = SimpleNamespace(
        healthcheck=healthcheck or _FakeHealthcheck(),
        policy=SimpleNamespace(policy_hash=CURRENT_POLICY_HASH),
        email=email,
        ntfy=ntfy,
        source_root=source_root or Path("/workspace"),
    )
    return r


def _seed_scan_item(r, txn_id, path="/workspace/erf/a.txt", size=10, error=None):
    r.state_db.execute(
        "INSERT INTO scan_items (txn_id, path, size, mtime_ns, classification, upload_status, sha256, error) "
        "VALUES (?, ?, ?, 0, 'CRITICAL', ?, 'hash', ?)",
        (txn_id, path, size, app_runner.UploadStatus.UPLOADED.value, error),
    )
    r.state_db.commit()


def _prepare_transaction(r, txn_id, date, phase):
    r._upsert_transaction(txn_id, date)
    r._set_phase(txn_id, phase)
    r.state_db.commit()


def _patch_staging(monkeypatch):
    monkeypatch.setattr(
        app_runner,
        "build_staging_report",
        lambda state_db, staging_dir: {"total_staging_bytes": 0},
    )


def test_send_transaction_notifications_isolates_ntfy_and_email(tmp_path, monkeypatch):
    """One channel returning failed must not stop the other from being called."""
    _patch_staging(monkeypatch)

    cases = [
        (
            NotificationResult(status="sent", attempts=1),
            NotificationResult(status="failed", attempts=1, error="smtp down"),
        ),
        (
            NotificationResult(status="failed", attempts=1, error="ntfy down"),
            NotificationResult(status="sent", attempts=1),
        ),
        (
            NotificationResult(status="failed", attempts=1, error="ntfy down"),
            NotificationResult(status="failed", attempts=1, error="smtp down"),
        ),
    ]

    for i, (ntfy_result, email_result) in enumerate(cases):
        ntfy = _FakeNtfyClient(success_result=ntfy_result)
        email = _FakeEmailClient(success_result=email_result)
        case_dir = tmp_path / f"case_{i}"
        case_dir.mkdir()
        r = _make_runner(case_dir, ntfy=ntfy, email=email)
        txn_id = "backup-2026-09-07"
        _prepare_transaction(r, txn_id, "2026-09-07", app_runner.Phase.SUCCESS)
        _seed_scan_item(r, txn_id)

        r._send_transaction_notifications(txn_id, success=True)  # must not raise

        assert len(ntfy.success_calls) == 1
        assert len(email.success_calls) == 1

        phase = r.state_db.execute(
            "SELECT phase FROM transactions WHERE txn_id = ?", (txn_id,)
        ).fetchone()["phase"]
        assert phase == "SUCCESS"


def test_ntfy_crash_does_not_propagate_or_stop_email(tmp_path, monkeypatch):
    _patch_staging(monkeypatch)
    ntfy = _FakeNtfyClient(raise_on_success=True)
    email = _FakeEmailClient()
    r = _make_runner(tmp_path, ntfy=ntfy, email=email)
    txn_id = "backup-2026-09-07"
    _prepare_transaction(r, txn_id, "2026-09-07", app_runner.Phase.SUCCESS)
    _seed_scan_item(r, txn_id)

    r._send_transaction_notifications(txn_id, success=True)  # must not raise

    assert len(email.success_calls) == 1
    phase = r.state_db.execute(
        "SELECT phase FROM transactions WHERE txn_id = ?", (txn_id,)
    ).fetchone()["phase"]
    assert phase == "SUCCESS"


def test_ntfy_crash_does_not_stop_healthcheck_in_run_for_date(tmp_path, monkeypatch):
    _patch_staging(monkeypatch)
    healthcheck = _FakeHealthcheck()
    ntfy = _FakeNtfyClient(raise_on_success=True)
    r = _make_runner(tmp_path, ntfy=ntfy, healthcheck=healthcheck)
    txn_date = "2026-09-07"
    txn_id = f"backup-{txn_date}"

    def fake_run_phases(txn_id_arg, txn_date_arg):
        r._set_phase(txn_id_arg, app_runner.Phase.SUCCESS)
        _seed_scan_item(r, txn_id_arg)

    monkeypatch.setattr(r, "_run_phases", fake_run_phases)

    r.run_for_date(txn_date)  # must not raise

    assert any(call[0] == "success" for call in healthcheck.calls)
    phase = r.state_db.execute(
        "SELECT phase FROM transactions WHERE txn_id = ?", (txn_id,)
    ).fetchone()["phase"]
    assert phase == "SUCCESS"


def test_send_interrupted_notification_fires_ntfy_only_no_healthcheck(tmp_path):
    ntfy = _FakeNtfyClient()
    healthcheck = _FakeHealthcheck()
    email = _FakeEmailClient()
    r = _make_runner(tmp_path, ntfy=ntfy, email=email, healthcheck=healthcheck)
    txn_id = "backup-2026-09-07"
    _prepare_transaction(r, txn_id, "2026-09-07", app_runner.Phase.UPLOADING)

    r._send_interrupted_notification(txn_id, reason="operator restart")

    assert len(ntfy.interrupted_calls) == 1
    assert ntfy.interrupted_calls[0] == (txn_id, "UPLOADING", "operator restart")
    assert healthcheck.calls == []
    assert email.success_calls == []
    assert email.failure_calls == []


def test_record_healthchecks_result_none_is_recorded_as_failed_client_raised(tmp_path):
    r = _make_runner(tmp_path)
    txn_id = "backup-2026-09-07"
    r._upsert_transaction(txn_id, "2026-09-07")
    r.state_db.commit()

    r._record_healthchecks_result(txn_id, "start", None)

    row = dict(
        r.state_db.execute(
            "SELECT healthchecks_last_kind, healthchecks_last_status, healthchecks_last_error "
            "FROM transactions WHERE txn_id = ?",
            (txn_id,),
        ).fetchone()
    )
    assert row["healthchecks_last_kind"] == "start"
    assert row["healthchecks_last_status"] == "failed"
    assert row["healthchecks_last_error"] == "healthcheck client raised"


def test_record_notification_result_writes_channel_columns_independently(tmp_path):
    r = _make_runner(tmp_path)
    txn_id = "backup-2026-09-07"
    r._upsert_transaction(txn_id, "2026-09-07")
    r.state_db.commit()

    r._record_notification_result(txn_id, "ntfy", NotificationResult(status="sent", attempts=2))
    row = dict(
        r.state_db.execute(
            "SELECT ntfy_status, ntfy_attempts, ntfy_error, notification_status "
            "FROM transactions WHERE txn_id = ?",
            (txn_id,),
        ).fetchone()
    )
    assert row["ntfy_status"] == "sent"
    assert row["ntfy_attempts"] == 2
    assert row["ntfy_error"] is None
    assert row["notification_status"] is None

    r._record_notification_result(
        txn_id, "email", NotificationResult(status="failed", attempts=1, error="smtp down")
    )
    row = dict(
        r.state_db.execute(
            "SELECT ntfy_status, ntfy_attempts, ntfy_error, "
            "notification_status, notification_attempts, notification_error "
            "FROM transactions WHERE txn_id = ?",
            (txn_id,),
        ).fetchone()
    )
    assert row["notification_status"] == "failed"
    assert row["notification_attempts"] == 1
    assert row["notification_error"] == "smtp down"
    # ntfy columns untouched by the email write
    assert row["ntfy_status"] == "sent"
    assert row["ntfy_attempts"] == 2
    assert row["ntfy_error"] is None


def test_healthcheck_endpoint_url_construction(tmp_path, monkeypatch):
    monkeypatch.setenv("BACKUP_HEALTHCHECKS_URL", "https://hc-ping.example/aaaa-bbbb")

    client = hc.HealthcheckClient()

    assert client.url == "https://hc-ping.example/aaaa-bbbb"
    assert client._endpoint_url("") == "https://hc-ping.example/aaaa-bbbb"
    assert client._endpoint_url("/start") == "https://hc-ping.example/aaaa-bbbb/start"
    assert client._endpoint_url("/fail") == "https://hc-ping.example/aaaa-bbbb/fail"


def test_success_summary_contains_meaningful_transaction_summary_text(tmp_path):
    r = _make_runner(tmp_path)
    txn_id = "backup-2026-09-07"
    r._upsert_transaction(txn_id, "2026-09-07")
    r.state_db.commit()
    _seed_scan_item(r, txn_id, size=123)

    summary = r._success_summary(txn_id)

    assert txn_id in summary
    assert "1 files uploaded" in summary
    assert "123 bytes selected" in summary


def test_ping_healthcheck_isolates_client_exception_and_returns_none(tmp_path):
    r = _make_runner(tmp_path, healthcheck=_RaisingHealthcheck())

    result = r._ping_healthcheck("start")  # must not raise

    assert result is None


def test_healthcheck_crash_does_not_stop_run_for_date_or_ntfy(tmp_path, monkeypatch):
    _patch_staging(monkeypatch)
    healthcheck = _RaisingHealthcheck()
    ntfy = _FakeNtfyClient()
    r = _make_runner(tmp_path, ntfy=ntfy, healthcheck=healthcheck)
    txn_date = "2026-09-07"
    txn_id = f"backup-{txn_date}"

    def fake_run_phases(txn_id_arg, txn_date_arg):
        r._set_phase(txn_id_arg, app_runner.Phase.SUCCESS)
        _seed_scan_item(r, txn_id_arg)

    monkeypatch.setattr(r, "_run_phases", fake_run_phases)

    r.run_for_date(txn_date)  # must not raise despite both healthcheck pings crashing

    assert len(ntfy.success_calls) == 1
    phase = r.state_db.execute(
        "SELECT phase FROM transactions WHERE txn_id = ?", (txn_id,)
    ).fetchone()["phase"]
    assert phase == "SUCCESS"

    row = dict(
        r.state_db.execute(
            "SELECT healthchecks_last_kind, healthchecks_last_status, healthchecks_last_error "
            "FROM transactions WHERE txn_id = ?",
            (txn_id,),
        ).fetchone()
    )
    assert row["healthchecks_last_status"] == "failed"
    assert row["healthchecks_last_error"] == "healthcheck client raised"

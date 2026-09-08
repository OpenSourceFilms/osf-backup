"""Unit tests for error_count and total_selected_bytes in build_transaction_report."""
import sqlite3
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import runner as app_runner


def _make_state(tmp_path):
    conn = sqlite3.connect(tmp_path / "state.sqlite")
    conn.row_factory = sqlite3.Row
    r = object.__new__(app_runner.Runner)
    r.state_db = conn
    r.staging_dir = tmp_path / "staging"
    r._init_schema()
    r.config = SimpleNamespace(policy=SimpleNamespace(policy_hash="test-hash"))
    return r


def _prepare_transaction(r, txn_id):
    r._upsert_transaction(txn_id, "2026-09-07")
    r.state_db.commit()


def _insert_scan_item(r, txn_id, path, size, error=None, upload_status=None):
    if upload_status is None:
        upload_status = app_runner.UploadStatus.UPLOADED.value
    r.state_db.execute(
        "INSERT INTO scan_items (txn_id, path, size, mtime_ns, classification, upload_status, sha256, error) "
        "VALUES (?, ?, ?, 0, 'CRITICAL', ?, 'hash', ?)",
        (txn_id, path, size, upload_status, error),
    )
    r.state_db.commit()


def test_error_count_counts_scan_items_with_non_null_error(tmp_path):
    r = _make_state(tmp_path)
    txn_id = "backup-2026-09-07"
    _prepare_transaction(r, txn_id)

    _insert_scan_item(r, txn_id, "/workspace/a", 10, error="upload boom")
    _insert_scan_item(r, txn_id, "/workspace/b", 20, error=None)
    _insert_scan_item(r, txn_id, "/workspace/c", 30, error="sha mismatch")

    report = app_runner.build_transaction_report(r.state_db, txn_id, Path("/workspace"))

    assert report["error_count"] == 2


def test_total_selected_bytes_sums_all_scan_item_sizes(tmp_path):
    r = _make_state(tmp_path)
    txn_id = "backup-2026-09-07"
    _prepare_transaction(r, txn_id)

    _insert_scan_item(r, txn_id, "/workspace/a", 10)
    _insert_scan_item(
        r, txn_id, "/workspace/b", 20,
        upload_status=app_runner.UploadStatus.DEFERRED_ACTIVE.value,
    )
    _insert_scan_item(r, txn_id, "/workspace/c", 30)

    report = app_runner.build_transaction_report(r.state_db, txn_id, Path("/workspace"))

    assert report["total_selected_bytes"] == 60


def test_fields_present_and_typed_with_zero_items_and_zero_errors(tmp_path):
    r = _make_state(tmp_path)
    txn_id = "backup-2026-09-07"
    _prepare_transaction(r, txn_id)

    report = app_runner.build_transaction_report(r.state_db, txn_id, Path("/workspace"))

    assert report["error_count"] == 0
    assert report["total_selected_bytes"] == 0
    assert isinstance(report["error_count"], int)
    assert isinstance(report["total_selected_bytes"], int)

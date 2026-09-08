"""Regression tests for the staging lifecycle added 2026-08-29 alongside
removing the local blob cache: per-item cleanup of git-rescue/sqlite
staging right after its own successful upload (Runner._cleanup_staging_file,
called from _upload), the final SUCCESS-time sweep
(Runner._cleanup_transaction_staging, called from _post_verify), and
build_staging_report's stale/unknown-owner detection (used by
`backupctl staging-status`/`doctor`).

Uses the same object.__new__ + manual attribute style as
tests/test_runner_failed_resume.py and tests/test_local_cache_removed.py.
"""

import sqlite3
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import runner as app_runner


class _FakeBlobStore:
    def __init__(self, *, fail=False):
        self.objects: dict[str, bytes] = {}
        self._fail = fail

    def head_object(self, key):
        return len(self.objects[key]) if key in self.objects else None

    def put_object_from_file(self, key, path):
        if self._fail:
            raise RuntimeError("simulated upload failure")
        self.objects[key] = Path(path).read_bytes()

    def get_object_to_file(self, key, dest):
        Path(dest).write_bytes(self.objects[key])


def _make_runner(tmp_path, blob_store=None):
    conn = sqlite3.connect(tmp_path / "state.sqlite")
    conn.row_factory = sqlite3.Row
    r = object.__new__(app_runner.Runner)
    r.state_db = conn
    r.staging_dir = tmp_path / "staging"
    r._init_schema()
    r.config = SimpleNamespace(
        blob_store=blob_store or _FakeBlobStore(),
        key_prefix="",
        capacity_soft_ratio=0.80,
        capacity=SimpleNamespace(
            current_usage_bytes=lambda: 0,
            hard_threshold_bytes=10**15,
            soft_threshold_bytes=10**15,
        ),
        retention=SimpleNamespace(run=lambda txn_id: None),
        gc_weekday=None,
    )
    return r


def _insert_transaction(r, txn_id, phase="UPLOADING"):
    r.state_db.execute(
        "INSERT INTO transactions (txn_id, txn_date, phase, created_at, updated_at) "
        "VALUES (?, '2026-01-01', ?, '2026-01-01T00:00:00', '2026-01-01T00:00:00')",
        (txn_id, phase),
    )
    r.state_db.commit()


def _seed_staging_scan_item(r, txn_id, source_stub, staging_file, classification):
    stat = staging_file.stat()
    r.state_db.execute(
        "INSERT INTO scan_items "
        "(txn_id, path, staging_path, size, mtime_ns, classification, upload_status) "
        "VALUES (?, ?, ?, ?, ?, ?, 'PENDING_UPLOAD')",
        (
            txn_id,
            str(source_stub),
            str(staging_file),
            stat.st_size,
            stat.st_mtime_ns,
            classification,
        ),
    )
    r.state_db.commit()


def test_staging_artifact_cleaned_right_after_its_own_successful_upload(tmp_path):
    """Once its scan_item is durably UPLOADED, a git-rescue/sqlite staging
    file must be deleted immediately -- it is no longer REQUIRED_FOR_RESUME
    (a resumed _upload() never re-processes an already-UPLOADED row)."""
    r = _make_runner(tmp_path)
    txn_id = "backup-staging1"
    _insert_transaction(r, txn_id)

    staging_file = r.staging_dir / txn_id / "sqlite" / "erf.db"
    staging_file.parent.mkdir(parents=True)
    staging_file.write_bytes(b"sqlite-snapshot-bytes")
    source_stub = Path("/workspace/erf/state/erf.db")

    _seed_staging_scan_item(r, txn_id, source_stub, staging_file, "SQLITE_DATABASES")

    r._upload(txn_id)

    assert not staging_file.exists()
    row = r.state_db.execute(
        "SELECT upload_status FROM scan_items WHERE txn_id=? AND path=?",
        (txn_id, str(source_stub)),
    ).fetchone()
    assert row["upload_status"] == "UPLOADED"


def test_staging_artifact_survives_a_failed_upload(tmp_path):
    """The exact Aug-19 shape: if the upload itself fails, the staging
    artifact must NOT be cleaned up -- it is still REQUIRED_FOR_RESUME."""
    r = _make_runner(tmp_path, blob_store=_FakeBlobStore(fail=True))
    txn_id = "backup-staging2"
    _insert_transaction(r, txn_id)

    staging_file = r.staging_dir / txn_id / "sqlite" / "erf.db"
    staging_file.parent.mkdir(parents=True)
    staging_file.write_bytes(b"sqlite-snapshot-bytes")
    source_stub = Path("/workspace/erf/state/erf.db")

    _seed_staging_scan_item(r, txn_id, source_stub, staging_file, "SQLITE_DATABASES")

    with pytest.raises(RuntimeError, match="simulated upload failure"):
        r._upload(txn_id)

    assert staging_file.exists()
    row = r.state_db.execute(
        "SELECT upload_status FROM scan_items WHERE txn_id=? AND path=?",
        (txn_id, str(source_stub)),
    ).fetchone()
    assert row["upload_status"] != "UPLOADED"


def test_success_sweep_removes_leftover_staging_and_records_cleaned(tmp_path):
    """The safety-net sweep in _post_verify (via _cleanup_transaction_staging)
    must remove whatever's left under a transaction's staging directory
    once every remote durability gate has passed, and record
    local_cleanup_status='cleaned' -- never affecting transaction phase."""
    r = _make_runner(tmp_path)
    txn_id = "backup-staging3"
    _insert_transaction(r, txn_id, phase="POST_VERIFY")

    leftover = r.staging_dir / txn_id / "manifest.json"
    leftover.parent.mkdir(parents=True)
    leftover.write_text("{}")

    r._cleanup_transaction_staging(txn_id)

    assert not (r.staging_dir / txn_id).exists()
    row = r.state_db.execute(
        "SELECT local_cleanup_status, local_cleanup_error, local_cleanup_bytes_left "
        "FROM transactions WHERE txn_id=?",
        (txn_id,),
    ).fetchone()
    assert row["local_cleanup_status"] == "cleaned"
    assert row["local_cleanup_error"] is None
    assert row["local_cleanup_bytes_left"] == 0


def test_success_sweep_failure_is_recorded_not_raised(tmp_path, monkeypatch):
    """Cleanup failure must never turn a valid remote backup into FAILED
    -- it's recorded as local_cleanup_status='failed' with the bytes left,
    and _cleanup_transaction_staging itself must not raise."""
    r = _make_runner(tmp_path)
    txn_id = "backup-staging4"
    _insert_transaction(r, txn_id, phase="POST_VERIFY")

    leftover = r.staging_dir / txn_id / "manifest.json"
    leftover.parent.mkdir(parents=True)
    leftover.write_bytes(b"{}")

    def failing_rmtree(path):
        raise OSError("simulated permission error")

    monkeypatch.setattr(app_runner.shutil, "rmtree", failing_rmtree)

    r._cleanup_transaction_staging(txn_id)  # must not raise

    row = r.state_db.execute(
        "SELECT local_cleanup_status, local_cleanup_error, local_cleanup_bytes_left "
        "FROM transactions WHERE txn_id=?",
        (txn_id,),
    ).fetchone()
    assert row["local_cleanup_status"] == "failed"
    assert "simulated permission error" in row["local_cleanup_error"]
    assert row["local_cleanup_bytes_left"] > 0
    assert leftover.exists()


def test_build_staging_report_flags_stale_success_staging(tmp_path):
    conn = sqlite3.connect(tmp_path / "state.sqlite")
    conn.row_factory = sqlite3.Row
    conn.execute(
        "CREATE TABLE transactions (txn_id TEXT PRIMARY KEY, phase TEXT NOT NULL)"
    )
    conn.execute(
        "INSERT INTO transactions VALUES ('backup-2026-08-23', 'SUCCESS')"
    )
    conn.commit()

    staging_dir = tmp_path / "staging"
    leftover = staging_dir / "backup-2026-08-23" / "sqlite" / "erf.db"
    leftover.parent.mkdir(parents=True)
    leftover.write_bytes(b"leftover")

    report = app_runner.build_staging_report(conn, staging_dir)

    assert report["total_staging_bytes"] == len(b"leftover")
    txn = report["transactions"][0]
    assert txn["txn_id"] == "backup-2026-08-23"
    assert txn["stale"] is True
    assert txn["unknown_owner"] is False


def test_build_staging_report_flags_unknown_owner(tmp_path):
    conn = sqlite3.connect(tmp_path / "state.sqlite")
    conn.row_factory = sqlite3.Row
    conn.execute(
        "CREATE TABLE transactions (txn_id TEXT PRIMARY KEY, phase TEXT NOT NULL)"
    )
    conn.commit()

    staging_dir = tmp_path / "staging"
    leftover = staging_dir / "backup-does-not-exist" / "sqlite" / "erf.db"
    leftover.parent.mkdir(parents=True)
    leftover.write_bytes(b"orphaned")

    report = app_runner.build_staging_report(conn, staging_dir)

    txn = report["transactions"][0]
    assert txn["unknown_owner"] is True
    assert txn["stale"] is False


def test_build_staging_report_flags_nonempty_blob_cache(tmp_path):
    conn = sqlite3.connect(tmp_path / "state.sqlite")
    conn.row_factory = sqlite3.Row
    conn.execute(
        "CREATE TABLE transactions (txn_id TEXT PRIMARY KEY, phase TEXT NOT NULL)"
    )
    conn.commit()

    staging_dir = tmp_path / "staging"
    blob_cache_dir = tmp_path / "blob_cache"
    residue = blob_cache_dir / "ab" / "abcdef"
    residue.parent.mkdir(parents=True)
    residue.write_bytes(b"should not exist anymore")

    report = app_runner.build_staging_report(conn, staging_dir, blob_cache_dir)

    assert report["blob_cache_bytes"] == len(b"should not exist anymore")

"""Regression tests for app.runner.build_transaction_report -- the data
source for the daily email report (app/notify_email.py) and for
`backupctl status`'s notification surfacing. Drives a real sqlite3
database using the real transactions/scan_items schema, the same style
already used by tests/test_backupctl_run_guard.py and
tests/test_runner_failed_resume.py for schema-shaped regression tests
that don't need a full live Runner/S3 setup.
"""

import sqlite3
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.runner import build_transaction_report


def _make_db(tmp_path):
    conn = sqlite3.connect(tmp_path / "state.sqlite")
    conn.row_factory = sqlite3.Row
    conn.execute(
        """
        CREATE TABLE transactions (
            txn_id TEXT PRIMARY KEY,
            txn_date TEXT NOT NULL,
            phase TEXT NOT NULL,
            error TEXT,
            resume_phase TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE scan_items (
            txn_id TEXT NOT NULL,
            path TEXT NOT NULL,
            size INTEGER NOT NULL,
            classification TEXT NOT NULL,
            upload_status TEXT NOT NULL,
            sha256 TEXT,
            error TEXT,
            PRIMARY KEY (txn_id, path)
        )
        """
    )
    return conn


def _insert_txn(conn, txn_id, txn_date, phase, **extra):
    fields = {
        "txn_id": txn_id,
        "txn_date": txn_date,
        "phase": phase,
        "error": None,
        "resume_phase": None,
        "created_at": f"{txn_date}T00:00:00+10:00",
        "updated_at": f"{txn_date}T01:00:00+10:00",
    }
    fields.update(extra)
    conn.execute(
        "INSERT INTO transactions "
        "(txn_id, txn_date, phase, error, resume_phase, created_at, updated_at) "
        "VALUES (:txn_id, :txn_date, :phase, :error, :resume_phase, :created_at, :updated_at)",
        fields,
    )
    conn.commit()


def _insert_item(
    conn, txn_id, path, size, classification, upload_status="UPLOADED", sha256="deadbeef"
):
    conn.execute(
        "INSERT INTO scan_items (txn_id, path, size, classification, upload_status, sha256) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (txn_id, path, size, classification, upload_status, sha256),
    )
    conn.commit()


def test_basic_success_report_counts_and_classification(tmp_path):
    conn = _make_db(tmp_path)
    _insert_txn(conn, "backup-2026-08-28", "2026-08-28", "SUCCESS")
    _insert_item(conn, "backup-2026-08-28", "/workspace/erf/a.txt", 100, "CRITICAL", sha256="h1")
    _insert_item(conn, "backup-2026-08-28", "/workspace/BTS/b.mov", 300, "BTS", sha256="h2")
    _insert_item(conn, "backup-2026-08-28", "/workspace/erf/c.txt", 50, "CRITICAL", sha256="h3")

    report = build_transaction_report(conn, "backup-2026-08-28", "/workspace")

    assert report["file_count"] == 3
    assert report["protected_bytes"] == 450
    assert report["by_classification"]["CRITICAL"] == {"count": 2, "bytes": 150}
    assert report["by_classification"]["BTS"] == {"count": 1, "bytes": 300}
    roots = {r["root"]: r for r in report["top_roots"]}
    assert roots["erf"]["bytes"] == 150
    assert roots["BTS"]["bytes"] == 300
    assert report["diff_from_previous_success"] is None


def test_deferred_active_excluded_from_counts_but_reported_separately(tmp_path):
    conn = _make_db(tmp_path)
    _insert_txn(conn, "backup-2026-08-28", "2026-08-28", "SUCCESS")
    _insert_item(conn, "backup-2026-08-28", "/workspace/erf/a.txt", 100, "CRITICAL")
    _insert_item(
        conn, "backup-2026-08-28", "/workspace/erf/live.txt", 20, "CRITICAL",
        upload_status="DEFERRED_ACTIVE",
    )

    report = build_transaction_report(conn, "backup-2026-08-28", "/workspace")

    assert report["file_count"] == 1
    assert report["protected_bytes"] == 100
    assert report["deferred_active"] == {
        "count": 1,
        "paths": ["/workspace/erf/live.txt"],
        "truncated": False,
    }


def test_diff_from_previous_success_added_changed_disappeared(tmp_path):
    conn = _make_db(tmp_path)
    _insert_txn(conn, "backup-2026-08-27", "2026-08-27", "SUCCESS")
    _insert_item(conn, "backup-2026-08-27", "/workspace/erf/unchanged.txt", 10, "CRITICAL", sha256="same")
    _insert_item(conn, "backup-2026-08-27", "/workspace/erf/changed.txt", 20, "CRITICAL", sha256="old")
    _insert_item(conn, "backup-2026-08-27", "/workspace/erf/gone.txt", 30, "CRITICAL", sha256="gonehash")

    _insert_txn(conn, "backup-2026-08-28", "2026-08-28", "SUCCESS")
    _insert_item(conn, "backup-2026-08-28", "/workspace/erf/unchanged.txt", 10, "CRITICAL", sha256="same")
    _insert_item(conn, "backup-2026-08-28", "/workspace/erf/changed.txt", 25, "CRITICAL", sha256="new")
    _insert_item(conn, "backup-2026-08-28", "/workspace/erf/added.txt", 40, "CRITICAL", sha256="addedhash")

    report = build_transaction_report(conn, "backup-2026-08-28", "/workspace")
    diff = report["diff_from_previous_success"]

    assert diff["previous_txn_id"] == "backup-2026-08-27"
    assert diff["added_paths"] == ["/workspace/erf/added.txt"]
    assert diff["changed_paths"] == ["/workspace/erf/changed.txt"]
    assert diff["disappeared_paths"] == ["/workspace/erf/gone.txt"]
    # Only "addedhash" and "new" are genuinely new content vs the previous
    # success's {"same", "old", "gonehash"} hash set: added.txt (40) +
    # changed.txt's new content (25) = 65 new unique CAS bytes.
    assert diff["new_unique_bytes"] == 65


def test_failure_report_progress_and_last_known_path(tmp_path):
    conn = _make_db(tmp_path)
    _insert_txn(
        conn, "backup-2026-08-29", "2026-08-29", "FAILED",
        error="simulated failure", resume_phase="UPLOADING",
    )
    _insert_item(
        conn, "backup-2026-08-29", "/workspace/erf/a.txt", 10, "CRITICAL",
        upload_status="UPLOADED",
    )
    _insert_item(
        conn, "backup-2026-08-29", "/workspace/erf/b.txt", 10, "CRITICAL",
        upload_status="PENDING_UPLOAD",
    )

    report = build_transaction_report(conn, "backup-2026-08-29", "/workspace")

    assert report["phase"] == "FAILED"
    assert report["error"] == "simulated failure"
    assert report["resume_phase"] == "UPLOADING"
    assert report["progress"] == {"uploaded": 1, "total_scan_items": 2}
    assert report["last_known_path"] == "/workspace/erf/b.txt"


def test_unknown_transaction_raises(tmp_path):
    conn = _make_db(tmp_path)
    with pytest.raises(RuntimeError):
        build_transaction_report(conn, "backup-does-not-exist", "/workspace")

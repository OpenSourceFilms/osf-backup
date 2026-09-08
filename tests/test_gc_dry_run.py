"""Regression test for GCRunner.plan(), the dry-run/plan-only mode added
so a GC safety gate can be exercised without ever calling delete_object or
delete_blob. Before this, GCRunner only exposed run(), which deletes for
real -- there was no way to preview GC candidates.
"""

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.statedb import StateDB
from app import retention as app_retention


class _RecordingBlobStore:
    def __init__(self):
        self.deleted = []

    def delete_object(self, key):
        self.deleted.append(key)


def _seed_blob(db, hash_value, *, refcount, unreferenced_since):
    db.record_blob(hash_value, 100, remote_key=f"blobs/{hash_value[:2]}/{hash_value}")
    if refcount:
        db.bump_refcount(hash_value, delta=refcount)
    if unreferenced_since is not None:
        db.conn.execute(
            "UPDATE blobs SET unreferenced_since = ? WHERE hash = ?",
            (unreferenced_since, hash_value),
        )
        db.conn.commit()


def test_plan_returns_candidates_without_deleting_anything(tmp_path):
    db = StateDB(tmp_path / "state.sqlite")
    old = (datetime.now(timezone.utc) - timedelta(days=60)).isoformat()
    _seed_blob(db, "orphan", refcount=0, unreferenced_since=old)
    _seed_blob(db, "referenced", refcount=1, unreferenced_since=None)

    blob_store = _RecordingBlobStore()
    gc = app_retention.GCRunner(db, blob_store, grace_days=30)

    candidates = gc.plan()

    assert [c["hash"] for c in candidates] == ["orphan"]
    assert blob_store.deleted == []
    assert db.conn.execute("SELECT COUNT(*) FROM blobs").fetchone()[0] == 2


def test_run_deletes_exactly_what_plan_reported(tmp_path):
    db = StateDB(tmp_path / "state.sqlite")
    old = (datetime.now(timezone.utc) - timedelta(days=60)).isoformat()
    _seed_blob(db, "orphan", refcount=0, unreferenced_since=old)

    blob_store = _RecordingBlobStore()
    gc = app_retention.GCRunner(db, blob_store, grace_days=30)

    planned = [c["hash"] for c in gc.plan()]
    gc.run()

    assert planned == ["orphan"]
    assert blob_store.deleted == ["blobs/or/orphan"]
    assert db.conn.execute("SELECT COUNT(*) FROM blobs").fetchone()[0] == 0

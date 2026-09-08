"""Regression tests for StateDB.batch(), the fix for backup-2026-08-23's
observed ~104-minute COMMITTING phase: Runner._upload_manifest previously
made three per-entry mutator calls (each its own durable commit) against a
MooseFS-mounted state DB where one commit costs ~25-33ms -- 83,772 entries
* 3 calls = ~251,000 fsync round-trips. batch() collapses a whole loop into
one durable transaction, strengthening crash consistency (one atomic unit
instead of ~251,000 independently-committed fragments) rather than trading
it away. This fix landed after backup-2026-08-23 already committed, so it
has never been exercised by a real full seed -- these are its first tests.
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.statedb import StateDB


def _blob_rows(db: StateDB):
    return sorted(
        (r["hash"], r["size"], r["reference_count"])
        for r in db.conn.execute("SELECT hash, size, reference_count FROM blobs")
    )


def _snapshot_entry_rows(db: StateDB):
    return sorted(
        (r["run_date"], r["path"], r["hash"])
        for r in db.conn.execute("SELECT run_date, path, hash FROM snapshot_entries")
    )


def test_batch_success_commits_atomically(tmp_path):
    db = StateDB(tmp_path / "state.sqlite")
    with db.batch():
        db.record_blob("aaa", 10, remote_key="blobs/aa/aaa")
        db.record_blob("bbb", 20, remote_key="blobs/bb/bbb")
        db.bump_refcount("aaa", delta=1)
        db.bump_refcount("bbb", delta=2)
    assert _blob_rows(db) == [("aaa", 10, 1), ("bbb", 20, 2)]


def test_batch_exception_rolls_back_the_whole_batch(tmp_path):
    db = StateDB(tmp_path / "state.sqlite")
    with pytest.raises(RuntimeError):
        with db.batch():
            db.record_blob("aaa", 10, remote_key="blobs/aa/aaa")
            db.bump_refcount("aaa", delta=1)
            raise RuntimeError("simulated failure mid-batch")
    assert _blob_rows(db) == []


def test_normal_non_batch_mutation_still_commits_per_call(tmp_path):
    db = StateDB(tmp_path / "state.sqlite")
    db.record_blob("aaa", 10, remote_key="blobs/aa/aaa")
    db.bump_refcount("aaa", delta=1)
    assert _blob_rows(db) == [("aaa", 10, 1)]


def test_reentrant_batch_calls_still_commit_as_one_unit(tmp_path):
    """A batched caller may call helpers that batch too (documented
    re-entrant behavior) -- the outer batch still owns the single commit."""
    db = StateDB(tmp_path / "state.sqlite")

    def _record_and_bump(hash_value, size):
        with db.batch():
            db.record_blob(hash_value, size, remote_key=f"blobs/{hash_value[:2]}/{hash_value}")
            db.bump_refcount(hash_value, delta=1)

    with db.batch():
        _record_and_bump("aaa", 10)
        _record_and_bump("bbb", 20)
    assert _blob_rows(db) == [("aaa", 10, 1), ("bbb", 20, 1)]


def test_manifest_bookkeeping_is_equivalent_batched_vs_unbatched(tmp_path):
    """Replicates Runner._upload_manifest's exact per-entry call shape
    (record_blob + bump_refcount per entry, then add_snapshot_entry per
    entry) once wrapped in batch() and once not, and asserts the final
    state is identical either way -- batching only changes commit
    granularity, never the resulting bookkeeping, so rebuild_state (which
    only ever reads this final state) stays logically equivalent too."""
    entries = [
        ("path/a.txt", "hash-a", 100),
        ("path/b.txt", "hash-b", 200),
        ("path/c.txt", "hash-a", 100),  # same hash as a.txt -- real dedup case
    ]
    run_date = "2026-08-27"

    unbatched = StateDB(tmp_path / "unbatched.sqlite")
    for path, digest, size in entries:
        unbatched.record_blob(digest, size, remote_key=f"blobs/{digest[:2]}/{digest}")
        unbatched.bump_refcount(digest, delta=1)
    for path, digest, size in entries:
        unbatched.add_snapshot_entry(run_date, path, digest)

    batched = StateDB(tmp_path / "batched.sqlite")
    with batched.batch():
        for path, digest, size in entries:
            batched.record_blob(digest, size, remote_key=f"blobs/{digest[:2]}/{digest}")
            batched.bump_refcount(digest, delta=1)
    with batched.batch():
        for path, digest, size in entries:
            batched.add_snapshot_entry(run_date, path, digest)

    assert _blob_rows(batched) == _blob_rows(unbatched)
    assert _snapshot_entry_rows(batched) == _snapshot_entry_rows(unbatched)
    assert _blob_rows(batched) == [("hash-a", 100, 2), ("hash-b", 200, 1)]

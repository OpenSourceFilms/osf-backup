"""Offline hash-cache integrity regressions using disposable backup transactions.

No network, RunPod pods, credentials, B2 uploads or deployed backup state.
"""
from __future__ import annotations

import hashlib
import os
import sqlite3
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.runner import Runner


class MemoryBlobStore:
    def __init__(self):
        self.objects = {}
        self.puts = []

    def head_object(self, key):
        return len(self.objects[key]) if key in self.objects else None

    def put_object_from_file(self, key, path):
        self.puts.append(key)
        self.objects[key] = Path(path).read_bytes()


def runner(tmp_path):
    conn = sqlite3.connect(tmp_path / "test-state.sqlite")
    conn.row_factory = sqlite3.Row
    r = object.__new__(Runner)
    r.state_db = conn
    r.staging_dir = tmp_path / "staging"
    r._init_schema()
    store = MemoryBlobStore()
    r.config = SimpleNamespace(
        blob_store=store,
        key_prefix="",
        should_stop=None,
        capacity_soft_ratio=0.8,
        capacity=SimpleNamespace(
            current_usage_bytes=lambda: 0,
            hard_threshold_bytes=10**12,
            soft_threshold_bytes=10**12,
        ),
    )
    return r, store


def seed_transaction(r, txn, logical_path, *, staging=None):
    actual = staging or logical_path
    st = actual.stat()
    r.state_db.execute(
        "INSERT INTO transactions (txn_id, txn_date, phase, created_at, updated_at)"
        " VALUES (?, '2026-10-27', 'UPLOADING', 'now', 'now')", (txn,),
    )
    r.state_db.execute(
        "INSERT INTO scan_items"
        " (txn_id, path, staging_path, size, mtime_ns, classification)"
        " VALUES (?, ?, ?, ?, ?, 'OUTPUTS')",
        (txn, str(logical_path), str(staging) if staging else None,
         st.st_size, st.st_mtime_ns),
    )
    r.state_db.commit()


def uploaded_sha(r, txn, logical_path):
    row = r.state_db.execute(
        "SELECT sha256, upload_status FROM scan_items WHERE txn_id=? AND path=?",
        (txn, str(logical_path)),
    ).fetchone()
    assert row["upload_status"] == "UPLOADED"
    return row["sha256"]


def test_same_size_same_mtime_replacement_still_uploads_changed_blob(tmp_path):
    r, store = runner(tmp_path)
    source = tmp_path / "source.mov"
    a, b = b"AAAAAA", b"BBBBBB"
    source.write_bytes(a)
    original_mtime = source.stat().st_mtime_ns

    seed_transaction(r, "first", source)
    r._upload("first")
    old_sha = uploaded_sha(r, "first", source)
    assert old_sha == hashlib.sha256(a).hexdigest()

    replacement = tmp_path / "replacement.tmp"
    replacement.write_bytes(b)
    os.utime(replacement, ns=(original_mtime, original_mtime))
    os.replace(replacement, source)
    assert source.stat().st_size == len(a)
    assert source.stat().st_mtime_ns == original_mtime

    seed_transaction(r, "second", source)
    r._upload("second")
    new_sha = uploaded_sha(r, "second", source)
    assert new_sha == hashlib.sha256(b).hexdigest()
    assert new_sha != old_sha
    assert len(store.puts) == 2
    assert store.objects[r._blob_key(new_sha)] == b


def test_legacy_hash_rows_are_never_trusted_as_content_identifiers(tmp_path):
    r, store = runner(tmp_path)
    path = tmp_path / "proof.dat"
    payload = b"current"
    path.write_bytes(payload)
    st = path.stat()
    old_sha = "f" * 64
    r.state_db.execute(
        "INSERT INTO known_hashes (path, size, mtime_ns, sha256) VALUES (?, ?, ?, ?)",
        (str(path), st.st_size, st.st_mtime_ns, old_sha),
    )
    r.state_db.commit()

    seed_transaction(r, "legacy", path)
    r._upload("legacy")
    assert uploaded_sha(r, "legacy", path) == hashlib.sha256(payload).hexdigest()
    assert store.puts == [r._blob_key(hashlib.sha256(payload).hexdigest())]
    assert r.state_db.execute("SELECT count(*) FROM known_hashes").fetchone()[0] == 1


def test_unchanged_source_reuses_v2_hash_without_rehashing(tmp_path):
    r, store = runner(tmp_path)
    path = tmp_path / "stable.dat"
    path.write_bytes(b"keep")
    seed_transaction(r, "first", path)
    r._upload("first")

    seed_transaction(r, "again", path)
    def unexpected_rehash(*args, **kwargs):
        raise AssertionError("unchanged file should reuse a trusted v2 digest")
    r._sha256_file = unexpected_rehash
    r._upload("again")
    assert uploaded_sha(r, "again", path) == hashlib.sha256(b"keep").hexdigest()
    assert len(store.puts) == 1


def test_staged_source_identity_not_logical_path_controls_cache(tmp_path):
    r, store = runner(tmp_path)
    logical = tmp_path / "logical.sqlite"
    logical.write_bytes(b"source")
    staged = tmp_path / "db-snapshot.bin"
    staged.write_bytes(b"FIRST")
    original_mtime = staged.stat().st_mtime_ns

    seed_transaction(r, "staged-first", logical, staging=staged)
    r._upload("staged-first")
    replacement = tmp_path / "new-staged.tmp"
    replacement.write_bytes(b"OTHER")
    os.utime(replacement, ns=(original_mtime, original_mtime))
    os.replace(replacement, staged)
    seed_transaction(r, "staged-second", logical, staging=staged)
    r._upload("staged-second")

    assert uploaded_sha(r, "staged-first", logical) != uploaded_sha(
        r, "staged-second", logical
    )
    assert len(store.puts) == 2


def test_inplace_edit_with_restored_mtime_invalidates_cached_hash(tmp_path):
    r, _store = runner(tmp_path)
    path = tmp_path / "inplace.dat"
    path.write_bytes(b"ONE")
    before = path.stat()
    identity_before = r._source_identity(path, before.st_size, before.st_mtime_ns)
    path.write_bytes(b"TWO")
    os.utime(path, ns=(before.st_mtime_ns, before.st_mtime_ns))
    identity_after = r._source_identity(path, before.st_size, before.st_mtime_ns)
    assert identity_before != identity_after


def test_changed_scan_metadata_refuses_hash_cache_lookup(tmp_path):
    r, _store = runner(tmp_path)
    path = tmp_path / "changed.dat"
    path.write_bytes(b"old")
    initial = path.stat()
    path.write_bytes(b"much longer contents")
    import pytest
    from app.runner import ActiveFileError
    with pytest.raises(ActiveFileError, match="changed since the backup scan"):
        r._source_identity(path, initial.st_size, initial.st_mtime_ns)

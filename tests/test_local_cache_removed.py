"""Regression tests locking in the 2026-08-29 fix that removed the local
blob cache from the ordinary-file upload path (see app.runner.Config's
docstring and Runner._upload's "not remote_exists" branch) -- the defect
that let state/blob_cache/ grow to 174 GiB, 99.999% of it already durable
on remote S3 the whole time. Uses the same object.__new__ + manual
attribute style as tests/test_runner_failed_resume.py rather than a full
Config/Policy/S3 setup, per that file's own stated reasoning: exercising
the full CLI/wiring would be disproportionate to what these tests need to
prove.

Fixtures use small files (KB, not GiB) -- the property under test (no
second local copy is ever created) doesn't depend on file size, only on
the code path taken.
"""

import sqlite3
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import runner as app_runner


class _FakeBlobStore:
    """Minimal in-memory stand-in for RunPodStorage -- just enough surface
    for Runner._upload to exercise its real logic without real S3."""

    def __init__(self):
        self.objects: dict[str, bytes] = {}

    def head_object(self, key):
        return len(self.objects[key]) if key in self.objects else None

    def put_object_from_file(self, key, path):
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
    )
    return r


def _insert_transaction(r, txn_id, phase="UPLOADING"):
    r.state_db.execute(
        "INSERT INTO transactions (txn_id, txn_date, phase, created_at, updated_at) "
        "VALUES (?, '2026-01-01', ?, '2026-01-01T00:00:00', '2026-01-01T00:00:00')",
        (txn_id, phase),
    )
    r.state_db.commit()


def _seed_scan_item(
    r,
    txn_id,
    path,
    *,
    staging_path=None,
    classification="OUTPUTS",
    upload_status="PENDING_UPLOAD",
):
    stat = Path(path).stat()
    r.state_db.execute(
        "INSERT INTO scan_items "
        "(txn_id, path, staging_path, size, mtime_ns, classification, upload_status) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (
            txn_id,
            str(path),
            str(staging_path) if staging_path else None,
            stat.st_size,
            stat.st_mtime_ns,
            classification,
            upload_status,
        ),
    )
    r.state_db.commit()


def _staging_bytes(r) -> int:
    if not r.staging_dir.exists():
        return 0
    return sum(p.stat().st_size for p in r.staging_dir.rglob("*") if p.is_file())


def test_ordinary_file_upload_creates_no_local_copy(tmp_path):
    """The core regression: uploading an ordinary (non-staging) file must
    never create a second local copy anywhere -- only the caller's
    original source file and state.sqlite's own files should exist
    afterward."""
    source = tmp_path / "source"
    source.mkdir()
    big_file = source / "video.mov"
    big_file.write_bytes(b"x" * (1024 * 1024))

    r = _make_runner(tmp_path)
    txn_id = "backup-test1"
    _insert_transaction(r, txn_id)
    _seed_scan_item(r, txn_id, big_file)

    r._upload(txn_id)

    all_files_after = {p for p in tmp_path.rglob("*") if p.is_file()}
    unexpected = {
        p
        for p in all_files_after
        if p != big_file and not p.name.startswith("state.sqlite")
    }
    assert unexpected == set(), f"unexpected files created: {unexpected}"

    row = r.state_db.execute(
        "SELECT upload_status, sha256 FROM scan_items WHERE txn_id=? AND path=?",
        (txn_id, str(big_file)),
    ).fetchone()
    assert row["upload_status"] == "UPLOADED"
    assert row["sha256"] is not None


def test_no_payload_sized_staging_survives_upload(tmp_path):
    """A multi-megabyte ordinary file's bytes must never appear a second
    time under staging_dir -- proves there is no payload-sized persistent
    staging step at all for ordinary files."""
    source = tmp_path / "source"
    source.mkdir()
    big_file = source / "render.mp4"
    big_file.write_bytes(b"y" * (2 * 1024 * 1024))

    r = _make_runner(tmp_path)
    txn_id = "backup-test2"
    _insert_transaction(r, txn_id)
    _seed_scan_item(r, txn_id, big_file)

    r._upload(txn_id)

    assert _staging_bytes(r) == 0


def test_deduplicated_remote_blob_is_never_materialized_locally(tmp_path):
    """A blob already confirmed on remote (runner_blobs.remote_exists=1)
    must be skipped -- never re-uploaded via put_object_from_file, never
    given a second local copy -- proving the remote_exists fast path
    never falls through to any local-cache code. _upload() still does a
    real HeadObject size check regardless (real production behavior, not
    part of what this test is about), so the fake store's object is
    pre-populated directly to simulate "already durably on remote from a
    previous transaction", never through put_object_from_file itself.
    """
    source = tmp_path / "source"
    source.mkdir()
    dup_file = source / "already_uploaded.dat"
    payload = b"z" * 4096
    dup_file.write_bytes(payload)

    store = _FakeBlobStore()
    r = _make_runner(tmp_path, blob_store=store)
    txn_id = "backup-test3"
    _insert_transaction(r, txn_id)

    sha = r._sha256_file(dup_file, expected_size=dup_file.stat().st_size)
    blob_key = r._blob_key(sha)
    store.objects[blob_key] = payload
    r._upsert_blob(
        sha,
        dup_file.stat().st_size,
        local_path=str(dup_file),
        remote_checked=True,
        remote_exists=True,
    )
    _seed_scan_item(r, txn_id, dup_file)

    put_calls = []
    original_put = store.put_object_from_file
    store.put_object_from_file = lambda key, path: (
        put_calls.append(key),
        original_put(key, path),
    )[-1]

    r._upload(txn_id)

    assert put_calls == []
    assert _staging_bytes(r) == 0

    row = r.state_db.execute(
        "SELECT upload_status, sha256 FROM scan_items WHERE txn_id=? AND path=?",
        (txn_id, str(dup_file)),
    ).fetchone()
    assert row["upload_status"] == "UPLOADED"
    assert row["sha256"] == sha

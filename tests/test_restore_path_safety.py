"""Regression tests for app.restore's manifest path normalization and
containment (_normalize_relpath / _manifest_relpath / _safe_join).

Added after the 2026-08-25 post-seed restore drill reported 0 restored /
83772 failed against a fully healthy remote: every manifest entry's path is
the ABSOLUTE source path Runner recorded (e.g. "/workspace/erf/x"), and the
guard that rejects absolute paths outright is _normalize_relpath, meant for
operator-supplied strings (path_filter) -- not manifest entries.
_manifest_relpath strips the leading anchor from an absolute manifest path
first, then hands the remainder to _normalize_relpath so the "../escape"
checks still run. Containment of the final destination is enforced
separately by _safe_join. This file locks that fix in.
"""

import hashlib
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import restore as app_restore
from app.restore import _safe_join


def _dir_manifest(path: str) -> dict:
    return {"entries": [{"path": path, "type": "dir"}]}


def test_absolute_source_path_restores_under_destination(tmp_path):
    """/workspace/foo/bar -> safe destination workspace/foo/bar under
    tmp_path. _manifest_relpath only strips the leading "/" character, not
    a semantic "workspace" anchor -- the full absolute path (minus the
    leading slash) is preserved under the destination by design, since the
    manifest is also the audit record of exactly which path was captured.
    """
    report = app_restore.restore(
        storage=None,
        manifest=_dir_manifest("/workspace/foo/bar"),
        destination=str(tmp_path),
    )
    assert report.failed_entries == 0
    assert report.restored_entries == 1
    assert (tmp_path / "workspace" / "foo" / "bar").is_dir()


def test_dot_dot_traversal_is_rejected(tmp_path):
    report = app_restore.restore(
        storage=None,
        manifest=_dir_manifest("/workspace/../../escape"),
        destination=str(tmp_path),
    )
    assert report.failed_entries == 1
    assert report.restored_entries == 0
    assert not (tmp_path.parent.parent / "escape").exists()


def test_absolute_path_outside_source_anchor_is_contained_not_escaped(tmp_path):
    report = app_restore.restore(
        storage=None,
        manifest=_dir_manifest("/etc/passwd"),
        destination=str(tmp_path),
    )
    assert report.failed_entries == 0
    assert (tmp_path / "etc" / "passwd").is_dir()
    if os.path.exists("/etc/passwd"):
        assert os.path.isfile("/etc/passwd")


def test_relative_dot_dot_path_filter_is_rejected(tmp_path):
    report = app_restore.restore(
        storage=None,
        manifest=_dir_manifest("/workspace/ok"),
        path_filter="../escape",
        destination=str(tmp_path),
    )
    assert report.total_entries == 1
    assert report.failed_entries == 1
    assert report.restored_entries == 0
    assert not (tmp_path / "ok").exists()


def test_safe_join_rejects_traversal_directly(tmp_path):
    with pytest.raises(ValueError):
        _safe_join(tmp_path, "../escape")


def test_safe_join_accepts_ordinary_relative_path(tmp_path):
    target = _safe_join(tmp_path, "a/b/c")
    assert target == tmp_path / "a" / "b" / "c"

class _SyntheticStorage:
    """Read-only in-memory object store, not a live backup backend."""

    def __init__(self, payload: bytes):
        self.payload = payload
        self.read_keys = []

    def get_object_bytes(self, key: str) -> bytes:
        self.read_keys.append(key)
        return self.payload


def _file_manifest_entry(path: str, payload: bytes) -> dict:
    return {"path": path, "type": "file", "sha256": hashlib.sha256(payload).hexdigest()}


def test_manifest_created_absolute_symlink_cannot_redirect_later_file(tmp_path):
    destination = tmp_path / "restored"
    outside = tmp_path / "outside"
    outside.mkdir()
    sentinel = outside / "keep.txt"
    sentinel.write_bytes(b"existing-external-data")
    payload = b"synthetic-restored-payload"
    storage = _SyntheticStorage(payload)
    manifest = {"entries": [
        {"type": "symlink", "path": "alias", "target": str(outside)},
        _file_manifest_entry("alias/escape.txt", payload),
    ]}
    report = app_restore.restore(
        storage=storage, manifest=manifest, destination=str(destination)
    )
    assert report.total_entries == 2
    assert report.restored_entries == 1
    assert report.failed_entries == 1
    assert "escapes destination root" in report.failures[0]["error"]
    assert (destination / "alias").is_symlink()
    assert not (outside / "escape.txt").exists()
    assert sentinel.read_bytes() == b"existing-external-data"
    assert storage.read_keys == [], "reject path before reading remote blob"


def test_manifest_created_relative_symlink_cannot_redirect_nested_file(tmp_path):
    destination = tmp_path / "restored"
    outside = tmp_path / "outside"
    outside.mkdir()
    payload = b"synthetic-data"
    storage = _SyntheticStorage(payload)
    manifest = {"entries": [
        {"type": "dir", "path": "nested"},
        {"type": "symlink", "path": "nested/link", "target": str(outside)},
        _file_manifest_entry("nested/link/payload.bin", payload),
    ]}
    report = app_restore.restore(
        storage=storage, manifest=manifest, destination=str(destination)
    )
    assert report.total_entries == 3
    assert report.restored_entries == 2
    assert report.failed_entries == 1
    assert "escapes destination root" in report.failures[0]["error"]
    assert (destination / "nested" / "link").is_symlink()
    assert not (outside / "payload.bin").exists()
    assert storage.read_keys == []


def test_manifest_created_symlink_within_destination_preserves_restore(tmp_path):
    destination = tmp_path / "restored"
    payload = b"correct-synthetic-file"
    storage = _SyntheticStorage(payload)
    manifest = {"entries": [
        {"type": "dir", "path": "real"},
        {"type": "symlink", "path": "alias", "target": str(destination / "real")},
        _file_manifest_entry("alias/document.txt", payload),
    ]}
    report = app_restore.restore(
        storage=storage, manifest=manifest, destination=str(destination)
    )
    assert report.total_entries == 3
    assert report.restored_entries == 3
    assert report.failed_entries == 0
    assert (destination / "alias").is_symlink()
    assert (destination / "real" / "document.txt").read_bytes() == payload
    assert storage.read_keys == [
        f"blobs/sha256/{hashlib.sha256(payload).hexdigest()[:2]}/{hashlib.sha256(payload).hexdigest()}"
    ]

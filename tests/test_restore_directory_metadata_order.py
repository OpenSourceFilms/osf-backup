"""Offline restore-directory metadata-order checks, using disposable trees.

No B2, backup daemon, RunPod, network, or production filesystem is used.
"""
from __future__ import annotations

from pathlib import Path
import os
import stat
from unittest import mock

import pytest

from app import restore as app_restore


@pytest.mark.parametrize("reverse_manifest", [False, True])
def test_directory_metadata_is_finalized_child_before_parent(tmp_path, reverse_manifest):
    """A read-only parent must not block chmod/utime of its descendants."""
    root = tmp_path / "restored"
    rows = [
        {"type": "dir", "path": "project", "mode": "0500", "mtime": 1700000100},
        {"type": "dir", "path": "project/shots", "mode": "0550", "mtime": 1700000200},
        {"type": "dir", "path": "project/shots/shot-001", "mode": "0700", "mtime": 1700000300},
    ]
    if reverse_manifest:
        rows.reverse()

    calls = []
    finalized = set()
    native_chmod = os.chmod

    def guard_chmod(path, mode, *args, **kwargs):
        # Deterministic stand-in for the EACCES a non-root restorer would
        # see after locking down an ancestor. Works even if CI runs as root.
        resolved = Path(path)
        if any(parent in finalized for parent in resolved.parents):
            raise PermissionError("synthetic ancestor finalized too early")
        calls.append((resolved.relative_to(root).as_posix(), mode))
        finalized.add(resolved)
        return native_chmod(path, mode, *args, **kwargs)

    try:
        with mock.patch.object(app_restore.os, "chmod", side_effect=guard_chmod):
            result = app_restore.restore(
                storage=None, manifest={"entries": rows}, destination=str(root)
            )
        assert result.failed_entries == 0, result.failures
        assert result.restored_entries == 3
        assert calls == [
            ("project/shots/shot-001", 0o700),
            ("project/shots", 0o550),
            ("project", 0o500),
        ]
        for entry in rows:
            target = root / entry["path"]
            assert target.is_dir()
            assert stat.S_IMODE(target.stat().st_mode) == int(entry["mode"], 8)
            assert target.stat().st_mtime == pytest.approx(float(entry["mtime"]))
    finally:
        # Recover writable permissions for non-root pytest cleanup.
        for relative in ("project", "project/shots", "project/shots/shot-001"):
            target = root / relative
            if target.exists():
                native_chmod(target, 0o755)


def test_nested_readonly_directory_still_allows_file_restore_before_finalization(tmp_path):
    root = tmp_path / "restored"
    body = b"synthetic-restored-pixel-data"
    import hashlib

    digest = hashlib.sha256(body).hexdigest()

    class SyntheticObjectStore:
        def get_object_bytes(self, key):
            assert key == f"blobs/sha256/{digest[:2]}/{digest}"
            return body

    entries = [
        {"type": "dir", "path": "read-only", "mode": "0500"},
        {"type": "dir", "path": "read-only/shot", "mode": "0555"},
        {"type": "file", "path": "read-only/shot/plate.png", "sha256": digest},
    ]
    try:
        report = app_restore.restore(
            SyntheticObjectStore(), {"entries": entries}, destination=str(root)
        )
        assert report.failed_entries == 0, report.failures
        assert report.restored_entries == 3
        assert report.restored_bytes == len(body)
        assert (root / "read-only" / "shot" / "plate.png").read_bytes() == body
    finally:
        for relative in ("read-only", "read-only/shot"):
            p = root / relative
            if p.exists():
                os.chmod(p, 0o755)

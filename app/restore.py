from __future__ import annotations

import contextlib
import hashlib
import json
import logging
import os
import re
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Iterator

try:
    import zstandard as zstd
except ImportError:
    try:
        import zstd
    except ImportError:
        zstd = None

logger = logging.getLogger(__name__)

SNAPSHOT_INDEX_KEY = "meta/snapshot-index.json"
DAILY_PREFIX = "snapshots/daily/"
MANIFEST_KEY_TEMPLATE = "meta/manifests/{date}.json.zstd"


@dataclass
class RestoreReport:
    """Summary returned by :func:`restore`."""

    total_entries: int = 0
    restored_entries: int = 0
    skipped_entries: int = 0
    failed_entries: int = 0
    restored_bytes: int = 0
    skipped_bytes: int = 0
    failed_bytes: int = 0
    failures: list[dict] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Storage access helpers
# ---------------------------------------------------------------------------

def _read_bytes(storage: Any, key: str) -> bytes:
    """Read *key* from storage. RunPodStorage's real method is
    get_object_bytes(key) -> bytes -- calling it directly, not guessing
    across a list of method names that don't match the real class."""
    return storage.get_object_bytes(key)


# Meta/index keys (snapshot-index.json, daily markers) are always few --
# this is deliberately not the same generous limit used for real blob
# listings, and this function must never be pointed at the blob tree
# (see list_snapshots's docstring).
_META_LIST_MAX_KEYS = 10_000


def _list_prefix(storage: Any, prefix: str) -> list[str]:
    """List keys under *prefix*. RunPodStorage's real method is
    list_prefix(prefix, max_keys) -> list[dict] (each with a "Key" field),
    not a bare iterable of key strings -- calling it directly and
    extracting "Key", not guessing across a list of method names/shapes
    that don't match the real class."""
    objects = storage.list_prefix(prefix, max_keys=_META_LIST_MAX_KEYS)
    return [str(obj["Key"]) for obj in objects if "Key" in obj]


def _zstd_decompress(data: bytes) -> bytes:
    if zstd is None:
        raise RuntimeError("zstd decompression library is not available")

    if hasattr(zstd, "decompress"):
        try:
            return zstd.decompress(data)
        except Exception:
            pass

    if hasattr(zstd, "ZstdDecompressor"):
        try:
            return zstd.ZstdDecompressor().decompress(data)
        except Exception:
            pass

    raise RuntimeError("zstd decompression failed")


# ---------------------------------------------------------------------------
# Snapshot index helpers
# ---------------------------------------------------------------------------

_DATE_RE = re.compile(r"\d{4}-\d{2}-\d{2}(?:[T_]\d{2}[-:]\d{2})?")


def _looks_like_date(value: str) -> bool:
    return bool(_DATE_RE.fullmatch(value.strip()))


def _extract_dates(data: Any) -> list[str]:
    """Best-effort extraction of date strings from snapshot index JSON."""
    dates: list[str] = []

    def walk(obj: Any) -> None:
        if isinstance(obj, dict):
            for field in ("date", "name", "id", "snapshot_date"):
                if field in obj:
                    val = obj[field]
                    if isinstance(val, str) and _looks_like_date(val):
                        dates.append(val)

            # Some indexes are date -> manifest-key mappings.
            for key, val in obj.items():
                if _looks_like_date(str(key)) and isinstance(val, (dict, list, str)):
                    dates.append(str(key))
                walk(val)

        elif isinstance(obj, list):
            for item in obj:
                walk(item)

        elif isinstance(obj, str) and _looks_like_date(obj):
            dates.append(obj)

    walk(data)
    return dates


def _find_manifest_key_in_index(data: Any, date: str) -> str | None:
    """Find a manifest reference for *date* in snapshot-index data."""
    manifest_keys: list[str] = []

    def walk(obj: Any) -> None:
        if isinstance(obj, dict):
            matched = False
            for field in ("date", "name", "id", "snapshot_date"):
                if obj.get(field) == date:
                    matched = True
                    break

            if matched:
                for field in ("manifest", "manifest_key", "manifest_path", "path", "ref", "hash"):
                    val = obj.get(field)
                    if isinstance(val, str):
                        manifest_keys.append(val)
                        return

            # date -> manifest-key mapping
            if date in obj:
                val = obj[date]
                if isinstance(val, str):
                    manifest_keys.append(val)
                    return
                if isinstance(val, dict):
                    for field in ("manifest", "manifest_key", "manifest_path", "path", "ref", "hash"):
                        v = val.get(field)
                        if isinstance(v, str):
                            manifest_keys.append(v)
                            return

            for val in obj.values():
                walk(val)

        elif isinstance(obj, list):
            for item in obj:
                walk(item)

    walk(data)
    return manifest_keys[0] if manifest_keys else None


# ---------------------------------------------------------------------------
# Public snapshot/manifest loading
# ---------------------------------------------------------------------------

def list_snapshots(storage: Any, prefix: str = "") -> list[str]:
    """
    Return snapshot dates from ``<prefix>meta/snapshot-index.json``.

    If the index cannot be read or yields no dates, fall back to listing only
    ``<prefix>snapshots/daily/``.  Never list the blob tree. *prefix* namespaces
    every key this reads (see Runner.Config.key_prefix -- must match whatever
    prefix the writer used, or nothing will be found).
    """
    index_key = prefix + SNAPSHOT_INDEX_KEY
    daily_prefix = prefix + DAILY_PREFIX

    try:
        raw = _read_bytes(storage, index_key)
        data = json.loads(raw)
        dates = _extract_dates(data)
        if dates:
            return sorted(set(dates))
        logger.warning(
            "snapshot-index.json contained no usable dates; falling back to %s",
            daily_prefix,
        )
    except Exception as exc:
        logger.warning(
            "Could not read snapshot index from %s: %s. Falling back to %s",
            index_key,
            exc,
            daily_prefix,
        )

    try:
        keys = _list_prefix(storage, daily_prefix)
    except Exception as exc:
        logger.warning("Could not list %s: %s", daily_prefix, exc)
        return []

    dates = []
    for key in keys:
        if not key.startswith(daily_prefix):
            continue
        rel = key[len(daily_prefix) :]
        first = rel.split("/")[0]
        if first and _looks_like_date(first):
            dates.append(first)

    return sorted(set(dates))


def _resolve_manifest_key(storage: Any, date: str, prefix: str = "") -> str:
    index_key = prefix + SNAPSHOT_INDEX_KEY
    try:
        raw = _read_bytes(storage, index_key)
        data = json.loads(raw)
        key = _find_manifest_key_in_index(data, date)
        if key:
            return key
    except Exception:
        logger.debug("Unable to resolve manifest key from snapshot index for %s", date)

    key = prefix + MANIFEST_KEY_TEMPLATE.format(date=date)
    logger.warning("No manifest key found for %s in snapshot index; using %s", date, key)
    return key


def load_manifest(storage: Any, date: str, prefix: str = "") -> dict:
    """Fetch and zstd-decompress the manifest for *date*."""
    key = _resolve_manifest_key(storage, date, prefix)
    logger.info("Loading manifest for %s from %s", date, key)
    raw = _read_bytes(storage, key)

    try:
        data = _zstd_decompress(raw)
    except Exception:
        logger.debug("zstd decompression failed for %s; attempting raw JSON", key)
        try:
            return json.loads(raw)
        except Exception:
            raise

    return json.loads(data)


# ---------------------------------------------------------------------------
# Manifest entry helpers
# ---------------------------------------------------------------------------

def _manifest_entries(manifest: Any) -> list[Any]:
    if isinstance(manifest, list):
        return list(manifest)

    if not isinstance(manifest, dict):
        raise ValueError("manifest must be a dict or list")

    if isinstance(manifest.get("entries"), list):
        return list(manifest["entries"])

    entries: list[Any] = []
    for key, typ in (
        ("directories", "dir"),
        ("dirs", "dir"),
        ("files", "file"),
        ("symlinks", "symlink"),
    ):
        if isinstance(manifest.get(key), list):
            for ent in manifest[key]:
                if isinstance(ent, dict):
                    ent_copy = dict(ent)
                    if "type" not in ent_copy:
                        ent_copy["type"] = typ
                    entries.append(ent_copy)
                else:
                    entries.append(ent)

    if not entries:
        raise ValueError("manifest does not contain a recognised entry list")

    return entries


def _entry_path(entry: Any) -> str | None:
    if isinstance(entry, str):
        return entry
    if isinstance(entry, dict):
        for field in ("path", "name", "relpath", "filename"):
            val = entry.get(field)
            if isinstance(val, str) and val:
                return val
    return None


def _entry_type(entry: Any) -> str:
    if isinstance(entry, dict):
        t = entry.get("type")
        if isinstance(t, str):
            tl = t.lower()
            if tl in ("file", "regular", "f"):
                return "file"
            if tl in ("dir", "directory", "d"):
                return "dir"
            if tl in ("symlink", "link", "s"):
                return "symlink"

        if _entry_symlink_target(entry):
            return "symlink"
        if _entry_blob_key(entry):
            return "file"

    elif isinstance(entry, str):
        return "file"

    return "dir"


def _entry_blob_key(entry: Any) -> str | None:
    if not isinstance(entry, dict):
        return None

    for field in ("sha256", "hash", "digest", "blob", "ref", "key"):
        if field in entry:
            val = entry[field]
            if isinstance(val, str) and val:
                return val

    blob = entry.get("blob")
    if isinstance(blob, dict):
        for field in ("sha256", "hash", "digest", "key", "ref"):
            val = blob.get(field)
            if isinstance(val, str) and val:
                return val

    return None


def _is_hex64(value: str) -> bool:
    return bool(re.fullmatch(r"[0-9a-fA-F]{64}", value.strip()))


def _entry_sha256(entry: Any, blob_key: str | None = None) -> str | None:
    digest = None

    if isinstance(entry, dict):
        for field in ("sha256", "hash", "digest"):
            val = entry.get(field)
            if isinstance(val, str) and _is_hex64(val):
                digest = val.lower()
                break

    if digest is None and blob_key:
        key = str(blob_key).strip()
        if _is_hex64(key):
            digest = key.lower()
        elif ":" in key:
            after = key.split(":", 1)[1]
            if _is_hex64(after):
                digest = after.lower()

    return digest


def _entry_size(entry: Any) -> int:
    if not isinstance(entry, dict):
        return 0

    for field in ("size", "length", "bytes"):
        if field in entry:
            try:
                return int(entry[field])
            except (TypeError, ValueError):
                pass

    blob = entry.get("blob")
    if isinstance(blob, dict):
        for field in ("size", "length", "bytes"):
            if field in blob:
                try:
                    return int(blob[field])
                except (TypeError, ValueError):
                    pass

    return 0


def _entry_mode(entry: Any, default: int = 0o644) -> int:
    if not isinstance(entry, dict):
        return default

    for field in ("mode", "permissions", "posix_mode"):
        if field in entry:
            val = entry[field]
            try:
                if isinstance(val, str):
                    return int(val, 8)
                return int(val)
            except (TypeError, ValueError):
                pass

    return default


def _entry_mtime(entry: Any) -> float | None:
    if not isinstance(entry, dict):
        return None

    for field in ("mtime", "modified", "timestamp"):
        if field in entry:
            try:
                return float(entry[field])
            except (TypeError, ValueError):
                pass

    return None


def _entry_symlink_target(entry: Any) -> str | None:
    if not isinstance(entry, dict):
        return None

    for field in ("target", "link", "symlink_target"):
        if field in entry:
            val = entry[field]
            if isinstance(val, str) and val:
                return val

    return None


# ---------------------------------------------------------------------------
# Path safety and filtering
# ---------------------------------------------------------------------------

def _normalize_relpath(path: str) -> str:
    if not isinstance(path, str) or not path.strip():
        raise ValueError("path must be a non-empty string")

    p = path.replace("\\", "/")
    if p.startswith("/") or re.match(r"^[A-Za-z]:", p):
        raise ValueError(f"absolute path not permitted: {path!r}")

    parts = [part for part in p.split("/") if part not in ("", ".")]
    for part in parts:
        if part == "..":
            raise ValueError(f".. component not permitted: {path!r}")

    if not parts:
        raise ValueError(f"path resolves to empty: {path!r}")

    return "/".join(parts)


def _manifest_relpath(path: str) -> str:
    """Normalise a *manifest entry* path to a destination-relative path.

    Runner._upload_manifest records ``scan_items.path``, which is the
    absolute source path (``/workspace/erf/...``) -- that is deliberate on
    the writer side: the manifest is also the audit record of exactly which
    file on this volume was captured. The reader side must therefore accept
    it, while _normalize_relpath (correctly) refuses absolute input because
    it is the guard used for operator-supplied strings.

    Strip the anchor, then hand the remainder to _normalize_relpath so the
    ``..``/empty-component checks still run. Containment of the final
    destination is still enforced separately by _safe_join.

    Without this every entry failed validation and the snapshot was not
    restorable at all -- see the 2026-08-25 post-seed restore drill, which
    reported 0 restored / 83772 failed against a fully healthy remote.
    """
    if not isinstance(path, str) or not path.strip():
        raise ValueError("path must be a non-empty string")

    p = path.replace("\\", "/")
    p = re.sub(r"^[A-Za-z]:", "", p)
    p = p.lstrip("/")
    if not p:
        raise ValueError(f"path resolves to empty: {path!r}")

    return _normalize_relpath(p)


def _matches_prefix(relpath: str, prefix: str) -> bool:
    return relpath == prefix or relpath.startswith(prefix + "/")


def _safe_join(root: Path, relpath: str) -> Path:
    """Join *relpath* to *root* after verifying it cannot escape the root."""
    rel = _normalize_relpath(relpath)
    target = root / rel

    resolved_parent = target.parent.resolve(strict=False)
    try:
        resolved_parent.relative_to(root)
    except ValueError as exc:
        raise ValueError(f"path {relpath!r} escapes destination root") from exc

    return target


# ---------------------------------------------------------------------------
# Restore
# ---------------------------------------------------------------------------

def restore(
    storage: Any,
    manifest: Any,
    path_filter: str | None = None,
    destination: str = ".",
    verify: bool = True,
    prefix: str = "",
) -> RestoreReport:
    """
    Restore entries from *manifest* under *destination*.

    Only relative paths matching *path_filter* are restored.  A filter is a
    prefix, so ``"a/b"`` matches both ``"a/b"`` and ``"a/b/c"``.
    """
    report = RestoreReport()

    dest_root = Path(destination).resolve()
    try:
        dest_root.mkdir(parents=True, exist_ok=True)
        dest_root = dest_root.resolve(strict=True)
    except Exception as exc:
        report.failed_entries += 1
        report.failures.append(
            {"path": str(dest_root), "error": f"destination unusable: {exc}"}
        )
        return report

    try:
        entries = _manifest_entries(manifest)
    except Exception as exc:
        logger.warning("Cannot parse manifest entries: %s", exc)
        report.failed_entries += 1
        report.failures.append({"path": "<manifest>", "error": str(exc)})
        return report

    report.total_entries = len(entries)

    normalized_filter: str | None = None
    if path_filter:
        try:
            # Accept both the absolute form the operator reads out of the
            # manifest ("/workspace/erf/x") and the already-relative form
            # ("workspace/erf/x") -- they must select the same entries.
            normalized_filter = _manifest_relpath(path_filter)
        except ValueError as exc:
            report.failed_entries += 1
            report.failures.append({"path": path_filter, "error": str(exc)})
            return report

    # Validate and filter entries first so we don't double-report errors.
    prepared: list[tuple[Any, str, Path]] = []
    for entry in entries:
        if not isinstance(entry, dict):
            report.failed_entries += 1
            report.failures.append(
                {"path": str(entry), "error": "entry is not a JSON object"}
            )
            continue

        rel = _entry_path(entry)
        if not rel:
            report.failed_entries += 1
            report.failures.append(
                {"path": "<missing>", "error": "entry has no path"}
            )
            continue

        try:
            rel_norm = _manifest_relpath(rel)
        except ValueError as exc:
            report.failed_entries += 1
            report.failures.append({"path": rel, "error": str(exc)})
            continue

        if normalized_filter is not None and not _matches_prefix(rel_norm, normalized_filter):
            size = _entry_size(entry)
            report.skipped_entries += 1
            report.skipped_bytes += size
            continue

        try:
            target = _safe_join(dest_root, rel_norm)
        except ValueError as exc:
            report.failed_entries += 1
            report.failures.append({"path": rel_norm, "error": str(exc)})
            continue

        prepared.append((entry, rel_norm, target))

    # Create directories first so files and symlinks have a place to go.
    dirs_to_finalize: list[tuple[Path, Any, str]] = []
    for entry, rel, target in prepared:
        if _entry_type(entry) != "dir":
            continue

        try:
            # A manifest's earlier entry must not turn a previously checked
            # ancestor into a symlink outside the destination.
            _safe_join(dest_root, rel)
            if target.is_symlink():
                raise ValueError(f"directory path is a symlink: {target}")
            target.mkdir(parents=True, exist_ok=True)
            dirs_to_finalize.append((target, entry, rel))
        except Exception as exc:
            report.failed_entries += 1
            report.failures.append({"path": rel, "error": str(exc)})

    # Restore symlinks and files.
    for entry, rel, target in prepared:
        typ = _entry_type(entry)
        if typ == "dir":
            continue

        try:
            # All paths were checked during preparation, but earlier manifest
            # entries may have created symlinks since then. Recheck *before*
            # mkdir or object reads so a later entry cannot follow a fresh
            # link outside the restore root.
            _safe_join(dest_root, rel)
            target.parent.mkdir(parents=True, exist_ok=True)
            _safe_join(dest_root, rel)

            if typ == "symlink":
                link_target = _entry_symlink_target(entry)
                if link_target is None:
                    raise ValueError("symlink entry missing target")

                if target.is_symlink() and os.readlink(target) == link_target:
                    report.skipped_entries += 1
                    continue

                if target.exists() or target.is_symlink():
                    if target.is_dir() and not target.is_symlink():
                        raise FileExistsError(
                            f"cannot replace directory with symlink: {target}"
                        )
                    target.unlink()

                os.symlink(link_target, target)
                mtime = _entry_mtime(entry)
                if mtime is not None:
                    os.utime(target, (mtime, mtime), follow_symlinks=False)
                report.restored_entries += 1

            elif typ == "file":
                blob_key = _entry_blob_key(entry)
                if not blob_key:
                    raise ValueError("file entry missing blob key")

                expected = _entry_sha256(entry, blob_key)
                if not expected:
                    raise ValueError("file entry has no resolvable sha256")
                # The manifest entry's own field (often literally "sha256")
                # is not a storage key -- the actual object lives at the
                # sharded blob key Runner._blob_key constructs, under
                # whatever prefix this snapshot was written with.
                remote_key = f"{prefix}blobs/sha256/{expected[:2]}/{expected}"
                raw = _read_bytes(storage, remote_key)

                if verify:
                    actual = hashlib.sha256(raw).hexdigest()
                    if actual != expected:
                        raise ValueError(
                            f"sha256 mismatch (expected {expected}, got {actual})"
                        )

                if target.is_dir() and not target.is_symlink():
                    raise FileExistsError(
                        f"cannot replace directory with file: {target}"
                    )

                mode = _entry_mode(entry, default=0o644)
                mtime = _entry_mtime(entry)
                size = len(raw)

                fd, temp_name = tempfile.mkstemp(
                    prefix=".restore-", dir=str(target.parent)
                )
                try:
                    with os.fdopen(fd, "wb") as f:
                        f.write(raw)
                        f.flush()
                        os.fsync(f.fileno())

                    os.chmod(temp_name, mode & 0o777)
                    if mtime is not None:
                        os.utime(temp_name, (mtime, mtime))

                    os.replace(temp_name, target)
                finally:
                    if os.path.exists(temp_name):
                        os.unlink(temp_name)

                report.restored_entries += 1
                report.restored_bytes += size

            else:
                raise ValueError(f"unknown entry type {typ!r}")

        except Exception as exc:
            logger.warning("Failed to restore %s: %s", rel, exc)
            report.failed_entries += 1
            report.failures.append({"path": rel, "error": str(exc)})

    # Finalize directory metadata after children have been written.
    for target, entry, rel in dirs_to_finalize:
        try:
            # The symlink/file pass runs after directory creation, so any
            # mutable parent and the final directory must be checked again
            # before chmod/utime, both of which follow symlinks.
            _safe_join(dest_root, rel)
            if target.is_symlink() or not target.is_dir():
                raise ValueError(f"directory path became symlinked or non-directory: {target}")
            mode = _entry_mode(entry, default=0o755)
            os.chmod(target, mode & 0o777)

            mtime = _entry_mtime(entry)
            if mtime is not None:
                os.utime(target, (mtime, mtime))

            report.restored_entries += 1
        except Exception as exc:
            logger.warning("Failed to finalize directory %s: %s", rel, exc)
            report.failed_entries += 1
            report.failures.append({"path": rel, "error": str(exc)})

    return report


# ---------------------------------------------------------------------------
# State DB rebuild
# ---------------------------------------------------------------------------

@contextlib.contextmanager
def _statedb_batch(statedb: Any) -> Iterator[None]:
    """Batch *statedb* writes if it supports it, else run unbatched.

    ``rebuild_state`` takes ``statedb: Any`` and is exercised with light
    test doubles as well as the real StateDB, so batching must degrade to a
    no-op rather than becoming a new required method on every double.
    """
    batch = getattr(statedb, "batch", None)
    if batch is None:
        yield
        return
    with batch():
        yield


def rebuild_state(storage: Any, statedb: Any, prefix: str = "") -> dict[str, Any]:
    """
    Rebuild local StateDB bookkeeping from remote snapshot manifests.

    Reconstructs, for every snapshot date remote storage knows about
    (oldest first -- see below): ``record_snapshot`` + ``add_snapshot_entry``
    per file entry, and ``record_blob`` + one ``bump_refcount(delta=1)`` per
    referencing entry (matching Runner._upload_manifest's per-row bump
    exactly, so a blob referenced by N retained snapshots ends up with
    refcount N) -- these mirror what the live write path actually does.

    It also calls ``upsert_source_file`` to populate ``source_files`` with
    per-path classification, but this is NOT mirroring live behavior:
    Runner._upload_manifest never calls ``upsert_source_file`` at all, so
    production's ``source_files`` normally has zero rows. ``source_files``
    is a rebuild/recovery-derived convenience index, populated only here,
    not a live source of truth -- nothing in the live scan/dedup/upload/
    verify/retention/GC/restore/scheduling path reads it (confirmed
    2026-08-27; see HANDOFF.md). Do not treat its absence in production as
    a defect, and do not add a live write path for it without first finding
    a real feature that needs to read it.

    Oldest-first processing matters: ``StateDB.record_blob`` is
    ``INSERT ... ON CONFLICT(hash) DO NOTHING``, so whichever snapshot is
    processed first for a given hash wins that blob's ``first_uploaded``
    -- must be the chronologically first snapshot that referenced it, not
    an arbitrary one. ``list_snapshots(storage, ...)`` already returns
    dates ascending, so no explicit sort is needed here.

    *prefix* must match whatever ``Runner.Config.key_prefix`` wrote the
    snapshots with, same as every other function in this module.
    """
    rebuilt: dict[str, Any] = {"snapshots": [], "blobs": 0, "source_files": 0}

    for date in list_snapshots(storage, prefix):
        try:
            manifest_key = _resolve_manifest_key(storage, date, prefix)
            manifest = load_manifest(storage, date, prefix)
            entries = _manifest_entries(manifest)
        except Exception as exc:
            logger.warning("Rebuild state: could not load manifest for %s: %s", date, exc)
            continue

        file_entries = [e for e in entries if _entry_type(e) == "file"]
        committed_at = manifest.get("created_at") if isinstance(manifest, dict) else None
        selected_bytes = sum(_entry_size(e) for e in file_entries)

        try:
            statedb.record_snapshot(
                date,
                manifest_key,
                committed_at,
                selected_bytes,
                selected_bytes,
                0,
                len(file_entries),
            )
        except Exception as exc:
            logger.warning("Rebuild state: failed to record snapshot %s: %s", date, exc)
            continue

        rebuilt["snapshots"].append(date)

        # One durable transaction per snapshot instead of four per entry
        # (record_blob / bump_refcount / add_snapshot_entry /
        # upsert_source_file). Same reasoning as Runner._upload_manifest --
        # see StateDB.batch(). A rebuild is a recovery operation, so the
        # all-or-nothing-per-snapshot boundary is also the semantics you
        # want: a half-reconstructed snapshot is worse than none.
        with _statedb_batch(statedb):
            for entry in file_entries:
                path = _entry_path(entry)
                blob_key = _entry_blob_key(entry)
                sha256 = _entry_sha256(entry, blob_key)
                size = _entry_size(entry)

                if not sha256:
                    logger.warning("Rebuild state: file entry %s missing sha256", path)
                    continue

                remote_key = f"{prefix}blobs/sha256/{sha256[:2]}/{sha256}"
                try:
                    statedb.record_blob(sha256, size, remote_key, first_uploaded=committed_at)
                    statedb.bump_refcount(sha256, delta=1)
                except Exception as exc:
                    logger.warning("Rebuild state: failed to record blob %s: %s", sha256, exc)
                    continue

                rebuilt["blobs"] += 1

                if not path:
                    continue

                try:
                    statedb.add_snapshot_entry(date, path, sha256)
                except Exception as exc:
                    logger.warning(
                        "Rebuild state: failed to record snapshot entry %s/%s: %s", date, path, exc
                    )

                classification = entry.get("classification", "") if isinstance(entry, dict) else ""
                mtime_ns = entry.get("mtime_ns", 0) if isinstance(entry, dict) else 0
                try:
                    statedb.upsert_source_file(
                        path, size, int(mtime_ns or 0), sha256, classification or "", date
                    )
                    rebuilt["source_files"] += 1
                except Exception as exc:
                    logger.warning(
                        "Rebuild state: failed to record source file %s: %s", path, exc
                    )

    return rebuilt


# ---------------------------------------------------------------------------
# Standalone verify -- outside the live transaction phase machine
# ---------------------------------------------------------------------------

@dataclass
class VerifyReport:
    date: str
    total_entries: int
    verified_entries: int
    download_verified_entries: int
    failed_entries: int
    failures: list[str]


def verify_snapshot(
    storage: Any, date: str, *, sample_max_bytes: int = 0, prefix: str = ""
) -> VerifyReport:
    """Post-hoc integrity check of an already-committed remote snapshot.

    Independent of Runner's live transaction phase machine (that one only
    verifies blobs freshly uploaded in the same run, before commit) -- this
    re-checks a manifest that's already committed, any time, by re-reading
    it from remote storage the same way restore() does. Every referenced
    blob gets a HeadObject size check; up to sample_max_bytes total gets
    downloaded and hash-verified (0 disables sampling, HeadObject-only).
    *prefix* must match whatever Runner.Config.key_prefix wrote the
    snapshot with.
    """
    manifest = load_manifest(storage, date, prefix)
    entries = [e for e in _manifest_entries(manifest) if _entry_type(e) == "file"]

    report = VerifyReport(
        date=date,
        total_entries=len(entries),
        verified_entries=0,
        download_verified_entries=0,
        failed_entries=0,
        failures=[],
    )

    downloaded_bytes = 0
    with tempfile.TemporaryDirectory(prefix="verify-") as tmp:
        tmp_dir = Path(tmp)
        for entry in entries:
            path = _entry_path(entry) or "<unknown>"
            blob_key = _entry_blob_key(entry)
            sha256 = _entry_sha256(entry, blob_key)
            size = _entry_size(entry)

            if not sha256:
                report.failed_entries += 1
                report.failures.append(f"{path}: manifest entry has no sha256")
                continue

            remote_key = f"{prefix}blobs/sha256/{sha256[:2]}/{sha256}"
            head = storage.head_object(remote_key)
            if head != size:
                report.failed_entries += 1
                report.failures.append(
                    f"{path}: HeadObject size mismatch for {sha256} "
                    f"(expected {size}, got {head!r})"
                )
                continue

            if sample_max_bytes > 0 and downloaded_bytes + size <= sample_max_bytes:
                dest = tmp_dir / sha256
                storage.get_object_to_file(remote_key, dest)
                digest = hashlib.sha256()
                with dest.open("rb") as fh:
                    for chunk in iter(lambda: fh.read(1024 * 1024), b""):
                        digest.update(chunk)
                dest.unlink()
                if digest.hexdigest() != sha256:
                    report.failed_entries += 1
                    report.failures.append(f"{path}: downloaded content hash mismatch")
                    continue
                downloaded_bytes += size
                report.download_verified_entries += 1

            report.verified_entries += 1

    return report

from __future__ import annotations

import logging
import os
import time
from dataclasses import dataclass
from enum import Enum
from typing import Iterable, Iterator

logger = logging.getLogger(__name__)

DEFAULT_SETTLE_SECONDS = 600
PROGRESS_LOG_INTERVAL_SECONDS = 30
GIT_DIR_NAME = ".git"
BACKUP_ALWAYS_MARKER = ".backup-always"
BACKUP_NEVER_MARKER = ".backup-never"
_MARKER_NAMES = frozenset({BACKUP_ALWAYS_MARKER, BACKUP_NEVER_MARKER})

# WARN: these directory names are not part of the .backup-* marker protocol.
# They are a project-specific convenience used to skip large regenerable data,
# such as camera rushes/caches. Adjust them for your own storage layout.
DEFAULT_REDOWNLOADABLE_DIR_NAMES = frozenset({
    "camera_rushes",
    "camera-rushes",
    "redownloadable",
    "downloads",
    "cache",
    "venv",
    "venvs",
    ".venv",
    ".cache",
    "weights",
    ".pod-persist",
    "models_shared",
    "__pycache__",
    "node_modules",
})


class BackupDisposition(Enum):
    """Directory policy that is propagated down the walk."""
    PRINCIPAL = "PRINCIPAL"
    REDOWNLOADABLE = "REDOWNLOADABLE"
    HARD_EXCLUDED = "HARD_EXCLUDED"


class CandidateStatus(Enum):
    """Consistency/retry status for a file candidate."""
    OK = "OK"
    DEFERRED_ACTIVE = "DEFERRED_ACTIVE"
    DEFERRED_CHANGED = "DEFERRED_CHANGED"


@dataclass(slots=True)
class Candidate:
    absolute_path: str
    relative_path: str
    size: int
    mtime_ns: int
    mode: int
    is_symlink: bool
    symlink_target: str | None
    status: CandidateStatus = CandidateStatus.OK


@dataclass(slots=True)
class RepoCandidate:
    absolute_path: str
    relative_path: str


def _join_rel(rel_base: str, name: str) -> str:
    """Join a relative path fragment without a leading ./."""
    return name if not rel_base else os.path.join(rel_base, name)


def _prunes(disposition: BackupDisposition) -> bool:
    """Directories we never descend into."""
    return disposition in {
        BackupDisposition.REDOWNLOADABLE,
        BackupDisposition.HARD_EXCLUDED,
    }


def _effective_disposition(
    parent: BackupDisposition,
    dir_name: str,
    has_always: bool,
    has_never: bool,
    redownloadable_names: frozenset[str],
) -> BackupDisposition:
    """Determine policy for the current directory.

    .backup-never is a hard exclusion and wins over .backup-always.
    .backup-always explicitly forces PRINCIPAL.
    A name match for a redownloadable tree is pruned unless explicitly overridden.
    Otherwise, the parent policy is inherited.
    """
    if has_never:
        return BackupDisposition.HARD_EXCLUDED
    if has_always:
        return BackupDisposition.PRINCIPAL
    if dir_name in redownloadable_names:
        return BackupDisposition.REDOWNLOADABLE
    return parent


def _is_recently_modified(mtime_ns: int, now_ns: int, settle_seconds: float) -> bool:
    """A file is active if its mtime is newer than settle_seconds."""
    if settle_seconds <= 0:
        return False
    settle_ns = int(settle_seconds * 1_000_000_000)
    return (now_ns - mtime_ns) < settle_ns


def _build_candidate(
    entry: os.DirEntry,
    rel_base: str,
    *,
    is_symlink: bool,
    symlink_target: str | None,
    now_ns: int,
    settle_seconds: float,
) -> Candidate | None:
    """Stat a file/symlink without following links and create a Candidate.

    Vanished and unreadable entries are logged as warnings and skipped, so an
    actively written or disappearing output file never aborts the scan.
    """
    try:
        st = entry.stat(follow_symlinks=False)
    except FileNotFoundError:
        logger.warning("Path vanished before stat: %s", entry.path)
        return None
    except PermissionError as exc:
        logger.warning("Permission denied statting %s: %s", entry.path, exc)
        return None
    except OSError as exc:
        logger.warning("Cannot stat %s: %s", entry.path, exc)
        return None

    status = (
        CandidateStatus.DEFERRED_ACTIVE
        if _is_recently_modified(st.st_mtime_ns, now_ns, settle_seconds)
        else CandidateStatus.OK
    )

    return Candidate(
        absolute_path=entry.path,
        relative_path=_join_rel(rel_base, entry.name),
        size=st.st_size,
        mtime_ns=st.st_mtime_ns,
        mode=st.st_mode,
        is_symlink=is_symlink,
        symlink_target=symlink_target,
        status=status,
    )


def _read_symlink_target(entry: os.DirEntry) -> str | None:
    """Read a symlink target, recording a warning on failure."""
    try:
        return os.readlink(entry.path)
    except (FileNotFoundError, PermissionError, OSError) as exc:
        logger.warning("Cannot read symlink target for %s: %s", entry.path, exc)
        return None


def recheck_candidate(candidate: Candidate) -> CandidateStatus:
    """Re-stat a Candidate after hashing.

    If size or mtime_ns changed since the candidate was emitted, the file is
    considered deferred and should be retried on the next run.
    """
    try:
        st = os.lstat(candidate.absolute_path)
    except FileNotFoundError:
        logger.warning("Candidate vanished after hashing: %s", candidate.absolute_path)
        return CandidateStatus.DEFERRED_CHANGED
    except PermissionError as exc:
        logger.warning("Permission denied re-stat'ing %s: %s", candidate.absolute_path, exc)
        return CandidateStatus.DEFERRED_CHANGED
    except OSError as exc:
        logger.warning("Cannot re-stat %s: %s", candidate.absolute_path, exc)
        return CandidateStatus.DEFERRED_CHANGED

    if st.st_size != candidate.size or st.st_mtime_ns != candidate.mtime_ns:
        logger.info("Candidate changed during hashing: %s", candidate.absolute_path)
        return CandidateStatus.DEFERRED_CHANGED

    return CandidateStatus.OK


def scan(
    root: str | os.PathLike[str],
    *,
    settle_seconds: float = DEFAULT_SETTLE_SECONDS,
    now_ns: int | None = None,
    redownloadable_names: Iterable[str] | None = None,
    include_symlinks: bool = False,
    progress_interval_seconds: float = PROGRESS_LOG_INTERVAL_SECONDS,
) -> Iterator[Candidate | RepoCandidate]:
    """Walk root with os.scandir and yield backup candidates.

    Parameters:
        root: Root directory to scan.
        settle_seconds: Files with mtime newer than this are marked
            DEFERRED_ACTIVE and should not be hashed on this run.
        now_ns: Optional current time in nanoseconds, mainly for deterministic tests.
        redownloadable_names: Directory names that classify a subtree as
            redownloadable and prune it. Defaults to DEFAULT_REDOWNLOADABLE_DIR_NAMES.
        include_symlinks: Yield symlinks as Candidate entries in addition to
            regular files. Symlink directories are never descended.

    Yields:
        Candidate for regular files (and optionally symlinks).
        RepoCandidate for .git directories, without descending into them.
    """
    if now_ns is None:
        now_ns = time.time_ns()

    if redownloadable_names is None:
        redownloadable = DEFAULT_REDOWNLOADABLE_DIR_NAMES
    else:
        redownloadable = frozenset(redownloadable_names)

    root_path = os.path.abspath(os.fspath(root))
    if isinstance(root_path, bytes):
        root_path = os.fsdecode(root_path)

    if not os.path.isdir(root_path):
        logger.warning("Scan root is not a directory: %s", root_path)
        return

    scan_start = time.monotonic()
    last_progress_log = scan_start
    dirs_visited = 0
    files_yielded = 0
    repos_yielded = 0
    bytes_seen = 0

    def maybe_log_progress() -> None:
        nonlocal last_progress_log
        now_mono = time.monotonic()
        if now_mono - last_progress_log >= progress_interval_seconds:
            last_progress_log = now_mono
            logger.info(
                "Scan progress: elapsed=%.1fs dirs=%d files=%d repos=%d bytes=%d path=%s",
                now_mono - scan_start,
                dirs_visited,
                files_yielded,
                repos_yielded,
                bytes_seen,
                dir_path,
            )

    # Stack entries: (absolute directory path, relative path base, inherited disposition)
    stack: list[tuple[str, str, BackupDisposition]] = [
        (root_path, "", BackupDisposition.PRINCIPAL)
    ]

    while stack:
        dir_path, rel_base, parent_disposition = stack.pop()

        try:
            # First lightweight pass: detect policy markers in this directory.
            has_always = has_never = False
            with os.scandir(dir_path) as marker_it:
                for entry in marker_it:
                    if entry.name == BACKUP_ALWAYS_MARKER:
                        has_always = True
                    elif entry.name == BACKUP_NEVER_MARKER:
                        has_never = True

            if has_always and has_never:
                logger.warning(
                    "Directory %s contains both %s and %s; using hard-exclude",
                    dir_path,
                    BACKUP_ALWAYS_MARKER,
                    BACKUP_NEVER_MARKER,
                )

            dir_name = os.path.basename(dir_path.rstrip(os.sep)) or dir_path
            disposition = _effective_disposition(
                parent_disposition,
                dir_name,
                has_always,
                has_never,
                redownloadable,
            )

            if _prunes(disposition):
                logger.debug("Pruning %s (%s)", dir_path, disposition.value)
                continue

            dirs_visited += 1
            maybe_log_progress()

            # Second pass: process files and child directories.
            with os.scandir(dir_path) as it:
                for entry in it:
                    name = entry.name

                    if name in _MARKER_NAMES:
                        continue

                    try:
                        if name == GIT_DIR_NAME:
                            if entry.is_dir(follow_symlinks=False):
                                yield RepoCandidate(
                                    absolute_path=entry.path,
                                    relative_path=_join_rel(rel_base, name),
                                )
                                repos_yielded += 1
                                maybe_log_progress()
                            # Never descend into .git itself.
                            continue

                        if entry.is_symlink():
                            if include_symlinks:
                                target = _read_symlink_target(entry)
                                candidate = _build_candidate(
                                    entry,
                                    rel_base,
                                    is_symlink=True,
                                    symlink_target=target,
                                    now_ns=now_ns,
                                    settle_seconds=settle_seconds,
                                )
                                if candidate is not None:
                                    yield candidate
                                    files_yielded += 1
                                    bytes_seen += candidate.size
                                    maybe_log_progress()
                            continue

                        if entry.is_dir(follow_symlinks=False):
                            child_rel = _join_rel(rel_base, name)
                            stack.append((entry.path, child_rel, disposition))
                            continue

                        if entry.is_file(follow_symlinks=False):
                            candidate = _build_candidate(
                                entry,
                                rel_base,
                                is_symlink=False,
                                symlink_target=None,
                                now_ns=now_ns,
                                settle_seconds=settle_seconds,
                            )
                            if candidate is not None:
                                yield candidate
                                files_yielded += 1
                                bytes_seen += candidate.size
                                maybe_log_progress()

                    except FileNotFoundError:
                        logger.warning("Path vanished while scanning: %s", entry.path)
                    except PermissionError as exc:
                        logger.warning("Permission denied inspecting %s: %s", entry.path, exc)
                    except OSError as exc:
                        logger.warning("Cannot inspect %s: %s", entry.path, exc)

        except FileNotFoundError:
            logger.warning("Directory vanished while scanning: %s", dir_path)
        except PermissionError as exc:
            logger.warning("Permission denied scanning directory %s: %s", dir_path, exc)
        except OSError as exc:
            logger.warning("Cannot scan directory %s: %s", dir_path, exc)


__all__ = [
    "BACKUP_ALWAYS_MARKER",
    "BACKUP_NEVER_MARKER",
    "DEFAULT_REDOWNLOADABLE_DIR_NAMES",
    "DEFAULT_SETTLE_SECONDS",
    "BackupDisposition",
    "Candidate",
    "CandidateStatus",
    "RepoCandidate",
    "recheck_candidate",
    "scan",
]

import logging
import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from typing import Any, Iterator, Optional

LOGGER = logging.getLogger(__name__)

SCHEMA_VERSION = "1"


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _row_to_dict(row: Optional[sqlite3.Row]) -> Optional[dict[str, Any]]:
    if row is None:
        return None
    return {key: row[key] for key in row.keys()}


class StateDB:
    def __init__(self, path: str | os.PathLike[str]) -> None:
        self.path = os.fspath(path)
        self.conn = sqlite3.connect(self.path)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA foreign_keys=ON")
        self._batch_depth = 0
        self.migrate()

    @contextmanager
    def batch(self) -> Iterator[None]:
        """Group many mutator calls into ONE durable transaction.

        Every mutator below opens its own ``transaction()``, i.e. its own
        ``conn.commit()`` -- and this state DB lives on a MooseFS network
        mount where one durable commit measures ~25-33 ms (measured
        2026-08-25) against ~0.15 ms on local disk. Runner._upload_manifest
        does three mutator calls per manifest entry, so an 83,772-entry
        snapshot cost ~251,000 fsync round-trips = the observed 104-minute
        COMMITTING phase, a cost paid in full on every run because
        _upload_manifest walks every UPLOADED row, not just changed ones.

        Wrapping those loops in this context manager collapses them to a
        single commit. It *strengthens* crash consistency rather than
        trading it away: the bookkeeping for one snapshot becomes one
        atomic unit instead of ~251,000 independently-committed fragments
        that could previously be torn anywhere in the middle.

        Re-entrant, so a batched caller may call helpers that batch too.
        """
        if self._batch_depth:
            self._batch_depth += 1
            try:
                yield
            finally:
                self._batch_depth -= 1
            return

        self._batch_depth = 1
        try:
            yield
        except Exception:
            self.conn.rollback()
            raise
        else:
            self.conn.commit()
        finally:
            self._batch_depth = 0

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Cursor]:
        cur = self.conn.cursor()
        try:
            yield cur
        except Exception:
            # Inside a batch the enclosing batch() owns rollback/commit --
            # rolling back here would silently discard the batch's earlier
            # work while the caller still believes it is in one unit.
            if not self._batch_depth:
                self.conn.rollback()
            raise
        else:
            if not self._batch_depth:
                self.conn.commit()
        finally:
            cur.close()

    def migrate(self) -> None:
        with self.transaction() as cur:
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS runs (
                    run_date TEXT PRIMARY KEY,
                    phase TEXT,
                    started_at TEXT,
                    finished_at TEXT,
                    error TEXT,
                    attempt INTEGER
                )
                """
            )
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS snapshots (
                    run_date TEXT PRIMARY KEY,
                    manifest_key TEXT,
                    committed_at TEXT,
                    selected_bytes INTEGER,
                    uploaded_bytes INTEGER,
                    deduped_bytes INTEGER,
                    file_count INTEGER
                )
                """
            )
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS source_files (
                    path TEXT PRIMARY KEY,
                    size INTEGER,
                    mtime_ns INTEGER,
                    hash TEXT,
                    classification TEXT,
                    last_seen_snapshot TEXT
                )
                """
            )
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS blobs (
                    hash TEXT PRIMARY KEY,
                    size INTEGER,
                    remote_key TEXT,
                    first_uploaded TEXT,
                    last_verified TEXT,
                    reference_count INTEGER,
                    unreferenced_since TEXT
                )
                """
            )
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS snapshot_entries (
                    run_date TEXT,
                    path TEXT,
                    hash TEXT,
                    PRIMARY KEY (run_date, path)
                )
                """
            )
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS git_repos (
                    path TEXT PRIMARY KEY,
                    status TEXT,
                    remote_url TEXT,
                    head_commit TEXT,
                    upstream_commit TEXT,
                    last_seen TEXT
                )
                """
            )
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS deferred_files (
                    path TEXT,
                    run_date TEXT,
                    reason TEXT,
                    PRIMARY KEY (path, run_date)
                )
                """
            )
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS gc_candidates (
                    hash TEXT PRIMARY KEY,
                    unreferenced_since TEXT,
                    deleted_at TEXT
                )
                """
            )
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS settings (
                    key TEXT PRIMARY KEY,
                    value TEXT
                )
                """
            )

            cur.execute(
                """
                INSERT OR IGNORE INTO settings (key, value)
                VALUES ('schema_version', ?)
                """,
                (SCHEMA_VERSION,),
            )

            cur.execute("SELECT value FROM settings WHERE key = 'schema_version'")
            version_row = cur.fetchone()
            if version_row is not None and version_row["value"] != SCHEMA_VERSION:
                LOGGER.warning(
                    "StateDB schema_version is %s but code expects %s",
                    version_row["value"],
                    SCHEMA_VERSION,
                )

    def upsert_source_file(
        self,
        path: str,
        size: int,
        mtime_ns: int,
        hash_value: str,
        classification: str,
        last_seen_snapshot: str,
    ) -> None:
        with self.transaction() as cur:
            cur.execute(
                """
                INSERT INTO source_files (
                    path, size, mtime_ns, hash, classification, last_seen_snapshot
                )
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(path) DO UPDATE SET
                    size = excluded.size,
                    mtime_ns = excluded.mtime_ns,
                    hash = excluded.hash,
                    classification = excluded.classification,
                    last_seen_snapshot = excluded.last_seen_snapshot
                """,
                (path, size, mtime_ns, hash_value, classification, last_seen_snapshot),
            )

    def get_source_file(self, path: str) -> Optional[dict[str, Any]]:
        row = self.conn.execute(
            """
            SELECT path, size, mtime_ns, hash, classification, last_seen_snapshot
            FROM source_files
            WHERE path = ?
            """,
            (path,),
        ).fetchone()
        return _row_to_dict(row)

    def record_blob(
        self,
        hash_value: str,
        size: int,
        remote_key: str,
        first_uploaded: Optional[str] = None,
    ) -> None:
        if first_uploaded is None:
            first_uploaded = _utc_now()

        with self.transaction() as cur:
            cur.execute(
                """
                INSERT INTO blobs (hash, size, remote_key, first_uploaded, reference_count)
                VALUES (?, ?, ?, ?, 0)
                ON CONFLICT(hash) DO NOTHING
                """,
                (hash_value, size, remote_key, first_uploaded),
            )

    def blob_exists(self, hash_value: str) -> bool:
        row = self.conn.execute(
            "SELECT 1 FROM blobs WHERE hash = ?", (hash_value,)
        ).fetchone()
        return row is not None

    def bump_refcount(self, hash_value: str, delta: int = 1) -> None:
        with self.transaction() as cur:
            cur.execute(
                """
                UPDATE blobs
                SET reference_count = reference_count + ?,
                    unreferenced_since = CASE
                        WHEN reference_count + ? > 0 THEN NULL
                        ELSE unreferenced_since
                    END
                WHERE hash = ?
                """,
                (delta, delta, hash_value),
            )
            if cur.rowcount == 0:
                LOGGER.warning(
                    "bump_refcount called for unknown blob hash %s", hash_value
                )

    def expire_reference(self, hash_value: str) -> None:
        """Decrement one reference and, only on the transition to zero,
        stamp unreferenced_since. This must be one atomic operation: doing
        the read-current-refcount check and the decrement as two separate
        calls would race, and calling mark_unreferenced() unconditionally
        every time a snapshot expires would keep resetting the GC grace
        clock on a blob that's already at zero, so it would never actually
        become eligible for collection.
        """
        with self.transaction() as cur:
            cur.execute(
                """
                UPDATE blobs
                SET reference_count = reference_count - 1,
                    unreferenced_since = CASE
                        WHEN reference_count - 1 <= 0 AND unreferenced_since IS NULL
                            THEN ?
                        ELSE unreferenced_since
                    END
                WHERE hash = ?
                """,
                (_utc_now(), hash_value),
            )
            if cur.rowcount == 0:
                LOGGER.warning(
                    "expire_reference called for unknown blob hash %s", hash_value
                )

    def delete_blob(self, hash_value: str) -> None:
        """Remove a blob's row after GC has deleted the remote object.

        Callers must delete the remote object first and only call this once
        that has succeeded -- this just drops the local bookkeeping row, it
        does not touch remote storage.
        """
        with self.transaction() as cur:
            cur.execute("DELETE FROM blobs WHERE hash = ?", (hash_value,))
            if cur.rowcount == 0:
                LOGGER.warning(
                    "delete_blob called for unknown blob hash %s", hash_value
                )

    def mark_unreferenced(
        self, hash_value: str, unreferenced_since: Optional[str] = None
    ) -> None:
        with self.transaction() as cur:
            row = cur.execute(
                "SELECT reference_count FROM blobs WHERE hash = ?", (hash_value,)
            ).fetchone()

            if row is None:
                LOGGER.warning(
                    "mark_unreferenced called for unknown blob hash %s", hash_value
                )
                return

            reference_count = row["reference_count"] or 0
            if reference_count > 0:
                LOGGER.warning(
                    "mark_unreferenced called for blob %s with reference_count=%s",
                    hash_value,
                    reference_count,
                )

            cur.execute(
                "UPDATE blobs SET unreferenced_since = ? WHERE hash = ?",
                (unreferenced_since or _utc_now(), hash_value),
            )

    def set_phase(
        self,
        run_date: str,
        phase: str,
        error: Optional[str] = None,
        started_at: Optional[str] = None,
        finished_at: Optional[str] = None,
        attempt: Optional[int] = None,
    ) -> None:
        with self.transaction() as cur:
            row = cur.execute(
                "SELECT 1 FROM runs WHERE run_date = ?", (run_date,)
            ).fetchone()

            if row is not None:
                cur.execute(
                    """
                    UPDATE runs
                    SET phase = ?,
                        error = ?,
                        started_at = COALESCE(?, started_at),
                        finished_at = COALESCE(?, finished_at),
                        attempt = COALESCE(?, attempt)
                    WHERE run_date = ?
                    """,
                    (phase, error, started_at, finished_at, attempt, run_date),
                )
            else:
                cur.execute(
                    """
                    INSERT INTO runs (
                        run_date, phase, started_at, finished_at, error, attempt
                    )
                    VALUES (?, ?, ?, ?, ?, COALESCE(?, 1))
                    """,
                    (
                        run_date,
                        phase,
                        started_at or _utc_now(),
                        finished_at,
                        error,
                        attempt,
                    ),
                )

    def get_run(self, run_date: str) -> Optional[dict[str, Any]]:
        row = self.conn.execute(
            """
            SELECT run_date, phase, started_at, finished_at, error, attempt
            FROM runs
            WHERE run_date = ?
            """,
            (run_date,),
        ).fetchone()
        return _row_to_dict(row)

    def record_deferred(self, path: str, run_date: str, reason: str) -> None:
        with self.transaction() as cur:
            cur.execute(
                """
                INSERT INTO deferred_files (path, run_date, reason)
                VALUES (?, ?, ?)
                ON CONFLICT(path, run_date) DO UPDATE SET
                    reason = excluded.reason
                """,
                (path, run_date, reason),
            )

    def backup_to(self, dest: str | os.PathLike[str] | sqlite3.Connection) -> None:
        if isinstance(dest, sqlite3.Connection):
            self.conn.backup(dest)
            return

        target = sqlite3.connect(os.fspath(dest))
        try:
            self.conn.backup(target)
        finally:
            target.close()

    def close(self) -> None:
        self.conn.close()

    # ------------------------------------------------------------------ #
    # Capacity ledger
    #
    # The repository's used size is tracked locally rather than discovered
    # by a failing upload: RunPod network volumes have fixed capacity and
    # report exhaustion late (and multipart completion can fail outright).
    # ------------------------------------------------------------------ #

    def total_repository_bytes(self) -> int:
        row = self.conn.execute(
            "SELECT COALESCE(SUM(size), 0) AS total FROM blobs"
        ).fetchone()
        return int(row["total"] if row is not None else 0)

    def blob_count(self) -> int:
        row = self.conn.execute("SELECT COUNT(*) AS n FROM blobs").fetchone()
        return int(row["n"] if row is not None else 0)

    # ------------------------------------------------------------------ #
    # Garbage collection
    # ------------------------------------------------------------------ #

    def unreferenced_blobs(self, now_iso: str, grace_days: int) -> list[dict[str, Any]]:
        """Blobs eligible for GC: unreferenced for LONGER than the full grace.

        A source file disappearing must never delete its blob. The blob only
        becomes a candidate once every snapshot referencing it has expired AND
        the orphan grace period has fully elapsed on top of that.
        """
        cutoff = (
            datetime.fromisoformat(now_iso) - timedelta(days=grace_days)
        ).isoformat(timespec="seconds")
        rows = self.conn.execute(
            """
            SELECT hash, size, remote_key, unreferenced_since
            FROM blobs
            WHERE reference_count <= 0
              AND unreferenced_since IS NOT NULL
              AND unreferenced_since < ?
            ORDER BY unreferenced_since
            """,
            (cutoff,),
        ).fetchall()
        return [_row_to_dict(r) for r in rows]

    # ------------------------------------------------------------------ #
    # Snapshots
    # ------------------------------------------------------------------ #

    def record_snapshot(self, run_date, manifest_key, committed_at,
                        selected_bytes, uploaded_bytes, deduped_bytes,
                        file_count) -> None:
        with self.transaction() as cur:
            cur.execute(
                """
                INSERT INTO snapshots (run_date, manifest_key, committed_at,
                    selected_bytes, uploaded_bytes, deduped_bytes, file_count)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(run_date) DO UPDATE SET
                    manifest_key   = excluded.manifest_key,
                    committed_at   = excluded.committed_at,
                    selected_bytes = excluded.selected_bytes,
                    uploaded_bytes = excluded.uploaded_bytes,
                    deduped_bytes  = excluded.deduped_bytes,
                    file_count     = excluded.file_count
                """,
                (run_date, manifest_key, committed_at, selected_bytes,
                 uploaded_bytes, deduped_bytes, file_count),
            )

    def list_snapshots(self) -> list[str]:
        return [r["run_date"] for r in self.conn.execute(
            "SELECT run_date FROM snapshots ORDER BY run_date DESC")]

    def add_snapshot_entry(self, run_date: str, path: str, digest: str) -> None:
        with self.transaction() as cur:
            cur.execute(
                """INSERT INTO snapshot_entries (run_date, path, hash)
                   VALUES (?, ?, ?)
                   ON CONFLICT(run_date, path) DO UPDATE SET hash = excluded.hash""",
                (run_date, path, digest),
            )

    def snapshot_entry_hashes(self, run_date: str) -> list[dict[str, Any]]:
        return [_row_to_dict(r) for r in self.conn.execute(
            "SELECT path, hash FROM snapshot_entries WHERE run_date = ? ORDER BY path",
            (run_date,))]

    def delete_snapshot_entries(self, run_date: str) -> None:
        """Drop a run's local snapshot_entries after retention has expired
        it (decremented every referenced blob's refcount). Restore is
        authoritative from the remote manifest index, not this local cache
        (see README), so this is safe to delete -- and doing so is what
        makes RetentionRunner.run() idempotent: a run_date with no entries
        left has nothing further to expire on a later retention pass.
        """
        with self.transaction() as cur:
            cur.execute(
                "DELETE FROM snapshot_entries WHERE run_date = ?", (run_date,)
            )

    def deferred_for_run(self, run_date: str) -> list[dict[str, Any]]:
        return [_row_to_dict(r) for r in self.conn.execute(
            "SELECT path, run_date, reason FROM deferred_files WHERE run_date = ? ORDER BY path",
            (run_date,))]

    # ------------------------------------------------------------------ #
    # Run terminal states
    #
    # Phase names are UPPERCASE and match the documented state machine
    # (PENDING/SCANNING/UPLOADING/VERIFYING/COMMITTING/POST_VERIFY/
    #  SUCCESS/FAILED) -- backupd branches on these exact strings when
    #  deciding whether to resume, catch up, or hold.
    # ------------------------------------------------------------------ #

    def mark_run_failed(self, run_date: str, error: str) -> None:
        self.set_phase(run_date, "FAILED", error=error, finished_at=_utc_now())

    def mark_run_success(self, run_date: str) -> None:
        self.set_phase(run_date, "SUCCESS", error=None, finished_at=_utc_now())

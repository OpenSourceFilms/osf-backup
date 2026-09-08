from __future__ import annotations

import hashlib
import json
import logging
import os
import shutil
import sqlite3
import tempfile
import time
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Iterable
from zoneinfo import ZoneInfo

from app.statedb import StateDB
from app.policy import Classification
from app import git_rescue
from app.scanner import Candidate, RepoCandidate, CandidateStatus

logger = logging.getLogger(__name__)

MELBOURNE = ZoneInfo("Australia/Melbourne")

# Classifications that must never be capacity-skipped: protected outright.
# GIT_RESCUE and SQLITE_DATABASES join CRITICAL/BTS here because losing
# unpushed git work or a live database snapshot to a soft-capacity skip
# would defeat the point of backing them up at all. Anything else reaching
# _check_capacity is a bug (see _capacity_tier) -- fail loud, don't guess.
HARD_PROTECTED_CLASSIFICATIONS = frozenset(
    {
        Classification.CRITICAL,
        Classification.BTS,
        Classification.GIT_RESCUE,
        Classification.SQLITE_DATABASES,
    }
)
DISCRETIONARY_CLASSIFICATIONS = frozenset({Classification.OUTPUTS})

# How many UNCLASSIFIED paths a scan names in its warning line. Enough to
# start adjudicating from the log alone, few enough that a policy gap over
# a huge tree can't flood logs.all with one line per file -- `backupctl
# dry-run` remains the place to get the exhaustive list.
UNCLASSIFIED_SAMPLE_LIMIT = 20


def _capacity_tier(classification: Classification) -> str:
    """Map the real Classification to the two capacity behaviours Runner
    actually needs: HARD_PROTECTED (never capacity-skipped) or DISCRETIONARY
    (may be skipped once the soft threshold is crossed). Only classifications
    that are policy-selected for backup should ever reach this function --
    anything else is a bug upstream, not a case to silently default."""
    if classification in HARD_PROTECTED_CLASSIFICATIONS:
        return "HARD_PROTECTED"
    if classification in DISCRETIONARY_CLASSIFICATIONS:
        return "DISCRETIONARY"
    raise ValueError(
        f"Unexpected classification reaching capacity check: {classification!r} "
        "-- only selected-for-backup classifications should reach here"
    )


def _blob_key(sha256: str) -> str:
    """Canonical remote object key for a content-addressed blob.

    Sharded by the first two hex characters to avoid an enormous flat
    prefix as the blob count grows. This must be the one place that
    constructs a blob key -- StateDB.record_blob's remote_key and every
    blob_store call (upload/head/download) all go through this so they can
    never drift apart again.
    """
    return f"blobs/sha256/{sha256[:2]}/{sha256}"


class Phase(str, Enum):
    PENDING = "PENDING"
    SCANNING = "SCANNING"
    UPLOADING = "UPLOADING"
    VERIFYING = "VERIFYING"
    COMMITTING = "COMMITTING"
    POST_VERIFY = "POST_VERIFY"
    SUCCESS = "SUCCESS"
    FAILED = "FAILED"
    ABANDONED = "ABANDONED"
    # Terminal, deliberate, and distinct from FAILED. A transaction whose
    # process was killed (SIGKILL, pod restart) stops mid-phase and nothing
    # will ever advance it: Runner only moves a transaction from inside a
    # live process, and backupd only ever considers today's date. Left
    # alone it reads forever as "in progress" while being nothing of the
    # kind. FAILED is the wrong word for it -- the transaction did not
    # fail, and calling it FAILED would additionally arm the
    # FAILED-resume/reset machinery and, for a date newer than the last
    # SUCCESS, block `backupctl run` indefinitely. ABANDONED records what
    # actually happened, keeps the evidence (scan_items and all) intact,
    # and closes the row.


class UploadStatus(str, Enum):
    PENDING_UPLOAD = "PENDING_UPLOAD"
    UPLOADING = "UPLOADING"
    UPLOADED = "UPLOADED"
    DEFERRED_CAPACITY_SOFT = "DEFERRED_CAPACITY_SOFT"
    DEFERRED_ACTIVE = "DEFERRED_ACTIVE"
    ERROR = "ERROR"


class VerifyStatus(str, Enum):
    PENDING = "PENDING"
    VERIFIED = "VERIFIED"
    DOWNLOAD_VERIFIED = "DOWNLOAD_VERIFIED"
    ERROR = "ERROR"


class CapacityError(RuntimeError):
    pass


class ActiveFileError(RuntimeError):
    pass


class SoftCapacitySkip(Exception):
    pass


class TransactionCancelled(Exception):
    """A cooperative stop was requested mid-transaction (Config.should_stop).

    Deliberately NOT a failure: run_for_date catches this before its
    generic failure handler and leaves the transaction's phase exactly as
    it stands, so the next run resumes from there. Marking a
    deliberately-stopped transaction FAILED would be a lie in the one
    table this system treats as ground truth, and would additionally
    trigger the FAILED-resume reset/policy-guard path on a transaction
    that never actually failed.
    """


@dataclass(frozen=True)
class ScanEntry:
    path: Path
    size: int
    mtime_ns: int
    classification: Classification
    staging_path: Path | None = None


@dataclass
class Config:
    source_root: Path
    state_db_path: Path
    staging_dir: Path
    # No local_blob_cache_dir: ordinary files upload directly from their
    # source path (or, for a git-rescue/sqlite artifact, its
    # transaction-owned staging path) -- never a second local CAS copy
    # under _BACKUP. See _upload()'s "not remote_exists" branch and its
    # 2026-08-29 comment for the incident this replaced (174 GiB of
    # redundant local copies).

    healthcheck: Any
    scanner: Any
    policy: Any
    blob_store: Any
    capacity: Any
    retention: Any
    gc: Any

    capacity_soft_ratio: float = 0.80
    verify_download_cap_bytes: int = 0
    gc_weekday: int | None = None
    key_prefix: str = ""  # e.g. "_osf_backup/"; must end in "/" if non-empty.
    # Namespaces every remote key this Runner writes (blobs/manifests/catalog/
    # snapshot index). Load-bearing for test isolation, not just tidiness --
    # a Runner without a distinct prefix would read and write the exact same
    # manifest/index keys production uses.
    should_stop: Callable[[], bool] | None = None
    # Optional cooperative-cancellation predicate, polled between units of
    # work inside the long phases (SCANNING/UPLOADING/VERIFYING) and at
    # every phase boundary. app/backupd.py passes its own
    # threading.Event.is_set here so a SIGTERM stops an in-progress
    # transaction instead of only taking effect between transactions --
    # the gap confirmed live on 2026-08-29, where a graceful SIGTERM was
    # logged as received while the run kept burning CPU and SIGKILL was
    # the only way out. None (the default) means "never cancel", which is
    # exactly the right behaviour for a foreground `backupctl run`.

    email: Any = None
    # Optional app.notify_email.EmailClient (or a test double). None is a
    # legitimate, common value -- callers/tests that don't care about
    # notifications (most of them, see test_runner_integration.py's env
    # fixture) never need to construct one. Runner treats a missing/failing
    # email client exactly like a missing/failing healthcheck client:
    # best-effort, isolated in its own try/except, never able to affect
    # backup SUCCESS/FAILED (see _send_transaction_notifications).
    ntfy: Any = None
    # Optional app.notify_ntfy.NtfyClient (or a test double), added
    # 2026-09-08 as the primary daily-report channel (a phone push,
    # concise, immediate) -- email is optional/secondary per Daniel's
    # ruling. Same isolation contract as email and healthcheck: None is a
    # legitimate no-op, and nothing it does can affect backup SUCCESS/
    # FAILED (see _send_transaction_notifications).


class Runner:
    """Daily backup transaction engine with a persisted phase state machine."""

    def __init__(self, config: Config) -> None:
        self.config = config

        self.state_db_path = Path(config.state_db_path)
        self.state_db_path.parent.mkdir(parents=True, exist_ok=True)

        self.staging_dir = Path(config.staging_dir)
        self.staging_dir.mkdir(parents=True, exist_ok=True)

        self.state_db = sqlite3.connect(str(self.state_db_path))
        self.state_db.row_factory = sqlite3.Row
        self._init_schema()

        self.canonical_state = StateDB(self.state_db_path)

    # ------------------------------------------------------------------
    # Remote key helpers -- everything goes through these two so
    # config.key_prefix can never be applied inconsistently.
    # ------------------------------------------------------------------
    def _key(self, suffix: str) -> str:
        return f"{self.config.key_prefix}{suffix}"

    def _blob_key(self, sha256: str) -> str:
        return self._key(_blob_key(sha256))

    # ------------------------------------------------------------------
    # Cooperative cancellation
    # ------------------------------------------------------------------
    def _raise_if_cancelled(self, where: str) -> None:
        """Raise TransactionCancelled if the caller asked us to stop.

        Called only at points where stopping leaves durable state
        consistent and resumable: phase boundaries, and between whole
        items in the scan/upload/verify loops (each of those commits its
        own per-item row before the next iteration begins). It is
        deliberately NOT called inside COMMITTING/POST_VERIFY -- the
        manifest/catalog/snapshot-index write is the one part of the
        transaction that must land as a unit, and it is short now that
        StateDB.batch() collapses the per-entry commits.

        A should_stop predicate that itself raises is treated as "do not
        cancel": a broken cancellation hook must never be able to stop a
        backup that would otherwise succeed.

        Read with getattr because a Config is not always the real dataclass
        -- most of this suite builds SimpleNamespace doubles carrying only
        the fields the code under test actually uses, and an optional
        feature must not make every one of those a mandatory rewrite. The
        real interface is still pinned: tests/test_transaction_cancellation
        .py asserts app.runner.Config itself declares should_stop, so a
        rename can't silently turn cancellation into a permanent no-op.
        """
        predicate = getattr(self.config, "should_stop", None)
        if predicate is None:
            return
        try:
            stop = bool(predicate())
        except Exception:
            logger.exception("should_stop predicate raised; continuing transaction")
            return
        if stop:
            raise TransactionCancelled(f"stop requested during {where}")

    # ------------------------------------------------------------------
    # Melbourne date helpers
    # ------------------------------------------------------------------
    def _now_iso(self) -> str:
        return datetime.now(MELBOURNE).isoformat()

    def _melbourne_today(self) -> str:
        return datetime.now(MELBOURNE).date().isoformat()

    def _melbourne_weekday(self) -> int:
        return datetime.now(MELBOURNE).weekday()

    # ------------------------------------------------------------------
    # SQLite state schema
    # ------------------------------------------------------------------
    def _init_schema(self) -> None:
        with self.state_db:
            self.state_db.executescript(
                """
                CREATE TABLE IF NOT EXISTS transactions (
                    txn_id TEXT PRIMARY KEY,
                    txn_date TEXT NOT NULL,
                    phase TEXT NOT NULL,
                    manifest_committed INTEGER NOT NULL DEFAULT 0,
                    catalog_uploaded INTEGER NOT NULL DEFAULT 0,
                    retention_done INTEGER NOT NULL DEFAULT 0,
                    gc_done INTEGER NOT NULL DEFAULT 0,
                    error TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS scan_items (
                    txn_id TEXT NOT NULL,
                    path TEXT NOT NULL,
                    staging_path TEXT,
                    size INTEGER NOT NULL,
                    mtime_ns INTEGER NOT NULL,
                    classification TEXT NOT NULL,
                    sha256 TEXT,
                    blob_key TEXT,
                    upload_status TEXT NOT NULL DEFAULT 'PENDING_UPLOAD',
                    verify_status TEXT NOT NULL DEFAULT 'PENDING',
                    size_remote INTEGER,
                    download_verified INTEGER NOT NULL DEFAULT 0,
                    error TEXT,
                    PRIMARY KEY (txn_id, path)
                );

                CREATE TABLE IF NOT EXISTS known_hashes (
                    path TEXT NOT NULL,
                    size INTEGER NOT NULL,
                    mtime_ns INTEGER NOT NULL,
                    sha256 TEXT NOT NULL,
                    PRIMARY KEY (path, size, mtime_ns)
                );

                CREATE TABLE IF NOT EXISTS runner_blobs (
                    sha256 TEXT PRIMARY KEY,
                    size INTEGER NOT NULL,
                    local_path TEXT,
                    remote_checked INTEGER NOT NULL DEFAULT 0,
                    remote_exists INTEGER NOT NULL DEFAULT 0,
                    uploaded_at TEXT
                );

                CREATE TABLE IF NOT EXISTS deferred_items (
                    txn_id TEXT NOT NULL,
                    path TEXT NOT NULL,
                    reason TEXT NOT NULL,
                    PRIMARY KEY (txn_id, path)
                );

                CREATE INDEX IF NOT EXISTS idx_scan_items_upload
                    ON scan_items(txn_id, upload_status);
                """
            )

            # Migration: transactions predates resume_phase, which lets a
            # FAILED retry resume at the phase it actually failed in instead
            # of always rescanning (see _mark_failed / _resume_transaction).
            cols = {
                row["name"]
                for row in self.state_db.execute("PRAGMA table_info(transactions)")
            }
            if "resume_phase" not in cols:
                self.state_db.execute(
                    "ALTER TABLE transactions ADD COLUMN resume_phase TEXT"
                )
            # Migration: transactions predates policy_hash, which lets a
            # resume refuse to trust scan_items produced under a policy.yaml
            # that has since changed (see _check_resumable_policy). NULL on
            # pre-migration rows is treated as "unknown policy" and also
            # refused -- fail closed, not fail open.
            if "policy_hash" not in cols:
                self.state_db.execute(
                    "ALTER TABLE transactions ADD COLUMN policy_hash TEXT"
                )
            # Migration: transactions predates the email-notification
            # columns (added for daily email reporting). Notification
            # outcome is deliberately tracked separately from the
            # transaction's own phase/error -- a notification failure must
            # never be conflated with (or able to flip) backup SUCCESS/
            # FAILED, see _record_notification_result and
            # app/notify_email.py's module docstring.
            if "notification_status" not in cols:
                self.state_db.execute(
                    "ALTER TABLE transactions ADD COLUMN notification_status TEXT"
                )
            if "notification_attempts" not in cols:
                self.state_db.execute(
                    "ALTER TABLE transactions ADD COLUMN notification_attempts INTEGER"
                )
            if "notification_error" not in cols:
                self.state_db.execute(
                    "ALTER TABLE transactions ADD COLUMN notification_error TEXT"
                )
            # Migration: transactions predates the local-staging-cleanup
            # columns (added 2026-08-29 alongside removing the local blob
            # cache -- see _cleanup_transaction_staging). A cleanup
            # failure is tracked separately from backup phase/error for
            # the same reason notification outcome is: it must never be
            # able to turn a genuine remote SUCCESS into FAILED.
            if "local_cleanup_status" not in cols:
                self.state_db.execute(
                    "ALTER TABLE transactions ADD COLUMN local_cleanup_status TEXT"
                )
            if "local_cleanup_error" not in cols:
                self.state_db.execute(
                    "ALTER TABLE transactions ADD COLUMN local_cleanup_error TEXT"
                )
            if "local_cleanup_bytes_left" not in cols:
                self.state_db.execute(
                    "ALTER TABLE transactions ADD COLUMN local_cleanup_bytes_left INTEGER"
                )
            # Migration: transactions predates scan_skipped (2026-09-08),
            # a JSON object of {classification: count} for everything the
            # scan declined to select. Persisted rather than only logged
            # because "what was NOT backed up" belongs in the daily email:
            # the 2026-09-07 run skipped 46,748 files as UNCLASSIFIED, and
            # a report that only lists what succeeded gives an operator no
            # way to notice that. NULL on pre-migration rows means "this
            # transaction predates the accounting", which the report
            # renders as unknown -- never as zero.
            if "scan_skipped" not in cols:
                self.state_db.execute(
                    "ALTER TABLE transactions ADD COLUMN scan_skipped TEXT"
                )
            # Migration: transactions predates per-channel notification
            # tracking (2026-09-08, ntfy/Healthchecks integration). The
            # pre-existing notification_status/attempts/error triple is
            # EMAIL's -- kept under its original name rather than renamed,
            # since real historical rows already carry it and nothing is
            # gained by churning it. ntfy gets its own equivalent triple;
            # Healthchecks gets a last-ping triple naming which of
            # start/success/fail it was, since all three share one URL and
            # only the most recent matters operationally.
            if "ntfy_status" not in cols:
                self.state_db.execute(
                    "ALTER TABLE transactions ADD COLUMN ntfy_status TEXT"
                )
            if "ntfy_attempts" not in cols:
                self.state_db.execute(
                    "ALTER TABLE transactions ADD COLUMN ntfy_attempts INTEGER"
                )
            if "ntfy_error" not in cols:
                self.state_db.execute(
                    "ALTER TABLE transactions ADD COLUMN ntfy_error TEXT"
                )
            if "healthchecks_last_kind" not in cols:
                self.state_db.execute(
                    "ALTER TABLE transactions ADD COLUMN healthchecks_last_kind TEXT"
                )
            if "healthchecks_last_status" not in cols:
                self.state_db.execute(
                    "ALTER TABLE transactions ADD COLUMN healthchecks_last_status TEXT"
                )
            if "healthchecks_last_error" not in cols:
                self.state_db.execute(
                    "ALTER TABLE transactions ADD COLUMN healthchecks_last_error TEXT"
                )

    # ------------------------------------------------------------------
    # Transaction helpers
    # ------------------------------------------------------------------
    def _upsert_transaction(self, txn_id: str, txn_date: str) -> None:
        now = self._now_iso()
        with self.state_db:
            self.state_db.execute(
                """
                INSERT INTO transactions (txn_id, txn_date, phase, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(txn_id) DO UPDATE SET
                    txn_date = excluded.txn_date,
                    updated_at = excluded.updated_at
                """,
                (txn_id, txn_date, Phase.PENDING.value, now, now),
            )

    def _get_transaction(self, txn_id: str) -> sqlite3.Row:
        row = self.state_db.execute(
            "SELECT * FROM transactions WHERE txn_id = ?", (txn_id,)
        ).fetchone()
        if row is None:
            raise RuntimeError(f"No persisted transaction for {txn_id}")
        return row

    def _get_phase(self, txn_id: str) -> Phase:
        return Phase(self._get_transaction(txn_id)["phase"])

    def _set_phase(self, txn_id: str, phase: Phase) -> None:
        with self.state_db:
            self.state_db.execute(
                "UPDATE transactions SET phase = ?, updated_at = ? WHERE txn_id = ?",
                (phase.value, self._now_iso(), txn_id),
            )

    def _reset_transaction(self, txn_id: str) -> None:
        """Full reset: wipe scan progress and start over from PENDING.

        Only safe when the failure happened during (or before) SCANNING --
        once SCANNING has completed, scan_items reflects a real, complete
        walk of the source tree and must not be discarded (see
        _resume_transaction).
        """
        with self.state_db:
            self.state_db.execute(
                "DELETE FROM scan_items WHERE txn_id = ?", (txn_id,)
            )
            self.state_db.execute(
                "DELETE FROM deferred_items WHERE txn_id = ?", (txn_id,)
            )
            self.state_db.execute(
                """
                UPDATE transactions
                   SET phase = ?,
                       manifest_committed = 0,
                       catalog_uploaded = 0,
                       retention_done = 0,
                       gc_done = 0,
                       error = NULL,
                       resume_phase = NULL,
                       updated_at = ?
                 WHERE txn_id = ?
                """,
                (Phase.PENDING.value, self._now_iso(), txn_id),
            )

    def _resume_transaction(self, txn_id: str, resume_phase: Phase) -> None:
        """Resume a FAILED transaction at the phase it failed in, without
        touching scan_items/deferred_items or the manifest/catalog/retention/
        gc flags -- those reflect real completed sub-steps and each phase
        (_upload/_verify/_commit/_post_verify) already resumes correctly
        from partial state on its own."""
        with self.state_db:
            self.state_db.execute(
                """
                UPDATE transactions
                   SET phase = ?,
                       error = NULL,
                       resume_phase = NULL,
                       updated_at = ?
                 WHERE txn_id = ?
                """,
                (resume_phase.value, self._now_iso(), txn_id),
            )

    def _check_resumable_policy(self, txn_id: str) -> None:
        """Refuse to resume scan_items produced under a different policy.

        _resume_transaction trusts scan_items verbatim -- it does not
        re-run Policy.classify(). If config/policy.yaml has changed since
        the scan that populated scan_items (including a NULL policy_hash on
        a pre-migration transaction, which means "unknown policy"), those
        rows are not known-good against the current policy and must not be
        silently resumed from.
        """
        tx = self._get_transaction(txn_id)
        scanned_hash = tx["policy_hash"]
        current_hash = self.config.policy.policy_hash
        if scanned_hash != current_hash:
            raise RuntimeError(
                f"Transaction {txn_id} was scanned under a different policy "
                f"(scan policy_hash={scanned_hash!r}, current policy_hash="
                f"{current_hash!r}). scan_items reflect stale classification "
                "and must not be resumed from as-is -- reconcile scan_items "
                "against the current policy or rescan before retrying."
            )

    def _mark_success(self, txn_id: str) -> None:
        with self.state_db:
            self.state_db.execute(
                """
                UPDATE transactions
                   SET phase = ?, error = NULL, resume_phase = NULL, updated_at = ?
                 WHERE txn_id = ?
                """,
                (Phase.SUCCESS.value, self._now_iso(), txn_id),
            )

    def abandon_transaction(self, txn_date: str, reason: str) -> None:
        """Close an interrupted transaction as deliberately ABANDONED.

        Runner is the single writer to `transactions`, so this lives here
        rather than in the CLI poking the table directly. Refuses to touch
        a transaction that is already terminal: a SUCCESS must never be
        rewritten, and a FAILED one is historical evidence in its own right
        (backup-2026-08-19 is exactly that). Nothing else is deleted --
        scan_items, deferred_items and any uploaded blobs stay exactly as
        they are, because the point is to record the truth about the row,
        not to erase it.
        """
        if not reason or not reason.strip():
            raise ValueError("abandoning a transaction requires an explicit reason")

        txn_id = f"backup-{txn_date}"
        row = self.state_db.execute(
            "SELECT phase FROM transactions WHERE txn_id = ?", (txn_id,)
        ).fetchone()
        if row is None:
            raise ValueError(f"no such transaction: {txn_id}")

        phase = row["phase"]
        if phase in (Phase.SUCCESS.value, Phase.FAILED.value, Phase.ABANDONED.value):
            raise ValueError(
                f"{txn_id} is already terminal ({phase}); refusing to rewrite it"
            )

        with self.state_db:
            self.state_db.execute(
                """
                UPDATE transactions
                   SET phase = ?, error = ?, resume_phase = NULL, updated_at = ?
                 WHERE txn_id = ?
                """,
                (Phase.ABANDONED.value, reason, self._now_iso(), txn_id),
            )
        logger.info("Marked %s ABANDONED (was %s): %s", txn_id, phase, reason)

    def _mark_failed(self, txn_id: str, error: str) -> None:
        with self.state_db:
            current = self.state_db.execute(
                "SELECT phase FROM transactions WHERE txn_id = ?", (txn_id,)
            ).fetchone()["phase"]
            # current is the phase _run_phases was executing when it raised
            # (phase is only advanced by _set_phase *after* a phase
            # succeeds) -- that's exactly where a retry should resume.
            resume_phase = current if current != Phase.FAILED.value else None
            self.state_db.execute(
                """
                UPDATE transactions
                   SET phase = ?, error = ?, resume_phase = ?, updated_at = ?
                 WHERE txn_id = ?
                """,
                (Phase.FAILED.value, error, resume_phase, self._now_iso(), txn_id),
            )

    # ------------------------------------------------------------------
    # Scan phase
    # ------------------------------------------------------------------
    def _scan(self, txn_id: str) -> None:
        logger.info("Scanning source for %s", txn_id)
        with self.state_db:
            self.state_db.execute("DELETE FROM scan_items WHERE txn_id = ?", (txn_id,))
            self.state_db.execute(
                "DELETE FROM deferred_items WHERE txn_id = ?", (txn_id,)
            )
            # Record which policy produced this scan, so a later resume can
            # refuse to trust these scan_items if policy.yaml has changed
            # since (see _check_resumable_policy).
            self.state_db.execute(
                "UPDATE transactions SET policy_hash = ? WHERE txn_id = ?",
                (self.config.policy.policy_hash, txn_id),
            )

        sqlite_paths = {
            (self.config.source_root / rel).resolve()
            for rel in self.config.policy.sqlite_databases()
        }
        self._scan_source(txn_id, sqlite_paths)
        self._scan_sqlite_databases(txn_id)

    def _scan_source(self, txn_id: str, sqlite_paths: set[Path]) -> None:
        """Walk the real scanner output.

        Two explicit branches, matching the scanner's own two candidate
        shapes -- a repository is not a file and must not be forced through
        the file pipeline (see _handle_repo_candidate). Only a scanner-
        reported DEFERRED_* status (an expected, documented condition) is
        handled specially; an unrecognized candidate type or classification
        aborts the transaction rather than being silently dropped. A backup
        system that silently skips what it doesn't understand can report
        success while protecting nothing -- exactly the failure mode this
        rewrite exists to close off.

        Everything policy declines to select is counted and reported at
        the end of the walk. Before 2026-09-07 only `backupctl dry-run`
        produced any UNCLASSIFIED accounting; the real run path dropped
        non-selected files with a bare `continue`, so a policy gap that
        left genuinely-authored work UNCLASSIFIED was invisible in exactly
        the runs that mattered -- the nightly ones. That is how
        table_read/Media, Media_H264 and braw_decode (25 files, ~23 GiB,
        including the sole local copy of a recovery-pending recording)
        went unprotected through a whole seed without one log line saying
        so. Counting is deliberately in-memory and logged rather than
        written per-path into scan_items/deferred_items: the skipped set
        is routinely hundreds of thousands of files (caches, venvs, model
        weights), and the operator needs its shape, not a row for each.
        """
        skipped_counts: dict[str, int] = {}
        unclassified_samples: list[str] = []

        for entry in self.config.scanner.scan(self.config.source_root):
            self._raise_if_cancelled("SCANNING")

            if isinstance(entry, RepoCandidate):
                self._handle_repo_candidate(
                    txn_id, Path(entry.absolute_path), sqlite_paths
                )
                continue

            if not isinstance(entry, Candidate):
                raise RuntimeError(
                    f"Scanner yielded unrecognized candidate type: {type(entry)!r}"
                )

            if entry.status != CandidateStatus.OK:
                self._add_deferred(txn_id, entry.absolute_path, entry.status.value)
                continue

            file_path = Path(entry.absolute_path)
            if file_path.resolve() in sqlite_paths:
                continue  # handled by _scan_sqlite_databases via the backup API instead

            # Policy path rules are written relative to source.root (see
            # config/policy.yaml, e.g. "BTS", "erf/catalog") -- classify()
            # must be given that same relative path, not an absolute
            # filesystem path, or every rule silently fails to match and
            # everything falls through to UNCLASSIFIED.
            relative_path = file_path.relative_to(self.config.source_root)
            decision = self.config.policy.classify(
                relative_path, file_size=entry.size, mtime=entry.mtime_ns / 1e9
            )
            if not decision.selected:
                name = decision.classification.value
                skipped_counts[name] = skipped_counts.get(name, 0) + 1
                if (
                    decision.classification == Classification.UNCLASSIFIED
                    and len(unclassified_samples) < UNCLASSIFIED_SAMPLE_LIMIT
                ):
                    unclassified_samples.append(str(relative_path))
                continue

            self._insert_scan_item(
                txn_id,
                ScanEntry(
                    path=file_path,
                    size=entry.size,
                    mtime_ns=entry.mtime_ns,
                    classification=decision.classification,
                    staging_path=None,
                ),
            )

        self._log_skipped_summary(txn_id, skipped_counts, unclassified_samples)

    def _log_skipped_summary(
        self,
        txn_id: str,
        skipped_counts: dict[str, int],
        unclassified_samples: list[str],
    ) -> None:
        """Report what the scan chose not to protect, and why.

        UNCLASSIFIED is escalated to WARNING on its own: every other
        skipped class is a deliberate policy decision someone wrote down,
        while UNCLASSIFIED means policy has no opinion about a file at all
        -- the one outcome that is never intentional and has already cost
        this project real unprotected data (see _scan_source).
        """
        # Persist before logging: the daily email reports "what was NOT
        # backed up" from this column, and a log line nobody reads is what
        # this accounting exists to replace.
        with self.state_db:
            self.state_db.execute(
                "UPDATE transactions SET scan_skipped = ? WHERE txn_id = ?",
                (json.dumps(skipped_counts, sort_keys=True), txn_id),
            )

        if not skipped_counts:
            logger.info("Scan for %s: no files skipped by classification", txn_id)
            return

        total = sum(skipped_counts.values())
        breakdown = ", ".join(
            f"{name}={count}" for name, count in sorted(skipped_counts.items())
        )
        logger.info(
            "Scan for %s skipped %d files by classification: %s",
            txn_id, total, breakdown,
        )

        n_unclassified = skipped_counts.get(Classification.UNCLASSIFIED.value, 0)
        if n_unclassified:
            logger.warning(
                "Scan for %s left %d file(s) UNCLASSIFIED -- policy has no rule "
                "covering them, so they are NOT being backed up. Adjudicate them "
                "in config/policy.yaml. First %d: %s",
                txn_id,
                n_unclassified,
                len(unclassified_samples),
                ", ".join(unclassified_samples),
            )

    def _handle_repo_candidate(
        self, txn_id: str, repo_path: Path, sqlite_paths: set[Path]
    ) -> None:
        """Repo-level semantic rescue (see git_rescue.build_rescue_package):
        REMOTE_BACKED_CLEAN -> reconstruction metadata only, no bulk data;
        RESCUE_PACKAGED -> the generated delta artifacts (bundle/diffs/
        untracked archive/metadata) become ordinary FileCandidates through
        the normal CAS/upload/manifest pipeline; FULL_BACKUP_REQUIRED (the
        UNCERTAIN fallback, or a packaging failure) -> conservative per-file
        walk, still whole-file-CAS-deduped, never a single repo-wide tar."""
        rescue_staging = self.staging_dir / txn_id / "git_rescue"
        rescue_staging.mkdir(parents=True, exist_ok=True)

        result = git_rescue.build_rescue_package(repo_path, rescue_staging)
        status = result["status"]

        if status == "REMOTE_BACKED_CLEAN":
            info = git_rescue.collect_repo_info(repo_path)
            try:
                head_commit = git_rescue._run_git(
                    repo_path, ["rev-parse", "HEAD"]
                ).strip()
            except git_rescue.GitRescueError:
                head_commit = None
            meta = {
                "repo_path": str(repo_path),
                "classification": "REMOTE_BACKED_CLEAN",
                "remotes": info.get("remotes"),
                "current_branch": info.get("current_branch"),
                "head_commit": head_commit,
            }
            meta_dir = rescue_staging / f"{repo_path.name}_clean"
            meta_dir.mkdir(parents=True, exist_ok=True)
            meta_path = meta_dir / "reconstruction_metadata.json"
            meta_path.write_text(
                json.dumps(meta, indent=2, sort_keys=True, default=str)
            )
            stat = meta_path.stat()
            self._insert_scan_item(
                txn_id,
                ScanEntry(
                    path=meta_path,
                    size=stat.st_size,
                    mtime_ns=stat.st_mtime_ns,
                    classification=Classification.GIT_RESCUE,
                    staging_path=meta_path,
                ),
            )
            return

        if status == "RESCUE_PACKAGED":
            package_dir = Path(result["package_dir"])
            for artifact_path in sorted(package_dir.iterdir()):
                if not artifact_path.is_file():
                    continue
                stat = artifact_path.stat()
                self._insert_scan_item(
                    txn_id,
                    ScanEntry(
                        path=artifact_path,
                        size=stat.st_size,
                        mtime_ns=stat.st_mtime_ns,
                        classification=Classification.GIT_RESCUE,
                        staging_path=artifact_path,
                    ),
                )
            return

        if status == "FULL_BACKUP_REQUIRED":
            self._walk_repo_full_backup(txn_id, repo_path, sqlite_paths)
            return

        raise RuntimeError(
            f"Unrecognized git_rescue.build_rescue_package status {status!r} "
            f"for repo {repo_path}"
        )

    def _walk_repo_full_backup(
        self, txn_id: str, repo_path: Path, sqlite_paths: set[Path]
    ) -> None:
        """Conservative per-file fallback. Whole-file CAS dedup means an
        unchanged file across nightly runs costs nothing extra, unlike a
        single repo-wide tarball -- see the design discussion this was
        chosen over. Nested repos are pruned from this walk and handled
        through their own _handle_repo_candidate call, not swept under this
        repo's classification."""
        for dirpath_str, dirnames, filenames in os.walk(repo_path):
            dirpath = Path(dirpath_str)

            nested_repos = []
            for name in list(dirnames):
                if name == ".git":
                    dirnames.remove(name)
                    continue
                if (dirpath / name / ".git").exists():
                    dirnames.remove(name)
                    nested_repos.append(dirpath / name)
            for nested_repo in nested_repos:
                self._handle_repo_candidate(txn_id, nested_repo, sqlite_paths)

            for name in filenames:
                file_path = dirpath / name
                if file_path.resolve() in sqlite_paths:
                    continue
                try:
                    stat = file_path.stat()
                except OSError as exc:
                    raise RuntimeError(
                        f"Cannot stat file during full-repo fallback: {file_path}"
                    ) from exc
                self._insert_scan_item(
                    txn_id,
                    ScanEntry(
                        path=file_path,
                        size=stat.st_size,
                        mtime_ns=stat.st_mtime_ns,
                        classification=Classification.GIT_RESCUE,
                        staging_path=None,
                    ),
                )

    def _scan_sqlite_databases(self, txn_id: str) -> None:
        """Live SQLite databases go through sqlite3's own online backup API
        (_backup_sqlite), never a raw filesystem copy, regardless of which
        classification would otherwise apply to their containing directory
        -- a raw copy of a WAL-mode database mid-write can be inconsistent.
        A failure here aborts the transaction rather than being logged and
        skipped: silently dropping one of these is exactly the risk this
        path exists to close off, not a best-effort nicety."""
        for relative_path in self.config.policy.sqlite_databases():
            db_path = (self.config.source_root / relative_path).resolve()
            if not db_path.exists():
                logger.warning(
                    "Configured SQLite database does not exist: %s", db_path
                )
                continue

            staging_path = self._backup_sqlite(db_path, txn_id)
            stat = staging_path.stat()
            self._insert_scan_item(
                txn_id,
                ScanEntry(
                    path=db_path,
                    size=stat.st_size,
                    mtime_ns=stat.st_mtime_ns,
                    classification=Classification.SQLITE_DATABASES,
                    staging_path=staging_path,
                ),
            )

    def _backup_sqlite(self, db_path: Path, txn_id: str) -> Path:
        dest_dir = self.staging_dir / txn_id / "sqlite"
        dest_dir.mkdir(parents=True, exist_ok=True)
        dest = dest_dir / db_path.name
        if dest.exists():
            dest.unlink()

        with sqlite3.connect(str(db_path)) as src, sqlite3.connect(str(dest)) as dst:
            src.backup(dst)
        return dest

    def _insert_scan_item(self, txn_id: str, entry: ScanEntry) -> None:
        with self.state_db:
            self.state_db.execute(
                """
                INSERT OR IGNORE INTO scan_items (
                    txn_id, path, staging_path, size, mtime_ns, classification
                )
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    txn_id,
                    str(entry.path),
                    str(entry.staging_path) if entry.staging_path else None,
                    entry.size,
                    entry.mtime_ns,
                    entry.classification.value,
                ),
            )

    # ------------------------------------------------------------------
    # Hash helpers
    # ------------------------------------------------------------------
    def _known_hash(self, path: Path, size: int, mtime_ns: int) -> str | None:
        row = self.state_db.execute(
            "SELECT sha256 FROM known_hashes WHERE path = ? AND size = ? AND mtime_ns = ?",
            (str(path), size, mtime_ns),
        ).fetchone()
        return row["sha256"] if row else None

    def _remember_hash(self, path: Path, size: int, mtime_ns: int, sha256: str) -> None:
        with self.state_db:
            self.state_db.execute(
                """
                INSERT OR REPLACE INTO known_hashes (path, size, mtime_ns, sha256)
                VALUES (?, ?, ?, ?)
                """,
                (str(path), size, mtime_ns, sha256),
            )

    def _sha256_file(
        self,
        file_path: Path,
        expected_size: int | None = None,
        expected_mtime_ns: int | None = None,
    ) -> str:
        h = hashlib.sha256()
        before = file_path.stat()

        if expected_size is not None and before.st_size != expected_size:
            raise ActiveFileError(
                f"{file_path} size changed before hashing: "
                f"expected {expected_size}, got {before.st_size}"
            )
        if expected_mtime_ns is not None and before.st_mtime_ns != expected_mtime_ns:
            raise ActiveFileError(
                f"{file_path} mtime_ns changed before hashing: "
                f"expected {expected_mtime_ns}, got {before.st_mtime_ns}"
            )

        with file_path.open("rb") as fh:
            for chunk in iter(lambda: fh.read(1024 * 1024), b""):
                h.update(chunk)

        after = file_path.stat()
        if before.st_size != after.st_size or before.st_mtime_ns != after.st_mtime_ns:
            raise ActiveFileError(f"{file_path} changed while hashing")

        if expected_size is not None and after.st_size != expected_size:
            raise ActiveFileError(
                f"{file_path} size changed while hashing: expected {expected_size}, got {after.st_size}"
            )
        if expected_mtime_ns is not None and after.st_mtime_ns != expected_mtime_ns:
            raise ActiveFileError(
                f"{file_path} mtime_ns changed while hashing: "
                f"expected {expected_mtime_ns}, got {after.st_mtime_ns}"
            )

        return h.hexdigest()

    # ------------------------------------------------------------------
    # Blob helpers
    # ------------------------------------------------------------------
    def _get_local_blob(self, sha256: str) -> sqlite3.Row | None:
        row = self.state_db.execute(
            "SELECT * FROM runner_blobs WHERE sha256 = ?", (sha256,)
        ).fetchone()
        return row if row else None

    def _upsert_blob(
        self,
        sha256: str,
        size: int,
        local_path: str | None = None,
        remote_checked: bool = False,
        remote_exists: bool = False,
        uploaded_at: str | None = None,
    ) -> None:
        with self.state_db:
            self.state_db.execute(
                """
                INSERT INTO runner_blobs (
                    sha256, size, local_path, remote_checked, remote_exists, uploaded_at
                )
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(sha256) DO UPDATE SET
                    size = excluded.size,
                    local_path = COALESCE(excluded.local_path, runner_blobs.local_path),
                    remote_checked = excluded.remote_checked,
                    remote_exists = excluded.remote_exists,
                    uploaded_at = excluded.uploaded_at
                """,
                (
                    sha256,
                    size,
                    local_path,
                    int(remote_checked),
                    int(remote_exists),
                    uploaded_at or self._now_iso(),
                ),
            )

    # ------------------------------------------------------------------
    # Capacity
    # ------------------------------------------------------------------
    def _check_capacity(self, size: int, classification: Classification) -> None:
        tier = _capacity_tier(classification)  # raises on an unexpected classification

        current = self.config.capacity.current_usage_bytes()
        hard = self.config.capacity.hard_threshold_bytes
        soft = getattr(
            self.config.capacity,
            "soft_threshold_bytes",
            int(hard * self.config.capacity_soft_ratio),
        )

        if tier == "DISCRETIONARY" and current >= soft:
            raise SoftCapacitySkip(
                f"Discretionary output rejected at {current}/{soft} bytes"
            )

        if tier == "HARD_PROTECTED" and current + size > hard:
            raise CapacityError(
                f"Refusing {classification.value} upload of {size} bytes: "
                f"would cross hard capacity threshold ({current} + {size} > {hard})"
            )

    # ------------------------------------------------------------------
    # Upload phase
    # ------------------------------------------------------------------
    def _set_upload_status(
        self, txn_id: str, path: Path | str, status: UploadStatus
    ) -> None:
        with self.state_db:
            self.state_db.execute(
                "UPDATE scan_items SET upload_status = ? WHERE txn_id = ? AND path = ?",
                (status.value, txn_id, str(path)),
            )

    def _add_deferred(self, txn_id: str, path: Path | str, reason: str) -> None:
        if path is None:
            return
        with self.state_db:
            self.state_db.execute(
                """
                INSERT OR REPLACE INTO deferred_items (txn_id, path, reason)
                VALUES (?, ?, ?)
                """,
                (txn_id, str(path), reason),
            )

    def _head_object_after_upload(
        self, key: str, *, attempts: int = 3, delay_seconds: float = 2.0
    ) -> int | None:
        """HeadObject immediately following a fresh upload, tolerant of a
        transient RunPod S3 gateway consistency gap.

        Confirmed live (tests/test_s3_transfer_paths.py's real multipart
        test, plus a standalone diagnostic): a real multipart upload
        completes, list_prefix shows the object immediately, but the very
        next HeadObject can 403 once before succeeding ~3s later. 403 is
        not a botocore-retryable status (Config(retries={"mode": "standard"})
        never covers it), so this was previously a real seed-time failure
        mode for any file over the multipart threshold, not a hypothetical
        -- Runner would raise "HeadObject size mismatch after upload" on a
        completely successful upload. Only used for post-upload
        verification, never for the pre-upload "does this blob already
        exist remotely" lookup, where a genuine absence (None) is normal
        and must not be retried into an error.
        """
        last_exc: Exception | None = None
        for attempt in range(attempts):
            try:
                return self.config.blob_store.head_object(key)
            except Exception as exc:
                last_exc = exc
                if attempt < attempts - 1:
                    logger.warning(
                        "HeadObject after upload failed for %s (attempt %d/%d), retrying: %s",
                        key,
                        attempt + 1,
                        attempts,
                        exc,
                    )
                    time.sleep(delay_seconds)
        assert last_exc is not None
        raise last_exc

    def _upload(self, txn_id: str) -> None:
        logger.info("Upload phase for %s", txn_id)

        with self.state_db:
            self.state_db.execute(
                """
                UPDATE scan_items
                   SET upload_status = ?
                 WHERE txn_id = ? AND upload_status = ?
                """,
                (UploadStatus.PENDING_UPLOAD.value, txn_id, UploadStatus.UPLOADING.value),
            )

        rows = self.state_db.execute(
            """
            SELECT *
              FROM scan_items
             WHERE txn_id = ?
               AND upload_status IN (?, ?)
             ORDER BY path
            """,
            (
                txn_id,
                UploadStatus.PENDING_UPLOAD.value,
                UploadStatus.ERROR.value,
            ),
        ).fetchall()

        for row in rows:
            self._raise_if_cancelled("UPLOADING")

            path = Path(row["path"])
            upload_path = (
                Path(row["staging_path"]) if row["staging_path"] else path
            )
            classification = Classification(row["classification"])
            size = int(row["size"])
            mtime_ns = int(row["mtime_ns"])

            try:
                sha = self._known_hash(path, size, mtime_ns)
                if not sha:
                    sha = self._sha256_file(
                        upload_path, expected_size=size, expected_mtime_ns=mtime_ns
                    )
                    self._remember_hash(path, size, mtime_ns, sha)

                local = self._get_local_blob(sha)
                head = None
                remote_exists = False

                # Local lookup first, then HeadObject before deciding to upload.
                if local is not None and local["remote_exists"]:
                    remote_exists = True
                else:
                    # RunPodStorage.head_object returns a bare int size, or
                    # None if the object doesn't exist -- not an object with
                    # a .size attribute.
                    head = self.config.blob_store.head_object(self._blob_key(sha))
                    if head == size:
                        remote_exists = True
                        self._upsert_blob(
                            sha,
                            size,
                            local_path=local["local_path"] if local else None,
                            remote_checked=True,
                            remote_exists=True,
                        )

                if not remote_exists:
                    self._check_capacity(size, classification)

                    # Upload directly from the source (or, for a
                    # git-rescue/sqlite artifact, its transaction-owned
                    # staging path) -- never a second local copy. Before
                    # 2026-08-29 this called _ensure_local_cache(), which
                    # shutil.copyfile'd every not-yet-remote blob into
                    # state/blob_cache/ regardless of classification; that
                    # grew to 174 GiB, 99.999% of which was already
                    # durable on remote S3 the whole time. runner_blobs
                    # .local_path is now purely informational provenance
                    # (which local path was hashed/uploaded from), never a
                    # second CAS this code reads back from -- see
                    # _get_local_blob's only real use above (deciding
                    # whether a HeadObject call can be skipped).
                    self._upsert_blob(
                        sha,
                        size,
                        local_path=str(upload_path),
                        remote_checked=False,
                        remote_exists=False,
                    )
                    self._set_upload_status(txn_id, path, UploadStatus.UPLOADING)

                    # put_object_from_file only returns after internally
                    # verifying the object is present at the expected size
                    # (see storage.py's module docstring on the three real
                    # RunPod gateway quirks it absorbs) -- no separate
                    # post-upload HeadObject check needed here anymore.
                    self.config.blob_store.put_object_from_file(self._blob_key(sha), upload_path)
                    head = size

                    self._upsert_blob(
                        sha,
                        size,
                        local_path=str(upload_path),
                        remote_checked=True,
                        remote_exists=True,
                    )

                # Each newly-reached blob is verified by HeadObject size.
                if head is None:
                    head = self.config.blob_store.head_object(self._blob_key(sha))
                if head != size:
                    raise RuntimeError(
                        f"Remote size mismatch for {sha}: expected {size}, got {head!r}"
                    )

                with self.state_db:
                    self.state_db.execute(
                        """
                        UPDATE scan_items
                           SET sha256 = ?,
                               blob_key = ?,
                               upload_status = ?,
                               size_remote = ?,
                               error = NULL
                         WHERE txn_id = ? AND path = ?
                        """,
                        (
                            sha,
                            sha,
                            UploadStatus.UPLOADED.value,
                            size,
                            txn_id,
                            str(path),
                        ),
                    )

                # Once durably UPLOADED, a staged git-rescue/sqlite
                # artifact is no longer REQUIRED_FOR_RESUME -- a resumed
                # transaction's _upload() only re-processes PENDING_UPLOAD/
                # ERROR rows (see the SELECT above), never UPLOADED ones.
                # Delete it now rather than letting every transaction's
                # staging accumulate for its whole lifetime.
                if row["staging_path"]:
                    self._cleanup_staging_file(Path(row["staging_path"]))

            except ActiveFileError as exc:
                logger.warning("File actively being written, deferring %s: %s", path, exc)
                self._add_deferred(txn_id, path, "DEFERRED_ACTIVE")
                self._set_upload_status(txn_id, path, UploadStatus.DEFERRED_ACTIVE)

            except SoftCapacitySkip:
                logger.info("Discretionary output skipped at capacity threshold: %s", path)
                self._set_upload_status(txn_id, path, UploadStatus.DEFERRED_CAPACITY_SOFT)

            except CapacityError:
                raise

    def _cleanup_staging_file(self, staging_path: Path) -> None:
        """Best-effort delete of one staged artifact (a git-rescue file or
        a SQLite .backup snapshot) once it's no longer needed. Never
        allowed to fail the upload/transaction -- a leftover staging file
        is a leak to fix later, not a reason to mark real backed-up data
        FAILED."""
        try:
            staging_path.unlink(missing_ok=True)
        except OSError:
            logger.warning(
                "Could not remove staging artifact %s (leaving it; a stale "
                "leftover now that it's uploaded, not a resume dependency)",
                staging_path,
            )

    # ------------------------------------------------------------------
    # Verify phase
    # ------------------------------------------------------------------
    def _verify(self, txn_id: str) -> None:
        logger.info("Verify phase for %s", txn_id)

        rows = self.state_db.execute(
            """
            SELECT *
              FROM scan_items
             WHERE txn_id = ?
               AND upload_status = ?
               AND verify_status = ?
             ORDER BY path
            """,
            (
                txn_id,
                UploadStatus.UPLOADED.value,
                VerifyStatus.PENDING.value,
            ),
        ).fetchall()

        downloaded_bytes = 0
        cap = self.config.verify_download_cap_bytes

        for row in rows:
            self._raise_if_cancelled("VERIFYING")

            path = Path(row["path"])
            sha = str(row["sha256"])
            size = int(row["size"])

            head = self._head_object_after_upload(self._blob_key(sha))
            if head != size:
                raise RuntimeError(
                    f"HeadObject verification failed for {sha}: "
                    f"expected {size}, got {head!r}"
                )

            verify_status = VerifyStatus.VERIFIED
            download_verified = 0

            if cap > 0 and downloaded_bytes + size <= cap:
                # Ephemeral scratch file (default tempfile location, e.g.
                # /tmp) -- this is download, hash-check, discard within
                # this one call. A resume never needs yesterday's
                # downloaded sample; it just re-downloads. Deliberately
                # never written under persistent staging_dir -- see
                # app.runner's module docstring on ephemeral vs.
                # transaction-owned persistent staging.
                fd, tmp_name = tempfile.mkstemp(prefix=f"verify-{sha}-")
                os.close(fd)
                dest = Path(tmp_name)
                try:
                    self.config.blob_store.get_object_to_file(self._blob_key(sha), dest)
                    digest = self._sha256_file(dest, expected_size=size)
                    if digest != sha:
                        raise RuntimeError(f"Download verification failed for {sha}")
                    downloaded_bytes += size
                    verify_status = VerifyStatus.DOWNLOAD_VERIFIED
                    download_verified = 1
                finally:
                    dest.unlink(missing_ok=True)

            with self.state_db:
                self.state_db.execute(
                    """
                    UPDATE scan_items
                       SET verify_status = ?,
                           size_remote = ?,
                           download_verified = ?,
                           error = NULL
                     WHERE txn_id = ? AND path = ?
                    """,
                    (
                        verify_status.value,
                        size,
                        download_verified,
                        txn_id,
                        str(path),
                    ),
                )

    # ------------------------------------------------------------------
    # Commit phase
    # ------------------------------------------------------------------
    def _commit(self, txn_id: str, txn_date: str) -> None:
        logger.info("Commit phase for %s", txn_id)

        tx = self._get_transaction(txn_id)
        if not tx["manifest_committed"]:
            self._upload_manifest(txn_id, txn_date)
            with self.state_db:
                self.state_db.execute(
                    """
                    UPDATE transactions
                       SET manifest_committed = 1,
                           updated_at = ?
                     WHERE txn_id = ?
                    """,
                    (self._now_iso(), txn_id),
                )

        tx = self._get_transaction(txn_id)
        if not tx["catalog_uploaded"]:
            self._upload_catalog(txn_id)
            with self.state_db:
                self.state_db.execute(
                    """
                    UPDATE transactions
                       SET catalog_uploaded = 1,
                           updated_at = ?
                     WHERE txn_id = ?
                    """,
                    (self._now_iso(), txn_id),
                )

    def _upload_manifest(self, txn_id: str, txn_date: str) -> None:
        entries = self.state_db.execute(
            """
            SELECT path, size, mtime_ns, sha256, classification
              FROM scan_items
             WHERE txn_id = ?
               AND upload_status = ?
             ORDER BY path
            """,
            (txn_id, UploadStatus.UPLOADED.value),
        ).fetchall()

        deferred = self.state_db.execute(
            "SELECT path, reason FROM deferred_items WHERE txn_id = ? ORDER BY path",
            (txn_id,),
        ).fetchall()

        manifest = {
            "txn_id": txn_id,
            "date": txn_date,
            "created_at": self._now_iso(),
            "policy_hash": self.config.policy.policy_hash,
            "entries": [
                {
                    "path": row["path"],
                    "size": int(row["size"]),
                    "mtime_ns": int(row["mtime_ns"]),
                    "sha256": row["sha256"],
                    "classification": row["classification"],
                }
                for row in entries
            ],
            "deferred": [
                {"path": row["path"], "reason": row["reason"]} for row in deferred
            ],
        }

        # One durable transaction for the whole loop, not two per entry --
        # see StateDB.batch(): on this MooseFS-mounted state DB the
        # per-entry commits were the entire ~104-minute COMMITTING phase.
        with self.canonical_state.batch():
            for row in entries:
                sha = row["sha256"]
                if not sha:
                    continue
                try:
                    self.canonical_state.record_blob(
                        sha,
                        int(row["size"]),
                        remote_key=self._blob_key(sha),
                        first_uploaded=self._now_iso(),
                    )
                    self.canonical_state.bump_refcount(sha, delta=1)
                except Exception:
                    logger.warning(
                        "Failed to update canonical blob state for %s during manifest commit %s",
                        sha,
                        txn_id,
                        exc_info=True,
                    )

        # Ephemeral scratch file: rebuilt fresh from scan_items/
        # deferred_items every call (including a resume that reaches this
        # phase again after a crash), so it is never itself something a
        # resume depends on -- deliberately not under persistent
        # staging_dir. See app.runner's module docstring.
        fd, tmp_name = tempfile.mkstemp(prefix=f"manifest-{txn_id}-", suffix=".json")
        try:
            with os.fdopen(fd, "w") as fh:
                json.dump(manifest, fh, indent=2, sort_keys=True)
            manifest_key = self._key(f"manifests/{txn_id}.json")
            self.config.blob_store.put_object_from_file(manifest_key, tmp_name)
        finally:
            Path(tmp_name).unlink(missing_ok=True)

        self._upload_policy_provenance()

        # Local StateDB record of this snapshot -- required for retention to
        # have anything to expire (RetentionRunner reads list_snapshots /
        # snapshot_entry_hashes) and for restore/verify's remote snapshot
        # index (meta/snapshot-index.json) to find this manifest at all.
        # uploaded_bytes/deduped_bytes are an approximation (selected_bytes
        # for both fields) -- Runner doesn't currently distinguish "freshly
        # uploaded this run" from "already present, deduped" at the byte-sum
        # level, only per-blob. Reporting-only; nothing downstream depends
        # on this split being exact.
        selected_bytes = sum(int(row["size"]) for row in entries)
        self.canonical_state.record_snapshot(
            txn_date,
            manifest_key,
            self._now_iso(),
            selected_bytes,
            selected_bytes,
            0,
            len(entries),
        )
        with self.canonical_state.batch():
            for row in entries:
                if row["sha256"]:
                    self.canonical_state.add_snapshot_entry(
                        txn_date, row["path"], row["sha256"]
                    )

        self._update_snapshot_index(txn_date, manifest_key)

    def _upload_policy_provenance(self) -> None:
        """Preserve the exact canonical policy bytes that produced this
        transaction's policy_hash, content-addressed by that hash.

        Before this, only policy_hash was durably recorded (in the
        transactions row and, transitively, catalog/<txn_id>.db) -- a
        fingerprint, not the policy itself. Aug23's mid-run policy.yaml
        edit was only forensically recoverable afterwards because
        policy.yaml happened to still be in scope for backup that day; this
        makes recovery deliberate rather than lucky. Content-addressed by
        policy_hash, so an unchanged policy across many days costs one
        upload, not one per snapshot -- verified against existing content
        before ever being treated as "already there," and never
        overwritten if a different artifact is somehow already at that key.
        """
        policy_hash = self.config.policy.policy_hash
        canonical_bytes = self.config.policy.canonical_bytes
        key = self._key(f"meta/policies/{policy_hash}.json")

        if self.config.blob_store.exists(key):
            existing = self.config.blob_store.get_object_bytes(key)
            if existing != canonical_bytes:
                logger.error(
                    "Policy provenance artifact %s already exists with "
                    "content that does NOT match the current canonical "
                    "policy bytes despite matching policy_hash -- leaving "
                    "the existing artifact untouched rather than "
                    "overwriting; this needs manual investigation.",
                    key,
                )
            return

        self.config.blob_store.put_object_bytes(key, canonical_bytes)

    def _update_snapshot_index(self, txn_date: str, manifest_key: str) -> None:
        """Read-modify-write meta/snapshot-index.json so restore.py's
        list_snapshots/_resolve_manifest_key (which only ever read this
        index, never list the blob tree) can find what was just committed.
        """
        index_key = self._key("meta/snapshot-index.json")
        try:
            raw = self.config.blob_store.get_object_bytes(index_key)
            index = json.loads(raw)
            if not isinstance(index, dict):
                index = {}
        except Exception:
            index = {}

        index[txn_date] = manifest_key
        self.config.blob_store.put_object_bytes(
            index_key, json.dumps(index, indent=2, sort_keys=True).encode("utf-8")
        )

    def _upload_catalog(self, txn_id: str) -> None:
        # Ephemeral scratch copy of state.sqlite -- src.backup(dst) below
        # can regenerate this fresh from the CURRENT state.sqlite on any
        # resume that reaches this phase again, so it is never itself
        # something a resume depends on (unlike the SQLite/git-rescue
        # source-database staging in _backup_sqlite/_handle_repo_candidate,
        # which captures point-in-time bytes that must survive to upload
        # time). Deliberately never under persistent staging_dir -- see
        # app.runner's module docstring.
        fd, tmp_name = tempfile.mkstemp(prefix=f"catalog-{txn_id}-", suffix=".db")
        os.close(fd)
        catalog_staging = Path(tmp_name)
        try:
            with sqlite3.connect(str(self.state_db_path)) as src, sqlite3.connect(
                str(catalog_staging)
            ) as dst:
                src.backup(dst)

            self.config.blob_store.put_object_from_file(
                self._key(f"catalog/{txn_id}.db"), catalog_staging
            )
        finally:
            catalog_staging.unlink(missing_ok=True)

    # ------------------------------------------------------------------
    # Post-verify phase
    # ------------------------------------------------------------------
    def _post_verify(self, txn_id: str) -> None:
        logger.info("Post-verify phase for %s", txn_id)

        tx = self._get_transaction(txn_id)

        if not tx["retention_done"]:
            self.config.retention.run(txn_id)
            with self.state_db:
                self.state_db.execute(
                    "UPDATE transactions SET retention_done = 1, updated_at = ? WHERE txn_id = ?",
                    (self._now_iso(), txn_id),
                )

        if self.config.gc_weekday is not None and self._melbourne_weekday() == self.config.gc_weekday:
            tx = self._get_transaction(txn_id)
            if not tx["gc_done"]:
                try:
                    self.config.gc.run()
                except Exception as exc:
                    logger.warning(
                        "Garbage collection failed for %s: %s. Run will continue.",
                        txn_id,
                        exc,
                    )
                finally:
                    with self.state_db:
                        self.state_db.execute(
                            "UPDATE transactions SET gc_done = 1, updated_at = ? WHERE txn_id = ?",
                            (self._now_iso(), txn_id),
                        )

        self._cleanup_transaction_staging(txn_id)

    def _cleanup_transaction_staging(self, txn_id: str) -> None:
        """Best-effort final sweep of this transaction's own staging
        directory, run only after every required remote durability gate
        above has passed (manifest committed, catalog uploaded, retention
        done). By this point almost nothing should remain: _upload()
        already deletes each git-rescue/sqlite artifact right after its
        own successful upload (see _cleanup_staging_file), and manifest/
        catalog/verify staging never touch staging_dir at all (ephemeral
        tempfiles instead). This is a safety net, not the primary cleanup
        mechanism -- it catches whatever per-item cleanup missed, e.g. a
        file that failed to delete earlier, or an artifact belonging to a
        DEFERRED_ACTIVE/DEFERRED_CAPACITY_SOFT item that was never
        uploaded this run and has no other owner once the transaction
        itself is done.

        Cleanup failure must never turn a valid remote backup into FAILED
        (see _init_schema's migration comment) -- recorded separately as
        local_cleanup_status/local_cleanup_error/local_cleanup_bytes_left,
        surfaced by `backupctl status`/`staging-status`/`doctor`, never
        fed back into transaction phase.
        """
        txn_staging = self.staging_dir / txn_id
        if not txn_staging.exists():
            self._record_local_cleanup_result(txn_id, "nothing_to_clean", None, 0)
            return

        try:
            shutil.rmtree(txn_staging)
            self._record_local_cleanup_result(txn_id, "cleaned", None, 0)
        except Exception as exc:
            bytes_left = self._directory_size_bytes(txn_staging)
            logger.warning(
                "Local staging cleanup for %s failed (backup remains SUCCESS): %s",
                txn_id,
                exc,
            )
            self._record_local_cleanup_result(txn_id, "failed", str(exc), bytes_left)

    def _directory_size_bytes(self, path: Path) -> int:
        total = 0
        for dirpath, _dirnames, filenames in os.walk(path):
            for name in filenames:
                try:
                    total += (Path(dirpath) / name).stat().st_size
                except OSError:
                    continue
        return total

    def _record_local_cleanup_result(
        self, txn_id: str, status: str, error: str | None, bytes_left: int
    ) -> None:
        with self.state_db:
            self.state_db.execute(
                """
                UPDATE transactions
                   SET local_cleanup_status = ?,
                       local_cleanup_error = ?,
                       local_cleanup_bytes_left = ?
                 WHERE txn_id = ?
                """,
                (status, error, bytes_left, txn_id),
            )

    # ------------------------------------------------------------------
    # Top-level run
    # ------------------------------------------------------------------
    def run(self) -> None:
        self.run_for_date(self._melbourne_today())

    def run_for_date(self, txn_date: str) -> None:
        """Run (or resume, or retry a FAILED) the transaction for txn_date.

        run() is just this called with today's Melbourne date. retry uses
        this directly with an explicit past date -- the FAILED-phase reset
        below is the same mechanism either way, there is no separate retry
        code path to drift out of sync with normal resume.
        """
        txn_id = f"backup-{txn_date}"

        self._upsert_transaction(txn_id, txn_date)
        phase = self._get_phase(txn_id)

        if phase == Phase.SUCCESS:
            logger.info("Transaction %s already succeeded; nothing to do", txn_id)
            return

        if phase == Phase.ABANDONED:
            # Terminal by deliberate operator action. Resuming it would
            # silently undo that decision, so this refuses rather than
            # no-ops quietly -- an operator who typed `retry <date>` at an
            # abandoned transaction has a wrong belief worth correcting.
            raise RuntimeError(
                f"Transaction {txn_id} was deliberately ABANDONED and will not be "
                f"resumed. Start a new transaction for a current date instead."
            )

        if phase == Phase.FAILED:
            tx = self._get_transaction(txn_id)
            raw_resume_phase = tx["resume_phase"]
            resume_phase = Phase(raw_resume_phase) if raw_resume_phase else Phase.PENDING

            if resume_phase in (Phase.PENDING, Phase.SCANNING):
                logger.warning(
                    "Transaction %s previously failed during %s; scan state "
                    "isn't complete, resetting for a full new attempt",
                    txn_id, resume_phase.value,
                )
                self._reset_transaction(txn_id)
            else:
                # Resuming past SCANNING reuses scan_items as-is and does
                # NOT re-classify against policy.yaml -- if the policy that
                # produced them is not the policy in force right now, those
                # rows may carry stale classification/selection. Fail closed
                # rather than silently upload/skip the wrong set of files.
                self._check_resumable_policy(txn_id)
                logger.warning(
                    "Transaction %s previously failed during %s; resuming "
                    "there (scan_items already complete, not re-scanning)",
                    txn_id, resume_phase.value,
                )
                self._resume_transaction(txn_id, resume_phase)
            phase = self._get_phase(txn_id)

        try:
            self._record_healthchecks_result(
                txn_id, "start", self._ping_healthcheck("start")
            )
            self._run_phases(txn_id, txn_date)
            self._mark_success(txn_id)
        except TransactionCancelled as exc:
            # A requested stop is not a failure. Leave the phase exactly
            # where it stands (durable, resumable), send no failure email
            # and ping neither healthcheck endpoint: a cancelled run
            # produced no snapshot, so the dead-man switch SHOULD go
            # unfed and alert if the day ends without a real success. A
            # concise ntfy "Interrupted" push is still sent, best-effort --
            # distinct from both SUCCESS and FAILED, so a deliberate
            # operator stop is never described as either.
            logger.info(
                "Transaction %s stopped on request (%s); phase left at %s for resume",
                txn_id,
                exc,
                self._get_phase(txn_id).value,
            )
            self._send_interrupted_notification(txn_id, reason=str(exc))
            raise
        except Exception as exc:
            self._mark_failed(txn_id, str(exc))
            self._record_healthchecks_result(
                txn_id, "fail", self._ping_healthcheck("fail", str(exc))
            )
            logger.exception("Backup transaction %s failed", txn_id)
            self._send_transaction_notifications(txn_id, success=False)
            raise

        # The transaction is already durably SUCCESS in StateDB at this
        # point. A dead Healthchecks.io endpoint must not turn a good
        # backup into FAILED -- ping is best-effort, deliberately outside
        # the block above.
        self._record_healthchecks_result(
            txn_id, "success", self._ping_healthcheck("success", self._success_summary(txn_id))
        )

        self._send_transaction_notifications(txn_id, success=True)

    def _ping_healthcheck(self, kind: str, body: str | None = None) -> Any:
        """Call the configured Healthchecks kind (start/success/fail),
        isolated in its own try/except -- HealthcheckClient itself already
        never raises (see app/healthcheck.py), but this survives even a
        test double or future implementation that does."""
        try:
            if kind == "start":
                return self.config.healthcheck.start()
            if kind == "fail":
                return self.config.healthcheck.fail(body)
            return self.config.healthcheck.success(body)
        except Exception:
            logger.exception("Healthcheck %s ping itself failed", kind)
            return None

    def _success_summary(self, txn_id: str) -> str:
        row = self.state_db.execute(
            """
            SELECT COUNT(*) AS n, COALESCE(SUM(size), 0) AS total_bytes
              FROM scan_items
             WHERE txn_id = ? AND upload_status = ?
            """,
            (txn_id, UploadStatus.UPLOADED.value),
        ).fetchone()
        return (
            f"{txn_id}: {row['n']} files uploaded, {row['total_bytes']} bytes selected"
        )

    def _send_transaction_notifications(self, txn_id: str, *, success: bool) -> None:
        """Best-effort daily report over every configured channel (ntfy,
        email) -- same isolation contract as the healthcheck ping: a dead
        transport or a malformed report must never turn a good backup into
        FAILED, and a good notification must never be required for a real
        FAILED transaction to be reported as such. Each channel is wrapped
        in its own try/except so ntfy and email can never take each other
        down -- a dead SMTP server must not silence the phone push, and a
        dead ntfy.sh must not silence the email.
        """
        email_client = getattr(self.config, "email", None)
        ntfy_client = getattr(self.config, "ntfy", None)
        if email_client is None and ntfy_client is None:
            return

        try:
            report = build_transaction_report(
                self.state_db, txn_id, self.config.source_root
            )
            # Whole-system snapshot at report time, not per-transaction --
            # surfaced in the report so a staging regression (another
            # 174-GiB-style leak) is visible in the same place operators
            # already glance at daily, per Daniel's 2026-08-29 request.
            staging = build_staging_report(self.state_db, self.staging_dir)
            report["local_staging_bytes"] = staging["total_staging_bytes"]
        except Exception:
            logger.exception(
                "Building the transaction report for %s failed; no notification "
                "sent on any channel (backup result unaffected)",
                txn_id,
            )
            return

        if ntfy_client is not None:
            try:
                notifier = ntfy_client.success if success else ntfy_client.failure
                self._record_notification_result(txn_id, "ntfy", notifier(report))
            except Exception:
                logger.exception(
                    "ntfy notification for %s failed (backup result unaffected)", txn_id
                )

        if email_client is not None:
            try:
                notifier = email_client.success if success else email_client.failure
                self._record_notification_result(txn_id, "email", notifier(report))
            except Exception:
                logger.exception(
                    "Email notification for %s failed (backup result unaffected)", txn_id
                )

    def _send_interrupted_notification(self, txn_id: str, *, reason: str) -> None:
        """Best-effort ntfy-only push for a deliberate cooperative stop
        (see TransactionCancelled). Not sent over email -- email has no
        concept of this state and building one is not worth it for a
        channel Daniel no longer treats as primary. Healthchecks
        deliberately gets no ping here either (see run_for_date's
        TransactionCancelled handler) -- a cancelled run produced no
        snapshot, and the dead-man switch should stay hungry.
        """
        ntfy_client = getattr(self.config, "ntfy", None)
        if ntfy_client is None:
            return
        try:
            phase = self._get_phase(txn_id)
            result = ntfy_client.interrupted(txn_id, phase.value, reason)
            self._record_notification_result(txn_id, "ntfy", result)
        except Exception:
            logger.exception(
                "Interrupted-notification for %s failed (backup result unaffected)",
                txn_id,
            )

    def _record_healthchecks_result(self, txn_id: str, kind: str, result: Any) -> None:
        """Persist the most recent Healthchecks ping's outcome -- which of
        start/success/fail it was, and whether the ping itself transported.
        Never affects backup SUCCESS/FAILED; surfaced by `backupctl
        status`/`doctor` only. A None result (the ping call itself raised,
        see _ping_healthcheck) is recorded as a transport error rather than
        silently skipped -- an operator checking status after a real
        failure should see that the LAST thing that happened was a broken
        ping, not nothing.
        """
        if result is None:
            status, error = "failed", "healthcheck client raised"
        else:
            status = getattr(result, "status", None)
            error = getattr(result, "error", None)
        with self.state_db:
            self.state_db.execute(
                """
                UPDATE transactions
                   SET healthchecks_last_kind = ?,
                       healthchecks_last_status = ?,
                       healthchecks_last_error = ?
                 WHERE txn_id = ?
                """,
                (kind, status, error, txn_id),
            )

    def _record_notification_result(self, txn_id: str, channel: str, result: Any) -> None:
        """Persist one channel's notification outcome separately from
        transaction phase/error (see _init_schema's migration comment) --
        surfaced by `backupctl status`/`backupctl doctor`, never fed back
        into backup SUCCESS/FAILED. ``channel`` is "email" or "ntfy";
        each has its own status/attempts/error triple so one channel's
        result can never overwrite another's.
        """
        if channel not in ("email", "ntfy"):
            raise ValueError(f"unknown notification channel: {channel!r}")
        status_col = "notification_status" if channel == "email" else "ntfy_status"
        attempts_col = "notification_attempts" if channel == "email" else "ntfy_attempts"
        error_col = "notification_error" if channel == "email" else "ntfy_error"
        with self.state_db:
            self.state_db.execute(
                f"""
                UPDATE transactions
                   SET {status_col} = ?,
                       {attempts_col} = ?,
                       {error_col} = ?
                 WHERE txn_id = ?
                """,
                (
                    getattr(result, "status", None),
                    getattr(result, "attempts", None),
                    getattr(result, "error", None),
                    txn_id,
                ),
            )

    def _run_phases(self, txn_id: str, txn_date: str) -> None:
        phase = self._get_phase(txn_id)

        if phase == Phase.PENDING:
            self._raise_if_cancelled("PENDING")
            self._set_phase(txn_id, Phase.SCANNING)
            phase = Phase.SCANNING

        if phase == Phase.SCANNING:
            self._raise_if_cancelled("SCANNING")
            self._scan(txn_id)
            self._set_phase(txn_id, Phase.UPLOADING)
            phase = Phase.UPLOADING

        if phase == Phase.UPLOADING:
            self._raise_if_cancelled("UPLOADING")
            self._upload(txn_id)
            self._set_phase(txn_id, Phase.VERIFYING)
            phase = Phase.VERIFYING

        if phase == Phase.VERIFYING:
            self._raise_if_cancelled("VERIFYING")
            self._verify(txn_id)
            self._set_phase(txn_id, Phase.COMMITTING)
            phase = Phase.COMMITTING

        # No cancellation check from here on: COMMITTING/POST_VERIFY are
        # the manifest-landing unit of work (see _raise_if_cancelled).
        if phase == Phase.COMMITTING:
            self._commit(txn_id, txn_date)
            self._set_phase(txn_id, Phase.POST_VERIFY)
            phase = Phase.POST_VERIFY

        if phase == Phase.POST_VERIFY:
            self._post_verify(txn_id)
            self._set_phase(txn_id, Phase.SUCCESS)


def _relative_root(path: str, source_root: str) -> str:
    """First path component of ``path`` relative to ``source_root`` -- the
    "main source root" grouping key for build_transaction_report's top-roots
    breakdown. "(other)" for anything not actually under source_root (should
    not happen for real scan_items rows, but reporting must never raise on
    unexpected shape)."""
    try:
        parts = Path(path).relative_to(source_root).parts
    except ValueError:
        return "(other)"
    return parts[0] if parts else "(other)"


def _decode_scan_skipped(raw: Any) -> dict[str, int] | None:
    """Decode the transactions.scan_skipped JSON blob.

    Returns None for a transaction that predates the column or whose value
    is unreadable -- deliberately distinct from an empty dict, which means
    "the scan ran and skipped nothing". Collapsing those two would let a
    report claim nothing was missed when in truth nothing was measured.
    """
    if not raw:
        return None
    try:
        decoded = json.loads(raw)
    except (TypeError, ValueError):
        return None
    if not isinstance(decoded, dict):
        return None
    return {str(k): int(v) for k, v in decoded.items()}


def build_transaction_report(
    conn: sqlite3.Connection, txn_id: str, source_root: Path | str
) -> dict[str, Any]:
    """Read-only operator-facing summary of one transaction, for the daily
    email report (app/notify_email.py) and `backupctl report`/`status`.
    Everything here is derived directly from Runner's own
    transactions/scan_items tables -- nothing fabricated or sampled.

    Safe to call against a still-in-progress or FAILED transaction (that's
    exactly the FAILURE-email case) as well as a finished SUCCESS one.
    ``conn`` must have ``row_factory = sqlite3.Row`` set (both Runner's own
    self.state_db and app.statedb.StateDB's .conn already do).
    """
    source_root = str(Path(source_root))

    txn_row = conn.execute(
        "SELECT * FROM transactions WHERE txn_id = ?", (txn_id,)
    ).fetchone()
    if txn_row is None:
        raise RuntimeError(f"No transaction {txn_id!r} to report on")
    txn = dict(txn_row)

    rows = conn.execute(
        "SELECT path, size, classification, upload_status, sha256, error "
        "FROM scan_items WHERE txn_id = ?",
        (txn_id,),
    ).fetchall()

    uploaded = [r for r in rows if r["upload_status"] == UploadStatus.UPLOADED.value]
    deferred_active = [
        r for r in rows if r["upload_status"] == UploadStatus.DEFERRED_ACTIVE.value
    ]
    # Rows still carrying an error at report time. For a real SUCCESS this
    # is structurally always 0 (an unhandled upload exception aborts the
    # whole transaction before SUCCESS is reachable -- see _upload), but a
    # FAILED-transaction report needs the real count, not an assumption,
    # and a compact ntfy/email line ("0 upload errors") should say so
    # because it measured it, not because it guessed.
    error_count = sum(1 for r in rows if r["error"])
    total_selected_bytes = sum(r["size"] for r in rows)

    by_classification: dict[str, dict[str, int]] = {}
    by_root: dict[str, dict[str, int]] = {}
    for r in uploaded:
        c = by_classification.setdefault(r["classification"], {"count": 0, "bytes": 0})
        c["count"] += 1
        c["bytes"] += r["size"]

        root = _relative_root(r["path"], source_root)
        rt = by_root.setdefault(root, {"count": 0, "bytes": 0})
        rt["count"] += 1
        rt["bytes"] += r["size"]

    top_roots = sorted(
        ({"root": k, **v} for k, v in by_root.items()),
        key=lambda x: x["bytes"],
        reverse=True,
    )[:15]

    deferred_paths = [r["path"] for r in deferred_active]
    deferred_summary = {
        "count": len(deferred_paths),
        "paths": deferred_paths[:20],
        "truncated": len(deferred_paths) > 20,
    }

    prev_row = conn.execute(
        "SELECT txn_id FROM transactions WHERE phase = ? AND txn_date < ? "
        "ORDER BY txn_date DESC LIMIT 1",
        (Phase.SUCCESS.value, txn["txn_date"]),
    ).fetchone()

    diff: dict[str, Any] | None = None
    if prev_row is not None:
        prev_txn_id = prev_row["txn_id"]
        prev_rows = conn.execute(
            "SELECT path, sha256 FROM scan_items WHERE txn_id = ? AND upload_status = ?",
            (prev_txn_id, UploadStatus.UPLOADED.value),
        ).fetchall()
        prev_by_path = {r["path"]: r["sha256"] for r in prev_rows}
        curr_by_path = {r["path"]: r["sha256"] for r in uploaded}

        added = sorted(set(curr_by_path) - set(prev_by_path))
        disappeared = sorted(set(prev_by_path) - set(curr_by_path))
        changed = sorted(
            p
            for p in (set(curr_by_path) & set(prev_by_path))
            if curr_by_path[p] != prev_by_path[p]
        )
        prev_hashes = set(prev_by_path.values())
        new_unique_bytes = sum(
            r["size"] for r in uploaded if r["sha256"] not in prev_hashes
        )

        diff = {
            "previous_txn_id": prev_txn_id,
            "added_count": len(added),
            "added_paths": added[:20],
            "changed_count": len(changed),
            "changed_paths": changed[:20],
            "disappeared_count": len(disappeared),
            "disappeared_paths": disappeared[:20],
            "new_unique_bytes": new_unique_bytes,
        }

    last_row = conn.execute(
        "SELECT path FROM scan_items WHERE txn_id = ? ORDER BY rowid DESC LIMIT 1",
        (txn_id,),
    ).fetchone()

    return {
        "txn_id": txn_id,
        "txn_date": txn["txn_date"],
        "phase": txn["phase"],
        "error": txn["error"],
        "resume_phase": txn.get("resume_phase"),
        "created_at": txn["created_at"],
        "updated_at": txn["updated_at"],
        "file_count": len(uploaded),
        "protected_bytes": sum(r["size"] for r in uploaded),
        "by_classification": by_classification,
        "top_roots": top_roots,
        "deferred_active": deferred_summary,
        "diff_from_previous_success": diff,
        "progress": {"uploaded": len(uploaded), "total_scan_items": len(rows)},
        "error_count": error_count,
        "total_selected_bytes": total_selected_bytes,
        # What the scan declined to select, by classification (see
        # Runner._log_skipped_summary). None means the transaction predates
        # this accounting -- the report must render that as unknown, never
        # as "nothing was skipped".
        "scan_skipped": _decode_scan_skipped(txn.get("scan_skipped")),
        # Best-effort only: scan_items has no processing-order timestamp,
        # so this is sqlite's implicit rowid (insertion order) as a proxy
        # for "most recently touched", not a guaranteed "currently active" path.
        "last_known_path": last_row["path"] if last_row else None,
    }


_STAGING_CATEGORIES = ("sqlite", "git_rescue", "catalog", "manifest", "verify")


def _walk_bytes_by_category(root: Path) -> tuple[int, dict[str, int], float | None]:
    """Total bytes, bytes-by-top-level-subdir, and oldest file mtime under
    root. A file directly in root (no category subdir) or under an
    unrecognised subdir name is bucketed as "unknown" -- see
    _STAGING_CATEGORIES; catalog.db/manifest.json staging is expected to
    be absent entirely post-2026-08-29 (moved to ephemeral tempfiles), so
    finding one here again is itself a regression signal.
    """
    total = 0
    by_category: dict[str, int] = {}
    oldest_mtime: float | None = None
    for dirpath, _dirnames, filenames in os.walk(root):
        for name in filenames:
            fp = Path(dirpath) / name
            try:
                stat = fp.stat()
            except OSError:
                continue
            size = stat.st_size
            total += size
            rel_parts = fp.relative_to(root).parts
            if len(rel_parts) > 1 and rel_parts[0] in _STAGING_CATEGORIES:
                category = rel_parts[0]
            elif name == "manifest.json":
                category = "manifest"
            elif name == "catalog.db":
                category = "catalog"
            else:
                category = "unknown"
            by_category[category] = by_category.get(category, 0) + size
            if oldest_mtime is None or stat.st_mtime < oldest_mtime:
                oldest_mtime = stat.st_mtime
    return total, by_category, oldest_mtime


def build_staging_report(
    conn: sqlite3.Connection,
    staging_dir: Path | str,
    blob_cache_dir: Path | str | None = None,
) -> dict[str, Any]:
    """Read-only summary of everything currently under staging_dir (and,
    if given, the legacy blob_cache_dir -- which should be empty/absent
    after the 2026-08-29 fix removing the local blob cache from the
    upload path entirely; any non-zero size there is itself a warning
    sign). Used by `backupctl staging-status`, `backupctl status`'s
    "Local staging" line, `backupctl doctor`'s staging-size warnings, and
    the daily email report.

    Each transaction subdirectory is classified:
      - "stale": owning transaction's phase is SUCCESS -- a SUCCESS
        transaction should retain essentially no payload staging (see
        Runner._cleanup_transaction_staging); non-empty here means that
        sweep didn't run or failed.
      - "unknown_owner": the directory name doesn't match any row in
        `transactions` at all.
    Neither condition raises; both are just flagged in the returned dict
    for the caller to warn on.
    """
    staging_dir = Path(staging_dir)

    phase_by_txn = {
        row["txn_id"]: row["phase"]
        for row in conn.execute("SELECT txn_id, phase FROM transactions").fetchall()
    }

    transactions_report: list[dict[str, Any]] = []
    total_bytes = 0
    oldest_mtime: float | None = None
    oldest_txn: str | None = None

    if staging_dir.exists():
        for txn_dir in sorted(p for p in staging_dir.iterdir() if p.is_dir()):
            txn_id = txn_dir.name
            txn_bytes, categories, txn_oldest_mtime = _walk_bytes_by_category(txn_dir)
            if txn_bytes == 0:
                continue

            phase = phase_by_txn.get(txn_id)
            total_bytes += txn_bytes
            transactions_report.append(
                {
                    "txn_id": txn_id,
                    "phase": phase,
                    "bytes": txn_bytes,
                    "categories": categories,
                    "stale": phase == Phase.SUCCESS.value,
                    "unknown_owner": phase is None,
                }
            )
            if txn_oldest_mtime is not None and (
                oldest_mtime is None or txn_oldest_mtime < oldest_mtime
            ):
                oldest_mtime = txn_oldest_mtime
                oldest_txn = txn_id

    blob_cache_bytes = 0
    if blob_cache_dir is not None:
        blob_cache_bytes, _cats, _oldest = _walk_bytes_by_category(Path(blob_cache_dir))

    return {
        "total_staging_bytes": total_bytes,
        "blob_cache_bytes": blob_cache_bytes,
        "transactions": transactions_report,
        "oldest_txn_id": oldest_txn,
        "oldest_age_seconds": (
            time.time() - oldest_mtime if oldest_mtime is not None else None
        ),
    }

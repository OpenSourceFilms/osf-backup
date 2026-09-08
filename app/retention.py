"""GFS snapshot retention and blob garbage-collection helpers.

The core retention function, :func:`select_retained`, is intentionally pure:
it only works with datetime values and returns a deterministic result.

The reference-counting helpers are in-memory helpers.  They deliberately do
not delete blobs; they only update reference counts and mark zero-reference
blobs with ``unreferenced_since`` so a later GC step can collect them after
the configured grace period.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable, Mapping, MutableMapping
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

logger = logging.getLogger(__name__)

_UTC = ZoneInfo("UTC")


@dataclass(frozen=True)
class SnapshotManifest:
    """In-memory representation of a snapshot manifest.

    ``blob_ids`` identifies the blobs this manifest references.  A real
    implementation would normally store this in a database table; here the
    data structure is intentionally small because the prompt does not provide
    a DB schema.
    """

    id: str
    blob_ids: frozenset[str] = field(default_factory=frozenset)

    def __post_init__(self) -> None:
        if isinstance(self.blob_ids, str):
            object.__setattr__(self, "blob_ids", frozenset({self.blob_ids}))
        else:
            object.__setattr__(self, "blob_ids", frozenset(self.blob_ids))


@dataclass
class Blob:
    """In-memory representation of a stored blob.

    ``refcount`` is the number of live manifests referencing this blob.
    ``unreferenced_since`` is stamped when ``refcount`` reaches zero.
    """

    id: str
    refcount: int = 0
    unreferenced_since: datetime | None = None


def _coerce_aware(value: datetime | date | str, tz: ZoneInfo) -> datetime | None:
    """Return an aware datetime in ``tz``, or ``None`` for invalid input.

    Naive dattetimes are interpreted as wall-clock times in ``tz``.  Strings
    are parsed with :func:`datetime.fromisoformat`.
    """

    try:
        if isinstance(value, str):
            value = datetime.fromisoformat(value)
        elif isinstance(value, date) and not isinstance(value, datetime):
            value = datetime(value.year, value.month, value.day, tzinfo=tz)

        if not isinstance(value, datetime):
            logger.warning(
                "Skipping unsupported snapshot date value %r", value
            )
            return None

        if value.tzinfo is None:
            return value.replace(tzinfo=tz)
        return value.astimezone(tz)
    except (ValueError, TypeError, OverflowError) as exc:
        logger.warning("Skipping invalid snapshot date %r: %s", value, exc)
        return None


def _reasons_for(existing: str | None, reason: str) -> str:
    if existing:
        return f"{existing},{reason}"
    return reason


def select_retained(
    snapshot_dates: Iterable[datetime | date | str],
    daily: int = 14,
    weekly: int = 8,
    monthly: int = 6,
    tzname: str = "UTC",
) -> tuple[dict[datetime, str], list[datetime]]:
    """Apply GFS retention to snapshot dates.

    Parameters
    ----------
    snapshot_dates:
        Candidate snapshot timestamps.  Naive dattetimes and date-only strings
        are interpreted in ``tzname``.
    daily:
        Number of latest daily snapshots to keep.  A daily snapshot is the
        latest snapshot on a local date.
    weekly:
        Number of latest weekly snapshots to keep.  A weekly snapshot is the
        latest snapshot in each ISO week.
    monthly:
        Number of latest monthly snapshots to keep.  A monthly snapshot is
        the latest snapshot in each calendar month.
    tzname:
        ZoneInfo timezone name used for all date grouping.

    Returns
    -------
    tuple:
        The first element is ``kept_set``, a dict mapping each retained
        snapshot datetime to the reason(s) it was kept.  The second element
        is ``expired_list``, a list of snapshot datetimes not retained,
        ordered oldest first.

    Raises
    ------
    Nothing; invalid entries are logged and skipped.
    """

    try:
        tz = ZoneInfo(tzname)
    except KeyError:
        logger.warning("Unknown timezone %r; falling back to UTC", tzname)
        tz = ZoneInfo("UTC")

    snapshots: list[datetime] = []
    for raw in snapshot_dates:
        coerced = _coerce_aware(raw, tz)
        if coerced is not None:
            snapshots.append(coerced)

    if not snapshots:
        return {}, []

    snapshots.sort(reverse=True)

    kept_reasons: dict[datetime, str] = {}

    # Daily: one per local date, keep the latest N dates.
    if daily > 0:
        by_date: dict[date, datetime] = {}
        for snap in snapshots:
            snap_date = snap.date()
            if snap_date not in by_date:
                by_date[snap_date] = snap

        for snap_date in sorted(by_date.keys(), reverse=True)[:daily]:
            snap = by_date[snap_date]
            kept_reasons[snap] = _reasons_for(
                kept_reasons.get(snap), "daily"
            )

    # Weekly: one per ISO week, keep the latest N weekly snapshots.
    if weekly > 0:
        by_week: dict[tuple[int, int], datetime] = {}
        for snap in snapshots:
            iso = snap.isocalendar()
            key = (iso.year, iso.week)
            if key not in by_week:
                by_week[key] = snap

        weekly_reps = sorted(by_week.values(), reverse=True)[:weekly]
        for snap in weekly_reps:
            kept_reasons[snap] = _reasons_for(
                kept_reasons.get(snap), "weekly"
            )

    # Monthly: one per calendar month, keep the latest N monthly snapshots.
    if monthly > 0:
        by_month: dict[tuple[int, int], datetime] = {}
        for snap in snapshots:
            key = (snap.year, snap.month)
            if key not in by_month:
                by_month[key] = snap

        monthly_reps = sorted(by_month.values(), reverse=True)[:monthly]
        for snap in monthly_reps:
            kept_reasons[snap] = _reasons_for(
                kept_reasons.get(snap), "monthly"
            )

    expired_list = [snap for snap in snapshots if snap not in kept_reasons]
    expired_list.sort()

    return kept_reasons, expired_list


def _ensure_aware_utc(dt: datetime) -> datetime:
    """Return ``dt`` as an aware UTC datetime.

    Naive datetimes are assumed to be UTC and a WARN is emitted.  This is
    used only for reference-count/gc timestamps where no timezone name is
    supplied.
    """

    if dt.tzinfo is None:
        logger.warning(
            "Naive datetime %s provided; assuming UTC", dt.isoformat()
        )
        return dt.replace(tzinfo=_UTC)
    return dt.astimezone(_UTC)


def _blob_attr(blob: object, name: str, default: object = None) -> object:
    """Read a blob attribute from either a dataclass/object or a dict."""

    if isinstance(blob, dict):
        return blob.get(name, default)
    return getattr(blob, name, default)


def _set_blob_attr(blob: object, name: str, value: object) -> None:
    """Set a blob attribute on either a dataclass/object or a dict."""

    if isinstance(blob, dict):
        blob[name] = value
    else:
        setattr(blob, name, value)


def _blob_id(blob: object) -> object:
    # statedb.py's blobs table primary key is "hash", not "id".
    return _blob_attr(blob, "hash", _blob_attr(blob, "id"))


def expire_manifest(
    manifest: SnapshotManifest,
    blobs: MutableMapping[str, object],
    now: datetime | None = None,
) -> None:
    """Expire ``manifest`` by decrementing refcounts of the blobs it references.

    This is deliberately not a blob deletion function.  When a blob reaches
    zero references, it is stamped with ``unreferenced_since`` and remains in
    the blob store until :func:`gc_candidates` says its grace period has fully
    elapsed.

    Naive ``now`` values are interpreted as UTC; aware values are converted to
    UTC before stamping.
    """

    if now is None:
        now = datetime.now(_UTC)
    now_aware = _ensure_aware_utc(now)

    for blob_id in manifest.blob_ids:
        blob = blobs.get(blob_id) if isinstance(blobs, Mapping) else None

        if blob is None and not isinstance(blobs, Mapping):
            for candidate in blobs:
                if _blob_id(candidate) == blob_id:
                    blob = candidate
                    break

        if blob is None:
            logger.warning(
                "Manifest %s references unknown blob %s; skipping",
                manifest.id,
                blob_id,
            )
            continue

        refcount = _blob_attr(blob, "refcount", 0)
        if not isinstance(refcount, int) or refcount <= 0:
            logger.warning(
                "Blob %s has invalid refcount %r; skipping decrement",
                blob_id,
                refcount,
            )
            continue

        refcount -= 1
        _set_blob_attr(blob, "refcount", refcount)

        if refcount == 0:
            _set_blob_attr(blob, "unreferenced_since", now_aware)
            logger.debug(
                "Blob %s reached zero references at %s",
                blob_id,
                now_aware.isoformat(),
            )


def gc_candidates(
    blobs: Iterable[object] | Mapping[str, object],
    now: datetime,
    grace_days: int = 30,
) -> list[object]:
    """Return blobs whose zero-reference grace period has fully elapsed.

    A blob is eligible only if its ``refcount`` is zero, it has an
    ``unreferenced_since`` timestamp, and ``now >= unreferenced_since +
    grace_days``.  This function does not delete anything; it only returns
    candidates.
    """

    now_aware = _ensure_aware_utc(now)

    if isinstance(blobs, Mapping):
        blob_iter = iter(blobs.values())
    else:
        blob_iter = iter(blobs)

    candidates: list[object] = []

    for blob in blob_iter:
        # statedb.py's blobs table column is "reference_count", not "refcount".
        refcount = _blob_attr(blob, "reference_count", _blob_attr(blob, "refcount", 0))
        if refcount != 0:
            continue

        unreferenced_since = _blob_attr(blob, "unreferenced_since", None)
        if unreferenced_since is None:
            logger.warning(
                "Blob %s has zero refcount but no unreferenced_since; "
                "skipping GC",
                _blob_id(blob),
            )
            continue

        # SQLite hands timestamps back as ISO strings, not datetime objects.
        if isinstance(unreferenced_since, str):
            try:
                unreferenced_since = datetime.fromisoformat(unreferenced_since)
            except ValueError:
                logger.warning(
                    "Blob %s has invalid unreferenced_since value %r; skipping GC",
                    _blob_id(blob),
                    unreferenced_since,
                )
                continue
        elif not isinstance(unreferenced_since, datetime):
            logger.warning(
                "Blob %s has invalid unreferenced_since value %r; skipping GC",
                _blob_id(blob),
                unreferenced_since,
            )
            continue

        unreferenced_aware = _ensure_aware_utc(unreferenced_since)
        eligible_at = unreferenced_aware + timedelta(days=grace_days)

        if now_aware >= eligible_at:
            candidates.append(blob)

    return candidates


def delete_snapshot_source_file(
    manifest: SnapshotManifest,
    blobs: MutableMapping[str, object],
    now: datetime | None = None,
) -> None:
    """Simulate deleting only the snapshot source file.

    This wrapper calls :func:`expire_manifest`.  It never deletes a blob; it
    only adjusts refcounts and stamps zero-reference blobs with
    ``unreferenced_since``.
    """

    expire_manifest(manifest, blobs, now)


class RetentionRunner:
    """Wires select_retained to the real StateDB.

    The pure functions above stay pure and independently testable; this is
    the one place GFS retention actually touches persisted state. Idempotent
    by construction: expiring a run_date deletes its snapshot_entries
    (StateDB.delete_snapshot_entries), so a run_date with nothing left to
    expire is a safe no-op on a later pass rather than double-decrementing.
    """

    def __init__(
        self,
        state_db: object,
        *,
        daily: int = 14,
        weekly: int = 8,
        monthly: int = 6,
        tzname: str = "Australia/Melbourne",
    ) -> None:
        self._db = state_db
        self._daily = daily
        self._weekly = weekly
        self._monthly = monthly
        self._tzname = tzname

    def run(self, txn_id: str) -> None:
        run_dates = self._db.list_snapshots()
        if not run_dates:
            return

        kept, _expired = select_retained(
            run_dates,
            daily=self._daily,
            weekly=self._weekly,
            monthly=self._monthly,
            tzname=self._tzname,
        )
        kept_dates = {dt.date().isoformat() for dt in kept}

        for run_date in run_dates:
            if run_date in kept_dates:
                continue
            entries = self._db.snapshot_entry_hashes(run_date)
            if not entries:
                continue  # already expired on a prior run
            for entry in entries:
                self._db.expire_reference(entry["hash"])
            self._db.delete_snapshot_entries(run_date)


class GCRunner:
    """Deletes remote blobs whose grace period has fully elapsed.

    Remote deletion happens before the local StateDB row is dropped -- if
    the remote delete fails, the blob stays a GC candidate and is retried
    on the next run rather than the local row silently disappearing while
    the remote object still exists.
    """

    def __init__(
        self, state_db: object, blob_store: object, *, grace_days: int = 30
    ) -> None:
        self._db = state_db
        self._blob_store = blob_store
        self._grace_days = grace_days

    def plan(self) -> list[dict]:
        """Return GC candidates without deleting anything.

        Dry-run/plan mode: makes no remote or local mutations, safe to call
        at any time. run() below is exactly this candidate list followed by
        the real remote+local deletion -- callers that only want to know
        what WOULD be deleted should call this instead of run().
        """
        now_iso = datetime.now(_UTC).isoformat()
        return self._db.unreferenced_blobs(now_iso, self._grace_days)

    def run(self) -> None:
        candidates = self.plan()
        for blob in candidates:
            try:
                self._blob_store.delete_object(blob["remote_key"])
            except Exception:
                logger.exception(
                    "Failed to delete blob %s (key=%s) during GC; will "
                    "retry on the next run",
                    blob["hash"],
                    blob["remote_key"],
                )
                continue
            self._db.delete_blob(blob["hash"])


__all__ = [
    "Blob",
    "GCRunner",
    "RetentionRunner",
    "SnapshotManifest",
    "delete_snapshot_source_file",
    "expire_manifest",
    "gc_candidates",
    "select_retained",
]

# Notes:
# - ``select_retained`` returns ``kept_set`` as a dict mapping an aware
#   datetime snapshot to its retention reason string, not a literal set.
#   The name is kept for API clarity with the requirement.
# - Daily retention is interpreted as one snapshot per local calendar day,
#   keeping the latest N days.  This matches the GFS pattern used by weekly
#   and monthly retention.
# - Naive datetimes in ``select_retained`` are interpreted as wall-clock time
#   in the configured timezone.  Naive timestamps in refcount/gc functions
#   are assumed UTC and emit a WARN.
# - No DB schema was provided, so reference counting uses in-memory
#   ``SnapshotManifest`` and ``Blob`` dataclasses (or duck-typed objects).
# - ``expire_manifest`` and ``delete_snapshot_source_file`` never delete
#   blobs; they only decrement refcounts and stamp ``unreferenced_since``.
# - ``gc_candidates`` only returns candidates.  Actual deletion is left to a
#   separate GC process.

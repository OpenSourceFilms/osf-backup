"""Fast, non-live regression test for backupctl's `run` guard, added after
the 2026-08-19/20 seed's self-caught operator mistake: a plain `backupctl
run` after a date rollover silently started a brand new transaction and
rescan instead of resuming the still-FAILED backup-2026-08-19 (see
HANDOFF.md checkpoint -3i). `_older_failed_transaction_date` is the pure
decision function `_cmd_run` consults before proceeding; this test drives
it directly against a real sqlite3 connection using the real transactions
schema, rather than exercising the full CLI (which would need a complete
Config/policy/S3 setup disproportionate to this small guard).
"""

import sqlite3
import sys
from importlib.machinery import SourceFileLoader
from importlib.util import module_from_spec, spec_from_loader
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

BACKUPCTL_PATH = Path(__file__).resolve().parent.parent / "bin" / "backupctl"


def _load_backupctl():
    loader = SourceFileLoader("backupctl_under_test", str(BACKUPCTL_PATH))
    spec = spec_from_loader(loader.name, loader)
    module = module_from_spec(spec)
    loader.exec_module(module)
    return module


def _make_transactions_db(tmp_path):
    conn = sqlite3.connect(tmp_path / "state.sqlite")
    conn.row_factory = sqlite3.Row
    conn.execute(
        """
        CREATE TABLE transactions (
            txn_id TEXT PRIMARY KEY,
            txn_date TEXT NOT NULL,
            phase TEXT NOT NULL
        )
        """
    )
    return conn


def _insert(conn, txn_date, phase):
    conn.execute(
        "INSERT INTO transactions (txn_id, txn_date, phase) VALUES (?, ?, ?)",
        (f"backup-{txn_date}", txn_date, phase),
    )
    conn.commit()


def test_finds_an_older_failed_date(tmp_path):
    backupctl = _load_backupctl()
    conn = _make_transactions_db(tmp_path)
    _insert(conn, "2026-08-19", "FAILED")

    assert backupctl._older_failed_transaction_date(conn, "2026-08-20") == "2026-08-19"


def test_ignores_todays_own_failed_transaction(tmp_path):
    backupctl = _load_backupctl()
    conn = _make_transactions_db(tmp_path)
    _insert(conn, "2026-08-20", "FAILED")

    assert backupctl._older_failed_transaction_date(conn, "2026-08-20") is None


def test_ignores_older_transactions_that_are_not_failed(tmp_path):
    backupctl = _load_backupctl()
    conn = _make_transactions_db(tmp_path)
    _insert(conn, "2026-08-19", "SUCCESS")

    assert backupctl._older_failed_transaction_date(conn, "2026-08-20") is None


def test_no_transactions_at_all(tmp_path):
    backupctl = _load_backupctl()
    conn = _make_transactions_db(tmp_path)

    assert backupctl._older_failed_transaction_date(conn, "2026-08-20") is None


# ---------------------------------------------------------------------------
# Supersession semantics (added 2026-08-25, post-seed gate Phase 5).
#
# The guard above originally matched ANY FAILED row on another date. Once
# backup-2026-08-19 became a permanent historical FAILED record -- Daniel's
# explicit ruling: never retried, never reset, never fabricated to SUCCESS --
# that made the guard block `backupctl run` for every future date forever,
# which would have turned unattended nightly scheduling into a permanent
# no-op the first night it was switched on. An unresolved failure must still
# block; a failure already superseded by a later SUCCESS must not.
# ---------------------------------------------------------------------------


def test_unresolved_failure_with_no_later_success_still_blocks(tmp_path):
    """Aug19-style failed transaction + no later success -> blocks."""
    backupctl = _load_backupctl()
    conn = _make_transactions_db(tmp_path)
    _insert(conn, "2026-08-19", "FAILED")

    assert backupctl._older_failed_transaction_date(conn, "2026-08-25") == "2026-08-19"


def test_failure_superseded_by_later_success_does_not_block(tmp_path):
    """Aug19-style failed transaction + later successful fresh snapshot
    -> does not block a new date."""
    backupctl = _load_backupctl()
    conn = _make_transactions_db(tmp_path)
    _insert(conn, "2026-08-19", "FAILED")
    _insert(conn, "2026-08-23", "SUCCESS")

    assert backupctl._older_failed_transaction_date(conn, "2026-08-25") is None


def test_failure_newer_than_latest_success_still_blocks(tmp_path):
    """A success does not excuse a failure that came *after* it."""
    backupctl = _load_backupctl()
    conn = _make_transactions_db(tmp_path)
    _insert(conn, "2026-08-19", "SUCCESS")
    _insert(conn, "2026-08-23", "FAILED")

    assert backupctl._older_failed_transaction_date(conn, "2026-08-25") == "2026-08-23"


def test_abandoned_scanning_older_than_success_does_not_block(tmp_path):
    """Abandoned SCANNING transaction older than a later SUCCESS -> not
    resumed and not blocking. backup-2026-08-20 is exactly this shape."""
    backupctl = _load_backupctl()
    conn = _make_transactions_db(tmp_path)
    _insert(conn, "2026-08-20", "SCANNING")
    _insert(conn, "2026-08-23", "SUCCESS")

    assert backupctl._older_failed_transaction_date(conn, "2026-08-25") is None


def test_real_production_shape_does_not_block_a_new_date(tmp_path):
    """The exact live state as of 2026-08-25: Aug19 FAILED, Aug20 abandoned
    SCANNING, Aug23 SUCCESS. A new nightly date must be free to proceed."""
    backupctl = _load_backupctl()
    conn = _make_transactions_db(tmp_path)
    _insert(conn, "2026-08-19", "FAILED")
    _insert(conn, "2026-08-20", "SCANNING")
    _insert(conn, "2026-08-23", "SUCCESS")

    assert backupctl._older_failed_transaction_date(conn, "2026-08-24") is None
    assert backupctl._older_failed_transaction_date(conn, "2026-08-25") is None

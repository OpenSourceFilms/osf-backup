"""Regression tests for the `backupctl status` split-brain bug.

On 2026-08-29, a live check showed `backupctl status` reporting
``NEVER_STARTED`` / ``last successful snapshot: never`` even though two real
SUCCESS transactions existed on 2026-08-27 and 2026-08-28.  The status command
was reading app.statedb.StateDB's legacy ``runs`` table, while the runner was
writing to the real ``transactions`` table used by app.runner.Runner -- the same
class of split-brain bug app/backupd.py's module docstring already documented
having fixed on the daemon side.

This file deliberately mirrors tests/test_backupctl_run_guard.py: it drives the
pure functions from ``bin/backupctl`` directly against a real sqlite3 database
instead of constructing a full CLI run with Config/policy/S3 fixtures, because
the bug being covered is small and isolated to transaction lookup.
"""

import importlib.machinery
import importlib.util
import sqlite3
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo


_BIN_DIR = Path(__file__).resolve().parents[1] / "bin"
_BACKUPCTL_PATH = _BIN_DIR / "backupctl"
_LOADER = importlib.machinery.SourceFileLoader("backupctl", str(_BACKUPCTL_PATH))
_SPEC = importlib.util.spec_from_loader(_LOADER.name, _LOADER)
backupctl = importlib.util.module_from_spec(_SPEC)
_LOADER.exec_module(backupctl)


def _make_transactions_db(tmp_path: Path) -> Path:
    """Create a real sqlite3 state database with only the transactions table."""
    db_path = tmp_path / "state.sqlite"
    conn = sqlite3.connect(db_path)
    conn.execute(
        """
        CREATE TABLE transactions (
            txn_id TEXT PRIMARY KEY,
            txn_date TEXT NOT NULL,
            phase TEXT NOT NULL,
            error TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
        """
    )
    conn.commit()
    conn.close()
    return db_path


def _insert(
    db_path: Path,
    txn_date: str,
    phase: str,
    error: str | None = None,
    created_at: str | None = None,
    updated_at: str | None = None,
) -> None:
    """Insert one row with a deterministic txn_id and sensible timestamp defaults."""
    if created_at is None:
        created_at = f"{txn_date}T00:00:00+10:00"
    if updated_at is None:
        updated_at = f"{txn_date}T00:00:00+10:00"

    txn_id = f"backup-{txn_date}"
    conn = sqlite3.connect(db_path)
    conn.execute(
        """
        INSERT INTO transactions
            (txn_id, txn_date, phase, error, created_at, updated_at)
        VALUES
            (?, ?, ?, ?, ?, ?)
        """,
        (txn_id, txn_date, phase, error, created_at, updated_at),
    )
    conn.commit()
    conn.close()


def _last_success_run_date_from(transactions: list[dict]) -> str | None:
    """Return the txn_date of the most recent SUCCESS transaction, if any.

    ``transactions`` is already ordered most-recent-first by
    ``_read_transactions``.
    """
    for row in transactions:
        if row["phase"] == "SUCCESS":
            return row["txn_date"]
    return None


def test_read_transactions_missing_state_db_returns_empty_list(tmp_path):
    db_path = tmp_path / "state.sqlite"
    assert backupctl._read_transactions(db_path) == []


def test_read_transactions_empty_table_and_current_transaction_none(tmp_path):
    db_path = _make_transactions_db(tmp_path)
    transactions = backupctl._read_transactions(db_path)

    assert transactions == []
    assert backupctl._current_transaction(transactions) is None


def test_latest_success_after_failed_has_no_current_transaction(tmp_path):
    db_path = _make_transactions_db(tmp_path)
    _insert(db_path, "2026-08-19", "FAILED")
    _insert(db_path, "2026-08-23", "SUCCESS")

    transactions = backupctl._read_transactions(db_path)

    assert transactions[0]["txn_date"] == "2026-08-23"
    assert transactions[0]["phase"] == "SUCCESS"
    assert backupctl._current_transaction(transactions) is None
    assert _last_success_run_date_from(transactions) == "2026-08-23"


def test_current_transaction_returned_and_previous_success_still_visible(tmp_path):
    db_path = _make_transactions_db(tmp_path)
    _insert(db_path, "2026-08-28", "SUCCESS")
    _insert(db_path, "2026-08-29", "SCANNING")

    transactions = backupctl._read_transactions(db_path)
    current = backupctl._current_transaction(transactions)

    assert current is not None
    assert current["txn_date"] == "2026-08-29"
    assert current["phase"] == "SCANNING"
    assert _last_success_run_date_from(transactions) == "2026-08-28"


def test_interrupted_scanning_transaction_is_not_fabricated_to_terminal(tmp_path):
    db_path = _make_transactions_db(tmp_path)
    _insert(db_path, "2026-08-29", "SCANNING", error=None)

    transactions = backupctl._read_transactions(db_path)
    current = backupctl._current_transaction(transactions)

    assert current is not None
    assert current["txn_date"] == "2026-08-29"
    assert current["phase"] == "SCANNING"
    assert current["error"] is None
    assert _last_success_run_date_from(transactions) is None


def test_last_success_run_date_reads_the_configured_state_db_path(tmp_path, monkeypatch):
    """Exercises the real no-arg _last_success_run_date(), not just the
    _last_success_run_date_from() replica above -- confirms the production
    function itself reads the module's STATE_DB_PATH and correctly skips
    an older FAILED row in favor of the later SUCCESS."""
    db_path = _make_transactions_db(tmp_path)
    _insert(db_path, "2026-08-19", "FAILED")
    _insert(db_path, "2026-08-23", "SUCCESS")
    monkeypatch.setattr(backupctl, "STATE_DB_PATH", db_path)

    assert backupctl._last_success_run_date() == "2026-08-23"


def test_abandoned_past_day_transaction_is_not_reported_as_in_progress(tmp_path):
    """A past day's non-terminal transaction is dead, not running.

    Nothing advances it: Runner only moves a transaction from inside a
    live process, and backupd only ever considers today's date. Reporting
    it as "Current transaction in progress" (the behaviour until
    2026-09-07) let backup-2026-08-29 read as a healthy running backup for
    ten days while no backup had run at all.
    """
    db_path = _make_transactions_db(tmp_path)
    _insert(db_path, "2026-08-28", "SUCCESS")
    _insert(db_path, "2026-08-29", "SCANNING")

    transactions = backupctl._read_transactions(db_path)
    abandoned = backupctl._abandoned_transactions(transactions, "2026-09-07")

    assert [t["txn_id"] for t in abandoned] == ["backup-2026-08-29"]


def test_todays_in_progress_transaction_is_never_called_abandoned(tmp_path):
    """The live case must stay live -- an abandonment check that flags a
    transaction currently being worked on would train the operator to
    ignore the warning."""
    db_path = _make_transactions_db(tmp_path)
    _insert(db_path, "2026-09-07", "UPLOADING")

    transactions = backupctl._read_transactions(db_path)

    assert backupctl._abandoned_transactions(transactions, "2026-09-07") == []
    assert backupctl._current_transaction(transactions)["phase"] == "UPLOADING"


def test_terminal_transactions_are_never_abandoned(tmp_path):
    """SUCCESS and FAILED are finished states. backup-2026-08-19 is
    deliberately preserved FAILED historical evidence and must never start
    generating operator noise."""
    db_path = _make_transactions_db(tmp_path)
    _insert(db_path, "2026-08-19", "FAILED")
    _insert(db_path, "2026-08-23", "SUCCESS")

    transactions = backupctl._read_transactions(db_path)

    assert backupctl._abandoned_transactions(transactions, "2026-09-07") == []


def test_age_phrase_survives_a_malformed_timestamp(tmp_path):
    """Status output must never crash on bad data -- a status command that
    dies is worse than one reporting an unknown age."""
    assert backupctl._age_phrase(None) == "age unknown"
    assert backupctl._age_phrase("not-a-timestamp") == "age unknown"
    assert "ago" in backupctl._age_phrase("2026-08-29T00:00:00+10:00")


def test_next_melbourne_run_returns_future_aware_0400(tmp_path):
    melbourne = ZoneInfo("Australia/Melbourne")
    before = datetime.now(melbourne)

    result = backupctl._next_melbourne_run({"schedule": {"time": "04:00"}})

    assert result.tzinfo is not None
    assert result.hour == 4
    assert result.minute == 0
    assert result > before

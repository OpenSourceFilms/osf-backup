"""ABANDONED: an honest terminal state for a transaction nothing will finish.

A transaction whose process is killed mid-phase (SIGKILL, pod restart)
stops where it stands. Nothing will ever advance it -- Runner only moves a
transaction from inside a live process, and backupd only ever considers
today's date -- so it reads as "in progress" forever while being nothing of
the kind (backup-2026-08-29 did exactly that for ten days, and
backup-2026-08-20 for eighteen).

FAILED is the wrong word for it: the transaction did not fail, and marking
it FAILED would arm the resume/reset machinery and, for a date newer than
the last SUCCESS, block `backupctl run` indefinitely. ABANDONED records what
happened, keeps every scan_items row as evidence, and closes the row.
"""

import sqlite3
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import runner as app_runner


def _make_runner(tmp_path):
    conn = sqlite3.connect(tmp_path / "state.sqlite")
    conn.row_factory = sqlite3.Row
    r = object.__new__(app_runner.Runner)
    r.state_db = conn
    r._init_schema()
    r.config = SimpleNamespace(
        healthcheck=SimpleNamespace(
            start=lambda: None, success=lambda s: None, fail=lambda e: None
        ),
        policy=SimpleNamespace(policy_hash="h"),
        should_stop=None,
    )
    return r


def _interrupted(tmp_path, date="2026-08-29", phase=app_runner.Phase.SCANNING):
    r = _make_runner(tmp_path)
    r._upsert_transaction(f"backup-{date}", date)
    r._set_phase(f"backup-{date}", phase)
    r.state_db.execute(
        "INSERT INTO scan_items (txn_id, path, size, mtime_ns, classification) "
        "VALUES (?, ?, ?, ?, ?)",
        (f"backup-{date}", "/a", 10, 0, "CRITICAL"),
    )
    r.state_db.commit()
    return r


def test_abandon_closes_the_row_and_keeps_the_evidence(tmp_path):
    r = _interrupted(tmp_path)
    r.abandon_transaction("2026-08-29", "SIGKILLed mid-SCANNING, end of canary period")

    tx = r._get_transaction("backup-2026-08-29")
    assert tx["phase"] == "ABANDONED"
    assert tx["resume_phase"] is None
    assert "SIGKILL" in tx["error"]
    kept = r.state_db.execute(
        "SELECT COUNT(*) FROM scan_items WHERE txn_id = ?", ("backup-2026-08-29",)
    ).fetchone()[0]
    assert kept == 1, "abandoning must not delete the transaction's evidence"


def test_abandon_requires_a_reason(tmp_path):
    """An unexplained terminal state is the thing this exists to remove."""
    r = _interrupted(tmp_path)
    for bad in ("", "   "):
        with pytest.raises(ValueError):
            r.abandon_transaction("2026-08-29", bad)


def test_abandon_refuses_to_rewrite_a_terminal_transaction(tmp_path):
    """SUCCESS must never be rewritten, and a FAILED row is historical
    evidence in its own right -- backup-2026-08-19 is exactly that."""
    r = _make_runner(tmp_path)
    cases = (
        ("2026-08-28", app_runner.Phase.SUCCESS),
        ("2026-08-19", app_runner.Phase.FAILED),
    )
    for date, phase in cases:
        r._upsert_transaction(f"backup-{date}", date)
        r._set_phase(f"backup-{date}", phase)
        with pytest.raises(ValueError, match="already terminal"):
            r.abandon_transaction(date, "tidying up")
        assert r._get_transaction(f"backup-{date}")["phase"] == phase.value


def test_abandon_rejects_an_unknown_date(tmp_path):
    r = _make_runner(tmp_path)
    with pytest.raises(ValueError, match="no such transaction"):
        r.abandon_transaction("2026-01-01", "never existed")


def test_run_for_date_refuses_to_resume_an_abandoned_transaction(tmp_path):
    """Silently no-op'ing would let `retry <date>` look like it worked. An
    operator aiming at an abandoned transaction holds a wrong belief worth
    correcting out loud."""
    r = _interrupted(tmp_path)
    r.abandon_transaction("2026-08-29", "end of canary period")

    with pytest.raises(RuntimeError, match="ABANDONED"):
        r.run_for_date("2026-08-29")

    assert r._get_transaction("backup-2026-08-29")["phase"] == "ABANDONED"

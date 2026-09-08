"""Regression tests for backupctl._explain_sqlite_lock, which turns a
SQLite busy/lock error into an explicit operator diagnostic -- without it a
contended state DB looks like a hung backup (leftover read-only diagnostic
scripts were nearly mistaken for a stalled run before the Aug-23 seed).
"""

import sqlite3
import sys
from importlib.machinery import SourceFileLoader
from importlib.util import module_from_spec, spec_from_loader
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

BACKUPCTL_PATH = Path(__file__).resolve().parent.parent / "bin" / "backupctl"


def _load_backupctl():
    loader = SourceFileLoader("backupctl_under_test_sqlite_lock", str(BACKUPCTL_PATH))
    spec = spec_from_loader(loader.name, loader)
    module = module_from_spec(spec)
    loader.exec_module(module)
    return module


def test_real_locked_database_is_recognised_and_bounded(tmp_path):
    """Exercise a real SQLite lock, not a synthetic message string: hold an
    exclusive write transaction on one connection and provoke a genuine
    'database is locked' error on a second connection with a short
    timeout, so contention fails fast with a clear diagnostic instead of
    looking indefinitely hung."""
    backupctl = _load_backupctl()
    db_path = tmp_path / "state.sqlite"

    holder = sqlite3.connect(str(db_path))
    holder.execute("CREATE TABLE t (x INTEGER)")
    holder.execute("BEGIN IMMEDIATE")
    holder.execute("INSERT INTO t VALUES (1)")

    contender = sqlite3.connect(str(db_path), timeout=0.2)
    try:
        contender.execute("BEGIN IMMEDIATE")
        raise AssertionError("expected a real lock contention error")
    except sqlite3.OperationalError as exc:
        assert backupctl._explain_sqlite_lock(exc) is True
    finally:
        contender.close()
        holder.rollback()
        holder.close()


def test_non_lock_operational_error_is_not_explained():
    backupctl = _load_backupctl()
    exc = sqlite3.OperationalError("no such table: transactions")
    assert backupctl._explain_sqlite_lock(exc) is False


def test_non_sqlite_exception_is_not_explained():
    backupctl = _load_backupctl()
    assert backupctl._explain_sqlite_lock(RuntimeError("lock")) is False

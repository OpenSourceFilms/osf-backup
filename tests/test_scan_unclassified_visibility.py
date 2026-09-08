"""The real `run` path must say what it chose not to protect.

Before 2026-09-07 only `backupctl dry-run` produced UNCLASSIFIED
accounting; Runner._scan_source dropped every non-selected file with a
bare `continue`. That is how table_read/Media, Media_H264 and braw_decode
(25 files, ~23 GiB, one of them the sole local copy of a recovery-pending
recording) sat outside the protected set through an entire seed without a
single log line saying so -- the operator could only find it by running a
separate diagnostic they had no reason to suspect they needed.

Exercises the real _scan_source against a fake scanner/policy pair, not
_log_skipped_summary in isolation: the property under test is that the
production scan path reports, not that a helper can format a string.
"""

import sqlite3
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import runner as app_runner
from app.policy import Classification, Decision
from app.scanner import Candidate, CandidateStatus


def _candidate(root: Path, rel: str) -> Candidate:
    return Candidate(
        absolute_path=str(root / rel),
        relative_path=rel,
        size=1,
        mtime_ns=0,
        mode=0o644,
        is_symlink=False,
        symlink_target=None,
        status=CandidateStatus.OK,
    )


def _make_runner(tmp_path, entries, classify):
    conn = sqlite3.connect(tmp_path / "state.sqlite")
    conn.row_factory = sqlite3.Row
    r = object.__new__(app_runner.Runner)
    r.state_db = conn
    r._init_schema()
    r.config = SimpleNamespace(
        source_root=tmp_path,
        scanner=SimpleNamespace(scan=lambda root: iter(entries)),
        policy=SimpleNamespace(classify=classify, policy_hash="h"),
        should_stop=None,
    )
    return r


def _decide(rel):
    if rel.startswith("mystery"):
        cls, selected = Classification.UNCLASSIFIED, False
    elif rel.startswith("cache"):
        cls, selected = Classification.REDOWNLOADABLE, False
    else:
        cls, selected = Classification.CRITICAL, True
    return Decision(classification=cls, responsible_rule="test", selected=selected)


def test_scan_warns_about_unclassified_and_counts_every_skip(tmp_path, caplog):
    rels = ["keep.txt", "cache/a.bin", "cache/b.bin", "mystery/one.mov"]
    entries = [_candidate(tmp_path, rel) for rel in rels]
    r = _make_runner(
        tmp_path, entries, lambda rel, **_kw: _decide(str(rel))
    )

    with caplog.at_level("INFO", logger="app.runner"):
        r._scan_source("backup-2026-09-07", set())

    messages = [rec.getMessage() for rec in caplog.records]
    assert any("REDOWNLOADABLE=2" in m and "UNCLASSIFIED=1" in m for m in messages), messages
    warnings = [rec.getMessage() for rec in caplog.records if rec.levelname == "WARNING"]
    assert any("UNCLASSIFIED" in m and "mystery/one.mov" in m for m in warnings), warnings

    kept = r.state_db.execute(
        "SELECT path FROM scan_items WHERE txn_id = ?", ("backup-2026-09-07",)
    ).fetchall()
    assert [row["path"] for row in kept] == [str(tmp_path / "keep.txt")]


def test_scan_says_so_when_nothing_was_skipped(tmp_path, caplog):
    """Silence must never be the signal for "nothing skipped" -- absence of
    a warning has to mean the scan checked, not that it forgot to look."""
    entries = [_candidate(tmp_path, "keep.txt")]
    r = _make_runner(tmp_path, entries, lambda rel, **_kw: _decide(str(rel)))

    with caplog.at_level("INFO", logger="app.runner"):
        r._scan_source("backup-2026-09-07", set())

    assert any(
        "no files skipped by classification" in rec.getMessage()
        for rec in caplog.records
    )

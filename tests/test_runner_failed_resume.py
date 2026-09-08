"""Fast, non-live regression test for the FAILED-retry resume logic added
after backup-2026-08-19 (real seed) failed with a transient RunPod S3
gateway InvalidPart error during the upload phase.

Before this fix, run_for_date's FAILED branch unconditionally wiped
scan_items and restarted from PENDING on any retry -- including a failure
that happened well after a multi-hour SCANNING phase had already completed
cleanly. _mark_failed now records which phase was actually running when the
exception was raised (resume_phase); run_for_date only does a full reset
when that phase is PENDING/SCANNING (scan state not trustworthy), and
otherwise resumes directly at the recorded phase without touching
scan_items/deferred_items.

Builds a bare Runner instance (schema + state_db only, no Config/blob_store
needed for this code path) rather than the full @pytest.mark.live fixture
in test_runner_integration.py, mirroring the lightweight style already used
by test_runner_head_object_retry.py for the sibling gateway-quirk fix.
"""

import sqlite3
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import runner as app_runner


CURRENT_POLICY_HASH = "test-policy-hash-v1"


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
        policy=SimpleNamespace(policy_hash=CURRENT_POLICY_HASH),
    )
    return r


def _seed_one_scan_item(r, txn_id):
    r.state_db.execute(
        """
        INSERT INTO scan_items (txn_id, path, size, mtime_ns, classification)
        VALUES (?, ?, ?, ?, ?)
        """,
        (txn_id, "/a", 10, 0, "CRITICAL"),
    )
    r.state_db.commit()


def _scan_item_count(r, txn_id):
    return r.state_db.execute(
        "SELECT COUNT(*) FROM scan_items WHERE txn_id = ?", (txn_id,)
    ).fetchone()[0]


def _set_policy_hash(r, txn_id, policy_hash):
    r.state_db.execute(
        "UPDATE transactions SET policy_hash = ? WHERE txn_id = ?",
        (policy_hash, txn_id),
    )
    r.state_db.commit()


def test_mark_failed_captures_resume_phase(tmp_path):
    r = _make_runner(tmp_path)
    txn_id, txn_date = "backup-2026-08-19", "2026-08-19"
    r._upsert_transaction(txn_id, txn_date)
    r._set_phase(txn_id, app_runner.Phase.UPLOADING)

    r._mark_failed(txn_id, "simulated InvalidPart failure")

    tx = r._get_transaction(txn_id)
    assert tx["phase"] == "FAILED"
    assert tx["resume_phase"] == "UPLOADING"
    assert tx["error"] == "simulated InvalidPart failure"


def test_run_for_date_resumes_upload_without_wiping_scan_items(monkeypatch, tmp_path):
    r = _make_runner(tmp_path)
    txn_id, txn_date = "backup-2026-08-19", "2026-08-19"
    r._upsert_transaction(txn_id, txn_date)
    _seed_one_scan_item(r, txn_id)
    _set_policy_hash(r, txn_id, CURRENT_POLICY_HASH)
    r._set_phase(txn_id, app_runner.Phase.UPLOADING)
    r._mark_failed(txn_id, "simulated InvalidPart failure")

    seen_phases = []

    def fake_run_phases(txn_id_arg, txn_date_arg):
        seen_phases.append(r._get_phase(txn_id_arg))
        r._set_phase(txn_id_arg, app_runner.Phase.SUCCESS)

    monkeypatch.setattr(r, "_run_phases", fake_run_phases)

    r.run_for_date(txn_date)

    assert seen_phases == [app_runner.Phase.UPLOADING]
    assert _scan_item_count(r, txn_id) == 1
    tx = r._get_transaction(txn_id)
    assert tx["phase"] == "SUCCESS"
    assert tx["error"] is None
    assert tx["resume_phase"] is None


def test_run_for_date_full_resets_when_failed_during_scanning(monkeypatch, tmp_path):
    r = _make_runner(tmp_path)
    txn_id, txn_date = "backup-2026-08-19", "2026-08-19"
    r._upsert_transaction(txn_id, txn_date)
    _seed_one_scan_item(r, txn_id)
    r._set_phase(txn_id, app_runner.Phase.SCANNING)
    r._mark_failed(txn_id, "simulated scan crash")

    seen_phases = []

    def fake_run_phases(txn_id_arg, txn_date_arg):
        seen_phases.append(r._get_phase(txn_id_arg))
        r._set_phase(txn_id_arg, app_runner.Phase.SUCCESS)

    monkeypatch.setattr(r, "_run_phases", fake_run_phases)

    r.run_for_date(txn_date)

    assert seen_phases == [app_runner.Phase.PENDING]
    assert _scan_item_count(r, txn_id) == 0


# ---------------------------------------------------------------------------
# 2026-08-23: policy-fingerprint resume guard.
#
# Found this session: resuming past SCANNING reuses scan_items verbatim and
# never re-runs Policy.classify(), so a policy.yaml change between the
# original scan and a retry was silently ignored -- exactly the failure mode
# that let erf/media/{keyframes,proxies,thumbs} nearly get re-uploaded after
# the policy fix. _check_resumable_policy now compares the policy_hash
# recorded at scan time against the current policy and refuses to resume
# (fails closed) on any mismatch, including a NULL/legacy hash.
# ---------------------------------------------------------------------------


def test_run_for_date_refuses_resume_when_policy_changed(monkeypatch, tmp_path):
    r = _make_runner(tmp_path)
    txn_id, txn_date = "backup-2026-08-19", "2026-08-19"
    r._upsert_transaction(txn_id, txn_date)
    _seed_one_scan_item(r, txn_id)
    _set_policy_hash(r, txn_id, "stale-hash-from-old-policy")
    r._set_phase(txn_id, app_runner.Phase.UPLOADING)
    r._mark_failed(txn_id, "simulated InvalidPart failure")

    monkeypatch.setattr(
        r, "_run_phases", lambda *a, **k: pytest.fail("must not resume")
    )

    with pytest.raises(RuntimeError, match="scanned under a different policy"):
        r.run_for_date(txn_date)

    # Refused before touching anything: still FAILED, scan_items untouched.
    tx = r._get_transaction(txn_id)
    assert tx["phase"] == "FAILED"
    assert tx["resume_phase"] == "UPLOADING"
    assert _scan_item_count(r, txn_id) == 1


def test_run_for_date_refuses_resume_when_policy_hash_is_null(monkeypatch, tmp_path):
    """A transaction scanned before the policy_hash column existed (or
    before _scan ever ran under this feature) has policy_hash=NULL --
    treated as "unknown policy", refused just like a real mismatch."""
    r = _make_runner(tmp_path)
    txn_id, txn_date = "backup-2026-08-19", "2026-08-19"
    r._upsert_transaction(txn_id, txn_date)
    _seed_one_scan_item(r, txn_id)
    r._set_phase(txn_id, app_runner.Phase.UPLOADING)
    r._mark_failed(txn_id, "simulated InvalidPart failure")

    monkeypatch.setattr(
        r, "_run_phases", lambda *a, **k: pytest.fail("must not resume")
    )

    with pytest.raises(RuntimeError, match="scanned under a different policy"):
        r.run_for_date(txn_date)


def test_run_for_date_resumes_after_policy_hash_reconciled(monkeypatch, tmp_path):
    """Stands in for 'changed policy + successful reconciliation -> retry
    allowed': once whatever reconciles scan_items also brings policy_hash
    back in line with the current policy, resume proceeds normally again."""
    r = _make_runner(tmp_path)
    txn_id, txn_date = "backup-2026-08-19", "2026-08-19"
    r._upsert_transaction(txn_id, txn_date)
    _seed_one_scan_item(r, txn_id)
    _set_policy_hash(r, txn_id, "stale-hash-from-old-policy")
    r._set_phase(txn_id, app_runner.Phase.UPLOADING)
    r._mark_failed(txn_id, "simulated InvalidPart failure")

    # Simulate a completed reconciliation: scan_items updated elsewhere,
    # policy_hash brought forward to match current policy.
    _set_policy_hash(r, txn_id, CURRENT_POLICY_HASH)

    seen_phases = []

    def fake_run_phases(txn_id_arg, txn_date_arg):
        seen_phases.append(r._get_phase(txn_id_arg))
        r._set_phase(txn_id_arg, app_runner.Phase.SUCCESS)

    monkeypatch.setattr(r, "_run_phases", fake_run_phases)

    r.run_for_date(txn_date)

    assert seen_phases == [app_runner.Phase.UPLOADING]
    tx = r._get_transaction(txn_id)
    assert tx["phase"] == "SUCCESS"

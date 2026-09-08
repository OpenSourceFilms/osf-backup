"""Real end-to-end Runner integration tests: real S3 (disposable prefix,
cleaned up after), real StateDB, real Runner phase machine, real
retention/GC. Marked `live` (excluded from default runs, see pyproject.toml)
because it touches the actual v6a4zjiytb bucket with real credentials.

This is the gate-1 scenario Daniel specified: commit snapshot {A,B} ->
refcounts increment; commit second snapshot {A,C} -> A=2,B=1,C=1; expire
first snapshot -> A=1,B=0,C=1, B gets unreferenced_since but isn't deleted;
GC after grace collects it for real. Run manually with:
    .venv/bin/python -m pytest tests/test_runner_integration.py -m live -v
"""

import os
import sys
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.policy import Policy
from app import scanner as app_scanner
from app.statedb import StateDB
from app.storage import RunPodStorage
from app import healthcheck as app_healthcheck
from app import retention as app_retention
from app import runner as app_runner
from app import restore as app_restore

BUCKET = "v6a4zjiytb"


def _backdate(path: Path, seconds: int = 700) -> None:
    """Past the scanner's DEFAULT_SETTLE_SECONDS=600 window, so freshly
    written test files aren't deferred as DEFERRED_ACTIVE."""
    t = datetime.now().timestamp() - seconds
    os.utime(path, (t, t))


@pytest.fixture
def env(tmp_path):
    key_prefix = f"_test_runner_{uuid.uuid4().hex[:10]}/"

    source = tmp_path / "source"
    (source / "critical").mkdir(parents=True)
    (source / "outputs").mkdir()
    p = source / "critical" / "important.txt"
    p.write_text("critical content v1\n")
    _backdate(p)
    p = source / "outputs" / "result.txt"
    p.write_text("output content v1\n")
    _backdate(p)

    policy_yaml = tmp_path / "policy.yaml"
    policy_yaml.write_text(
        f"version: 1\nsource:\n  root: {source}\npaths:\n"
        f"  critical:\n    - critical\n  outputs:\n    - outputs\n"
        f"  sqlite_databases: []\nhard_exclude: []\n"
    )

    state_db = StateDB(tmp_path / "state.sqlite")
    storage = RunPodStorage(BUCKET)
    capacity = type(
        "Cap",
        (),
        {
            "hard_threshold_bytes": 10**12,
            "soft_threshold_bytes": 10**12,
            "current_usage_bytes": lambda self: state_db.total_repository_bytes(),
        },
    )()
    # Lenient auto-retention (attached to Runner, fires every _post_verify)
    # so 2 snapshots don't trigger expiry before the test explicitly wants
    # it to; a separate aggressive runner tests expiry itself.
    auto_retention = app_retention.RetentionRunner(
        state_db, daily=5, weekly=0, monthly=0, tzname="UTC"
    )
    expiry_retention = app_retention.RetentionRunner(
        state_db, daily=1, weekly=0, monthly=0, tzname="UTC"
    )
    gc = app_retention.GCRunner(state_db, storage, grace_days=-1)

    def build_runner():
        config = app_runner.Config(
            source_root=source,
            state_db_path=tmp_path / "state.sqlite",
            staging_dir=tmp_path / "staging",
            healthcheck=app_healthcheck.HealthcheckClient(),
            scanner=app_scanner,
            policy=Policy.from_yaml(policy_yaml),
            blob_store=storage,
            capacity=capacity,
            retention=auto_retention,
            gc=gc,
            gc_weekday=None,
            key_prefix=key_prefix,
        )
        return app_runner.Runner(config)

    yield {
        "source": source,
        "state_db": state_db,
        "storage": storage,
        "key_prefix": key_prefix,
        "build_runner": build_runner,
        "expiry_retention": expiry_retention,
        "gc": gc,
    }

    objs = storage.list_prefix(key_prefix, max_keys=1000)
    for obj in objs:
        storage.delete_object(obj["Key"])


@pytest.mark.live
def test_run_uploads_and_commits_real_manifest(env):
    r1 = env["build_runner"]()
    today = r1._melbourne_today()
    r1.run_for_date(today)

    blobs = env["state_db"].conn.execute(
        "SELECT hash, reference_count FROM blobs ORDER BY hash"
    ).fetchall()
    assert len(blobs) == 2
    assert all(b["reference_count"] == 1 for b in blobs)
    assert today in env["state_db"].list_snapshots()

    remote_snaps = app_restore.list_snapshots(env["storage"], env["key_prefix"])
    assert today in remote_snaps

    manifest = app_restore.load_manifest(env["storage"], today, env["key_prefix"])
    assert len(manifest.get("entries", [])) == 2

    report = app_restore.verify_snapshot(
        env["storage"], today, prefix=env["key_prefix"]
    )
    assert report.failed_entries == 0
    assert report.total_entries == 2


@pytest.mark.live
def test_gate1_refcount_expiry_and_gc_scenario(env):
    """The exact scenario Daniel specified for gate 1."""
    r1 = env["build_runner"]()
    today = r1._melbourne_today()
    r1.run_for_date(today)

    p = env["source"] / "outputs" / "result.txt"
    p.write_text("output content v2 CHANGED\n")
    _backdate(p)

    tomorrow = (datetime.now(timezone.utc) + timedelta(days=1)).date().isoformat()
    r2 = env["build_runner"]()
    r2.run_for_date(tomorrow)

    state_db = env["state_db"]
    blobs = {
        b["hash"]: b["reference_count"]
        for b in state_db.conn.execute(
            "SELECT hash, reference_count FROM blobs"
        ).fetchall()
    }
    assert len(blobs) == 3  # A (critical, shared) + B (old output) + C (new output)
    assert sorted(blobs.values()) == [1, 1, 2]  # A=2, B=1, C=1

    env["expiry_retention"].run(f"backup-{tomorrow}")

    blobs_after = {
        b["hash"]: (b["reference_count"], b["unreferenced_since"])
        for b in state_db.conn.execute(
            "SELECT hash, reference_count, unreferenced_since FROM blobs"
        ).fetchall()
    }
    zero_ref = [h for h, (rc, _) in blobs_after.items() if rc <= 0]
    assert len(zero_ref) == 1  # B expired
    assert blobs_after[zero_ref[0]][1] is not None  # unreferenced_since stamped

    shared_hash = [h for h, rc in blobs.items() if rc == 2][0]
    assert blobs_after[shared_hash] == (1, None)  # A back to 1, not touched

    # idempotent: running again must not double-decrement
    env["expiry_retention"].run(f"backup-{tomorrow}")
    blobs_after_2nd = {
        b["hash"]: b["reference_count"]
        for b in state_db.conn.execute("SELECT hash, reference_count FROM blobs")
    }
    assert blobs_after_2nd == {h: rc for h, (rc, _) in blobs_after.items()}

    # real GC: object actually deleted from S3, local row removed
    remote_key = state_db.conn.execute(
        "SELECT remote_key FROM blobs WHERE hash = ?", (zero_ref[0],)
    ).fetchone()["remote_key"]
    assert env["storage"].head_object(remote_key) is not None

    env["gc"].run()

    assert env["storage"].head_object(remote_key) is None
    row = state_db.conn.execute(
        "SELECT 1 FROM blobs WHERE hash = ?", (zero_ref[0],)
    ).fetchone()
    assert row is None


@pytest.mark.live
def test_rebuild_state_reconstructs_from_remote_manifests(env, tmp_path):
    """Simulates real disaster recovery: state.sqlite is gone, only the
    remote manifests survive. rebuild_state must recover the same logical
    state a from-scratch StateDB would have after the same two runs --
    not just import/run without error.
    """
    r1 = env["build_runner"]()
    today = r1._melbourne_today()
    r1.run_for_date(today)

    p = env["source"] / "outputs" / "result.txt"
    p.write_text("output content v2 CHANGED\n")
    _backdate(p)

    tomorrow = (datetime.now(timezone.utc) + timedelta(days=1)).date().isoformat()
    r2 = env["build_runner"]()
    r2.run_for_date(tomorrow)

    original_blobs = {
        b["hash"]: b["reference_count"]
        for b in env["state_db"].conn.execute(
            "SELECT hash, reference_count FROM blobs"
        ).fetchall()
    }
    original_snapshot_entries = {
        date: {
            row["path"]: row["hash"]
            for row in env["state_db"].conn.execute(
                "SELECT path, hash FROM snapshot_entries WHERE run_date = ?", (date,)
            )
        }
        for date in (today, tomorrow)
    }
    # Runner itself never writes app.statedb.StateDB.source_files (no
    # upsert_source_file call anywhere in runner.py -- confirmed dead until
    # this fix) -- so ground truth for classification/hash-per-path is the
    # remote manifests, the same source rebuild_state itself reads from.
    expected_source_files = {}
    for date in (today, tomorrow):
        manifest = app_restore.load_manifest(env["storage"], date, env["key_prefix"])
        for entry in manifest["entries"]:
            expected_source_files[entry["path"]] = (entry["sha256"], entry["classification"])

    # Simulate total local-state loss: a brand-new StateDB, never touched
    # by Runner, only by rebuild_state reading remote manifests.
    fresh_db = StateDB(tmp_path / "rebuilt-state.sqlite")
    result = app_restore.rebuild_state(env["storage"], fresh_db, env["key_prefix"])

    assert sorted(result["snapshots"]) == sorted([today, tomorrow])
    assert set(fresh_db.list_snapshots()) == {today, tomorrow}

    rebuilt_blobs = {
        b["hash"]: b["reference_count"]
        for b in fresh_db.conn.execute("SELECT hash, reference_count FROM blobs").fetchall()
    }
    assert rebuilt_blobs == original_blobs

    for date in (today, tomorrow):
        rebuilt_entries = {
            row["path"]: row["hash"]
            for row in fresh_db.conn.execute(
                "SELECT path, hash FROM snapshot_entries WHERE run_date = ?", (date,)
            )
        }
        assert rebuilt_entries == original_snapshot_entries[date]

    rebuilt_source_files = {
        row["path"]: (row["hash"], row["classification"])
        for row in fresh_db.conn.execute("SELECT path, hash, classification FROM source_files")
    }
    assert rebuilt_source_files == expected_source_files

    # Blob remote_key must be the real, fetchable key -- not the bare sha256
    # (the exact bug this function had before: HEAD would 404 forever).
    for h in rebuilt_blobs:
        remote_key = fresh_db.conn.execute(
            "SELECT remote_key FROM blobs WHERE hash = ?", (h,)
        ).fetchone()["remote_key"]
        assert env["storage"].head_object(remote_key) is not None

    fresh_db.close()

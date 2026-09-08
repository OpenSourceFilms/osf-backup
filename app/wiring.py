from __future__ import annotations

import os
import sys
from pathlib import Path

BACKUP_HOME = Path(
    os.environ.get("BACKUP_HOME", Path(__file__).resolve().parent.parent)
).expanduser()
if str(BACKUP_HOME) not in sys.path:
    sys.path.insert(0, str(BACKUP_HOME))

import yaml  # noqa: E402

from app.policy import Policy  # noqa: E402
from app import scanner as app_scanner  # noqa: E402
from app.statedb import StateDB  # noqa: E402
from app.storage import RunPodStorage, MissingCredentials, ListPrefixLimitExceeded  # noqa: E402
from app import healthcheck as app_healthcheck  # noqa: E402
from app import notify_email as app_notify_email  # noqa: E402
from app import notify_ntfy as app_notify_ntfy  # noqa: E402
from app import retention as app_retention  # noqa: E402
from app import runner as app_runner  # noqa: E402

POLICY_PATH = BACKUP_HOME / "config" / "policy.yaml"
RUNTIME_PATH = BACKUP_HOME / "config" / "runtime.yaml"
STATE_DB_PATH = BACKUP_HOME / "state" / "state.sqlite"
STAGING_DIR = BACKUP_HOME / "state" / "staging"
# Legacy path only -- Runner no longer creates or uses this (see
# app.runner.Config's docstring on removing the local blob cache,
# 2026-08-29). Kept here purely so `backupctl staging-status`/`doctor`
# can detect and warn if anything ever writes to it again.
BLOB_CACHE_DIR = BACKUP_HOME / "state" / "blob_cache"


def _load_policy_yaml() -> dict:
    with POLICY_PATH.open("r", encoding="utf-8") as fh:
        data = yaml.safe_load(fh)
    if not isinstance(data, dict):
        raise ValueError(f"{POLICY_PATH} must contain a top-level mapping")
    return data


def _load_runtime_yaml() -> dict:
    """The single shared loader for runtime/daemon scheduling config
    (config/runtime.yaml) -- backupd, backupctl status, and backupctl
    doctor must all read schedule.time through this one function so they
    can never silently disagree about when the nightly backup runs (the
    same duplicate-authority failure mode this module already exists to
    prevent for Runner construction). Deliberately separate from
    _load_policy_yaml: runtime.yaml controls WHEN/HOW the daemon runs, not
    WHAT gets backed up, and must never affect Policy.policy_hash."""
    with RUNTIME_PATH.open("r", encoding="utf-8") as fh:
        data = yaml.safe_load(fh)
    if not isinstance(data, dict):
        raise ValueError(f"{RUNTIME_PATH} must contain a top-level mapping")
    return data


def _load_policy() -> Policy:
    return Policy.from_yaml(POLICY_PATH)


def _source_root(raw_policy: dict) -> Path:
    return Path(raw_policy["source"]["root"]).expanduser()


def _key_prefix(raw_policy: dict) -> str:
    """Normalise target.prefix (e.g. "_osf_backup") to a key-prefix string
    ("_osf_backup/") every remote-key-touching command shares -- Runner's
    writer side and restore.py's reader side must agree on this or nothing
    written will ever be found."""
    prefix = str(raw_policy.get("target", {}).get("prefix") or "").strip("/")
    return f"{prefix}/" if prefix else ""


def _open_state_db() -> StateDB:
    STATE_DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    return StateDB(STATE_DB_PATH)


def _build_storage(raw_policy: dict) -> RunPodStorage:
    return RunPodStorage(raw_policy["target"]["bucket"])


class _CapacityTracker:
    """Real capacity accounting: current usage from StateDB's actual blob
    total, thresholds derived from policy.yaml's target.capacity_bytes and
    the storage.{hard,discretionary}_stop_fraction the operator already
    configured -- not invented adapter defaults."""

    def __init__(
        self,
        state_db: StateDB,
        *,
        capacity_bytes: int,
        hard_stop_fraction: float,
        discretionary_stop_fraction: float,
    ) -> None:
        self._state_db = state_db
        self.hard_threshold_bytes = int(capacity_bytes * hard_stop_fraction)
        self.soft_threshold_bytes = int(capacity_bytes * discretionary_stop_fraction)

    def current_usage_bytes(self) -> int:
        return self._state_db.total_repository_bytes()


def build_runner(should_stop=None) -> app_runner.Runner:
    """Construct a real Runner: every Config field is the actual module or
    a small, real object built from actual StateDB/policy.yaml data -- no
    permissive adapter standing in for a mismatched interface (see the
    2026-08-19 runner.py rewrite: the six interface mismatches found while
    wiring this were fixed in runner.py itself, not papered over here).

    ``should_stop`` is an optional zero-argument predicate the Runner
    polls between units of work (see app.runner.Config.should_stop).
    app/backupd.py passes its shutdown Event's .is_set so SIGTERM can
    stop a transaction that is already running; a foreground `backupctl
    run` passes nothing and is therefore never cancelled from inside.
    """
    raw_policy = _load_policy_yaml()
    raw_runtime = _load_runtime_yaml()
    policy = _load_policy()
    state_db = _open_state_db()
    storage = _build_storage(raw_policy)
    healthcheck_client = app_healthcheck.HealthcheckClient()
    email_client = app_notify_email.EmailClient()
    ntfy_client = app_notify_ntfy.NtfyClient()

    capacity = _CapacityTracker(
        state_db,
        capacity_bytes=raw_policy["target"]["capacity_bytes"],
        hard_stop_fraction=raw_policy["storage"]["hard_stop_fraction"],
        discretionary_stop_fraction=raw_policy["storage"]["discretionary_stop_fraction"],
    )

    retention_cfg = raw_policy.get("retention", {})
    retention = app_retention.RetentionRunner(
        state_db,
        daily=retention_cfg.get("daily", 14),
        weekly=retention_cfg.get("weekly", 8),
        monthly=retention_cfg.get("monthly", 4),
        tzname=raw_runtime["schedule"]["timezone"],
    )
    gc = app_retention.GCRunner(
        state_db,
        storage,
        grace_days=retention_cfg.get("unreferenced_blob_grace_days", 30),
    )

    config = app_runner.Config(
        source_root=_source_root(raw_policy),
        state_db_path=STATE_DB_PATH,
        staging_dir=STAGING_DIR,
        healthcheck=healthcheck_client,
        email=email_client,
        scanner=app_scanner,
        policy=policy,
        blob_store=storage,
        capacity=capacity,
        retention=retention,
        gc=gc,
        gc_weekday=6,  # Sunday, Melbourne time
        key_prefix=_key_prefix(raw_policy),
        should_stop=should_stop,
        ntfy=ntfy_client,
    )
    return app_runner.Runner(config)

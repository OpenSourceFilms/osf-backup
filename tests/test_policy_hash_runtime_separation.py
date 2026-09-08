"""Lock in the 2026-08-29 fix separating the daemon schedule from policy_hash.

Policy.policy_hash is a sha256 over the canonical JSON of the whole
config/policy.yaml file, and config/policy.yaml's old schedule.time field
was being included, so changing the daemon run time from 00:00 to 04:00
changed policy_hash and could fail app/runner.py's resume-policy guard
even though file selection and classification never changed.
Policy.from_yaml now never reads config/runtime.yaml, so these tests prove
real classification rule changes alter policy_hash while runtime schedule
changes do not.
"""

import sys
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.policy import Policy


BASE_POLICY = """\
version: 1
source:
  root: /tmp/does-not-matter
paths:
  critical:
    - foo
  outputs: []
  sqlite_databases: []
hard_exclude: []
"""

CHANGED_RULE_POLICY = """\
version: 1
source:
  root: /tmp/does-not-matter
paths:
  critical:
    - bar
  outputs: []
  sqlite_databases: []
hard_exclude: []
"""


def _write(tmp_path: Path, text: str, name: str) -> Path:
    path = tmp_path / name
    path.write_text(text, encoding="utf-8")
    return path


def test_policy_yaml_has_no_schedule_key_left():
    policy_path = Path(__file__).resolve().parent.parent / "config" / "policy.yaml"
    with policy_path.open(encoding="utf-8") as f:
        policy = yaml.safe_load(f)
    assert "schedule" not in policy


def test_real_classification_rule_change_does_change_policy_hash(tmp_path):
    policy_a = Policy.from_yaml(_write(tmp_path, BASE_POLICY, "policy_a.yaml"))
    policy_b = Policy.from_yaml(_write(tmp_path, CHANGED_RULE_POLICY, "policy_b.yaml"))

    assert policy_a.policy_hash != policy_b.policy_hash


def test_runtime_yaml_schedule_change_does_not_change_policy_hash(tmp_path, monkeypatch):
    """Reproduces the exact real scenario (the daemon schedule field
    changing) via the real app.wiring module-level path constants that
    app.wiring.build_runner()/backupd actually use, not a synthetic path.
    """
    import app.wiring as app_wiring

    policy_path = _write(tmp_path, BASE_POLICY, "policy.yaml")
    monkeypatch.setattr(app_wiring, "POLICY_PATH", policy_path)

    runtime_path = tmp_path / "runtime.yaml"
    runtime_path.write_text(
        'schedule:\n  timezone: Australia/Melbourne\n  time: "00:00"\n', encoding="utf-8"
    )
    monkeypatch.setattr(app_wiring, "RUNTIME_PATH", runtime_path)

    hash_before = app_wiring._load_policy().policy_hash

    runtime_path.write_text(
        'schedule:\n  timezone: Australia/Melbourne\n  time: "04:00"\n', encoding="utf-8"
    )
    hash_after = app_wiring._load_policy().policy_hash

    assert hash_before == hash_after

"""Regression tests for Runner._upload_policy_provenance, added because
Aug23's mid-run policy.yaml edit exposed a real gap: only policy_hash (a
fingerprint) was durably recorded, not the exact canonical policy bytes
that hash was computed from. Recovery of the real bytes for Aug23 only
worked because policy.yaml itself happened to still be in scope for
backup that day -- this makes it deliberate for every future snapshot.
"""

import hashlib
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import runner as app_runner
from app.policy import Policy

MINIMAL_POLICY_A = """
version: 1
source:
  root: /tmp/does-not-matter-a
paths:
  critical: []
  outputs: []
  sqlite_databases: []
hard_exclude: []
"""

MINIMAL_POLICY_B = """
version: 1
source:
  root: /tmp/does-not-matter-b
paths:
  critical: []
  outputs: []
  sqlite_databases: []
hard_exclude: []
"""


class _FakeBlobStore:
    def __init__(self):
        self.objects = {}

    def exists(self, key):
        return key in self.objects

    def get_object_bytes(self, key):
        return self.objects[key]

    def put_object_bytes(self, key, data):
        self.objects[key] = data


def _make_runner_for_provenance(policy):
    r = object.__new__(app_runner.Runner)
    r.config = SimpleNamespace(
        policy=policy, blob_store=_FakeBlobStore(), key_prefix="_osf_backup/"
    )
    return r


def _write_policy(tmp_path, text, name="policy.yaml"):
    p = tmp_path / name
    p.write_text(text)
    return p


def test_content_addressed_artifact_is_exactly_recoverable(tmp_path):
    policy = Policy.from_yaml(_write_policy(tmp_path, MINIMAL_POLICY_A))
    runner = _make_runner_for_provenance(policy)

    runner._upload_policy_provenance()

    key = f"_osf_backup/meta/policies/{policy.policy_hash}.json"
    assert runner.config.blob_store.exists(key)
    assert runner.config.blob_store.get_object_bytes(key) == policy.canonical_bytes


def test_stored_bytes_hash_equals_policy_hash(tmp_path):
    policy = Policy.from_yaml(_write_policy(tmp_path, MINIMAL_POLICY_A))
    runner = _make_runner_for_provenance(policy)

    runner._upload_policy_provenance()

    key = f"_osf_backup/meta/policies/{policy.policy_hash}.json"
    stored = runner.config.blob_store.get_object_bytes(key)
    assert hashlib.sha256(stored).hexdigest() == policy.policy_hash


def test_manifest_policy_hash_matches_provenance_artifact_key(tmp_path):
    """The policy_hash Runner writes into the manifest (see
    _upload_manifest) is exactly self.config.policy.policy_hash -- the same
    value the provenance artifact is content-addressed by, so the two can
    never drift apart."""
    policy = Policy.from_yaml(_write_policy(tmp_path, MINIMAL_POLICY_A))
    runner = _make_runner_for_provenance(policy)

    runner._upload_policy_provenance()
    manifest_policy_hash = runner.config.policy.policy_hash

    key = f"_osf_backup/meta/policies/{manifest_policy_hash}.json"
    assert runner.config.blob_store.exists(key)


def test_policy_change_after_load_does_not_affect_already_captured_bytes(tmp_path):
    """The exact scenario that exposed this gap: policy.yaml changes after
    a Policy object has already been loaded for a transaction. The
    already-loaded Policy's canonical_bytes/policy_hash must reflect what
    was loaded, not whatever is on disk now."""
    policy_path = _write_policy(tmp_path, MINIMAL_POLICY_A)
    policy = Policy.from_yaml(policy_path)
    runner = _make_runner_for_provenance(policy)

    original_hash = policy.policy_hash
    original_bytes = policy.canonical_bytes

    _write_policy(tmp_path, MINIMAL_POLICY_B, name=policy_path.name)  # concurrent edit

    runner._upload_policy_provenance()

    assert policy.policy_hash == original_hash
    assert policy.canonical_bytes == original_bytes
    key = f"_osf_backup/meta/policies/{original_hash}.json"
    assert runner.config.blob_store.get_object_bytes(key) == original_bytes


def test_reuse_does_not_reupload_matching_existing_artifact(tmp_path):
    policy = Policy.from_yaml(_write_policy(tmp_path, MINIMAL_POLICY_A))
    runner = _make_runner_for_provenance(policy)

    runner._upload_policy_provenance()
    before = dict(runner.config.blob_store.objects)
    runner._upload_policy_provenance()  # a later transaction, same policy
    assert runner.config.blob_store.objects == before


def test_mismatched_existing_artifact_is_never_overwritten(tmp_path, caplog):
    policy = Policy.from_yaml(_write_policy(tmp_path, MINIMAL_POLICY_A))
    runner = _make_runner_for_provenance(policy)

    key = f"_osf_backup/meta/policies/{policy.policy_hash}.json"
    runner.config.blob_store.objects[key] = b"unexpected-different-content"

    with caplog.at_level("ERROR"):
        runner._upload_policy_provenance()

    assert runner.config.blob_store.objects[key] == b"unexpected-different-content"

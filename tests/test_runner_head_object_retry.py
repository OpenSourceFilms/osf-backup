"""Fast, non-live regression test for Runner._head_object_after_upload:
tolerates a bounded number of transient HeadObject failures (the real
RunPod-gateway post-multipart-completion 403 confirmed in
tests/test_s3_transfer_paths.py) without needing real S3.

Calls the unbound method against a minimal stand-in for `self` -- the
method only touches `self.config.blob_store`, so a full Runner (which
needs a real StateDB, staging dirs, etc.) isn't required to exercise it.
"""

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import runner as app_runner


def _fake_self(blob_store):
    return SimpleNamespace(config=SimpleNamespace(blob_store=blob_store))


class _FlakyStore:
    def __init__(self, fail_times: int, result: int):
        self.fail_times = fail_times
        self.result = result
        self.calls = 0

    def head_object(self, key):
        self.calls += 1
        if self.calls <= self.fail_times:
            raise RuntimeError("simulated transient 403")
        return self.result


def test_retries_past_transient_failure_then_returns_real_size():
    store = _FlakyStore(fail_times=1, result=138412032)
    result = app_runner.Runner._head_object_after_upload(
        _fake_self(store), "some/key", attempts=3, delay_seconds=0.01
    )
    assert result == 138412032
    assert store.calls == 2


def test_raises_the_real_exception_once_attempts_are_exhausted():
    store = _FlakyStore(fail_times=5, result=123)
    with pytest.raises(RuntimeError, match="simulated transient 403"):
        app_runner.Runner._head_object_after_upload(
            _fake_self(store), "some/key", attempts=3, delay_seconds=0.01
        )
    assert store.calls == 3

"""Regression test for RunPodStorage.bucket_accessible(), the real fix
behind `backupctl doctor`'s S3 check (found broken 2026-08-23, HANDOFF -3n):
the old check called list_prefix("", max_keys=1) as a reachability probe,
but list_prefix's contract is to raise ListPrefixLimitExceeded the instant
more objects exist than max_keys allows -- so doctor permanently
false-failed against any real, populated bucket. bucket_accessible() asks
list_objects_v2 directly and treats truncation as a healthy, expected
result, never mistaking a bounded listing helper for an exhaustive one.
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import storage as app_storage


class _FakeS3Client:
    def __init__(self, *, is_truncated):
        self._is_truncated = is_truncated
        self.calls = []

    def list_objects_v2(self, **kwargs):
        self.calls.append(kwargs)
        return {"IsTruncated": self._is_truncated, "Contents": []}


def _make_storage(client):
    s = object.__new__(app_storage.RunPodStorage)
    s.bucket = "test-bucket"
    s.client = client
    return s


def test_healthy_empty_bucket_passes():
    client = _FakeS3Client(is_truncated=False)
    storage = _make_storage(client)
    assert storage.bucket_accessible() is True
    assert client.calls == [{"Bucket": "test-bucket", "MaxKeys": 1}]


def test_healthy_non_empty_bucket_passes():
    """The real bug: a populated bucket's single-page probe comes back
    truncated -- that must still mean "reachable", not "failed"."""
    client = _FakeS3Client(is_truncated=True)
    storage = _make_storage(client)
    assert storage.bucket_accessible() is True


def test_does_not_rely_on_the_bounded_list_prefix_helper():
    """list_prefix would raise on this same truncated response --
    bucket_accessible must not be built on it."""
    client = _FakeS3Client(is_truncated=True)
    storage = _make_storage(client)
    with pytest.raises(app_storage.ListPrefixLimitExceeded):
        storage.list_prefix("", max_keys=1)
    assert storage.bucket_accessible() is True

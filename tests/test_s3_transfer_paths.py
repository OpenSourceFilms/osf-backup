"""Closes the S3-functional-test ambiguity flagged in HANDOFF.md checkpoint
-3f: the gate-1 integration tests only ever moved tiny text files through a
real Runner, so neither the real multipart upload/download code path nor a
real transient-failure/retry path was ever actually exercised -- only
inferred. Both are proven here, for real, against the actual bucket, using
a disposable self-cleaning prefix like every other `live` test in this repo.

Multipart: the test file is deliberately sized just over
DEFAULT_MULTIPART_THRESHOLD (128 MiB) so boto3's managed transfer really
splits it into >1 part -- confirmed by counting real
CreateMultipartUpload/UploadPart/CompleteMultipartUpload API calls via
botocore's event system, not inferred from file size alone.

Retry: a `before-send` handler on the real client injects exactly one
synthetic HTTP 500 on the first send attempt, then steps aside for every
later attempt. botocore's own standard retry policy (already configured in
RunPodStorage, mode="standard") is what decides to retry and what actually
performs the second, real send to the real endpoint -- nothing about the
retry decision itself is mocked, only the one transient failure that
triggers it.

Run manually:
    .venv/bin/python -m pytest tests/test_s3_transfer_paths.py -m live -v
"""

import hashlib
import os
import sys
import time
import uuid
from pathlib import Path

import pytest
from botocore.awsrequest import AWSResponse

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.storage import RunPodStorage, DEFAULT_MULTIPART_THRESHOLD

BUCKET = "v6a4zjiytb"


class _RawErrorBody:
    """Minimal stand-in for the raw HTTP body botocore reads via
    AWSResponse.content -> self.raw.stream()."""

    def __init__(self, data: bytes) -> None:
        self._data = data

    def stream(self, *args, **kwargs):
        yield self._data


def _fake_internal_error_response(url: str) -> AWSResponse:
    body = (
        b'<?xml version="1.0" encoding="UTF-8"?>'
        b"<Error><Code>InternalError</Code>"
        b"<Message>induced by test_transient_failure_triggers_real_retry</Message>"
        b"<RequestId>test-induced-0001</RequestId></Error>"
    )
    return AWSResponse(
        url,
        500,
        {"content-type": "application/xml", "content-length": str(len(body))},
        _RawErrorBody(body),
    )


@pytest.fixture
def storage():
    return RunPodStorage(BUCKET)


@pytest.fixture
def prefix():
    p = f"_test_s3transfer_{uuid.uuid4().hex[:10]}/"
    yield p


@pytest.mark.live
def test_multipart_upload_and_download_roundtrip_with_real_multipart_api_calls(
    storage, prefix, tmp_path
):
    size = DEFAULT_MULTIPART_THRESHOLD + (4 * 1024 * 1024)  # 132 MiB: forces 2 real parts
    src = tmp_path / "multipart_source.bin"
    with src.open("wb") as fh:
        remaining = size
        chunk = os.urandom(8 * 1024 * 1024)
        while remaining > 0:
            take = min(len(chunk), remaining)
            fh.write(chunk[:take])
            remaining -= take
    original_sha256 = hashlib.sha256(src.read_bytes()).hexdigest()

    calls = {"create": 0, "upload_part": 0, "complete": 0}

    def _count(name):
        def _handler(**kwargs):
            calls[name] += 1
        return _handler

    events = storage.client.meta.events
    events.register("before-call.s3.CreateMultipartUpload", _count("create"))
    events.register("before-call.s3.UploadPart", _count("upload_part"))
    events.register("before-call.s3.CompleteMultipartUpload", _count("complete"))

    key = prefix + "multipart_object.bin"
    try:
        storage.put_object_from_file(key, src)

        # Not inferred: the real multipart API sequence actually ran.
        assert calls["create"] >= 1, "CreateMultipartUpload was never called"
        assert calls["upload_part"] >= 2, "UploadPart called <2 times -- not a real multi-part upload"
        assert calls["complete"] >= 1, "CompleteMultipartUpload was never called"

        # Discovered live while writing this test: RunPod's S3 gateway can
        # 403 a HeadObject issued immediately after a multipart
        # CompleteMultipartUpload, before the object is fully resolved
        # (list_prefix shows it right away; HeadObject briefly doesn't).
        # Not a botocore-retryable status, so it isn't already covered by
        # RunPodStorage's Config(retries={"mode": "standard"}). This exact
        # finding is what motivated the new Runner._head_object_after_upload
        # retry wrapper in app/runner.py -- reproducing the same bounded
        # tolerance here so this test reflects real endpoint behavior
        # instead of flaking on it.
        head_size = None
        for attempt in range(3):
            try:
                head_size = storage.head_object(key)
                break
            except Exception:
                if attempt == 2:
                    raise
                time.sleep(2.0)
        assert head_size == size

        dest = tmp_path / "multipart_downloaded.bin"
        storage.get_object_to_file(key, dest)
        downloaded_sha256 = hashlib.sha256(dest.read_bytes()).hexdigest()
        assert downloaded_sha256 == original_sha256
    finally:
        storage.delete_object(key)

    assert storage.head_object(key) is None


@pytest.mark.live
def test_transient_failure_triggers_real_retry_and_eventually_succeeds(storage, prefix):
    key = prefix + "retry_object.txt"
    payload = b"real retry-path exercise, real bytes, real bucket\n"

    send_attempts = {"n": 0}

    def _inject_one_failure_then_passthrough(request, **kwargs):
        send_attempts["n"] += 1
        if send_attempts["n"] == 1:
            return _fake_internal_error_response(request.url)
        return None  # real send for every attempt after the first

    events = storage.client.meta.events
    events.register("before-send.s3.PutObject", _inject_one_failure_then_passthrough)

    storage.put_object_bytes(key, payload)  # must not raise: retry must recover

    # Not inferred: a second real send attempt actually happened.
    assert send_attempts["n"] >= 2, "no retry attempt observed -- induced failure was never retried"

    try:
        assert storage.head_object(key) == len(payload)
        assert storage.get_object_bytes(key) == payload
    finally:
        storage.delete_object(key)

    assert storage.head_object(key) is None

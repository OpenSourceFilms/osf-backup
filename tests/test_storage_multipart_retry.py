"""Fast, non-live regression tests for RunPodStorage's multipart retry
hierarchy, added after backup-2026-08-19's real seed hit two distinct
transient RunPod S3 gateway failures on the same 426MB blob across two
independent attempts: InvalidPart on CompleteMultipartUpload, then a 524
gateway timeout on UploadPart. A same-size/same-part-count synthetic upload
against a disposable prefix succeeded cleanly moments later (see
tests/test_s3_transfer_paths.py), and the local blob's sha256 was verified
bit-for-bit intact both times -- pointing at gateway flakiness correlated
with transfer duration (real transfers at this size run 4+ minutes), not
malformed multipart state or a bad source file.

Design (see app/storage.py's module docstring for the full rationale):
  - A single transient UploadPart failure retries the SAME part, SAME
    upload_id, SAME bytes, with bounded exponential backoff.
  - A part that exhausts its transient retry budget aborts the current
    multipart upload and retries the WHOLE file under a fresh upload_id
    (bounded to MULTIPART_WHOLE_FILE_RETRY_ATTEMPTS).
  - CompleteMultipartUpload's InvalidPart quirk feeds the same whole-file
    retry path.
  - A permanent error (e.g. AccessDenied) is never retried at any level.

Uses an in-memory fake S3 client (no real network) that actually
reconstructs the uploaded object from its parts, so "the final object
matches the source bytes" is a genuine integrity check, not an assumption.
"""

import os
import sys
from pathlib import Path

import pytest
from botocore.exceptions import ClientError

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import storage as app_storage


def _client_error(status_code: int, code: str | None = None, operation: str = "UploadPart"):
    return ClientError(
        {
            "Error": {"Code": code or str(status_code), "Message": "synthetic"},
            "ResponseMetadata": {"HTTPStatusCode": status_code},
        },
        operation,
    )


class _FakeMultipartClient:
    def __init__(self):
        self.objects: dict[str, bytes] = {}
        self.create_calls: list[dict] = []
        self.upload_part_calls: list[dict] = []
        self.complete_calls: list[dict] = []
        self.aborted_upload_ids: list[str] = []
        self._parts_by_upload: dict[str, dict[int, bytes]] = {}
        self._next_upload_id = 0
        self._part_faults: dict[int, list[Exception | None]] = {}
        self._complete_faults: list[Exception | None] = []

    def faults_for_part(self, part_number: int, faults: list) -> None:
        self._part_faults[part_number] = faults

    def faults_for_complete(self, faults: list) -> None:
        self._complete_faults = list(faults)

    def create_multipart_upload(self, Bucket, Key):
        self._next_upload_id += 1
        upload_id = f"uid-{self._next_upload_id}"
        self._parts_by_upload[upload_id] = {}
        self.create_calls.append({"Bucket": Bucket, "Key": Key})
        return {"UploadId": upload_id}

    def upload_part(self, Bucket, Key, UploadId, PartNumber, Body):
        body = bytes(Body)
        self.upload_part_calls.append(
            {"UploadId": UploadId, "PartNumber": PartNumber, "Body": body}
        )
        schedule = self._part_faults.get(PartNumber, [])
        call_index = sum(
            1 for c in self.upload_part_calls if c["PartNumber"] == PartNumber
        ) - 1
        if call_index < len(schedule) and schedule[call_index] is not None:
            raise schedule[call_index]
        self._parts_by_upload[UploadId][PartNumber] = body
        return {"ETag": f"etag-{UploadId}-{PartNumber}"}

    def complete_multipart_upload(self, Bucket, Key, UploadId, MultipartUpload):
        self.complete_calls.append({"UploadId": UploadId, "Parts": MultipartUpload["Parts"]})
        if self._complete_faults:
            exc = self._complete_faults.pop(0)
            if exc is not None:
                raise exc
        parts = self._parts_by_upload[UploadId]
        ordered = b"".join(
            parts[p["PartNumber"]]
            for p in sorted(MultipartUpload["Parts"], key=lambda p: p["PartNumber"])
        )
        self.objects[Key] = ordered
        return {}

    def abort_multipart_upload(self, Bucket, Key, UploadId):
        self.aborted_upload_ids.append(UploadId)

    def head_object(self, Bucket, Key):
        if Key not in self.objects:
            raise ClientError(
                {"Error": {"Code": "404", "Message": "Not Found"}, "ResponseMetadata": {"HTTPStatusCode": 404}},
                "HeadObject",
            )
        return {"ContentLength": len(self.objects[Key])}


def _make_storage(client, *, multipart_threshold=10, multipart_chunksize=50):
    s = object.__new__(app_storage.RunPodStorage)
    s.bucket = "test-bucket"
    s.client = client
    s.transfer_config = app_storage.TransferConfig(
        multipart_threshold=multipart_threshold, multipart_chunksize=multipart_chunksize
    )
    return s


@pytest.fixture(autouse=True)
def _fast_backoff(monkeypatch):
    monkeypatch.setattr(app_storage, "MULTIPART_PART_RETRY_BASE_DELAY_SECONDS", 0.0)
    monkeypatch.setattr(app_storage, "MULTIPART_PART_RETRY_JITTER_SECONDS", 0.0)
    monkeypatch.setattr(app_storage, "MULTIPART_WHOLE_FILE_RETRY_DELAY_SECONDS", 0.0)
    monkeypatch.setattr(app_storage, "HEAD_OBJECT_RETRY_DELAY_SECONDS", 0.0)


def _write_source(tmp_path, size=120) -> tuple[Path, bytes]:
    content = os.urandom(size)
    src = tmp_path / "blob"
    src.write_bytes(content)
    return src, content


def test_upload_part_retries_same_part_same_upload_id_after_transient_524(tmp_path):
    src, content = _write_source(tmp_path)
    client = _FakeMultipartClient()
    client.faults_for_part(2, [_client_error(524)])
    storage = _make_storage(client)

    storage.put_object_from_file("k", src)

    part1_calls = [c for c in client.upload_part_calls if c["PartNumber"] == 1]
    part2_calls = [c for c in client.upload_part_calls if c["PartNumber"] == 2]
    assert len(part1_calls) == 1, "preceding successful part must not be resent"
    assert len(part2_calls) == 2
    assert part2_calls[0]["UploadId"] == part2_calls[1]["UploadId"], "same upload_id on retry"
    assert part2_calls[0]["Body"] == part2_calls[1]["Body"] == content[50:100], "same bytes on retry"
    assert len(client.complete_calls) == 1
    assert client.objects["k"] == content, "reconstructed object matches source"
    assert client.aborted_upload_ids == []


def test_part_exhausting_transient_retries_triggers_whole_file_retry_with_new_upload_id(tmp_path):
    src, content = _write_source(tmp_path)
    client = _FakeMultipartClient()
    client.faults_for_part(2, [_client_error(524)] * app_storage.MULTIPART_PART_RETRY_ATTEMPTS)
    storage = _make_storage(client)

    storage.put_object_from_file("k", src)

    assert len(client.create_calls) == 2, "first attempt's upload_id abandoned, fresh one created"
    assert len(client.aborted_upload_ids) == 1
    assert client.objects["k"] == content


def test_whole_file_retry_also_exhausted_fails_transaction_cleanly(tmp_path):
    src, _content = _write_source(tmp_path)
    client = _FakeMultipartClient()
    client.faults_for_part(
        2,
        [_client_error(524)]
        * (app_storage.MULTIPART_PART_RETRY_ATTEMPTS * app_storage.MULTIPART_WHOLE_FILE_RETRY_ATTEMPTS),
    )
    storage = _make_storage(client)

    with pytest.raises(RuntimeError, match="failed after"):
        storage.put_object_from_file("k", src)

    assert len(client.aborted_upload_ids) == app_storage.MULTIPART_WHOLE_FILE_RETRY_ATTEMPTS
    assert "k" not in client.objects, "never completed, nothing to mark UPLOADED"


def test_permanent_upload_part_error_is_not_retried_at_any_level(tmp_path):
    src, _content = _write_source(tmp_path)
    client = _FakeMultipartClient()
    client.faults_for_part(1, [_client_error(403, code="AccessDenied")])
    storage = _make_storage(client)

    with pytest.raises(ClientError):
        storage.put_object_from_file("k", src)

    assert len(client.upload_part_calls) == 1, "no pointless transient retry loop"
    assert len(client.create_calls) == 1, "no whole-file retry either for a permanent error"
    assert len(client.aborted_upload_ids) == 1, "still cleans up the one open upload"


def test_complete_multipart_invalid_part_triggers_whole_file_retry_with_new_upload_id(tmp_path):
    src, content = _write_source(tmp_path)
    client = _FakeMultipartClient()
    client.faults_for_complete([_client_error(400, code="InvalidPart", operation="CompleteMultipartUpload")])
    storage = _make_storage(client)

    storage.put_object_from_file("k", src)

    assert len(client.create_calls) == 2
    assert len(client.aborted_upload_ids) == 1
    assert client.objects["k"] == content


def test_head_object_after_complete_retries_transient_403_then_succeeds(tmp_path):
    src, content = _write_source(tmp_path)
    client = _FakeMultipartClient()
    storage = _make_storage(client)

    real_head_object = client.head_object
    calls = {"n": 0}

    def flaky_head_object(Bucket, Key):
        calls["n"] += 1
        if calls["n"] == 1:
            raise _client_error(403, code="Forbidden", operation="HeadObject")
        return real_head_object(Bucket, Key)

    storage.put_object_from_file("k", src)  # populates client.objects["k"] first
    client.head_object = flaky_head_object

    # Exercise the retry helper directly against the now-flaky head_object.
    storage._head_object_after_complete("k", len(content))
    assert calls["n"] == 2

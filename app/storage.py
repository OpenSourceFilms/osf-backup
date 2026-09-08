"""S3-compatible wrapper for RunPod network volume storage."""

from __future__ import annotations

import logging
import os
import random
import time
from typing import Any

from pathlib import Path

import boto3
from boto3.s3.transfer import TransferConfig
from botocore.config import Config
from botocore.exceptions import ClientError
from botocore.exceptions import ConnectionError as BotoConnectionError
from botocore.exceptions import HTTPClientError

logger = logging.getLogger(__name__)

PathLike = str | os.PathLike

# Designated, already-established location for supplied S3 credentials (see
# HANDOFF.md / config/policy.yaml hard_exclude) -- used only when the
# environment doesn't already have them. Never logged.
CREDENTIALS_ENV_PATH = Path(__file__).resolve().parent.parent / "config" / "credentials.env"

DEFAULT_ENDPOINT_URL = "https://s3api-eu-ro-1.runpod.io"
DEFAULT_REGION_NAME = "EU-RO-1"
DEFAULT_RETRY_MODE = "standard"
DEFAULT_MAX_ATTEMPTS = 10
DEFAULT_READ_TIMEOUT = 300
DEFAULT_MULTIPART_THRESHOLD = 128 * 1024 * 1024  # 128MiB
DEFAULT_MULTIPART_CHUNKSIZE = 128 * 1024 * 1024  # 128MiB
DEFAULT_MAX_CONCURRENCY = 4

# RunPod hard requirement: objects above 500MB must use multipart upload.
HARD_MULTIPART_UPPER_BYTES = 500 * 1000 * 1000

# ---------------------------------------------------------------------------
# Three real RunPod S3 gateway behaviours, confirmed live 2026-08-19/20
# during the backup-2026-08-19 seed (not speculative defensive code -- each
# was reproduced with real bucket evidence: no orphaned multipart uploads,
# no residue, local blob sha256 verified intact each time):
#
#   1. UploadPart can fail transient (HTTP 524 gateway timeout observed;
#      also budgeted for 408/429/500/502/503/504 and connection-level
#      resets/timeouts) mid-transfer on a large file. A same-size (426MB,
#      4-part) synthetic upload against a disposable prefix succeeded
#      cleanly moments later, and the multipart protocol itself round-trips
#      correctly (see tests/test_s3_transfer_paths.py) -- this reads as
#      gateway flakiness correlated with transfer duration (real transfers
#      at this size take ~4+ minutes), not malformed multipart state or a
#      bad source file. Recovery: retry the SAME part (same upload_id, same
#      part_number, same bytes) with bounded exponential backoff. Only a
#      genuinely transient failure is retried this way -- see
#      _is_transient_storage_error; a permanent error (bad credentials,
#      AccessDenied, ...) propagates immediately with no retry at any level.
#
#   2. CompleteMultipartUpload can raise InvalidPart ("part N is missing")
#      even when every UploadPart call actually succeeded -- the gateway
#      loses track of an already-acknowledged part before Complete sees it.
#      No partial-resume is possible once this happens; recovery is a fresh
#      whole-file multipart attempt under a brand new upload_id.
#
#   3. HeadObject issued immediately after a successful upload can 403 once
#      before succeeding ~seconds later (list_prefix shows the object right
#      away; HeadObject briefly doesn't). Recovery: bounded HeadObject retry
#      only, immediately after the upload it's verifying.
#
# None of these are botocore-retryable via Config(retries={"mode":
# "standard"}) -- 403/InvalidPart aren't retryable status/error codes at
# all, and 524 is a Cloudflare-origin-timeout status botocore doesn't
# recognise as transient. All three are handled here, in the storage layer,
# so callers of put_object_from_file only ever see a verified-successful
# upload or a terminal exception -- Runner does not need to know RunPod's
# transport-level quirks (see app/runner.py's _upload, which used to
# duplicate #3's retry itself; that call site was simplified once this
# method started guaranteeing verification internally).
# ---------------------------------------------------------------------------

MULTIPART_PART_RETRY_ATTEMPTS = 4
MULTIPART_PART_RETRY_BASE_DELAY_SECONDS = 2.0
MULTIPART_PART_RETRY_BACKOFF_MULTIPLIER = 2.5
MULTIPART_PART_RETRY_JITTER_SECONDS = 1.0

MULTIPART_WHOLE_FILE_RETRY_ATTEMPTS = 2
MULTIPART_WHOLE_FILE_RETRY_DELAY_SECONDS = 3.0

HEAD_OBJECT_RETRY_ATTEMPTS = 3
HEAD_OBJECT_RETRY_DELAY_SECONDS = 2.0

# Deliberately narrow allowlist, not a denylist -- anything not explicitly
# matched here (AccessDenied, InvalidAccessKeyId, SignatureDoesNotMatch,
# NoSuchBucket, ordinary malformed-request errors, ...) is treated as
# permanent and propagates without any retry.
TRANSIENT_HTTP_STATUS_CODES = frozenset({408, 429, 500, 502, 503, 504, 524})
TRANSIENT_ERROR_CODES = frozenset({
    "RequestTimeout",
    "RequestTimeoutException",
    "SlowDown",
    "InternalError",
    "ServiceUnavailable",
    "Throttling",
    "ThrottlingException",
})

S3_NOT_FOUND_CODES = frozenset({"404", "NoSuchKey", "NotFound"})

__all__ = ["RunPodStorage", "MissingCredentials", "ListPrefixLimitExceeded"]


class MissingCredentials(RuntimeError):
    """Raised when required S3 credentials are not present in the environment."""


class ListPrefixLimitExceeded(RuntimeError):
    """Raised when a list_objects_v2 response is truncated by the max_keys guard."""


class _TransientPartRetriesExhausted(Exception):
    """Internal: one UploadPart kept hitting transient gateway errors until
    its bounded per-part retry budget ran out. Caught in _upload_all_parts
    and converted to _MultipartRetryableFailure -- distinct from a
    permanent error (e.g. AccessDenied), which propagates immediately and
    is never wrapped into a whole-file retry."""


class _MultipartRetryableFailure(Exception):
    """Internal: this whole multipart attempt should be retried under a
    fresh upload_id -- either a part exhausted its transient retry budget,
    or CompleteMultipartUpload hit the RunPod InvalidPart gateway quirk.
    Caught only in _multipart_upload's whole-file retry loop; any other
    exception propagates immediately with no whole-file retry."""


class RunPodStorage:
    """Boto3 S3 wrapper for a RunPod S3-compatible network volume bucket."""

    def __init__(
        self,
        bucket: str,
        *,
        retry_mode: str = DEFAULT_RETRY_MODE,
        max_attempts: int = DEFAULT_MAX_ATTEMPTS,
        read_timeout: int = DEFAULT_READ_TIMEOUT,
        multipart_threshold: int = DEFAULT_MULTIPART_THRESHOLD,
        multipart_chunksize: int = DEFAULT_MULTIPART_CHUNKSIZE,
        max_concurrency: int = DEFAULT_MAX_CONCURRENCY,
    ) -> None:
        self.bucket = bucket

        access_key, secret_key = self._load_credentials()

        logger.debug(
            "Initializing RunPod S3 client for bucket=%s endpoint=%s region=%s",
            bucket,
            DEFAULT_ENDPOINT_URL,
            DEFAULT_REGION_NAME,
        )

        self.client = boto3.client(
            "s3",
            aws_access_key_id=access_key,
            aws_secret_access_key=secret_key,
            endpoint_url=DEFAULT_ENDPOINT_URL,
            region_name=DEFAULT_REGION_NAME,
            config=Config(
                retries={
                    "mode": retry_mode,
                    "max_attempts": max_attempts,
                },
                read_timeout=read_timeout,
            ),
        )

        self.transfer_config = TransferConfig(
            multipart_threshold=multipart_threshold,
            multipart_chunksize=multipart_chunksize,
            max_concurrency=max_concurrency,
        )

    @staticmethod
    def _load_credentials() -> tuple[str, str]:
        """Load S3 credentials from the environment, falling back to the
        project's designated credentials file (``CREDENTIALS_ENV_PATH``) for
        whichever of the two are not already set in the environment. Values
        are never logged.
        """
        required = ("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY")
        values = {name: os.environ.get(name) for name in required}
        missing = [name for name, value in values.items() if not value]

        if missing:
            file_values = RunPodStorage._read_dotenv(CREDENTIALS_ENV_PATH)
            for name in missing:
                if file_values.get(name):
                    values[name] = file_values[name]
            missing = [name for name, value in values.items() if not value]

        if missing:
            raise MissingCredentials(
                f"Missing required credential(s): {', '.join(missing)}. Set them as "
                f"environment variables, or in {CREDENTIALS_ENV_PATH}."
            )
        return values["AWS_ACCESS_KEY_ID"], values["AWS_SECRET_ACCESS_KEY"]

    @staticmethod
    def _read_dotenv(path: "Path") -> dict[str, str]:
        """Parse simple KEY=value lines from a dotenv-style file. Never logs
        or returns anything about lines it skips; missing file is not an
        error here (the environment-variable path may be sufficient)."""
        values: dict[str, str] = {}
        try:
            raw = path.read_text(encoding="utf-8")
        except OSError:
            return values
        for line in raw.splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            values[key.strip()] = value.strip().strip('"').strip("'")
        return values

    def _multipart_required(self, file_size: int) -> bool:
        """Always at least as aggressive as RunPod's hard 500MB rule, even
        if multipart_threshold were ever reconfigured above it."""
        effective_threshold = min(self.transfer_config.multipart_threshold, HARD_MULTIPART_UPPER_BYTES)
        return file_size > effective_threshold

    @staticmethod
    def _is_transient_storage_error(exc: BaseException) -> bool:
        if isinstance(exc, (BotoConnectionError, HTTPClientError)):
            return True
        if isinstance(exc, ClientError):
            status = exc.response.get("ResponseMetadata", {}).get("HTTPStatusCode")
            code = exc.response.get("Error", {}).get("Code")
            return status in TRANSIENT_HTTP_STATUS_CODES or code in TRANSIENT_ERROR_CODES
        return False

    @staticmethod
    def _part_retry_backoff_delay(attempt: int) -> float:
        base = MULTIPART_PART_RETRY_BASE_DELAY_SECONDS * (
            MULTIPART_PART_RETRY_BACKOFF_MULTIPLIER**attempt
        )
        return base + random.uniform(0, MULTIPART_PART_RETRY_JITTER_SECONDS)

    def put_object_from_file(self, key: str, path: PathLike) -> None:
        """Upload path, returning only once the object is confirmed present
        at the expected size -- callers never need a separate post-upload
        HeadObject check (see the module docstring block above for the
        three gateway quirks this absorbs)."""
        path_str = os.fsdecode(os.fspath(path))
        file_size = os.path.getsize(path_str)

        logger.debug(
            "Uploading object bucket=%s key=%s file=%s size=%d",
            self.bucket, key, path_str, file_size,
        )

        if self._multipart_required(file_size):
            self._multipart_upload(path_str, key)
        else:
            self.client.upload_file(
                Filename=path_str,
                Bucket=self.bucket,
                Key=key,
                Config=self.transfer_config,
            )

        self._head_object_after_complete(key, file_size)

    def _multipart_upload(self, path_str: str, key: str) -> None:
        last_exc: Exception | None = None
        for whole_attempt in range(MULTIPART_WHOLE_FILE_RETRY_ATTEMPTS):
            try:
                self._run_one_multipart_attempt(path_str, key)
                return
            except _MultipartRetryableFailure as exc:
                last_exc = exc
                is_last = whole_attempt == MULTIPART_WHOLE_FILE_RETRY_ATTEMPTS - 1
                if is_last:
                    raise RuntimeError(
                        f"Multipart upload for key={key} failed after "
                        f"{MULTIPART_WHOLE_FILE_RETRY_ATTEMPTS} whole-file "
                        f"attempts: {exc}"
                    ) from exc
                logger.warning(
                    "Multipart upload for key=%s needs a fresh attempt "
                    "(%d/%d) under a new upload_id: %s",
                    key, whole_attempt + 1, MULTIPART_WHOLE_FILE_RETRY_ATTEMPTS, exc,
                )
                time.sleep(MULTIPART_WHOLE_FILE_RETRY_DELAY_SECONDS)
        assert last_exc is not None  # loop always returns or raises above

    def _run_one_multipart_attempt(self, path_str: str, key: str) -> None:
        upload_id = self.client.create_multipart_upload(Bucket=self.bucket, Key=key)["UploadId"]
        try:
            parts = self._upload_all_parts(path_str, key, upload_id)
            self._complete_multipart_upload(key, upload_id, parts)
        except BaseException:
            self._safe_abort_multipart_upload(key, upload_id)
            raise

    def _upload_all_parts(self, path_str: str, key: str, upload_id: str) -> list[dict[str, Any]]:
        parts: list[dict[str, Any]] = []
        chunk_size = self.transfer_config.multipart_chunksize
        with open(path_str, "rb") as fh:
            part_number = 1
            while True:
                data = fh.read(chunk_size)
                if not data:
                    break
                try:
                    parts.append(self._upload_part_with_retry(key, upload_id, part_number, data))
                except _TransientPartRetriesExhausted as exc:
                    raise _MultipartRetryableFailure(
                        f"part {part_number} exhausted transient retries: {exc}"
                    ) from exc
                part_number += 1
        return parts

    def _upload_part_with_retry(
        self, key: str, upload_id: str, part_number: int, data: bytes
    ) -> dict[str, Any]:
        for attempt in range(MULTIPART_PART_RETRY_ATTEMPTS):
            try:
                response = self.client.upload_part(
                    Bucket=self.bucket,
                    Key=key,
                    UploadId=upload_id,
                    PartNumber=part_number,
                    Body=data,
                )
                return {"ETag": response["ETag"], "PartNumber": part_number}
            except Exception as exc:
                if not self._is_transient_storage_error(exc):
                    raise
                if attempt == MULTIPART_PART_RETRY_ATTEMPTS - 1:
                    raise _TransientPartRetriesExhausted(str(exc)) from exc
                delay = self._part_retry_backoff_delay(attempt)
                logger.warning(
                    "UploadPart transient gateway failure key=%s part=%d "
                    "(attempt %d/%d), retrying same part/upload_id in "
                    "%.1fs: %s",
                    key, part_number, attempt + 1, MULTIPART_PART_RETRY_ATTEMPTS, delay, exc,
                )
                time.sleep(delay)
        raise AssertionError("unreachable")  # loop always returns or raises above

    def _complete_multipart_upload(
        self, key: str, upload_id: str, parts: list[dict[str, Any]]
    ) -> None:
        try:
            self.client.complete_multipart_upload(
                Bucket=self.bucket,
                Key=key,
                UploadId=upload_id,
                MultipartUpload={"Parts": parts},
            )
        except ClientError as exc:
            if exc.response.get("Error", {}).get("Code") == "InvalidPart":
                raise _MultipartRetryableFailure(f"InvalidPart on complete: {exc}") from exc
            raise

    def _safe_abort_multipart_upload(self, key: str, upload_id: str) -> None:
        try:
            self.client.abort_multipart_upload(Bucket=self.bucket, Key=key, UploadId=upload_id)
        except Exception as exc:
            logger.warning(
                "AbortMultipartUpload failed for key=%s upload_id=%s "
                "(leaving for provider-side GC): %s", key, upload_id, exc,
            )

    def _head_object_after_complete(self, key: str, expected_size: int) -> None:
        last_exc: Exception | None = None
        size: int | None = None
        for attempt in range(HEAD_OBJECT_RETRY_ATTEMPTS):
            try:
                size = self.head_object(key)
                break
            except Exception as exc:
                last_exc = exc
                if attempt < HEAD_OBJECT_RETRY_ATTEMPTS - 1:
                    logger.warning(
                        "HeadObject after upload failed for %s (attempt "
                        "%d/%d), retrying: %s",
                        key, attempt + 1, HEAD_OBJECT_RETRY_ATTEMPTS, exc,
                    )
                    time.sleep(HEAD_OBJECT_RETRY_DELAY_SECONDS)
        else:
            assert last_exc is not None
            raise last_exc

        if size != expected_size:
            raise RuntimeError(
                f"HeadObject size mismatch after upload for key={key}: "
                f"expected {expected_size}, got {size!r}"
            )

    def head_object(self, key: str) -> int | None:
        """Return object size in bytes, or None if the object does not exist."""
        try:
            response = self.client.head_object(Bucket=self.bucket, Key=key)
            return int(response["ContentLength"])
        except ClientError as exc:
            status_code = exc.response.get("ResponseMetadata", {}).get("HTTPStatusCode")
            error_code = exc.response.get("Error", {}).get("Code")

            if status_code == 404 or error_code in S3_NOT_FOUND_CODES:
                return None

            logger.warning("head_object failed bucket=%s key=%s: %s", self.bucket, key, exc)
            raise

    def get_object_to_file(self, key: str, dest: PathLike) -> None:
        dest_str = os.fsdecode(os.fspath(dest))

        logger.debug("Downloading object bucket=%s key=%s dest=%s", self.bucket, key, dest_str)
        self.client.download_file(
            Bucket=self.bucket,
            Key=key,
            Filename=dest_str,
            Config=self.transfer_config,
        )

    def get_object_bytes(self, key: str) -> bytes:
        logger.debug("Reading object bytes bucket=%s key=%s", self.bucket, key)

        response = self.client.get_object(Bucket=self.bucket, Key=key)
        try:
            return response["Body"].read()
        finally:
            response["Body"].close()

    def put_object_bytes(self, key: str, data: bytes) -> None:
        logger.debug(
            "Uploading object bytes bucket=%s key=%s bytes=%d", self.bucket, key, len(data)
        )
        self.client.put_object(Bucket=self.bucket, Key=key, Body=data)

    def delete_object(self, key: str) -> None:
        # RunPod does not support bulk DeleteObjects; always use single-key DeleteObject.
        logger.debug("Deleting object bucket=%s key=%s", self.bucket, key)
        self.client.delete_object(Bucket=self.bucket, Key=key)

    def exists(self, key: str) -> bool:
        return self.head_object(key) is not None

    def bucket_accessible(self) -> bool:
        """Prove real S3 auth + bucket visibility with one bounded call.

        Deliberately NOT built on list_prefix(): that helper is a bounded
        *single-page* accessor whose contract is to raise
        ListPrefixLimitExceeded when more keys exist than were asked for.
        Using it as a reachability probe (``list_prefix("", max_keys=1)``)
        inverts its meaning -- it raised on every call from the moment this
        bucket held more than one object, so `backupctl doctor` reported
        "S3 authentication or bucket visibility failed" against a perfectly
        healthy bucket from 2026-08-19 until it was found on 2026-08-23,
        immediately before the seed.

        Truncation is the *expected* result for a healthy populated bucket,
        so this asks list_objects_v2 directly and treats any successful
        response as proof. Credential/permission/bucket errors still raise
        (botocore ClientError), which is what the caller must surface.
        """
        self.client.list_objects_v2(Bucket=self.bucket, MaxKeys=1)
        return True

    def list_prefix(self, prefix: str, max_keys: int) -> list[dict[str, Any]]:
        """List one non-paginated page of objects under a prefix.

        Raises ListPrefixLimitExceeded if S3 reports that more keys exist than
        max_keys allows. This method intentionally does not recurse or paginate.
        """
        if max_keys <= 0:
            raise ValueError("max_keys must be positive")

        logger.debug(
            "Listing prefix bucket=%s prefix=%s max_keys=%d",
            self.bucket,
            prefix,
            max_keys,
        )

        response = self.client.list_objects_v2(
            Bucket=self.bucket,
            Prefix=prefix,
            MaxKeys=max_keys,
        )

        if response.get("IsTruncated"):
            raise ListPrefixLimitExceeded(
                f"list_objects_v2 for prefix {prefix!r} exceeded max_keys={max_keys}"
            )

        return response.get("Contents", [])

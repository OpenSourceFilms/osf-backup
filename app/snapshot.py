"""Backup snapshot manifest builder, serializer, committer, and loader."""

from __future__ import annotations

import hashlib
import json
import logging
import os
import platform
import shutil
import socket
import subprocess
import sys
import tempfile
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Optional, Sequence, Union
from urllib.parse import urlsplit

import zstandard as zstd

logger = logging.getLogger(__name__)

SOFTWARE_VERSION = "1.0.0"
FORMAT_VERSION = "1.0"

EntryType = str  # "file" | "dir" | "symlink"
ClassificationResult = tuple[str, Optional[str], Optional[str]]
Classifier = Callable[[str, EntryType], ClassificationResult]
ManifestUploader = Callable[[bytes, str], str]


def _now_iso() -> str:
    """Return current UTC time as an ISO-8601 string with microseconds."""
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def _default_classifier(rel_path: str, entry_type: EntryType) -> ClassificationResult:
    return "unclassified", None, None


def _hash_file(file_path: str) -> str:
    """Compute the SHA-256 digest of a regular file."""
    digest = hashlib.sha256()
    with open(file_path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _policy_hash(policy: Any) -> str:
    """Return a stable hash for a policy object/dict without reading environment."""
    if policy is None:
        return hashlib.sha256(b"null").hexdigest()

    if hasattr(policy, "hash") and callable(policy.hash):
        try:
            return str(policy.hash())
        except Exception:
            logger.debug("Policy.hash() failed; falling back to serialization hash", exc_info=True)

    try:
        policy_bytes = json.dumps(policy, sort_keys=True, default=str).encode("utf-8")
    except (TypeError, ValueError):
        policy_bytes = repr(policy).encode("utf-8")

    return hashlib.sha256(policy_bytes).hexdigest()


def _policy_to_dict(policy: Any) -> dict:
    """Convert a policy object to a JSON-friendly dictionary."""
    if policy is None:
        return {}

    if hasattr(policy, "to_dict") and callable(policy.to_dict):
        try:
            result = policy.to_dict()
            if isinstance(result, dict):
                return result
        except Exception:
            logger.debug("Policy.to_dict() failed; falling back", exc_info=True)

    if isinstance(policy, dict):
        return dict(policy)

    try:
        return json.loads(json.dumps(policy, sort_keys=True, default=str))
    except (TypeError, ValueError):
        return {"policy_repr": repr(policy)}


def _scan_directory(
    source_root: str,
    classifier: Classifier,
    known_hashes: Optional[set[str]],
    warnings: list[str],
) -> tuple[list["ManifestEntry"], int, int, int, dict[str, int]]:
    """Walk source_root, classify entries, hash files, and collect manifest entries."""
    root = Path(source_root)
    entries: list[ManifestEntry] = []
    skipped_counts: dict[str, int] = {}
    seen_hashes = set(known_hashes) if known_hashes else set()

    selected_bytes = 0
    deduplicated_bytes = 0

    stack: list[Path] = [root]

    while stack:
        current_dir = stack.pop()

        try:
            dir_entries = sorted(os.scandir(current_dir), key=lambda e: e.name)
        except OSError as exc:
            msg = f"Could not scan directory {current_dir}: {exc}"
            warnings.append(msg)
            logger.warning(msg)
            skipped_counts["unscannable_dir"] = skipped_counts.get("unscannable_dir", 0) + 1
            continue

        for de in dir_entries:
            full_path = de.path
            rel_path = os.path.relpath(full_path, root).replace(os.sep, "/")

            try:
                st = de.stat(follow_symlinks=False)
            except OSError as exc:
                msg = f"Could not stat {rel_path}: {exc}"
                warnings.append(msg)
                logger.warning(msg)
                skipped_counts["unstatable"] = skipped_counts.get("unstatable", 0) + 1
                continue

            if de.is_symlink():
                entry_type: EntryType = "symlink"
                try:
                    symlink_target = os.readlink(full_path)
                except OSError as exc:
                    msg = f"Could not read symlink target for {rel_path}: {exc}"
                    warnings.append(msg)
                    logger.warning(msg)
                    skipped_counts["unreadable_symlink"] = skipped_counts.get("unreadable_symlink", 0) + 1
                    continue
                sha256 = hashlib.sha256(symlink_target.encode("utf-8")).hexdigest()
            elif de.is_dir(follow_symlinks=False):
                entry_type = "dir"
                symlink_target = None
                sha256 = None
            elif de.is_file(follow_symlinks=False):
                entry_type = "file"
                symlink_target = None
                try:
                    sha256 = _hash_file(full_path)
                except OSError as exc:
                    msg = f"Could not hash file {rel_path}: {exc}"
                    warnings.append(msg)
                    logger.warning(msg)
                    skipped_counts["unreadable_file"] = skipped_counts.get("unreadable_file", 0) + 1
                    continue
            else:
                msg = f"Skipping unsupported special file: {rel_path}"
                warnings.append(msg)
                logger.warning(msg)
                skipped_counts["unsupported_file_type"] = skipped_counts.get("unsupported_file_type", 0) + 1
                continue

            classification, rule_name, skip_reason = classifier(rel_path, entry_type)
            classification = classification or "unclassified"

            if skip_reason:
                skipped_counts[skip_reason] = skipped_counts.get(skip_reason, 0) + 1
                logger.info("Skipped %s by rule %s: %s", rel_path, rule_name, skip_reason)
                continue

            entries.append(
                ManifestEntry(
                    path=rel_path,
                    entry_type=entry_type,
                    sha256=sha256,
                    size=st.st_size,
                    mtime_ns=st.st_mtime_ns,
                    mode=st.st_mode,
                    symlink_target=symlink_target,
                    classification=classification,
                    policy_rule=rule_name,
                )
            )

            if entry_type == "file" and sha256:
                selected_bytes += st.st_size
                if sha256 in seen_hashes:
                    deduplicated_bytes += st.st_size
                else:
                    seen_hashes.add(sha256)

            if entry_type == "dir":
                stack.append(Path(full_path))

    entries.sort(key=lambda e: e.path)
    uploaded_bytes = selected_bytes - deduplicated_bytes
    return entries, selected_bytes, uploaded_bytes, deduplicated_bytes, skipped_counts


def _pip_freeze(warnings: list[str]) -> list[str]:
    """Collect pip freeze output. Does not read environment variables."""
    cmd = [sys.executable, "-m", "pip", "freeze", "--disable-pip-version-check"]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=30, check=False)
    except (OSError, subprocess.SubprocessError) as exc:
        msg = f"pip freeze failed: {exc}"
        warnings.append(msg)
        logger.warning(msg)
        return []

    if proc.returncode != 0:
        msg = f"pip freeze exited with status {proc.returncode}: {proc.stderr.strip()[:200]}"
        warnings.append(msg)
        logger.warning(msg)
        return []

    return [line.strip() for line in proc.stdout.splitlines() if line.strip()]


def _command_versions(commands: Sequence[str], warnings: list[str]) -> dict[str, str]:
    """Collect the first line of --version output for requested commands."""
    versions: dict[str, str] = {}
    for command in commands:
        command_path = shutil.which(command)
        if not command_path:
            msg = f"Command not found for version inventory: {command}"
            warnings.append(msg)
            logger.warning(msg)
            continue

        try:
            proc = subprocess.run(
                [command_path, "--version"],
                capture_output=True,
                text=True,
                timeout=10,
                check=False,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            msg = f"{command} --version failed: {exc}"
            warnings.append(msg)
            logger.warning(msg)
            continue

        if proc.returncode != 0:
            msg = f"{command} --version exited with status {proc.returncode}: {proc.stderr.strip()[:200]}"
            warnings.append(msg)
            logger.warning(msg)
            continue

        output = (proc.stdout or proc.stderr).strip()
        first_line = output.splitlines()[0].strip() if output else ""
        versions[command] = first_line[:200]

    return versions


def _sanitize_git_remote(remote: str) -> str:
    """Redact embedded credentials in git remote URLs."""
    if not remote:
        return remote

    if "://" in remote:
        try:
            parts = urlsplit(remote)
            if parts.username or parts.password:
                host = parts.hostname or ""
                if parts.port:
                    host = f"{host}:{parts.port}"
                path = parts.path or ""
                query = f"?{parts.query}" if parts.query else ""
                fragment = f"#{parts.fragment}" if parts.fragment else ""
                return f"{parts.scheme}://{host}{path}{query}{fragment}"
        except ValueError:
            pass
        return remote

    if "@" in remote and ":" in remote:
        user_part, rest = remote.split("@", 1)
        if "/" not in user_part and "\\" not in user_part:
            return rest

    return remote


def _git_remotes(repo_path: Path, warnings: list[str]) -> dict[str, str]:
    """Return sanitized git remote URLs for a repository."""
    try:
        proc = subprocess.run(
            ["git", "-C", str(repo_path), "remote"],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        msg = f"git remote list failed for {repo_path}: {exc}"
        warnings.append(msg)
        logger.warning(msg)
        return {}

    if proc.returncode != 0:
        msg = f"git remote list for {repo_path} exited with status {proc.returncode}: {proc.stderr.strip()[:200]}"
        warnings.append(msg)
        logger.warning(msg)
        return {}

    remotes: dict[str, str] = {}
    for name in [line.strip() for line in proc.stdout.splitlines() if line.strip()]:
        try:
            url_proc = subprocess.run(
                ["git", "-C", str(repo_path), "remote", "get-url", name],
                capture_output=True,
                text=True,
                timeout=10,
                check=False,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            msg = f"git remote get-url failed for {repo_path}, remote {name}: {exc}"
            warnings.append(msg)
            logger.warning(msg)
            continue

        if url_proc.returncode != 0:
            msg = f"git remote get-url for {repo_path}, remote {name} exited with status {url_proc.returncode}: {url_proc.stderr.strip()[:200]}"
            warnings.append(msg)
            logger.warning(msg)
            continue

        remotes[name] = _sanitize_git_remote(url_proc.stdout.strip())

    return remotes


def _git_commit(repo_path: Path, warnings: list[str]) -> Optional[str]:
    """Return the current HEAD commit hash for a git repository."""
    try:
        proc = subprocess.run(
            ["git", "-C", str(repo_path), "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        msg = f"git rev-parse HEAD failed for {repo_path}: {exc}"
        warnings.append(msg)
        logger.warning(msg)
        return None

    if proc.returncode != 0:
        msg = f"git rev-parse HEAD for {repo_path} exited with status {proc.returncode}: {proc.stderr.strip()[:200]}"
        warnings.append(msg)
        logger.warning(msg)
        return None

    commit = proc.stdout.strip()
    return commit or None


def _git_repositories(git_repo_paths: Sequence[Union[str, Path]], warnings: list[str]) -> list[dict]:
    """Collect git remotes and commits for specified repository paths."""
    repos: list[dict] = []
    for p in git_repo_paths:
        repo_path = Path(p).expanduser().resolve()

        if not repo_path.is_dir():
            msg = f"Git repo path is not a directory: {repo_path}"
            warnings.append(msg)
            logger.warning(msg)
            continue

        if not (repo_path / ".git").exists():
            msg = f"Path is not a git repository: {repo_path}"
            warnings.append(msg)
            logger.warning(msg)
            continue

        remotes = _git_remotes(repo_path, warnings)
        commit = _git_commit(repo_path, warnings)
        repos.append({"path": str(repo_path), "remotes": remotes, "commit": commit})

    return repos


@dataclass
class ManifestEntry:
    """Per-entry backup manifest record."""

    path: str
    entry_type: EntryType
    sha256: Optional[str]
    size: int
    mtime_ns: int
    mode: int
    symlink_target: Optional[str] = None
    classification: str = "unclassified"
    policy_rule: Optional[str] = None

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class SnapshotMetadata:
    """Metadata for one snapshot manifest."""

    snapshot_date: str
    start_time: str
    end_time: str
    hostname: str
    pod_identity: str
    software_version: str
    policy_hash: str
    source_root: str
    selected_bytes: int = 0
    uploaded_bytes: int = 0
    deduplicated_bytes: int = 0
    skipped_counts_by_reason: dict[str, int] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class MachineInventory:
    """Machine reconstruction inventory.

    This intentionally never reads or records environment variable values.
    """

    software_version: str = SOFTWARE_VERSION
    policy: dict = field(default_factory=dict)
    os_version: str = "unknown"
    python_version: str = "unknown"
    pip_freeze: list[str] = field(default_factory=list)
    command_versions: dict[str, str] = field(default_factory=dict)
    git_repositories: list[dict] = field(default_factory=list)
    excluded_model_checkpoint_inventory: list[dict] = field(default_factory=list)
    excluded_top_level_dirs: list[str] = field(default_factory=list)
    principal_footage_inventory: list[dict] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class SnapshotManifest:
    """Snapshot manifest containing metadata, entries, and machine inventory."""

    format_version: str
    metadata: SnapshotMetadata
    entries: list[ManifestEntry]
    machine_inventory: MachineInventory

    @property
    def snapshot_id(self) -> str:
        m = self.metadata
        raw = "|".join((m.snapshot_date, m.hostname, m.policy_hash, m.start_time))
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]

    def to_dict(self) -> dict:
        return {
            "format_version": self.format_version,
            "snapshot_id": self.snapshot_id,
            "metadata": self.metadata.to_dict(),
            "entries": [entry.to_dict() for entry in self.entries],
            "machine_inventory": self.machine_inventory.to_dict(),
        }

    def serialize(self) -> bytes:
        """Serialize to JSON and compress with zstandard."""
        raw = json.dumps(self.to_dict(), indent=2, sort_keys=True).encode("utf-8")
        return zstd.ZstdCompressor().compress(raw)

    @classmethod
    def from_dict(cls, data: dict) -> "SnapshotManifest":
        return cls(
            format_version=data.get("format_version", FORMAT_VERSION),
            metadata=SnapshotMetadata(**data["metadata"]),
            entries=[ManifestEntry(**entry) for entry in data.get("entries", [])],
            machine_inventory=MachineInventory(**data.get("machine_inventory", {})),
        )

    @classmethod
    def from_compressed(cls, data: bytes) -> "SnapshotManifest":
        """Load and parse a compressed snapshot manifest for restore."""
        return cls.from_dict(load(data))

    def commit(
        self,
        uploader: ManifestUploader,
        meta_store: Optional["MetaStore"] = None,
    ) -> str:
        """Upload compressed manifest, then update meta/latest.json and meta/snapshot-index.json."""
        if uploader is None:
            raise ValueError("uploader must be provided")

        if meta_store is None:
            meta_store = MetaStore()

        if not self.metadata.end_time:
            self.metadata.end_time = _now_iso()

        payload = self.serialize()
        manifest_location = uploader(payload, self.snapshot_id)
        if not manifest_location:
            raise ValueError("uploader returned an empty manifest location")

        committed_at = _now_iso()
        meta_store.update_latest(
            snapshot_id=self.snapshot_id,
            manifest_location=manifest_location,
            committed_at=committed_at,
            metadata=self.metadata,
        )
        meta_store.update_index(
            snapshot_id=self.snapshot_id,
            manifest_location=manifest_location,
            committed_at=committed_at,
            metadata=self.metadata,
        )

        logger.info("Committed snapshot %s to %s", self.snapshot_id, manifest_location)
        return manifest_location


class MetaStore:
    """Minimal idempotent file-based store for meta/latest.json and meta/snapshot-index.json."""

    def __init__(self, base_dir: Union[str, Path] = "meta"):
        self.base_dir = Path(base_dir)
        self.latest_path = self.base_dir / "latest.json"
        self.index_path = self.base_dir / "snapshot-index.json"

    def update_latest(
        self,
        snapshot_id: str,
        manifest_location: str,
        committed_at: str,
        metadata: SnapshotMetadata,
    ) -> None:
        data = {
            "latest_snapshot_id": snapshot_id,
            "latest_manifest_location": manifest_location,
            "committed_at": committed_at,
            "snapshot_date": metadata.snapshot_date,
            "hostname": metadata.hostname,
            "policy_hash": metadata.policy_hash,
        }
        self._atomic_write_json(self.latest_path, data)

    def update_index(
        self,
        snapshot_id: str,
        manifest_location: str,
        committed_at: str,
        metadata: SnapshotMetadata,
    ) -> None:
        existing = self._read_json(
            self.index_path,
            {"version": 1, "snapshots": []},
        )

        snapshots = existing.get("snapshots", [])
        if not isinstance(snapshots, list):
            logger.warning("snapshot-index.json snapshots field is invalid; resetting index")
            snapshots = []

        entry = {
            "snapshot_id": snapshot_id,
            "manifest_location": manifest_location,
            "committed_at": committed_at,
            "snapshot_date": metadata.snapshot_date,
            "hostname": metadata.hostname,
            "policy_hash": metadata.policy_hash,
        }

        snapshots = [s for s in snapshots if s.get("snapshot_id") != snapshot_id]
        snapshots.append(entry)
        snapshots.sort(key=lambda s: s.get("snapshot_date", ""))

        self._atomic_write_json(
            self.index_path,
            {"version": 1, "updated_at": committed_at, "snapshots": snapshots},
        )

    def _read_json(self, path: Path, default: Any) -> Any:
        try:
            with path.open("r", encoding="utf-8") as fh:
                return json.load(fh)
        except FileNotFoundError:
            return default
        except json.JSONDecodeError as exc:
            logger.warning("Could not decode %s: %s; starting from default", path, exc)
            return default

    def _atomic_write_json(self, path: Path, data: Any) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = path.parent / f".{path.name}.{uuid.uuid4().hex}.tmp"

        with tmp_path.open("w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=2, sort_keys=True)
            fh.flush()
            os.fsync(fh.fileno())

        tmp_path.replace(path)


def build_machine_inventory(
    policy: Any,
    *,
    git_repo_paths: Sequence[Union[str, Path]] = (),
    excluded_model_checkpoint_inventory: Sequence[dict] = (),
    excluded_top_level_dirs: Sequence[str] = (),
    principal_footage_inventory: Sequence[dict] = (),
    commands: Sequence[str] = ("python", "git", "rsync", "tar", "zstd"),
) -> tuple[MachineInventory, list[str]]:
    """Build the machine reconstruction inventory.

    This function never reads environment variable values.
    """
    warnings: list[str] = []

    os_version = platform.platform()
    python_version = platform.python_version()
    pip_freeze = _pip_freeze(warnings)
    command_versions = _command_versions(commands, warnings)
    git_repositories = _git_repositories(git_repo_paths, warnings)
    policy_dict = _policy_to_dict(policy)

    inventory = MachineInventory(
        os_version=os_version,
        python_version=python_version,
        pip_freeze=pip_freeze,
        command_versions=command_versions,
        git_repositories=git_repositories,
        excluded_model_checkpoint_inventory=list(excluded_model_checkpoint_inventory),
        excluded_top_level_dirs=list(excluded_top_level_dirs),
        principal_footage_inventory=list(principal_footage_inventory),
        policy=policy_dict,
        software_version=SOFTWARE_VERSION,
    )

    return inventory, warnings


def build_manifest(
    source_root: Union[str, os.PathLike],
    policy: Any,
    *,
    classifier: Optional[Classifier] = None,
    known_hashes: Optional[set[str]] = None,
    pod_identity: Optional[str] = None,
    git_repo_paths: Sequence[Union[str, Path]] = (),
    excluded_model_checkpoint_inventory: Sequence[dict] = (),
    excluded_top_level_dirs: Sequence[str] = (),
    principal_footage_inventory: Sequence[dict] = (),
    include_machine_inventory: bool = True,
) -> SnapshotManifest:
    """Build a snapshot manifest for source_root and optional machine inventory."""
    root = Path(source_root).expanduser().resolve()
    if not root.is_dir():
        raise FileNotFoundError(f"source root is not a directory: {root}")

    start_time = _now_iso()
    snapshot_date = start_time[:10]
    hostname = socket.gethostname()
    pod_identity = pod_identity or hostname

    warnings: list[str] = []

    if classifier is None:
        classifier = _default_classifier
        msg = "No classifier supplied; entries will be marked as unclassified"
        warnings.append(msg)
        logger.warning(msg)

    entries, selected_bytes, uploaded_bytes, deduplicated_bytes, skipped_counts = _scan_directory(
        str(root),
        classifier,
        known_hashes,
        warnings,
    )

    if include_machine_inventory:
        machine_inventory, inv_warnings = build_machine_inventory(
            policy,
            git_repo_paths=git_repo_paths,
            excluded_model_checkpoint_inventory=excluded_model_checkpoint_inventory,
            excluded_top_level_dirs=excluded_top_level_dirs,
            principal_footage_inventory=principal_footage_inventory,
        )
        warnings.extend(inv_warnings)
    else:
        machine_inventory = MachineInventory(
            os_version=platform.platform(),
            python_version=platform.python_version(),
            policy=_policy_to_dict(policy),
            software_version=SOFTWARE_VERSION,
            excluded_model_checkpoint_inventory=list(excluded_model_checkpoint_inventory),
            excluded_top_level_dirs=list(excluded_top_level_dirs),
            principal_footage_inventory=list(principal_footage_inventory),
        )

    end_time = _now_iso()

    metadata = SnapshotMetadata(
        snapshot_date=snapshot_date,
        start_time=start_time,
        end_time=end_time,
        hostname=hostname,
        pod_identity=pod_identity,
        software_version=SOFTWARE_VERSION,
        policy_hash=_policy_hash(policy),
        source_root=str(root),
        selected_bytes=selected_bytes,
        uploaded_bytes=uploaded_bytes,
        deduplicated_bytes=deduplicated_bytes,
        skipped_counts_by_reason=skipped_counts,
        warnings=warnings,
    )

    return SnapshotManifest(
        format_version=FORMAT_VERSION,
        metadata=metadata,
        entries=entries,
        machine_inventory=machine_inventory,
    )


def load(data: bytes) -> dict:
    """Decompress and parse a zstandard-compressed JSON snapshot manifest."""
    try:
        decompressed = zstd.ZstdDecompressor().decompress(data)
    except zstd.ZstdError as exc:
        raise ValueError(f"Snapshot manifest is not valid zstandard data: {exc}") from exc

    try:
        return json.loads(decompressed.decode("utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"Snapshot manifest is not valid JSON: {exc}") from exc


__all__ = [
    "FORMAT_VERSION",
    "SOFTWARE_VERSION",
    "ManifestEntry",
    "SnapshotMetadata",
    "MachineInventory",
    "SnapshotManifest",
    "MetaStore",
    "build_manifest",
    "build_machine_inventory",
    "load",
]

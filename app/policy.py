from __future__ import annotations

import hashlib
import json
import logging
import re
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Iterable

import yaml

LOGGER = logging.getLogger(__name__)


class Classification(str, Enum):
    """Backup classifications used by the policy engine.

    ``SQLITE_DATABASES`` is included because it is explicitly required by the
    configured path-rule categories, even though the prompt's class list omitted
    it.  Removal of that value is safe if a different mapping is desired.
    """

    CRITICAL = "CRITICAL"
    BTS = "BTS"
    PRINCIPAL = "PRINCIPAL"
    REDOWNLOADABLE = "REDOWNLOADABLE"
    OUTPUTS = "OUTPUTS"
    SQLITE_DATABASES = "SQLITE_DATABASES"
    UNCLASSIFIED = "UNCLASSIFIED"
    EXCLUDED_HARD = "EXCLUDED_HARD"
    GIT_REMOTE_BACKED = "GIT_REMOTE_BACKED"
    GIT_RESCUE = "GIT_RESCUE"


@dataclass(frozen=True)
class Decision:
    classification: Classification
    responsible_rule: str
    selected: bool


@dataclass(frozen=True)
class Rejection:
    path: str
    reason: str


@dataclass(frozen=True)
class PathRule:
    prefix: tuple[str, ...]
    classification: Classification
    source: str


@dataclass(frozen=True)
class FileCandidate:
    path: str
    size: int = 0
    mtime: float = 0.0


@dataclass
class OutputBudget:
    """Tracks per-file, per-root and global output budgets.

    The `classify_candidates` method processes files newest-first, which is how
    the policy engine implements "selecting newest-first" without requiring the
    caller to pre-sort.
    """

    per_file_max: int | None = None
    per_root_max: dict[str, int] = field(default_factory=dict)
    global_max: int | None = None

    per_root_used: dict[str, int] = field(default_factory=dict)
    global_used: int = 0

    def root_limit(self, root: str) -> int | None:
        # Explicit root limit > "*" fallback > "default" fallback.
        for key in (root, "*", "default"):
            if key in self.per_root_max:
                return self.per_root_max[key]
        return None

    def can_accept(self, size: int, root: str) -> tuple[bool, str | None]:
        if self.per_file_max is not None and size > self.per_file_max:
            return False, f"per-file max exceeded ({size} > {self.per_file_max})"

        limit = self.root_limit(root)
        if limit is not None:
            current = self.per_root_used.get(root, 0)
            if current + size > limit:
                return False, (
                    f"per-root max exceeded for root '{root}' "
                    f"({current + size} > {limit})"
                )

        if self.global_max is not None and self.global_used + size > self.global_max:
            return False, f"global max exceeded ({self.global_used + size} > {self.global_max})"

        return True, None

    def accept(self, root: str, size: int) -> None:
        self.per_root_used[root] = self.per_root_used.get(root, 0) + size
        self.global_used += size


_SIZE_SUFFIXES = {
    "k": 1024,
    "kb": 1024,
    "kib": 1024,
    "m": 1024**2,
    "mb": 1024**2,
    "mib": 1024**2,
    "g": 1024**3,
    "gb": 1024**3,
    "gib": 1024**3,
    "t": 1024**4,
    "tb": 1024**4,
    "tib": 1024**4,
}


def _parse_size(value: Any) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    if isinstance(value, str):
        text = value.strip().lower()
        if not text:
            return None
        match = re.match(r"^([0-9]+(?:\.[0-9]+)?)\s*([a-z]+)?$", text)
        if not match:
            raise ValueError(f"Invalid size value: {value!r}")
        number = float(match.group(1))
        suffix = match.group(2)
        if suffix:
            if suffix not in _SIZE_SUFFIXES:
                raise ValueError(f"Unknown size suffix in {value!r}")
            number *= _SIZE_SUFFIXES[suffix]
        return int(number)
    raise ValueError(f"Unsupported size value type: {type(value)!r}")


def _path_parts(path: str | Path) -> tuple[str, ...]:
    """Normalise a path for prefix matching.

    Backslashes are converted, leading ``/`` and ``./`` are ignored, and empty
    ``.`` components are removed.  This makes relative and absolute config paths
    comparable when the backup root is the implicit root.
    """

    text = str(path).replace("\\", "/")
    while text.startswith("./"):
        text = text[2:]
    text = text.lstrip("/")
    return tuple(part for part in text.split("/") if part not in ("", "."))


def _matches_prefix(file_parts: tuple[str, ...], prefix: tuple[str, ...]) -> bool:
    if len(file_parts) < len(prefix):
        return False
    for file_part, prefix_part in zip(file_parts, prefix):
        if prefix_part == "*":
            continue
        if file_part != prefix_part:
            return False
    return True


def _validate_rule_parts(parts: tuple[str, ...], source: str) -> None:
    """Reject unsupported wildcard components in configured path rules.

    Only a bare ``*`` path segment is supported.  Recursive ``**`` globs and
    partial-segment globs such as ``foo*`` or ``*bar`` are refused here instead
    of being silently treated as a literal or never matching.
    """
    for part in parts:
        if "*" not in part:
            continue
        if part == "*":
            continue
        if "**" in part:
            raise ValueError(
                f"Unsupported recursive glob component {part!r} in rule {source!r}; "
                "only a bare '*' path segment is supported (recursive globs are "
                "handled by scanner-level directory pruning, not path rules)"
            )
        raise ValueError(
            f"Unsupported wildcard component {part!r} in rule {source!r}; "
            "only a bare '*' path segment is supported"
        )


def _canonicalize(value: Any) -> Any:
    """Return a JSON-safe canonical representation of a YAML value."""

    if isinstance(value, dict):
        return {
            str(key): _canonicalize(val)
            for key, val in sorted(value.items(), key=lambda item: str(item[0]))
        }
    if isinstance(value, (list, tuple)):
        return [_canonicalize(item) for item in value]
    if isinstance(value, str):
        return value
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value
    if value is None:
        return None
    return str(value)


def _canonical_bytes(canonical: Any) -> bytes:
    """The exact, deterministic bytes policy_hash is a sha256 of. A single
    source of truth for this serialization so the hash and the recoverable
    provenance artifact (Runner._upload_policy_provenance) can never drift
    apart from each other."""
    return json.dumps(
        canonical, sort_keys=True, separators=(",", ":"), allow_nan=False
    ).encode("utf-8")


_PATH_RULE_CLASSES = {
    "critical": Classification.CRITICAL,
    "bts": Classification.BTS,
    "principal": Classification.PRINCIPAL,
    "redownloadable": Classification.REDOWNLOADABLE,
    "outputs": Classification.OUTPUTS,
    "sqlite_databases": Classification.SQLITE_DATABASES,
}


def _parse_system_exclusions(data: dict[str, Any]) -> tuple[PathRule, ...]:
    raw = data.get("hard_exclude", [])
    if raw is None:
        raw = []
    if isinstance(raw, str):
        raw = [raw]
    if not isinstance(raw, (list, tuple)):
        raise ValueError("'hard_exclude' must be a list of path strings")

    rules: list[PathRule] = []
    for path in raw:
        parts = _path_parts(path)
        if not parts:
            LOGGER.warning("Ignoring empty hard exclusion path %r", path)
            continue
        _validate_rule_parts(parts, f"hard_exclude:{path}")
        rules.append(
            PathRule(parts, Classification.EXCLUDED_HARD, f"hard_exclude:{path}")
        )
    return tuple(rules)


def _parse_path_rules(data: dict[str, Any]) -> tuple[PathRule, ...]:
    raw = data.get("paths", {})
    if raw is None:
        raw = {}
    if not isinstance(raw, dict):
        raise ValueError("'paths' must be a mapping")

    rules: list[PathRule] = []
    for rule_name, paths in raw.items():
        classif = _PATH_RULE_CLASSES.get(str(rule_name).lower())
        if classif is None:
            LOGGER.warning(
                "Ignoring unknown path rule class %r; known classes: %s",
                rule_name,
                ", ".join(sorted(_PATH_RULE_CLASSES)),
            )
            continue

        if isinstance(paths, str):
            paths = [paths]
        if paths is None:
            continue
        if not isinstance(paths, (list, tuple)):
            raise ValueError(f"paths.{rule_name} must be a list of path strings")

        for path in paths:
            parts = _path_parts(path)
            if not parts:
                LOGGER.warning("Ignoring empty path in paths.%s", rule_name)
                continue
            _validate_rule_parts(parts, f"paths:{rule_name}:{path}")
            rules.append(
                PathRule(parts, classif, f"paths:{rule_name}:{path}")
            )

    return tuple(rules)


def _parse_output_budget(data: dict[str, Any]) -> OutputBudget:
    raw = data.get("outputs", {})
    if raw is None:
        raw = {}
    if not isinstance(raw, dict):
        raise ValueError("'outputs' must be a mapping")

    per_file_max = _parse_size(raw.get("max_file_bytes"))

    max_root_bytes = _parse_size(raw.get("max_root_bytes"))
    # The production schema has a single root-wide cap, not a per-root mapping.
    # Store it under the existing "*" fallback key used by OutputBudget.root_limit.
    per_root_max = {"*": max_root_bytes}

    global_max = _parse_size(raw.get("max_total_bytes"))

    return OutputBudget(
        per_file_max=per_file_max,
        per_root_max=per_root_max,
        global_max=global_max,
    )


def _normalise_git(value: Any) -> Classification | None:
    if value is None:
        return None
    if isinstance(value, Classification):
        if value in (Classification.GIT_REMOTE_BACKED, Classification.GIT_RESCUE):
            return value
        LOGGER.warning("Ignoring non-git Classification %r as git-aware classification", value)
        return None

    text = str(value).strip().lower()
    if text in ("git_remote_backed", "git remote backed", "remote_backed"):
        return Classification.GIT_REMOTE_BACKED
    if text in ("git_rescue", "git rescue", "rescue"):
        return Classification.GIT_RESCUE

    LOGGER.warning("Ignoring unrecognised git-aware classification %r", value)
    return None


@dataclass
class Policy:
    system_exclusions: tuple[PathRule, ...]
    path_rules: tuple[PathRule, ...]
    output_budget: OutputBudget
    _canonical_config: dict[str, Any]
    _policy_hash: str
    _seen: dict[tuple[str, ...], Decision] = field(default_factory=dict)
    _rejections: list[Rejection] = field(default_factory=list)

    @property
    def policy_hash(self) -> str:
        return self._policy_hash

    @property
    def canonical_bytes(self) -> bytes:
        """The exact bytes policy_hash is a sha256 of -- the canonical JSON
        serialization of every rule this Policy actually applies. Recovering
        these bytes recovers the full semantic policy (not just its hash),
        even if the original policy.yaml has since changed or is otherwise
        unavailable -- see Runner._upload_policy_provenance."""
        return _canonical_bytes(self._canonical_config)

    @property
    def rejections(self) -> tuple[Rejection, ...]:
        return tuple(self._rejections)

    def sqlite_databases(self) -> tuple[str, ...]:
        """Return configured sqlite_databases path rules, source-root-relative.

        These are files that must go through SQLite's own online backup API
        (see Runner._backup_sqlite), never a raw filesystem copy -- they may
        be open in WAL mode during a live run and a raw copy can capture an
        inconsistent snapshot.
        """
        return tuple(
            "/".join(rule.prefix)
            for rule in self.path_rules
            if rule.classification == Classification.SQLITE_DATABASES
        )

    @classmethod
    def from_yaml(cls, path: str | Path) -> "Policy":
        path = Path(path)
        with path.open("r", encoding="utf-8") as fh:
            data = yaml.safe_load(fh) or {}
        if not isinstance(data, dict):
            raise ValueError(f"YAML config at {path} must contain a top-level mapping")

        canonical = _canonicalize(data)
        policy_hash = hashlib.sha256(_canonical_bytes(canonical)).hexdigest()

        return cls(
            system_exclusions=_parse_system_exclusions(data),
            path_rules=_parse_path_rules(data),
            output_budget=_parse_output_budget(data),
            _canonical_config=canonical,
            _policy_hash=policy_hash,
        )

    def _ancestor_marker(self, path: str | Path) -> str | None:
        file_path = Path(path).absolute()
        if file_path.name in (".backup-never", ".backup-always"):
            return file_path.name

        # .backup-never has higher precedence than .backup-always, so check all
        # ancestors for .backup-never before checking for .backup-always.
        for parent in file_path.parents:
            if (parent / ".backup-never").exists():
                return ".backup-never"
        for parent in file_path.parents:
            if (parent / ".backup-always").exists():
                return ".backup-always"
        return None

    def _match_path_rule(self, file_parts: tuple[str, ...]) -> PathRule | None:
        best: PathRule | None = None
        for rule in self.path_rules:
            if _matches_prefix(file_parts, rule.prefix):
                if best is None or len(rule.prefix) > len(best.prefix):
                    best = rule
        return best

    def _apply_output_budget(
        self,
        path: str,
        file_parts: tuple[str, ...],
        decision: Decision,
        file_size: int,
        root_name: str | None,
    ) -> Decision:
        if not decision.selected:
            return decision

        # Only OUTPUTS is ever subject to the output budget.
        if decision.classification != Classification.OUTPUTS:
            return decision

        try:
            size = int(file_size)
        except (TypeError, ValueError):
            size = 0
        if size < 0:
            size = 0

        root = root_name or (file_parts[0] if file_parts else "")
        can_accept, reason = self.output_budget.can_accept(size, root)

        if can_accept:
            self.output_budget.accept(root, size)
            return decision

        if reason:
            self._rejections.append(Rejection(path=str(path), reason=reason))
        return Decision(
            decision.classification,
            f"output_budget:{reason or 'rejected'}",
            False,
        )

    def classify(
        self,
        path: str | Path,
        *,
        git_classification: Classification | str | None = None,
        file_size: int = 0,
        mtime: float | None = None,
        root_name: str | None = None,
    ) -> Decision:
        """Classify one path.

        ``mtime`` is accepted for API compatibility and batch sorting.  It is
        not used inside `classify` itself; use `classify_candidates` for
        newest-first selection.
        """

        file_parts = _path_parts(path)
        if not file_parts:
            decision = Decision(Classification.UNCLASSIFIED, "unclassified", False)
            self._seen[file_parts] = decision
            return decision

        if file_parts in self._seen:
            return self._seen[file_parts]

        # 1. Hard system exclusion.
        for rule in self.system_exclusions:
            if _matches_prefix(file_parts, rule.prefix):
                decision = Decision(Classification.EXCLUDED_HARD, rule.source, False)
                self._seen[file_parts] = decision
                return decision

        # 2. Markers.  `.backup-never` outranks `.backup-always`.
        marker = self._ancestor_marker(path)
        if marker == ".backup-never":
            decision = Decision(
                Classification.EXCLUDED_HARD,
                "marker:.backup-never",
                False,
            )
            self._seen[file_parts] = decision
            return decision
        if marker == ".backup-always":
            # `.backup-always` means CRITICAL and is exempt from size quotas.
            decision = Decision(Classification.CRITICAL, "marker:.backup-always", True)
            self._seen[file_parts] = decision
            return decision

        # 3. Explicit path rules: longest prefix wins.
        rule = self._match_path_rule(file_parts)
        if rule is not None:
            selected = rule.classification in {
                Classification.CRITICAL,
                Classification.BTS,
                Classification.OUTPUTS,
                Classification.SQLITE_DATABASES,
            }
            decision = Decision(rule.classification, rule.source, selected)

            if decision.classification == Classification.OUTPUTS:
                decision = self._apply_output_budget(
                    str(path),
                    file_parts,
                    decision,
                    file_size,
                    "/".join(rule.prefix),
                )

            self._seen[file_parts] = decision
            return decision

        # 4. Git-aware classification passed in by the caller.
        git_class = _normalise_git(git_classification)
        if git_class is not None:
            selected = git_class == Classification.GIT_RESCUE
            decision = Decision(
                git_class,
                f"caller:git:{git_class.value.lower()}",
                selected,
            )
            decision = self._apply_output_budget(
                str(path), file_parts, decision, file_size, root_name
            )
            self._seen[file_parts] = decision
            return decision

        # 5. Unclassified is always reported, never silently dropped/included.
        decision = Decision(Classification.UNCLASSIFIED, "unclassified", False)
        self._seen[file_parts] = decision
        return decision

    def classify_candidates(
        self,
        candidates: Iterable[FileCandidate],
    ) -> list[Decision]:
        """Classify several files newest-first according to output budget."""

        ordered = sorted(candidates, key=lambda candidate: candidate.mtime, reverse=True)
        decisions: list[Decision] = []
        for candidate in ordered:
            decisions.append(
                self.classify(
                    candidate.path,
                    file_size=candidate.size,
                    mtime=candidate.mtime,
                )
            )
        return decisions


__all__ = [
    "Classification",
    "Decision",
    "Rejection",
    "PathRule",
    "FileCandidate",
    "OutputBudget",
    "Policy",
]

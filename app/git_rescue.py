#!/usr/bin/env python3
"""Git-aware backup classification and rescue package builder.

STRICTLY READ-ONLY with respect to the target repository.  This module
never runs fetch, gc, checkout, pull, push, clean, reset or any other
command that mutates repository state.  Every git subprocess call has an
explicit timeout.
"""

from __future__ import annotations

import argparse
import json
import logging
import re
import shutil
import subprocess
import tarfile
from datetime import datetime, timezone
from pathlib import Path

logger = logging.getLogger(__name__)

GIT_TIMEOUT_SECONDS = 30
GIT_BUNDLE_TIMEOUT_SECONDS = 120

FORBIDDEN_GIT_SUBCOMMANDS = {
    "fetch",
    "gc",
    "checkout",
    "pull",
    "push",
    "clean",
    "reset",
    "stash",
    "apply",
    "merge",
    "rebase",
    "commit",
    "add",
    "rm",
    "mv",
    "branch",
    "tag",
    "clone",
    "init",
    "submodule",
    "worktree",
}


class GitRescueError(Exception):
    """Raised when a read-only git operation fails or is ambiguous."""


def _run_git(
    repo_path: Path,
    args: list[str],
    *,
    text: bool = True,
    timeout: int = GIT_TIMEOUT_SECONDS,
) -> str | bytes:
    """Run a read-only git command inside *repo_path* and return stdout.

    Raises GitRescueError on non-zero exit, timeout, or a forbidden
    mutating subcommand.
    """
    if not args:
        raise GitRescueError("no git subcommand provided")
    if args[0] in FORBIDDEN_GIT_SUBCOMMANDS:
        raise GitRescueError(f"refusing to run mutating git subcommand: {args[0]}")

    cmd = ["git", *args]
    try:
        if text:
            proc = subprocess.run(
                cmd,
                cwd=str(repo_path),
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=timeout,
            )
        else:
            proc = subprocess.run(
                cmd,
                cwd=str(repo_path),
                capture_output=True,
                timeout=timeout,
            )
    except subprocess.TimeoutExpired as exc:
        raise GitRescueError(
            f"git {' '.join(args)} timed out after {timeout}s"
        ) from exc
    except OSError as exc:
        raise GitRescueError(f"failed to execute git {args[0]}: {exc}") from exc

    if proc.returncode != 0:
        stderr = (
            proc.stderr.strip()
            if text
            else proc.stderr.decode("utf-8", "replace").strip()
        )
        raise GitRescueError(
            f"git {' '.join(args)} failed with exit {proc.returncode}: {stderr}"
        )

    return proc.stdout


def _parse_remotes(raw: str) -> list[dict[str, str]]:
    """Parse ``git remote -v`` output into a list of remote dicts."""
    remotes: list[dict[str, str]] = []
    seen: set[str] = set()
    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        parts = line.split()
        if len(parts) < 2:
            continue
        name = parts[0]
        url = parts[1]
        kind = parts[2].strip("()") if len(parts) > 2 else ""
        if name not in seen:
            seen.add(name)
            remotes.append({"name": name, "url": url, "type": kind})
    return remotes


def _parse_status(raw: str) -> dict:
    """Parse ``git status --porcelain=v2 --branch`` output."""
    info = {
        "current_branch": None,
        "detached": None,
        "upstream": None,
        "ahead": None,
        "behind": None,
        "staged": False,
        "unstaged": False,
        "untracked": False,
    }

    for line in raw.splitlines():
        if line.startswith("# branch.head "):
            value = line[len("# branch.head ") :].strip()
            if value.startswith("(detached"):
                info["detached"] = True
                info["current_branch"] = None
            else:
                info["detached"] = False
                info["current_branch"] = value
        elif line.startswith("# branch.upstream "):
            info["upstream"] = line[len("# branch.upstream ") :].strip()
        elif line.startswith("# branch.ab "):
            match = re.match(r"# branch\.ab \+(\d+) -(\d+)", line)
            if match:
                info["ahead"] = int(match.group(1))
                info["behind"] = int(match.group(2))
        elif line.startswith("1 ") or line.startswith("2 "):
            try:
                parts = line.split(" ", 2)
                xy = parts[1]
                if len(xy) >= 1 and xy[0] != ".":
                    info["staged"] = True
                if len(xy) >= 2 and xy[1] != ".":
                    info["unstaged"] = True
            except IndexError:
                # Malformed porcelain line should not happen.  If it does,
                # the caller treats the classification as uncertain.
                continue
        elif line.startswith("u "):
            info["staged"] = True
            info["unstaged"] = True
        elif line.startswith("? "):
            info["untracked"] = True

    if info["detached"] is None:
        # If branch.head was not present, we cannot prove a branch is
        # checked out, so treat it as ambiguous/detached conservatively.
        info["detached"] = True

    return info


def _parse_refs(raw: str) -> tuple[list[dict], list[dict]]:
    """Parse ``git for-each-ref`` output into local and remote-tracking refs."""
    local_branches: list[dict] = []
    remote_branches: list[dict] = []

    for line in raw.splitlines():
        if not line:
            continue
        parts = line.split("\t")
        if len(parts) < 5:
            parts.extend([""] * (5 - len(parts)))

        refname = parts[0]
        oid = parts[1]
        upstream = parts[2] or None
        upstream_remote = parts[3] or None
        upstream_branch = parts[4] or None

        if refname.startswith("refs/heads/"):
            local_branches.append(
                {
                    "name": refname[len("refs/heads/") :],
                    "ref": refname,
                    "sha": oid,
                    "upstream": upstream,
                    "upstream_remote": upstream_remote,
                    "upstream_branch": upstream_branch,
                }
            )
        elif refname.startswith("refs/remotes/"):
            short_name = refname[len("refs/remotes/") :]
            remote_name, sep, branch_name = short_name.partition("/")
            remote_branches.append(
                {
                    "name": short_name,
                    "ref": refname,
                    "sha": oid,
                    "remote": remote_name or None,
                    "branch": branch_name or None,
                }
            )

    return local_branches, remote_branches


def _collect_local_only_commits(
    repo_path: Path,
    local_branches: list[dict],
    remote_branches: list[dict],
    detached: bool,
) -> tuple[set[str], bool]:
    """Return local-only commit hashes and whether any local branch tip is local-only."""
    positive_refs = [branch["ref"] for branch in local_branches]
    if detached:
        positive_refs.append("HEAD")

    if not positive_refs:
        return set(), False

    args = ["rev-list", *positive_refs]
    if remote_branches:
        args.extend(["--not", "--remotes"])

    raw = _run_git(repo_path, args, text=True)
    commits = set(raw.splitlines()) if raw else set()

    local_branch_tip_hashes = {branch["sha"] for branch in local_branches}
    local_only_branch_tips = bool(local_branch_tip_hashes & commits)

    return commits, local_only_branch_tips


def collect_repo_info(repo_path: Path) -> dict:
    """Collect git facts for a repository. Never mutates the repository."""
    info = {
        "repo_path": str(repo_path),
        "errors": [],
        "warnings": [],
        "has_remote": False,
        "remotes": [],
        "status_raw": None,
        "current_branch": None,
        "detached": None,
        "upstream": None,
        "ahead": None,
        "behind": None,
        "staged": None,
        "unstaged": None,
        "untracked": None,
        "local_branches": [],
        "remote_tracking_branches": [],
        "local_only_branch_tips": None,
        "local_only_commits_count": None,
        "_local_only_commits": set(),
    }

    # Remotes
    try:
        raw = _run_git(repo_path, ["remote", "-v"], text=True)
        info["remotes"] = _parse_remotes(raw)
        info["has_remote"] = bool(info["remotes"])
    except GitRescueError as exc:
        info["errors"].append(f"remote listing failed: {exc}")

    # Status
    try:
        raw = _run_git(
            repo_path,
            ["status", "--porcelain=v2", "--branch"],
            text=True,
        )
        info["status_raw"] = raw
        info.update(_parse_status(raw))
    except GitRescueError as exc:
        info["errors"].append(f"status failed: {exc}")

    # Local branches and remote-tracking refs
    refs_collected = False
    try:
        raw = _run_git(
            repo_path,
            [
                "for-each-ref",
                "--format=%(refname)%09%(objectname)%09%(upstream)%09%(upstream:remotename)%09%(upstream:remoteref)",
                "refs/heads",
                "refs/remotes",
            ],
            text=True,
        )
        local_branches, remote_branches = _parse_refs(raw)
        info["local_branches"] = local_branches
        info["remote_tracking_branches"] = remote_branches
        refs_collected = True
    except GitRescueError as exc:
        info["errors"].append(f"for-each-ref failed: {exc}")

    # Local-only commits / branch tips
    try:
        commits, branch_tips_local_only = _collect_local_only_commits(
            repo_path,
            info["local_branches"],
            info["remote_tracking_branches"],
            info.get("detached") is True,
        )
        info["_local_only_commits"] = commits
        info["local_only_commits_count"] = len(commits)
        info["local_only_branch_tips"] = branch_tips_local_only
    except GitRescueError as exc:
        info["errors"].append(f"local-only commit detection failed: {exc}")
        info["_local_only_commits"] = set()
        info["local_only_commits_count"] = None
        info["local_only_branch_tips"] = True

    if not refs_collected:
        # Without local/remote refs we cannot prove there are no local-only
        # branch tips.  Uncertain means back up.
        info["local_only_branch_tips"] = True

    return info


def _classify(info: dict) -> tuple[str, list[str]]:
    """Classify a repository based on collected facts.

    Three-way, not two-way: a repo whose git-status collection itself failed
    or timed out (``info["errors"]``) is classified UNCERTAIN, distinct from
    RESCUE_REQUIRED. When collection failed we cannot trust the other
    signals below (staged/unstaged/untracked/ahead/etc all default to their
    "needs rescue" value in that case, per the ``is not False``/``is None``
    checks below) -- treating that as an ordinary RESCUE_REQUIRED would let
    the semantic-rescue path (bundle + diffs + untracked archive) build
    artifacts from data we know is incomplete. UNCERTAIN routes to the
    conservative full-repo-backup fallback instead (see rescue_repository).
    Confirmed real case this classification exists for: erf_data_backup's
    `git status --porcelain=v2 --branch` timed out at 30s during the
    2026-08-17 audit run.
    """
    data_collection_errors = list(info.get("errors", []))
    if data_collection_errors:
        return "UNCERTAIN", [
            "git data collection error: " + "; ".join(data_collection_errors)
        ]

    reasons: list[str] = []

    if info.get("has_remote") is not True:
        reasons.append("no git remote configured")

    if info.get("staged") is not False:
        reasons.append("staged changes present")

    if info.get("unstaged") is not False:
        reasons.append("unstaged changes present")

    if info.get("untracked") is not False:
        reasons.append("non-ignored untracked files present")

    if info.get("detached") is not False:
        reasons.append("detached HEAD or unknown branch state")

    if info.get("upstream") is None:
        reasons.append("no upstream configured for current branch")

    if info.get("ahead") != 0:
        reasons.append(f"branch is ahead of upstream by {info.get('ahead')} commit(s)")

    if info.get("local_only_branch_tips") is not False:
        reasons.append("local-only branch tips exist or could not be ruled out")

    classification = "REMOTE_BACKED_CLEAN" if not reasons else "RESCUE_REQUIRED"
    return classification, reasons


def classify_repository(repo_path: str | Path) -> dict:
    """Collect repo facts and return a JSON-safe classification result."""
    repo_path = Path(repo_path).expanduser().resolve()
    info = collect_repo_info(repo_path)
    classification, reasons = _classify(info)

    result = {key: value for key, value in info.items() if not key.startswith("_")}
    result["classification"] = classification
    result["reasons"] = reasons
    return result


def _write_json(path: Path, data: dict) -> None:
    """Write JSON data to *path* in a deterministic, JSON-safe way."""
    path.write_text(
        json.dumps(data, indent=2, sort_keys=True, default=str),
        encoding="utf-8",
    )


def _build_untracked_tar(
    repo_path: Path,
    tar_path: Path,
    *,
    timeout: int = GIT_TIMEOUT_SECONDS,
) -> None:
    """Create a tar archive of non-ignored untracked files."""
    raw = _run_git(
        repo_path,
        ["ls-files", "--others", "--exclude-standard", "-z"],
        text=False,
        timeout=timeout,
    )

    if raw:
        names = raw.decode("utf-8", "surrogateescape").split("\0")
    else:
        names = []

    with tarfile.open(tar_path, "w") as tar:
        for name in names:
            if not name:
                continue
            full_path = repo_path / name
            if not full_path.exists():
                raise GitRescueError(
                    f"untracked file disappeared before packaging: {name}"
                )
            try:
                tar.add(full_path, arcname=name, recursive=False)
            except OSError as exc:
                raise GitRescueError(
                    f"failed to add untracked file {name} to tar: {exc}"
                ) from exc


def _build_local_only_bundle(
    repo_path: Path,
    bundle_path: Path,
    info: dict,
    *,
    timeout: int = GIT_BUNDLE_TIMEOUT_SECONDS,
) -> None:
    """Build a git bundle containing local-only commits.

    If no local-only commits exist, an empty placeholder is written and a
    warning is recorded.  This is deliberate: such a bundle is not needed
    for reproducing repository state, and the failure of ``git bundle`` in
    that case would otherwise force a full file-level backup.
    """
    local_only_commits = info.get("_local_only_commits", set())

    if not local_only_commits:
        bundle_path.write_bytes(b"")
        info.setdefault("warnings", []).append(
            "No local-only commits found; local_only.bundle is empty"
        )
        return

    positive_refs: list[str] = []
    for branch in info.get("local_branches", []):
        if branch.get("sha") in local_only_commits:
            positive_refs.append(branch["ref"])

    if info.get("detached") is True:
        try:
            head_sha = _run_git(
                repo_path,
                ["rev-parse", "HEAD"],
                text=True,
                timeout=timeout,
            ).strip()
        except GitRescueError:
            head_sha = None
        if head_sha and head_sha in local_only_commits:
            positive_refs.append("HEAD")

    positive_refs = list(dict.fromkeys(positive_refs))

    if not positive_refs:
        # This should not happen when local_only_commits is non-empty, but
        # if it does, fall back to all local branches and note the ambiguity.
        positive_refs = [branch["ref"] for branch in info.get("local_branches", [])]
        if not positive_refs:
            raise GitRescueError("no positive refs available for local-only bundle")
        info.setdefault("warnings", []).append(
            "Could not resolve local-only branch tips; bundling all local branches"
        )

    args = ["bundle", "create", str(bundle_path), *positive_refs]
    if info.get("remote_tracking_branches"):
        args.extend(["--not", "--remotes"])

    _run_git(repo_path, args, text=False, timeout=timeout)


def _build_metadata(
    info: dict,
    classification: str,
    reasons: list[str],
    package_files: list[str],
) -> dict:
    """Build the JSON-serializable rescue package metadata."""
    return {
        "schema_version": "1.0",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "classification": classification,
        "reasons": reasons,
        "repo_path": info.get("repo_path"),
        "has_remote": info.get("has_remote"),
        "remotes": info.get("remotes"),
        "current_branch": info.get("current_branch"),
        "detached": info.get("detached"),
        "upstream": info.get("upstream"),
        "ahead": info.get("ahead"),
        "behind": info.get("behind"),
        "staged": info.get("staged"),
        "unstaged": info.get("unstaged"),
        "untracked": info.get("untracked"),
        "local_branches": info.get("local_branches"),
        "remote_tracking_branches": info.get("remote_tracking_branches"),
        "local_only_branch_tips": info.get("local_only_branch_tips"),
        "local_only_commits_count": info.get("local_only_commits_count"),
        "warnings": info.get("warnings"),
        "errors": info.get("errors"),
        "package_files": package_files,
    }


def build_rescue_package(
    repo_path: str | Path,
    output_dir: str | Path,
    *,
    timeout: int = GIT_TIMEOUT_SECONDS,
) -> dict:
    """Build a rescue staging package for a repository, or mark it FULL.

    This function is idempotent with respect to the output directory: any
    existing package directory for the same repository name is removed
    before a new one is written.
    """
    repo_path = Path(repo_path).expanduser().resolve()
    output_dir = Path(output_dir).expanduser().resolve()

    if not repo_path.is_dir():
        return {
            "status": "FULL_BACKUP_REQUIRED",
            "classification": "RESCUE_REQUIRED",
            "reasons": [f"repository path is not a directory: {repo_path}"],
            "error": f"repository path is not a directory: {repo_path}",
        }

    info = collect_repo_info(repo_path)
    classification, reasons = _classify(info)

    if classification == "REMOTE_BACKED_CLEAN":
        return {
            "status": "REMOTE_BACKED_CLEAN",
            "classification": classification,
            "reasons": reasons,
            "package_dir": None,
        }

    if classification == "UNCERTAIN":
        # Collection itself failed -- staged/unstaged/untracked/ahead are
        # all unreliable, so building status.txt/diffs/bundle from them
        # would package data we know may be wrong, not just risk a crash.
        # Route straight to the conservative per-file full-repo fallback
        # instead of attempting semantic packaging. Caller (Runner's repo
        # handler) is responsible for walking the repo file-by-file here.
        return {
            "status": "FULL_BACKUP_REQUIRED",
            "classification": classification,
            "reasons": reasons,
            "package_dir": None,
        }

    repo_name = repo_path.name or "repo"
    package_dir = output_dir / f"{repo_name}_rescue"

    try:
        if package_dir.exists():
            if package_dir.is_dir():
                shutil.rmtree(package_dir)
            else:
                package_dir.unlink()
        package_dir.mkdir(parents=True, exist_ok=False)
    except OSError as exc:
        return {
            "status": "FULL_BACKUP_REQUIRED",
            "classification": classification,
            "reasons": reasons + [str(exc)],
            "error": f"failed to prepare package directory: {exc}",
        }

    try:
        if info.get("status_raw") is None:
            raise GitRescueError(
                "status output not available; cannot build complete rescue package"
            )

        (package_dir / "status.txt").write_text(
            info["status_raw"],
            encoding="utf-8",
        )

        worktree_diff = _run_git(
            repo_path,
            ["diff", "--binary"],
            text=False,
            timeout=timeout,
        )
        (package_dir / "worktree.diff").write_bytes(worktree_diff)

        staged_diff = _run_git(
            repo_path,
            ["diff", "--cached", "--binary"],
            text=False,
            timeout=timeout,
        )
        (package_dir / "staged.diff").write_bytes(staged_diff)

        _build_untracked_tar(
            repo_path,
            package_dir / "untracked_files.tar",
            timeout=timeout,
        )

        _build_local_only_bundle(
            repo_path,
            package_dir / "local_only.bundle",
            info,
            timeout=timeout or GIT_BUNDLE_TIMEOUT_SECONDS,
        )

        package_files = [
            "status.txt",
            "worktree.diff",
            "staged.diff",
            "untracked_files.tar",
            "local_only.bundle",
        ]
        metadata = _build_metadata(info, classification, reasons, package_files)
        _write_json(package_dir / "metadata.json", metadata)

        return {
            "status": "RESCUE_PACKAGED",
            "classification": classification,
            "reasons": reasons,
            "package_dir": str(package_dir),
            "warnings": info.get("warnings", []),
        }

    except Exception as exc:
        logger.exception("Failed to build rescue package for %s", repo_path)
        return {
            "status": "FULL_BACKUP_REQUIRED",
            "classification": classification,
            "reasons": reasons + [str(exc)],
            "package_dir": str(package_dir),
            "error": str(exc),
        }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("repo", help="Path to the git repository to inspect")
    parser.add_argument(
        "--staging-dir",
        default=".",
        help="Directory where rescue packages should be written (default: current dir)",
    )
    parser.add_argument(
        "--classify-only",
        action="store_true",
        help="Only classify the repository; do not build a rescue package",
    )
    args = parser.parse_args(argv)

    if args.classify_only:
        result = classify_repository(args.repo)
        print(json.dumps(result, indent=2, sort_keys=True))
        return 0 if result["classification"] == "REMOTE_BACKED_CLEAN" else 2

    result = build_rescue_package(args.repo, args.staging_dir)
    print(json.dumps(result, indent=2, sort_keys=True, default=str))
    if result.get("status") in {"REMOTE_BACKED_CLEAN", "RESCUE_PACKAGED"}:
        return 0
    return 2


if __name__ == "__main__":
    raise SystemExit(main())

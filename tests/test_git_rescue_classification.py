"""Regression tests for git_rescue.py's repo classification.

2026-08-19: _classify was two-way (REMOTE_BACKED_CLEAN / RESCUE_REQUIRED).
When git-status collection itself fails or times out (info["errors"]), the
other collected signals default to their "needs rescue" value -- folding a
collection failure into ordinary RESCUE_REQUIRED reasons meant the semantic
rescue path (bundle + diffs + untracked archive, see the runner rewrite)
would build artifacts from data known to be incomplete. erf_data_backup hit
this for real: `git status --porcelain=v2 --branch` timed out at 30s during
the 2026-08-17 audit. Made three-way: a collection error now short-circuits
to UNCERTAIN, which routes to the conservative full-repo-backup fallback
instead of semantic rescue.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "app"))

import git_rescue as G  # noqa: E402


def _clean_info(**overrides):
    """A synthetic info dict representing a fully clean, fully-collected repo."""
    base = dict(
        errors=[],
        has_remote=True,
        staged=False,
        unstaged=False,
        untracked=False,
        detached=False,
        upstream="origin/main",
        ahead=0,
        local_only_branch_tips=False,
    )
    base.update(overrides)
    return base


def test_clean_repo_with_no_errors_is_remote_backed_clean():
    classification, reasons = G._classify(_clean_info())
    assert classification == "REMOTE_BACKED_CLEAN"
    assert reasons == []


def test_dirty_repo_with_no_collection_errors_is_rescue_required():
    classification, reasons = G._classify(_clean_info(unstaged=True))
    assert classification == "RESCUE_REQUIRED"
    assert reasons == ["unstaged changes present"]


def test_collection_error_is_uncertain_not_rescue_required():
    info = _clean_info(errors=["status failed: timed out after 30s"])
    classification, reasons = G._classify(info)
    assert classification == "UNCERTAIN"
    assert "status failed: timed out after 30s" in reasons[0]


def test_collection_error_wins_even_when_repo_also_looks_dirty():
    """A collection error must short-circuit to UNCERTAIN even if other
    signals also independently indicate RESCUE_REQUIRED -- we can't trust
    those other signals either once collection has failed."""
    info = _clean_info(errors=["for-each-ref failed: timed out"], unstaged=True, ahead=3)
    classification, reasons = G._classify(info)
    assert classification == "UNCERTAIN"
    assert len(reasons) == 1  # only the collection-error reason, not a mix


def test_real_clean_remote_repo_is_remote_backed_clean():
    """Integration check against a real repo on disk, not a synthetic fixture."""
    result = G.classify_repository("/workspace/melb-acquire-bridge")
    assert result["classification"] == "REMOTE_BACKED_CLEAN"
    assert result["reasons"] == []

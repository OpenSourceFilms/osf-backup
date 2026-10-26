"""Offline consistency check for published dependency licence inventory.

Only verifies that named dependencies in the pinned requirements are
covered by documentation; it does not verify the legal licence itself.
"""
from pathlib import Path
import re

ROOT = Path(__file__).resolve().parents[1]


def test_pinned_dependencies_documented():
    requirements = (ROOT / "requirements.lock.txt").read_text(encoding="utf-8")
    notice = (ROOT / "DEPENDENCY_LICENSES.md").read_text(encoding="utf-8")
    entries = []
    for line in requirements.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        assert re.fullmatch(r"[A-Za-z][A-Za-z0-9_.-]*==[A-Za-z0-9.+_-]+", line), line
        entries.append(line)
    assert len(entries) >= 14
    assert len(entries) == len(set(entries))
    for item in entries:
        assert f"`{item}`" in notice, f"Missing pinned dependency notice: {item}"
    assert "not" in notice.lower()
    assert "owner" in notice.lower()


def test_readme_links_notice_without_granting_reuse():
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    notice = (ROOT / "DEPENDENCY_LICENSES.md").read_text(encoding="utf-8")
    assert "(DEPENDENCY_LICENSES.md)" in readme
    assert "licence" in notice.lower()
    assert "not" in readme.lower()

"""The shipped READMEs must not be gutted, truncated, or stripped of license.

This rides the existing tests.yml matrix rather than adding a workflow, so the
gate runs on every push and PR on the same enforcement path the suite already
proved out. See scripts/check_readme_integrity.py for why it exists.

The negative tests below are the point. A gate that only ever passes is
indistinguishable from a gate that cannot fail, so each check is exercised
against a malformed or incomplete README shape and asserted to reject it on its
own, without help from the others.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
CHECKER = REPO / "scripts" / "check_readme_integrity.py"

# scripts/check_readme_integrity.py resolves its REPO as the parent of its own
# directory and rglobs every shipped README from there. To exercise it against a
# mutated README we therefore stage a miniature repo -- the checker plus every
# README it inspects, at the same relative paths -- and point it at that. Same
# file set, same findings, and the real README.md is never written to.
_STAGE_NAME = "readme-stage"

# Representative gutted README shape: a truncated tag with no substantive
# document content.
GUTTED_README = (
    '<p align="center">\n'
    '  <img src="skills/token-o\n'
    "\n"
    "---\n"
    "* Incomplete document *\n"
)


def _run(root: Path = REPO) -> subprocess.CompletedProcess:
    """Run the checker rooted at ``root`` (the real repo, or a staged copy)."""
    return subprocess.run(
        [sys.executable, str(root / "scripts" / "check_readme_integrity.py")],
        capture_output=True,
        text=True,
        cwd=str(root),
    )


def _shipped_readmes() -> list[Path]:
    """Every README the checker would inspect, discovered the same way it does."""
    scratch = {"node_modules"}
    out = []
    for path in sorted(REPO.rglob("README*.md")):
        parts = path.relative_to(REPO).parts[:-1]
        if any(part.startswith(".") or part in scratch for part in parts):
            continue
        out.append(path)
    return out


def _stage_repo(tmp_path: Path) -> Path:
    """A throwaway repo root holding the checker + every shipped README."""
    stage = tmp_path / _STAGE_NAME
    (stage / "scripts").mkdir(parents=True)
    shutil.copy2(CHECKER, stage / "scripts" / CHECKER.name)
    for src in _shipped_readmes():
        dst = stage / src.relative_to(REPO)
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)
    return stage


@pytest.fixture
def readme_swap(tmp_path):
    """Swap the STAGED root README and run the checker against the staging root.

    This used to write the mutated README straight into the real repo and restore
    it in a ``finally``. That made the suite a writer: five tests rewrote the
    tracked README.md on every run, and any crash, ``KeyboardInterrupt`` or
    concurrent editor between the write and the restore left a gutted README on
    disk. The staged copy gives the negative tests the same checker behaviour with
    no path back to the working tree.
    """
    stage = _stage_repo(tmp_path)
    readme = stage / "README.md"

    def _swap(content: str):
        readme.write_text(content, encoding="utf-8")
        return _run(stage)

    _swap.original = readme.read_text(encoding="utf-8")
    yield _swap


def test_shipped_readmes_are_intact():
    result = _run()
    assert result.returncode == 0, (
        "A shipped README failed its integrity check:\n\n"
        f"{result.stdout}{result.stderr}"
    )


def test_rejects_gutted_readme(readme_swap):
    """A short README with an unterminated tag must fail both shape checks."""
    result = readme_swap(GUTTED_README)
    assert result.returncode == 1, "gutted README passed the gate"
    for expected in ("below the 400-line floor", "unterminated <img> tag"):
        assert expected in result.stdout, f"missing finding: {expected}"


def test_rejects_mass_deletion_alone(readme_swap):
    """Size floor fires even when the remaining content is well-formed.

    Guards against a future refactor that makes the HTML check load-bearing for
    the whole gate: a tidy 20-line README with the license intact is still a
    gutting, and must be rejected on size alone.
    """
    tidy = (
        "# Token Optimizer\n\nSee LICENSE (PolyForm Noncommercial).\n"
        "Created by Alex Greenshpun.\n"
    )
    result = readme_swap(tidy)
    assert result.returncode == 1
    assert "below the 400-line floor" in result.stdout
    assert "unterminated" not in result.stdout


def test_rejects_unterminated_tag_alone(readme_swap):
    """HTML check fires on a full-length README with one truncated tag.

    The realistic sabotage is not a five-line file, it is one bad tag buried in
    an otherwise normal document, which every other check would wave through.
    """
    result = readme_swap(
        readme_swap.original + '\n<img src="trailing-truncation\n'
    )
    assert result.returncode == 1
    assert "unterminated <img> tag" in result.stdout
    assert "line floor" not in result.stdout


def test_rejects_license_or_attribution_removal(readme_swap):
    """Stripping the license or the author's name fails on its own."""
    stripped = readme_swap.original.replace(
        "PolyForm Noncommercial", "MIT"
    ).replace("Alex Greenshpun", "Anonymous")
    result = readme_swap(stripped)
    assert result.returncode == 1
    assert "license name is missing" in result.stdout
    assert "author attribution is missing" in result.stdout


def test_arithmetic_less_than_is_not_a_tag(readme_swap):
    """`<400 tokens` in prose must not read as an unterminated tag.

    A false positive here would block ordinary prose edits.
    """
    result = readme_swap(
        readme_swap.original + "\nCompresses to <400 tokens on a clean run.\n"
    )
    assert result.returncode == 0, (
        f"prose '<400' misread as markup:\n\n{result.stdout}{result.stderr}"
    )

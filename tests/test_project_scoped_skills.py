"""Project-scoped skills and agents were never counted.

Claude Code injects `<cwd>/.claude/skills` into the skill listing for every
session in that cwd, exactly like `~/.claude/skills`. measure.py only ever
scanned the global dir (`skills_dir = CLAUDE_DIR / "skills"`, repeated at six
call sites), so any project using project-scoped skills was under-reported.
Measured on one real project in the issue: 28 skills, ~1,426 tokens of raw
frontmatter, 4 agents, and a reported total low by 5-7%.

The part that makes it worse than a rounding error: the shortfall landed in the
"estimated vs real" calibration line, so the number whose entire job is to flag
our own inaccuracy was silently absorbing a KNOWN inaccuracy.

NOT in scope here: the MEMORY.md half of the project-scoped skills fix, which was already fixed in
v5.3.10 (HOME project -> cwd-matched project -> newest-first scan).

Run: python3 -m pytest tests/test_project_scoped_skills.py -v
"""
import os
import sys
import tempfile
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parent.parent / "skills" / "token-optimizer" / "scripts"


@pytest.fixture()
def m(monkeypatch):
    monkeypatch.setenv("TOKEN_OPTIMIZER_SNAPSHOT_DIR", tempfile.mkdtemp(prefix="to-161-"))
    sys.path.insert(0, str(SCRIPTS))
    if "measure" in sys.modules:
        del sys.modules["measure"]
    import measure
    return measure


def _make_skill(root: Path, name: str, description: str = "does a thing"):
    d = root / name
    d.mkdir(parents=True, exist_ok=True)
    (d / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: {description}\n---\n\nBody text.\n",
        encoding="utf-8",
    )
    return d


def test_project_skills_dir_is_scanned(m, tmp_path, monkeypatch):
    """THE BUG: a project's own skills were invisible to every report."""
    proj = tmp_path / "job-search-os"
    _make_skill(proj / ".claude" / "skills", "resume-tailor")
    monkeypatch.chdir(proj)

    dirs = [str(d) for d in m._skill_scan_dirs()]
    assert any(str(proj / ".claude" / "skills") == d for d in dirs), (
        "project-scoped skills are injected into every session in this cwd and "
        "must be counted"
    )
    assert dirs[0] == str(m.CLAUDE_DIR / "skills"), "global must be scanned first"


def test_project_agents_dir_is_scanned(m, tmp_path, monkeypatch):
    proj = tmp_path / "job-search-os"
    (proj / ".claude" / "agents").mkdir(parents=True)
    monkeypatch.chdir(proj)

    dirs = [str(d) for d in m._agent_scan_dirs()]
    assert str(proj / ".claude" / "agents") in dirs


def test_no_project_dir_is_not_an_error(m, tmp_path, monkeypatch):
    """A plain directory must behave exactly as before the fix."""
    monkeypatch.chdir(tmp_path)
    assert [str(d) for d in m._skill_scan_dirs()] == [str(m.CLAUDE_DIR / "skills")]


def test_running_from_home_does_not_double_count(m, monkeypatch):
    """From ~, the project dir IS the global dir; one entry, not two."""
    monkeypatch.chdir(Path(m.CLAUDE_DIR).parent)
    dirs = m._skill_scan_dirs()
    assert len(dirs) == len(set(str(d) for d in dirs)), "a skill must never be counted twice"


def test_symlinked_project_claude_dir_is_refused(m, tmp_path, monkeypatch):
    """`.claude` as a symlink must not be a route out of the project tree."""
    proj = tmp_path / "proj"
    proj.mkdir()
    elsewhere = tmp_path / "elsewhere"
    (elsewhere / "skills").mkdir(parents=True)
    (proj / ".claude").symlink_to(elsewhere)
    monkeypatch.chdir(proj)

    assert m._project_claude_dir() is None
    assert [str(d) for d in m._skill_scan_dirs()] == [str(m.CLAUDE_DIR / "skills")]


def test_deleted_cwd_degrades_quietly(m, tmp_path, monkeypatch):
    """A stale shell (cwd removed under us) must not blow up a report."""
    gone = tmp_path / "gone"
    gone.mkdir()
    monkeypatch.chdir(gone)
    try:
        os.rmdir(gone)
    except OSError:
        pytest.skip("platform keeps the cwd alive after rmdir")
    # Must return a list, not raise.
    assert isinstance(m._skill_scan_dirs(), list)

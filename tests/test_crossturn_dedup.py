"""Cross-turn output dedup: a session-stateful compression a per-command tool
(e.g. Boost) structurally cannot do.

When the same read-only command is re-run in a session, Token Optimizer collapses
the repeat: identical output -> a one-line note, a small change -> just the diff.
Reuses the (previously dormant) command_outputs store + delta_diff. Fail-open and
self-sufficient (the caller attaches the progressive-disclosure pointer).
"""
import importlib
import sys
import tempfile
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parent.parent / "skills" / "token-optimizer" / "scripts"


@pytest.fixture()
def hook(monkeypatch):
    tmp = tempfile.mkdtemp(prefix="to-xturn-")
    monkeypatch.setenv("TOKEN_OPTIMIZER_SNAPSHOT_DIR", tmp)
    monkeypatch.setenv("CLAUDE_SESSION_ID", "test-xturn-session")
    sys.path.insert(0, str(SCRIPTS))
    for m in ("bash_compress_hook", "session_store", "delta_diff"):
        sys.modules.pop(m, None)
    mod = importlib.import_module("bash_compress_hook")
    importlib.reload(mod)
    yield mod
    sys.modules.pop("bash_compress_hook", None)


def _git_status(nfiles, branch="main"):
    base = [f"On branch {branch}", "Your branch is up to date.", "",
            "Changes not staged for commit:"]
    files = [f"\tmodified:   src/module_{i:02d}.py" for i in range(nfiles)]
    return "\n".join(base + files + ["", "no changes added, only modified files here"]) + "\n"


def test_first_run_is_not_deduped(hook):
    assert hook._crossturn_dedup("git status", _git_status(12)) is None


def test_identical_rerun_is_collapsed(hook):
    out = _git_status(12)
    assert hook._crossturn_dedup("git status", out) is None       # records
    ref = hook._crossturn_dedup("git status", out)                # identical
    assert ref is not None
    assert "identical" in ref.lower()
    assert len(ref) < len(out) * 0.5                              # big saving


def test_small_change_becomes_a_delta(hook):
    hook._crossturn_dedup("git status", _git_status(12))          # records
    ref = hook._crossturn_dedup("git status", _git_status(14))    # +2 files
    assert ref is not None
    assert "except" in ref.lower()
    assert len(ref) < len(_git_status(14)) * 0.85


def test_different_command_never_dedups(hook):
    hook._crossturn_dedup("git status", _git_status(12))
    other = "\n".join(f"-rw-r--r-- 1 u s {1000+i} module_{i:02d}.py" for i in range(30)) + "\n"
    assert hook._crossturn_dedup("ls -la", other) is None


def test_tiny_output_is_ignored(hook):
    # Below the 200-char floor -> not worth a reference.
    small = "On branch main\n"
    assert hook._crossturn_dedup("git status", small) is None
    assert hook._crossturn_dedup("git status", small) is None


def test_never_raises_without_session(hook, monkeypatch):
    monkeypatch.delenv("CLAUDE_SESSION_ID", raising=False)
    assert hook._crossturn_dedup("git status", _git_status(12)) is None

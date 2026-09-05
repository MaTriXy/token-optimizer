#!/usr/bin/env python3
"""D2 — "continue to <verb>" must NOT count as resume-intent.

This branch's _RESUME_INTENT_RE listed a bare ``to`` in the ``continue (?:...)``
group, so "continue to refactor", "continue to write the tests" etc. matched as
resume-intent. On Codex, any such prompt in a project with a recent checkpoint
then triggered a FULL lean-resume injection (the deliberate "pick up prior work"
path) -- a regression this branch itself introduced.

"continue to <verb>" means keep doing the CURRENT task, not resume a prior
session. Removing bare ``to`` closes the hole while genuine resume cues
("continue where we left off", "continue our work") still fire.
"""

from __future__ import annotations

import importlib
import json
import os
import sys
import tempfile
import time
from datetime import datetime
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
SCRIPTS = REPO / "skills" / "token-optimizer" / "scripts"

_UUID_A = "aaaa1111-2222-4333-8444-aaaaaaaaaaaa"

# Footer strings emitted ONLY by the full lean-resume block (build_lean_resume_context).
_CONFIDENT = "Use to re-orient on prior work"
_CONDITIONAL = "Use only if it matches the current request"


@pytest.fixture
def measure(monkeypatch):
    tmp = tempfile.mkdtemp(prefix="to-d2-test-")
    monkeypatch.setenv("TOKEN_OPTIMIZER_SNAPSHOT_DIR", tmp)
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", tmp)
    sys.path.insert(0, str(SCRIPTS))
    if "measure" in sys.modules:
        del sys.modules["measure"]
    mod = importlib.import_module("measure")
    importlib.reload(mod)
    cp_dir = Path(tmp) / "checkpoints"
    cp_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(mod, "CHECKPOINT_DIR", cp_dir, raising=True)
    monkeypatch.setattr(mod, "TRENDS_DB", Path(tmp) / "trends.db", raising=True)
    monkeypatch.setattr(mod, "_log_resume_lean_savings", lambda *a, **k: None, raising=True)
    monkeypatch.setattr(mod, "_checkpoint_in_project", lambda sc, cwd: True, raising=True)
    yield mod, cp_dir
    if "measure" in sys.modules:
        del sys.modules["measure"]


def _sidecar(sid, task="refactor the widget"):
    return {
        "session_id": sid,
        "active_task": task,
        "continuation": "left off mid-refactor",
        "decisions": ["Chose approach X"],
        "modified_files": [{"path": "/home/u/proj/src/widget.py"}],
        "recent_reads": ["/home/u/proj/README.md"],
        "git": {"branch": "main", "sha": "abc123"},
    }


def _write_checkpoint(cp_dir, sid, sidecar):
    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    base = f"{sid}-{ts}-auto"
    (cp_dir / f"{base}.md").write_text("# checkpoint\n", encoding="utf-8")
    (cp_dir / f"{base}.json").write_text(json.dumps(sidecar), encoding="utf-8")


# --- Unit level: the regex itself ---

@pytest.mark.parametrize("prompt", [
    "continue to refactor the widget",
    "continue to write the failing tests",
    "please continue to improve the parser",
    "let's continue to build out the API",
])
def test_continue_to_verb_is_not_resume_intent(measure, prompt):
    mod, _ = measure
    assert mod._resume_intent(prompt) is False, (
        f"'{prompt}' means keep doing the current task, not resume a prior session")


@pytest.mark.parametrize("prompt", [
    "continue where we left off on the widget",
    "continue our work on the widget",
    "continue the widget refactor from last session",
    "continue from checkpoint on the widget",
    "resume our work on the widget",
])
def test_genuine_resume_cues_still_fire(measure, prompt):
    mod, _ = measure
    assert mod._resume_intent(prompt) is True, (
        f"'{prompt}' is a genuine resume cue and must still match")


# --- Codex integration: the full lean-resume injection must not open on the hole ---

def test_codex_continue_to_does_not_inject_lean_resume(measure, monkeypatch):
    mod, cp_dir = measure
    monkeypatch.setattr(mod, "detect_runtime", lambda: "codex", raising=True)
    proj = "/home/u/proj"
    _write_checkpoint(cp_dir, _UUID_A, _sidecar(_UUID_A))

    # Sanity: a genuine resume prompt DOES open the full lean-resume block.
    genuine = mod.codex_prompt_hints(
        "continue where we left off on the widget", cwd=proj)
    assert (_CONDITIONAL in genuine) or (_CONFIDENT in genuine), (
        "a genuine resume cue must still trigger the lean-resume block")

    # D2: "continue to <verb>" must NOT trigger the full lean-resume injection.
    hole = mod.codex_prompt_hints("continue to refactor the widget", cwd=proj)
    assert _CONFIDENT not in hole and _CONDITIONAL not in hole, (
        "'continue to refactor' must not re-open the fresh-session lean injection")

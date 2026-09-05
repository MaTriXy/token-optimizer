#!/usr/bin/env python3
"""bash_hook.py must not rewrite Bash commands in worktree-isolated sessions.

Claude Code's worktree isolation guard statically parses every Bash command and
REFUSES anything it can't classify as "simple" — the bash_compress for-loop
wrapper is refused as "too complex", so every whitelisted command (ls, find,
grep, git status) fails inside `.claude/worktrees/` sessions. The hook now
short-circuits (pass-through, no rewrite) when the payload cwd is under
`.claude/worktrees/`; everywhere else the rewrite is unchanged.

Runs bash_hook.py exactly the way Claude Code invokes it: JSON hook payload on
stdin, JSON updatedInput response on stdout.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
BASH_HOOK = REPO / "skills" / "token-optimizer" / "scripts" / "bash_hook.py"

# Commands that hit the compression whitelist in real sessions (the whitelist
# names ls / find / grep / git status; git status is the canonical example).
_SAMPLE_COMMAND = "git status"
_SAMPLE_TOOL_INPUT = {"command": _SAMPLE_COMMAND}

# Payload shape mirrors Claude Code's PreToolUse hook input: cwd is a top-level
# key (hook_io tests already rely on this), tool_name == "Bash", and the
# command rides inside tool_input.
def _payload(cwd: str) -> str:
    return json.dumps(
        {
            "session_id": "test-session-141",
            "transcript_path": "/tmp/transcript.jsonl",
            "cwd": cwd,
            "hook_event_name": "PreToolUse",
            "tool_name": "Bash",
            "tool_input": _SAMPLE_TOOL_INPUT,
        }
    )


def _run_hook(cwd: str, tmp_home: Path) -> subprocess.CompletedProcess:
    """Run bash_hook.py with a hook payload for the given session cwd.

    CLAUDE_CONFIG_DIR is pointed at a fresh tmp dir so the rewrite's side
    effects (bash-rewrites.jsonl logging, quality-cache reads) stay out of the
    user's real ~/.claude. CLAUDE_PLUGIN_ROOT is dropped so the cross-check
    against the repo-relative __file__ paths can't spuriously fail-close.
    """
    env = {**os.environ, "CLAUDE_CONFIG_DIR": str(tmp_home)}
    env.pop("CLAUDE_PLUGIN_ROOT", None)
    return subprocess.run(
        [sys.executable, str(BASH_HOOK)],
        input=_payload(cwd),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=30,
        env=env,
    )


def _rewritten_command(proc: subprocess.CompletedProcess) -> str | None:
    """Extract the rewritten updatedInput command, or None for pass-through."""
    if not proc.stdout.strip():
        return None
    response = json.loads(proc.stdout)
    updated = response["hookSpecificOutput"].get("updatedInput")
    return updated.get("command") if updated else None


def test_worktree_cwd_skips_rewrite(tmp_path: Path) -> None:
    """cwd under .claude/worktrees/ => pass-through, no updatedInput."""
    proc = _run_hook(str(tmp_path / "project" / ".claude" / "worktrees" / "feat-x"), tmp_path)
    assert proc.returncode == 0, f"bash_hook crashed: {proc.stderr}"
    assert proc.stdout.strip() == "", (
        "worktree session must pass through untouched, got a rewrite response:\n"
        f"{proc.stdout}"
    )


def test_worktree_cwd_with_windows_separators_skips_rewrite(tmp_path: Path) -> None:
    """Windows-style cwd (backslashes) must be normalized before matching."""
    proc = _run_hook(r"C:\Users\dev\project\.claude\worktrees\feat-x", tmp_path)
    assert proc.returncode == 0, f"bash_hook crashed: {proc.stderr}"
    assert proc.stdout.strip() == "", (
        "worktree session (Windows cwd) must pass through untouched, got a "
        f"rewrite response:\n{proc.stdout}"
    )


def test_non_worktree_cwd_still_rewrites(tmp_path: Path) -> None:
    """Normal session cwd => rewrite still applied via updatedInput."""
    proc = _run_hook(str(tmp_path / "project"), tmp_path)
    assert proc.returncode == 0, f"bash_hook crashed: {proc.stderr}"
    rewritten = _rewritten_command(proc)
    assert rewritten is not None, (
        "non-worktree session must still rewrite; expected an updatedInput "
        f"response, got: stdout={proc.stdout!r} stderr={proc.stderr!r}"
    )
    assert rewritten != _SAMPLE_COMMAND
    assert "bash_compress" in rewritten, rewritten


@pytest.mark.parametrize("bad_cwd", [None, 42, ["x"], {"a": 1}, True])
def test_non_string_cwd_does_not_crash_the_hook(bad_cwd, tmp_path: Path) -> None:
    """A non-string cwd must not raise (fail-open contract): the worktree guard
    coerces cwd with str() before the substring check. A non-string cwd is not a
    worktree, so the rewrite still applies."""
    proc = _run_hook(bad_cwd, tmp_path)
    assert proc.returncode == 0, f"bash_hook crashed on cwd={bad_cwd!r}: {proc.stderr}"
    assert "AttributeError" not in proc.stderr, proc.stderr
    rewritten = _rewritten_command(proc)
    assert rewritten is not None and "bash_compress" in rewritten, (
        f"non-string cwd={bad_cwd!r} should still rewrite; got "
        f"stdout={proc.stdout!r} stderr={proc.stderr!r}"
    )
    assert rewritten.startswith("for b in bash"), rewritten

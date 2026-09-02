#!/usr/bin/env python3
"""Regression tests for Claude Code runtime detection in runtime_env (issue #120).

Bug: on a host running under Claude Code (CLAUDECODE=1) with a coexisting
``~/.codex`` DIRECTORY, the shell heuristic's ``elif [ -d "$HOME/.codex" ]``
branch fired with no Claude-Code env signal above it, so the runtime resolved
to ``codex`` and the skill scanned/mutated ``~/.codex`` instead of
``~/.claude``.

The Python ``detect_runtime()`` mirror adds a belt-and-suspenders tier: a
CLAUDECODE / CLAUDE_CODE_ENTRYPOINT / CLAUDE_CODE_SESSION_ID env signal
resolves to ``claude`` AFTER the explicit CODEX_HOME/HERMES_HOME checks (so a
nested-Codex session launched from a CC Bash tool, which inherits
CLAUDECODE=1 but sets CODEX_HOME, still resolves to ``codex``) and BEFORE the
weak directory heuristics.

Two cases are covered:

1. CLAUDECODE=1, CODEX_HOME unset, ``~/.codex`` dir present -> ``claude``.
   This is the reported bug. The ``~/.codex`` directory is materialized via a
   HOME monkeypatch so the test also guards against a future ``~/.codex``
   dir-existence check being added to ``detect_runtime()``.
2. CODEX_HOME set AND CLAUDECODE=1 -> ``codex``. The load-bearing
   nested-Codex regression guard: CLAUDECODE is inherited by every subprocess
   CC spawns, so the explicit CODEX_HOME env must win.

Run directly:  python3 tests/test_runtime_env_claudecode.py
Exits non-zero on first failure.
"""

import os
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SCRIPTS = REPO / "skills" / "token-optimizer" / "scripts"
sys.path.insert(0, str(SCRIPTS))

import runtime_env  # noqa: E402

# Every env var detect_runtime() consults — cleared before each controlled case.
_CONTROLLED_ENV = (
    "TOKEN_OPTIMIZER_RUNTIME",
    "CLAUDE_PLUGIN_ROOT",
    "CLAUDE_PLUGIN_DATA",
    "CLAUDECODE",
    "CLAUDE_CODE_ENTRYPOINT",
    "CLAUDE_CODE_SESSION_ID",
    "CODEX_HOME",
    "HERMES_HOME",
    "COPILOT_HOME",
    "TOKEN_OPTIMIZER_COPILOT_HOME",
    "TOKEN_OPTIMIZER_CURSOR_HOME",
    "CURSOR_PROJECT_DIR",
    "CURSOR_VERSION",
    "TOKEN_OPTIMIZER_NO_PROC_SCAN",
    "OPENCODE_BIN",
    "OPENCODE_CONFIG_DIR",
    "OPENCODE_DATA_DIR",
    "OPENCODE_CONFIG",
    "OPENCODE_CLIENT",
)


def _detect_with(env=None, process_opencode=False, home=None):
    """Resolve detect_runtime() under a controlled env + faked process signal.

    process_opencode stands in for the live ps-based ancestor scan so the tests
    are deterministic and never shell out. When ``home`` is given, HOME is
    monkeypatched to that path for the duration of the call (used to
    materialize a ``~/.codex`` directory without touching the real home).
    """
    env = env or {}
    saved_env = {k: os.environ.get(k) for k in _CONTROLLED_ENV}
    saved_home = os.environ.get("HOME")
    saved_proc = runtime_env._opencode_process_signal
    saved_copilot = runtime_env._copilot_signal
    try:
        for k in _CONTROLLED_ENV:
            os.environ.pop(k, None)
        for k, v in env.items():
            os.environ[k] = v
        if home is not None:
            os.environ["HOME"] = str(home)
        runtime_env._opencode_process_signal = lambda: process_opencode
        runtime_env._copilot_signal = lambda: False
        runtime_env.detect_runtime.cache_clear()
        return runtime_env.detect_runtime()
    finally:
        runtime_env._opencode_process_signal = saved_proc
        runtime_env._copilot_signal = saved_copilot
        for k, v in saved_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        if saved_home is None:
            os.environ.pop("HOME", None)
        else:
            os.environ["HOME"] = saved_home
        runtime_env.detect_runtime.cache_clear()


def test_claudecode_beats_codex_dir_with_codex_home_unset():
    """Reported bug: CLAUDECODE=1 + a ~/.codex dir + no CODEX_HOME -> claude.

    The ~/.codex directory is materialized in a tmp HOME so the assertion also
    fails if a future ``~/.codex`` dir-existence check is added to
    detect_runtime() ahead of the CLAUDECODE tier.
    """
    with tempfile.TemporaryDirectory() as tmp:
        tmp_home = Path(tmp)
        (tmp_home / ".codex").mkdir()
        assert _detect_with(
            env={"CLAUDECODE": "1"},
            process_opencode=False,
            home=tmp_home,
        ) == "claude"


def test_codex_home_still_wins_with_claudecode_set():
    """Nested-Codex regression guard (load-bearing).

    CLAUDECODE is inherited by every subprocess Claude Code spawns, so a
    genuine Codex session launched from a CC Bash tool carries CLAUDECODE=1
    AND sets CODEX_HOME. The explicit CODEX_HOME env must win so the nested
    Codex session stays ``codex``, not ``claude``.
    """
    with tempfile.TemporaryDirectory() as tmp:
        tmp_home = Path(tmp)
        (tmp_home / ".codex").mkdir()
        assert _detect_with(
            env={"CLAUDECODE": "1", "CODEX_HOME": str(tmp_home / ".codex")},
            process_opencode=False,
            home=tmp_home,
        ) == "codex"


def test_copilot_home_beats_claudecode_with_claudecode_set():
    """Copilot regression guard: the CLAUDECODE tier must NOT steal a
    genuine Copilot session. CLAUDECODE is inherited by every subprocess
    Claude Code spawns, so a Copilot CLI session launched from a CC Bash tool
    carries CLAUDECODE=1 AND sets COPILOT_HOME. The explicit COPILOT_HOME env
    must win so the nested Copilot session stays ``copilot``, not ``claude``.
    This mirrors the Codex guard above; before this fix, Copilot had no explicit-env
    tier above CLAUDECODE and was stolen.
    """
    with tempfile.TemporaryDirectory() as tmp:
        tmp_home = Path(tmp)
        (tmp_home / ".copilot").mkdir()
        assert _detect_with(
            env={"CLAUDECODE": "1", "COPILOT_HOME": str(tmp_home / ".copilot")},
            process_opencode=False,
            home=tmp_home,
        ) == "copilot"


def test_to_copilot_home_beats_claudecode():
    """The namespaced TOKEN_OPTIMIZER_COPILOT_HOME override also sits above the
    CLAUDECODE tier (same explicit-env tier as COPILOT_HOME)."""
    with tempfile.TemporaryDirectory() as tmp:
        tmp_home = Path(tmp)
        assert _detect_with(
            env={"CLAUDECODE": "1", "TOKEN_OPTIMIZER_COPILOT_HOME": str(tmp_home)},
            process_opencode=False,
            home=tmp_home,
        ) == "copilot"


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    failed = 0
    for t in tests:
        try:
            t()
            print(f"  PASS  {t.__name__}")
        except AssertionError as e:
            failed += 1
            print(f"  FAIL  {t.__name__}: {e}")
        except Exception as e:  # noqa: BLE001
            failed += 1
            print(f"  ERROR {t.__name__}: {type(e).__name__}: {e}")
    print(f"\n{len(tests) - failed}/{len(tests)} passed")
    sys.exit(1 if failed else 0)

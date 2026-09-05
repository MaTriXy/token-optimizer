#!/usr/bin/env python3
"""Regression tests for Cursor runtime detection in runtime_env.

Cursor joins the runtime lattice at two tiers:

1. Explicit env tier — TOKEN_OPTIMIZER_CURSOR_HOME sits ABOVE CLAUDECODE, so a
   Cursor session launched from a CC Bash tool (which inherits CLAUDECODE=1,
   and which Copilot's explicit env also beats) still resolves to ``cursor``.
2. Weak hook-spawned tier — CURSOR_PROJECT_DIR + CURSOR_VERSION both present
   implies Cursor BELOW the CLAUDECODE tier. There is deliberately NO ancestor
   scan (the Cursor CLI binary is ``agent``, too generic to scan).

Run directly:  python3 tests/test_runtime_env_cursor.py
Exits non-zero on first failure.
"""

import os
import sys
import tempfile
from contextlib import redirect_stderr
from io import StringIO
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


def _detect_with(env=None):
    """Resolve detect_runtime() under a controlled env. The cursor weak signal
    reads real env vars (not monkeypatched), so the env pair is exercised
    directly; Copilot/OpenCode process scans are neutralized for determinism."""
    env = env or {}
    saved_env = {k: os.environ.get(k) for k in _CONTROLLED_ENV}
    saved_proc = runtime_env._opencode_process_signal
    saved_copilot = runtime_env._copilot_signal
    try:
        for k in _CONTROLLED_ENV:
            os.environ.pop(k, None)
        for k, v in env.items():
            os.environ[k] = v
        runtime_env._opencode_process_signal = lambda: False
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
        runtime_env.detect_runtime.cache_clear()


def test_explicit_override_resolves_to_cursor():
    assert _detect_with({"TOKEN_OPTIMIZER_RUNTIME": "cursor"}) == "cursor"


def test_runtime_home_and_label_under_cursor():
    """TOKEN_OPTIMIZER_RUNTIME=cursor -> home ~/.cursor, label 'Cursor'."""
    saved = {k: os.environ.get(k) for k in ("TOKEN_OPTIMIZER_RUNTIME", "TOKEN_OPTIMIZER_CURSOR_HOME")}
    try:
        os.environ["TOKEN_OPTIMIZER_RUNTIME"] = "cursor"
        os.environ.pop("TOKEN_OPTIMIZER_CURSOR_HOME", None)
        runtime_env.detect_runtime.cache_clear()
        home = runtime_env.runtime_home()
        # ~/.cursor is what runtime_home must resolve to (never ~/.claude).
        assert home.name == ".cursor", home
        assert runtime_env.runtime_name_for_humans() == "Cursor"
        assert str(runtime_env.cursor_home()) == str(home)
    finally:
        runtime_env.detect_runtime.cache_clear()
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def test_to_cursor_home_beats_claudecode():
    """The namespaced override sits above CLAUDECODE (same guard as Copilot)."""
    with tempfile.TemporaryDirectory() as tmp:
        tmp_home = Path(tmp)
        cur = tmp_home / ".cursor"
        cur.mkdir()
        saved_home = os.environ.get("HOME")
        try:
            os.environ["HOME"] = str(tmp_home)
            assert _detect_with(
                {"CLAUDECODE": "1", "TOKEN_OPTIMIZER_CURSOR_HOME": str(cur)}
            ) == "cursor"
        finally:
            if saved_home is None:
                os.environ.pop("HOME", None)
            else:
                os.environ["HOME"] = saved_home


def test_cursor_home_outside_home_falls_back_with_warning():
    """A TOKEN_OPTIMIZER_CURSOR_HOME outside $HOME is rejected -> ~/.cursor."""
    runtime_env._warned_messages.clear()
    saved = {k: os.environ.get(k) for k in ("TOKEN_OPTIMIZER_CURSOR_HOME", "TOKEN_OPTIMIZER_RUNTIME")}
    try:
        os.environ["TOKEN_OPTIMIZER_RUNTIME"] = "cursor"
        os.environ["TOKEN_OPTIMIZER_CURSOR_HOME"] = "/tmp/.cursor-outside-home"
        runtime_env.detect_runtime.cache_clear()
        buf = StringIO()
        with redirect_stderr(buf):
            home = runtime_env.cursor_home()
        assert home == runtime_env._safe_home() / ".cursor", home
        assert "TOKEN_OPTIMIZER_CURSOR_HOME" in buf.getvalue()
        assert "rejected" in buf.getvalue()
    finally:
        runtime_env.detect_runtime.cache_clear()
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def test_hook_env_pair_resolves_to_cursor():
    """CURSOR_PROJECT_DIR + CURSOR_VERSION both set -> cursor."""
    assert _detect_with(
        {"CURSOR_PROJECT_DIR": "/tmp/proj", "CURSOR_VERSION": "3.18.9"}
    ) == "cursor"


def test_hook_env_either_alone_stays_claude():
    """Either hook env alone is too weak -> falls through to claude."""
    assert _detect_with({"CURSOR_PROJECT_DIR": "/tmp/proj"}) == "claude"
    assert _detect_with({"CURSOR_VERSION": "3.18.9"}) == "claude"


def test_hook_env_pair_loses_to_claudecode():
    """The weak tier sits BELOW CLAUDECODE: a Cursor launched from a CC Bash
    tool inherits CLAUDECODE=1, so the weak pair must not steal claude here."""
    assert _detect_with(
        {"CLAUDECODE": "1", "CURSOR_PROJECT_DIR": "/tmp/proj", "CURSOR_VERSION": "3.18.9"}
    ) == "claude"


def test_cursor_home_returned_by_public_api():
    """cursor_home() exposes the default ~/.cursor without needing detection."""
    saved = os.environ.get("TOKEN_OPTIMIZER_CURSOR_HOME")
    try:
        os.environ.pop("TOKEN_OPTIMIZER_CURSOR_HOME", None)
        assert runtime_env.cursor_home() == runtime_env._safe_home() / ".cursor"
    finally:
        if saved is not None:
            os.environ["TOKEN_OPTIMIZER_CURSOR_HOME"] = saved

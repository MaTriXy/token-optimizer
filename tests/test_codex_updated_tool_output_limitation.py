"""Behavioral tests for the Codex updatedToolOutput limitation.

Codex's hook output contract supports ``additionalContext`` (for injecting
context into the model's turn) but does NOT support ``updatedToolOutput``
(replacing the tool's output after execution). The bash_compress_hook.py
script, which relies on ``updatedToolOutput`` to replace verbose Bash output
with a compressed version, is therefore NOT wired for Codex. The long-output
collapse on Codex relies on ``archive_result.py`` (archival to disk) instead
of ``updatedToolOutput`` (in-place replacement).

These tests exercise real runtime behavior:
1. The Codex install profile passes TOKEN_OPTIMIZER_NO_UPDATED_TOOL_OUTPUT=1
   in the PostToolUse hook command.
2. With that env flag set, bash_compress_hook emits no updatedToolOutput
   but still records the thrash guard.
3. The Codex install profile does not wire bash_compress_hook directly.
4. The Codex hook bridge does not use updatedToolOutput.

Run: python3 -m pytest tests/test_codex_updated_tool_output_limitation.py -q
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import uuid
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SCRIPTS = REPO / "skills" / "token-optimizer" / "scripts"
BASH_COMPRESS_HOOK = SCRIPTS / "bash_compress_hook.py"


def test_codex_install_passes_no_updated_tool_output_env():
    """The Codex PostToolUse hook command must carry the
    TOKEN_OPTIMIZER_NO_UPDATED_TOOL_OUTPUT=1 env flag so bash_compress_hook
    suppresses updatedToolOutput emission at runtime."""
    sys.path.insert(0, str(SCRIPTS))
    import codex_install

    hooks = codex_install._managed_hooks(enable_hot_path_hooks=True)
    assert "PostToolUse" in hooks, "PostToolUse must be wired for Codex"
    blob = json.dumps(hooks)
    assert "TOKEN_OPTIMIZER_NO_UPDATED_TOOL_OUTPUT" in blob, (
        "The Codex PostToolUse hook command must set "
        "TOKEN_OPTIMIZER_NO_UPDATED_TOOL_OUTPUT=1 so bash_compress_hook "
        "suppresses updatedToolOutput emission (Codex does not honor it)."
    )


def test_codex_runtime_suppresses_updated_tool_output():
    """With TOKEN_OPTIMIZER_NO_UPDATED_TOOL_OUTPUT=1, bash_compress_hook
    must not emit updatedToolOutput on a real Bash PostToolUse payload."""
    sid = "test-codex-runtime-" + uuid.uuid4().hex[:8]
    cmd = "ls -la"
    tmp = tempfile.mkdtemp(prefix="to-codex-runtime-")
    stdout = "\n".join(f"file_{i}.py" for i in range(200))
    payload = json.dumps({
        "session_id": sid,
        "transcript_path": "/tmp/transcript.jsonl",
        "cwd": "/Users/test/project",
        "hook_event_name": "PostToolUse",
        "tool_name": "Bash",
        "tool_input": {"command": cmd},
        "tool_response": {
            "stdout": stdout,
            "stderr": "",
            "interrupted": False,
            "isImage": False,
            "exit_code": 0,
        },
    })
    env = {
        **os.environ,
        "TOKEN_OPTIMIZER_SNAPSHOT_DIR": tmp,
        "CLAUDE_SESSION_ID": sid,
        "TOKEN_OPTIMIZER_NO_UPDATED_TOOL_OUTPUT": "1",
    }
    env.pop("CLAUDE_PLUGIN_ROOT", None)
    proc = subprocess.run(
        [sys.executable, str(BASH_COMPRESS_HOOK)],
        input=payload, capture_output=True, text=True,
        encoding="utf-8", errors="replace", timeout=30, env=env,
    )
    assert proc.returncode == 0, f"hook failed: {proc.stderr}"
    if proc.stdout.strip():
        try:
            envelope = json.loads(proc.stdout)
            hso = envelope.get("hookSpecificOutput", {})
            assert "updatedToolOutput" not in hso, (
                "TOKEN_OPTIMIZER_NO_UPDATED_TOOL_OUTPUT=1 must suppress "
                "updatedToolOutput emission at runtime."
            )
        except json.JSONDecodeError:
            pass


def test_codex_runtime_still_records_thrash_guard():
    """With TOKEN_OPTIMIZER_NO_UPDATED_TOOL_OUTPUT=1, thrash_guard must
    still record the Bash run. The flag only suppresses emission, not
    recording."""
    sid = "test-codex-thrash-" + uuid.uuid4().hex[:8]
    cmd = "make check"
    tmp = tempfile.mkdtemp(prefix="to-codex-thrash-")
    payload = json.dumps({
        "session_id": sid,
        "transcript_path": "/tmp/transcript.jsonl",
        "cwd": "/Users/test/project",
        "hook_event_name": "PostToolUse",
        "tool_name": "Bash",
        "tool_input": {"command": cmd},
        "tool_response": f"Exit code 1\nerror output\n",
    })
    env = {
        **os.environ,
        "TOKEN_OPTIMIZER_SNAPSHOT_DIR": tmp,
        "CLAUDE_SESSION_ID": sid,
        "TOKEN_OPTIMIZER_NO_UPDATED_TOOL_OUTPUT": "1",
    }
    env.pop("CLAUDE_PLUGIN_ROOT", None)
    proc = subprocess.run(
        [sys.executable, str(BASH_COMPRESS_HOOK)],
        input=payload, capture_output=True, text=True,
        encoding="utf-8", errors="replace", timeout=30, env=env,
    )
    assert proc.returncode == 0, f"hook failed: {proc.stderr}"

    os.environ["TOKEN_OPTIMIZER_SNAPSHOT_DIR"] = tmp
    sys.path.insert(0, str(SCRIPTS))
    for m in ("session_store", "delta_diff", "thrash_guard"):
        sys.modules.pop(m, None)
    from session_store import SessionStore
    from delta_diff import content_hash
    store = SessionStore(sid)
    row = store.get_command_streak(content_hash(cmd.strip()))
    store.close()
    assert row is not None, (
        "thrash_guard must still record on Codex (no updatedToolOutput), "
        "but no streak row was found."
    )
    assert row["streak"] >= 1, (
        f"thrash_guard streak must be >= 1, got {row['streak']}"
    )


def test_codex_install_does_not_wire_bash_compress_hook():
    """The Codex install profile must NOT wire bash_compress_hook.py directly,
    which relies on ``updatedToolOutput`` (a field Codex does not honor)."""
    sys.path.insert(0, str(SCRIPTS))
    import codex_install

    hooks = codex_install._managed_hooks(
        enable_bash_compression=True,
        enable_hot_path_hooks=True,
        enable_prompt_hooks=True,
        enable_subagent_hooks=True,
    )
    blob = json.dumps(hooks)
    assert "bash_compress_hook" not in blob, (
        "Codex must not wire bash_compress_hook.py: it relies on "
        "updatedToolOutput, which Codex does not honor."
    )


def test_codex_hook_bridge_does_not_use_updated_tool_output():
    """The Codex hook bridge must not emit ``updatedToolOutput``."""
    bridge = SCRIPTS / "codex_hook_bridge.py"
    src = bridge.read_text(encoding="utf-8")
    assert "updatedToolOutput" not in src, (
        "codex_hook_bridge.py must not use updatedToolOutput: "
        "Codex does not honor this field."
    )

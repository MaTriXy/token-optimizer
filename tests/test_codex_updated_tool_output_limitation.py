"""Pin the verified limitation that Codex does not honor ``updatedToolOutput``.

Codex's hook output contract supports ``additionalContext`` (for injecting
context into the model's turn) but does NOT support ``updatedToolOutput``
(replacing the tool's output after execution). The bash_compress_hook.py
script, which relies on ``updatedToolOutput`` to replace verbose Bash output
with a compressed version, is therefore NOT wired for Codex. The long-output
collapse on Codex relies on ``archive_result.py`` (archival to disk) instead
of ``updatedToolOutput`` (in-place replacement).

This test pins that limitation so a future change that accidentally wires
``bash_compress_hook.py`` (or any ``updatedToolOutput``-dependent script) for
Codex fails here.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SCRIPTS = REPO / "skills" / "token-optimizer" / "scripts"


def test_codex_install_does_not_wire_bash_compress_hook():
    """The Codex install profile must NOT wire bash_compress_hook.py, which
    relies on ``updatedToolOutput`` (a field Codex does not honor)."""
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
        "updatedToolOutput, which Codex does not honor. Use archive_result.py "
        "(archival) for long-output collapse on Codex instead."
    )


def test_codex_install_does_not_emit_updated_tool_output():
    """No Codex-generated hook command may reference ``updatedToolOutput``
    in its script path or arguments."""
    sys.path.insert(0, str(SCRIPTS))
    import codex_install

    hooks = codex_install._managed_hooks(
        enable_bash_compression=True,
        enable_hot_path_hooks=True,
        enable_prompt_hooks=True,
        enable_subagent_hooks=True,
    )
    blob = json.dumps(hooks)
    assert "updatedToolOutput" not in blob, (
        "Codex-generated hooks must not reference updatedToolOutput: "
        "Codex does not honor this field."
    )


def test_codex_hook_bridge_does_not_use_updated_tool_output():
    """The Codex hook bridge must not emit ``updatedToolOutput``."""
    bridge = SCRIPTS / "codex_hook_bridge.py"
    src = bridge.read_text(encoding="utf-8")
    assert "updatedToolOutput" not in src, (
        "codex_hook_bridge.py must not use updatedToolOutput: "
        "Codex does not honor this field."
    )


def test_codex_install_uses_archive_result_for_post_tool_use():
    """The Codex PostToolUse path (now via posttooluse_runner.py) internally
    dispatches archive_result.py for long-output archival. This is the
    supported fallback for Codex (archival, not updatedToolOutput replacement).
    """
    sys.path.insert(0, str(SCRIPTS))
    import codex_install

    hooks = codex_install._managed_hooks(enable_hot_path_hooks=True)
    assert "PostToolUse" in hooks, "PostToolUse must be wired for Codex"
    blob = json.dumps(hooks)
    assert "posttooluse_runner.py" in blob, (
        "Codex PostToolUse must route through posttooluse_runner.py, which "
        "internally dispatches archive_result.py (archival) instead of "
        "bash_compress_hook.py (updatedToolOutput replacement)."
    )

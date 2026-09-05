"""Regression tests for the second harness-parity pass.

These tests pin the three fixes that can land without redesigning the foreign
runtime bridges: bounded Codex entry points, the Codex SessionStart health
guard, and the previously unbudgeted compact/cwd entries.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest


REPO = Path(__file__).resolve().parent.parent
SCRIPTS = REPO / "skills" / "token-optimizer" / "scripts"
sys.path.insert(0, str(SCRIPTS))

import codex_hook_bridge  # noqa: E402
from hook_runtime import resolve_entry_budget  # noqa: E402


@pytest.mark.parametrize(
    ("args", "host_seconds", "expected_seconds", "expected_label"),
    [
        (["session-start"], 15.0, 4.5, "Codex:SessionStart"),
        (["user-prompt-submit"], 12.0, 4.5, "Codex:UserPromptSubmit"),
        (["subagent-start"], 6.0, 2.5, "Codex:SubagentStart"),
        (["subagent-stop"], 6.0, 2.5, "Codex:SubagentStop"),
    ],
)
def test_codex_bridge_entries_have_measured_bounded_budgets(
    args, host_seconds, expected_seconds, expected_label
):
    seconds, label = resolve_entry_budget("codex_hook_bridge", args)
    assert seconds == expected_seconds
    assert label == expected_label
    assert seconds <= host_seconds / 2.0


@pytest.mark.parametrize(
    ("module", "args", "host_seconds", "expected_seconds", "expected_label"),
    [
        (
            "measure",
            ["dynamic-compact-instructions", "--quiet"],
            20.0,
            2.0,
            "PreCompact:dynamic-compact-instructions",
        ),
        (
            "measure",
            ["compact-capture", "--trigger", "auto", "--quiet"],
            20.0,
            2.5,
            "PreCompact:compact-capture",
        ),
        (
            "measure",
            ["quality-cache", "--force", "--quiet"],
            20.0,
            2.0,
            "PostCompact:quality-cache",
        ),
        (
            "read_cache",
            ["--clear", "--quiet"],
            10.0,
            2.0,
            "cache-clear",
        ),
    ],
)
def test_compact_and_cwd_entries_have_precise_budgets(
    module, args, host_seconds, expected_seconds, expected_label
):
    seconds, label = resolve_entry_budget(module, args)
    assert seconds == expected_seconds
    assert label == expected_label
    assert seconds <= host_seconds / 2.0


def test_codex_session_start_bounds_ensure_health(monkeypatch):
    calls = []
    sentinel = object()

    monkeypatch.setattr(
        codex_hook_bridge,
        "read_stdin_hook_input",
        lambda: {"session_id": "round2", "source": "startup"},
    )
    monkeypatch.setattr(
        codex_hook_bridge.measure,
        "_install_hook_budget",
        lambda seconds: calls.append(("install", seconds)) or sentinel,
    )
    monkeypatch.setattr(
        codex_hook_bridge.measure,
        "_clear_hook_budget",
        lambda deadline: calls.append(("clear", deadline)),
    )
    monkeypatch.setattr(
        codex_hook_bridge,
        "_capture_stdout",
        lambda func, *args, **kwargs: calls.append(("capture", func.__name__)) or "",
    )
    monkeypatch.setattr(codex_hook_bridge, "_emit_additional_context", lambda *args: None)

    codex_hook_bridge.handle_session_start()

    assert calls[0] == ("install", 4.5)
    assert calls[1] == ("capture", "run_ensure_health")
    assert calls[2] == ("clear", sentinel)

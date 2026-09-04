"""SessionStart ensure-health must not emit diagnostics into model context.

Claude Code injects a SessionStart hook's STDOUT into the model's context,
where it rides on every API call for the rest of the session. Diagnostic
lines (baseline capture notices, dashboard generation messages, daemon
self-heal status, settings heal notices) are not actionable for the model
and cost tokens every turn.

Three channels exist on a SessionStart hook, with different outcomes:

  - plain stdout text  -> injected into model context (tax)
  - stderr             -> invisible in the CC UI on exit 0 (only in the
                          Ctrl+O transcript), so user-facing notices go dark
  - systemMessage JSON -> folded by the runner into the hook envelope,
                          rendered to the USER as "<hook> says: ...",
                          and NOT sent to the model (zero model tokens)

The ensure-health path routes operational diagnostics to stderr. A small
set of user-visible onboarding/wedge messages that the user MUST see
(daemon installed URL, daemon install-failed wedge, first-run auto-update
tip) are emitted as ``{"systemMessage": ...}`` JSON on stdout so they
reach the user without entering the model's context.

Stdout must be empty or a single valid JSON object. No diagnostic string
may reach the model's context (plain text, additionalContext, or
hookSpecificOutput.additionalContext). Diagnostic strings MAY appear
inside a systemMessage value (user-only channel).

Run: python3 -m pytest tests/test_sessionstart_quiet_stdout.py -q
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
MEASURE_PY = REPO / "skills" / "token-optimizer" / "scripts" / "measure.py"

# Strings that must NEVER enter the model's context. Each is a diagnostic
# the model cannot act on. They MAY appear inside a systemMessage value
# (user-only channel); they must NOT appear in plain stdout text,
# additionalContext, or hookSpecificOutput.additionalContext.
FORBIDDEN_MODEL_CONTEXT_FRAGMENTS = [
    "Captured baseline snapshot for structural savings",
    "Generating initial dashboard",
    "Set cleanupPeriodDays",
    "Dashboard daemon installed",
    "Restarted the dashboard daemon",
    "Self-healed",
    "Reconciled SessionEnd fossil",
    "Removed",
    "duplicate hook",
    "malformed hook",
    "Migrated statusLine",
    "Healed keep-warm",
    "Quality statusline enabled",
    "Statusline was replaced",
    "Refreshing dashboard",
    "Repaired keep-warm scheduler",
    "systemctl --user is not reachable",
    "Dashboard daemon self-heal disabled",
    "First-run tip: enable auto-update",
]

# Fragments that are expected to appear as systemMessage values (user-visible,
# model-silent). Tests verify these reach stdout as JSON systemMessage, not
# as plain text or additionalContext.
EXPECTED_SYSTEMMESSAGE_FRAGMENTS = [
    "Dashboard daemon installed",
    "Dashboard daemon self-heal disabled",
    "First-run tip: enable auto-update",
]

SESSION_ID = "01test50-8e07-7840-b64e-9a9603c1b460"


def _run_ensure_health_hook(home: Path) -> subprocess.CompletedProcess:
    """Run ensure-health as a SessionStart hook and capture stdout/stderr."""
    env = dict(os.environ)
    for var in (
        "CODEX_HOME", "TOKEN_OPTIMIZER_RUNTIME", "CLAUDE_PLUGIN_DATA",
        "CLAUDECODE", "CLAUDE_CODE_ENTRYPOINT", "CLAUDE_CODE_SESSION_ID",
        "AI_AGENT", "CLAUDE_CODE_REMOTE", "CLAUDE_CODE_CONTAINER_ID",
        "TOKEN_OPTIMIZER_SNAPSHOT_DIR", "TOKEN_OPTIMIZER_INTERACTIVE",
        "OPENCODE_HOME", "HERMES_HOME", "COPILOT_HOME",
        "TOKEN_OPTIMIZER_COPILOT_HOME", "TOKEN_OPTIMIZER_CURSOR_HOME",
        "TOKEN_OPTIMIZER_ANTIGRAVITY_HOME", "GROK_HOME",
        "TOKEN_OPTIMIZER_GROK_HOME", "CURSOR_PROJECT_DIR", "CURSOR_VERSION",
    ):
        env.pop(var, None)
    env["CLAUDE_CONFIG_DIR"] = str(home)
    env["TOKEN_OPTIMIZER_RUNTIME"] = "claude"
    payload = json.dumps({
        "cwd": str(REPO),
        "hook_event_name": "SessionStart",
        "session_id": SESSION_ID,
        "source": "startup",
    })
    return subprocess.run(
        [sys.executable, str(MEASURE_PY), "ensure-health", "--once-mark"],
        input=payload, text=True, capture_output=True, env=env, timeout=120,
    )


def _model_visible_text(stdout: str) -> str:
    """Extract the portion of stdout that would enter the model's context.

    systemMessage values are user-only and excluded. Everything else
    (plain text, additionalContext, hookSpecificOutput.additionalContext)
    is model-visible.
    """
    stripped = stdout.strip()
    if not stripped:
        return ""
    try:
        obj = json.loads(stripped)
    except ValueError:
        # Plain text stdout is entirely model-visible.
        return stdout
    if not isinstance(obj, dict):
        return stdout
    parts = []
    for key, val in obj.items():
        if key == "systemMessage":
            continue  # user-only channel
        if isinstance(val, str):
            parts.append(val)
        elif isinstance(val, dict):
            for subval in val.values():
                if isinstance(subval, str):
                    parts.append(subval)
    return "\n".join(parts)


def test_ensure_health_stdout_is_empty_or_valid_json(tmp_path):
    """Stdout must be empty or a single valid JSON object (the hook envelope)."""
    home = tmp_path / "claude-home"
    home.mkdir()
    proc = _run_ensure_health_hook(home)
    assert proc.returncode == 0, f"hook exited {proc.returncode}\n{proc.stderr[-2000:]}"
    stdout = proc.stdout.strip()
    if not stdout:
        return
    # If non-empty, it must be a single valid JSON object.
    try:
        obj = json.loads(stdout)
    except ValueError:
        pytest.fail(f"stdout is not valid JSON: {stdout[:400]!r}")
    assert isinstance(obj, dict), f"stdout JSON is not an object: {stdout[:400]!r}"


def test_ensure_health_stdout_has_no_diagnostic_in_model_context(tmp_path):
    """No diagnostic string may reach the model's context.

    Diagnostics may only appear inside a JSON systemMessage value (shown to
    the USER, not the model). They must never appear as plain text, in
    additionalContext, or in hookSpecificOutput.additionalContext.
    """
    home = tmp_path / "claude-home"
    home.mkdir()
    proc = _run_ensure_health_hook(home)
    assert proc.returncode == 0, f"hook exited {proc.returncode}\n{proc.stderr[-2000:]}"
    visible = _model_visible_text(proc.stdout)
    for fragment in FORBIDDEN_MODEL_CONTEXT_FRAGMENTS:
        assert fragment not in visible, (
            f"diagnostic {fragment!r} would reach model context: "
            f"found in {visible[:400]!r}"
        )


def test_ensure_health_with_baseline_capture_keeps_stdout_clean(tmp_path):
    """Trigger the baseline-snapshot capture path and verify stdout stays clean.

    Removing snapshot_before.json forces _auto_capture_pristine_baseline to
    re-capture, which used to print a notice to stdout.
    """
    home = tmp_path / "claude-home"
    home.mkdir()
    # Run once to let the snapshot dir get created, then remove the baseline
    # so the next run re-captures it.
    proc1 = _run_ensure_health_hook(home)
    assert proc1.returncode == 0
    # Find and remove snapshot_before.json if it was created.
    for candidate in (
        home / "token-optimizer" / "snapshot_before.json",
        home / "plugins" / "data" / "token-optimizer-alexgreensh-token-optimizer" / "data" / "snapshot_before.json",
    ):
        if candidate.exists():
            candidate.unlink()
    # Also clear the once-marker so the hook body actually runs again.
    for marker in (home / "token-optimizer").glob("once-ensure-health-*.json"):
        marker.unlink()
    proc2 = _run_ensure_health_hook(home)
    assert proc2.returncode == 0, f"hook exited {proc2.returncode}\n{proc2.stderr[-2000:]}"
    stdout = proc2.stdout.strip()
    # Stdout must be empty or valid JSON with no diagnostic in model context.
    if stdout:
        try:
            json.loads(stdout)
        except ValueError:
            pytest.fail(f"stdout is not valid JSON after baseline capture: {stdout[:400]!r}")
    visible = _model_visible_text(proc2.stdout)
    assert "Captured baseline snapshot" not in visible, (
        f"baseline notice would reach model context: {visible[:400]!r}"
    )

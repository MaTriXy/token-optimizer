"""SessionStart ensure-health must not emit diagnostics on stdout.

Claude Code injects a SessionStart hook's STDOUT into the model's context,
where it rides on every API call for the rest of the session. Diagnostic
lines (baseline capture notices, dashboard generation messages, daemon
self-heal status, settings heal notices) are not actionable for the model
and cost tokens every turn.

The ensure-health path routes all diagnostics to stderr. Stdout must carry
only the hook's JSON result or nothing at all.

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

# Strings that must NEVER appear on stdout when ensure-health runs as a
# SessionStart hook. Each is a diagnostic the model cannot act on.
FORBIDDEN_STDOUT_FRAGMENTS = [
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


def test_ensure_health_stdout_has_no_diagnostic_text(tmp_path):
    """None of the diagnostic strings may appear on stdout."""
    home = tmp_path / "claude-home"
    home.mkdir()
    proc = _run_ensure_health_hook(home)
    assert proc.returncode == 0, f"hook exited {proc.returncode}\n{proc.stderr[-2000:]}"
    for fragment in FORBIDDEN_STDOUT_FRAGMENTS:
        assert fragment not in proc.stdout, (
            f"diagnostic {fragment!r} leaked to stdout: {proc.stdout[:400]!r}"
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
    # Stdout must be empty or valid JSON with no diagnostic text.
    if stdout:
        try:
            json.loads(stdout)
        except ValueError:
            pytest.fail(f"stdout is not valid JSON after baseline capture: {stdout[:400]!r}")
    assert "Captured baseline snapshot" not in proc2.stdout, (
        f"baseline notice leaked to stdout: {proc2.stdout[:400]!r}"
    )

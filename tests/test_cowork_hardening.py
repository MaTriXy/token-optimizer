#!/usr/bin/env python3
"""Cowork hardening fixes (branch cowork-hardening).

Covers the six doc-grounded Cowork fixes. Each fix is gated on ``is_cowork()``
and must never regress native desktop Claude Code, so every test asserts the
desktop path is unchanged alongside the Cowork behavior.

  FIX 1  injection shape: UserPromptSubmit-path context wrapped in the documented
         hookSpecificOutput.additionalContext envelope in Cowork.
  FIX 2  unified per-session state dir: quality-cache + checkpoints + run-once
         markers resolve under the SAME base as SNAPSHOT_DIR in Cowork.
  FIX 3  is_cowork() keys on the DOCUMENTED CLAUDE_CODE_REMOTE=true signal.
  FIX 4  measure.py self-location resolver recognizes the /plugins/synced/ tree.
  FIX 5  report prints an HONEST platform-overhead caveat in Cowork (no fabricated
         calibration number).

Run: python3 -m pytest tests/test_cowork_hardening.py -q
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
SCRIPTS = REPO / "skills" / "token-optimizer" / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import measure  # noqa: E402
import runtime_env  # noqa: E402


def _cache_clear(fn):
    clear = getattr(fn, "cache_clear", None)
    if callable(clear):
        clear()


_COWORK_ENV = (
    "CLAUDE_CODE_REMOTE",
    "CLAUDE_CODE_CONTAINER_ID",
    "AI_AGENT",
    "CLAUDE_PLUGIN_ROOT",
    "CLAUDE_PLUGIN_DATA",
)


def _reset_cowork_env(monkeypatch):
    for var in _COWORK_ENV:
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("TOKEN_OPTIMIZER_NO_PROC_SCAN", "1")
    _cache_clear(runtime_env.is_cowork)
    _cache_clear(runtime_env.detect_runtime)


# --------------------------------------------------------------------------- #
# FIX 3 — is_cowork() via the documented CLAUDE_CODE_REMOTE signal
# --------------------------------------------------------------------------- #

def test_is_cowork_true_via_documented_remote_env(monkeypatch):
    _reset_cowork_env(monkeypatch)
    assert runtime_env.is_cowork() is False  # nothing set -> not cowork

    _reset_cowork_env(monkeypatch)
    monkeypatch.setenv("CLAUDE_CODE_REMOTE", "true")
    _cache_clear(runtime_env.is_cowork)
    assert runtime_env.is_cowork() is True
    # Still the claude runtime, never a new runtime name.
    monkeypatch.setenv("CLAUDECODE", "1")
    _cache_clear(runtime_env.detect_runtime)
    assert runtime_env.detect_runtime() == "claude"


@pytest.mark.parametrize("val", ["true", "TRUE", "1", "yes", "on"])
def test_is_cowork_remote_truthy_values(monkeypatch, val):
    _reset_cowork_env(monkeypatch)
    monkeypatch.setenv("CLAUDE_CODE_REMOTE", val)
    _cache_clear(runtime_env.is_cowork)
    assert runtime_env.is_cowork() is True


@pytest.mark.parametrize("val", ["false", "0", "", "no", "off"])
def test_is_cowork_remote_falsy_values_leave_it_off(monkeypatch, val):
    _reset_cowork_env(monkeypatch)
    monkeypatch.setenv("CLAUDE_CODE_REMOTE", val)
    _cache_clear(runtime_env.is_cowork)
    assert runtime_env.is_cowork() is False


def test_is_cowork_undocumented_fallback_still_holds(monkeypatch):
    # Belt-and-suspenders: the observed CLAUDE_CODE_CONTAINER_ID still triggers.
    _reset_cowork_env(monkeypatch)
    monkeypatch.setenv("CLAUDE_CODE_CONTAINER_ID", "cowork-abc")
    _cache_clear(runtime_env.is_cowork)
    assert runtime_env.is_cowork() is True


# --------------------------------------------------------------------------- #
# FIX 1 — documented additionalContext envelope emitter
# --------------------------------------------------------------------------- #

def test_emit_additional_context_wraps_raw_text_userpromptsubmit(capsys):
    measure._emit_additional_context("continue where we left off", event="UserPromptSubmit")
    out = capsys.readouterr().out.strip()
    obj = json.loads(out)  # must be exactly one valid JSON object
    assert obj["hookSpecificOutput"]["hookEventName"] == "UserPromptSubmit"
    assert obj["hookSpecificOutput"]["additionalContext"] == "continue where we left off"
    assert obj["continue"] is True


def test_emit_additional_context_default_event_is_sessionstart(capsys):
    measure._emit_additional_context("hello")
    obj = json.loads(capsys.readouterr().out.strip())
    assert obj["hookSpecificOutput"]["hookEventName"] == "SessionStart"


def test_emit_additional_context_empty_emits_nothing(capsys):
    measure._emit_additional_context("")
    measure._emit_additional_context("   ")
    measure._emit_additional_context(None)
    assert capsys.readouterr().out == ""


def test_emit_additional_context_passthrough_when_already_enveloped(capsys):
    pre = json.dumps({"hookSpecificOutput": {"hookEventName": "UserPromptSubmit",
                                             "additionalContext": "x"}})
    measure._emit_additional_context(pre, event="SessionStart")
    out = capsys.readouterr().out.strip()
    # Passthrough: the pre-enveloped payload is emitted unchanged (event NOT
    # rewritten to SessionStart), never double-wrapped.
    assert json.loads(out) == json.loads(pre)


def test_codex_wrapper_is_alias_of_shared_emitter(capsys):
    measure._emit_codex_session_start("codex text")
    obj = json.loads(capsys.readouterr().out.strip())
    assert obj["hookSpecificOutput"]["hookEventName"] == "SessionStart"
    assert obj["hookSpecificOutput"]["additionalContext"] == "codex text"


# --------------------------------------------------------------------------- #
# FIX 2 — single per-session state dir in Cowork
# --------------------------------------------------------------------------- #

def test_state_dirs_share_one_base_structurally():
    # In-process invariant (holds in any runtime): quality-cache, checkpoints, and
    # the once-marker all derive from ONE base, so there is never a dual location.
    assert measure.QUALITY_CACHE_DIR == measure._STATE_BASE / "token-optimizer"
    assert measure.CHECKPOINT_DIR == measure._STATE_BASE / "token-optimizer" / "checkpoints"
    mk = measure._once_per_session_marker("ensure-health", "sess-123")
    assert mk is not None
    assert mk.parent == measure.QUALITY_CACHE_DIR


def _run_state_probe(tmp_path, *, cowork):
    """Import measure in a fresh subprocess under a fake Cowork home and return
    the resolved state dirs, so module-level path resolution is exercised for
    real (constants are import-time)."""
    home = tmp_path / "home"
    pdata = home / ".claude" / "plugins" / "data" / "token-optimizer-mkt"
    pdata.mkdir(parents=True)
    (home / ".claude" / "plugins" / "installed_plugins.json").write_text(
        json.dumps({"plugins": {"token-optimizer@mkt": {"version": "5.0.0"}}}),
        encoding="utf-8",
    )
    probe = (
        "import json, measure, runtime_env\n"
        "print(json.dumps({\n"
        "  'cowork': runtime_env.is_cowork(),\n"
        "  'state_base': str(measure._STATE_BASE),\n"
        "  'plugin_data': str(measure._RESOLVED_PLUGIN_DATA),\n"
        "  'snapshot': str(measure.SNAPSHOT_DIR),\n"
        "  'quality': str(measure.QUALITY_CACHE_DIR),\n"
        "  'checkpoint': str(measure.CHECKPOINT_DIR),\n"
        "}))\n"
    )
    env = {
        "PATH": __import__("os").environ.get("PATH", ""),
        "HOME": str(home),
        "PYTHONPATH": str(SCRIPTS),
        "CLAUDE_PLUGIN_DATA": str(pdata),
        "CLAUDECODE": "1",
        "TOKEN_OPTIMIZER_NO_PROC_SCAN": "1",
    }
    if cowork:
        env["CLAUDE_CODE_REMOTE"] = "true"
    res = subprocess.run([sys.executable, "-c", probe], capture_output=True,
                         text=True, env=env, timeout=60)
    assert res.returncode == 0, res.stderr
    return json.loads(res.stdout.strip().splitlines()[-1]), pdata


def test_cowork_unifies_state_under_plugin_data_base(tmp_path):
    data, pdata = _run_state_probe(tmp_path, cowork=True)
    assert data["cowork"] is True
    # All per-session state resolves under the SAME plugin-data base as snapshots
    # (no dual write to ~/.claude/token-optimizer).
    assert data["state_base"] == str(pdata)
    for key in ("snapshot", "quality", "checkpoint"):
        assert data[key].startswith(str(pdata)), (key, data[key])


def test_desktop_state_stays_on_runtime_home(tmp_path):
    # Same plugin-data dir resolvable, but NOT cowork -> state stays under
    # ~/.claude (RUNTIME_DIR), byte-identical to pre-fix behavior.
    data, pdata = _run_state_probe(tmp_path, cowork=False)
    assert data["cowork"] is False
    claude = str(Path(pdata).parents[2])  # <home>/.claude
    assert data["quality"] == str(Path(claude) / "token-optimizer")
    assert data["checkpoint"] == str(Path(claude) / "token-optimizer" / "checkpoints")
    # Snapshots still resolve to the plugin-data dir (unchanged).
    assert data["snapshot"].startswith(str(pdata))


# --------------------------------------------------------------------------- #
# FIX 4 — synced-plugin self-location resolver
# --------------------------------------------------------------------------- #

def test_synced_plugin_detected_and_uses_plugin_root_path(monkeypatch):
    synced = "/root/.claude/plugins/synced/acct/token-optimizer/skills/token-optimizer/scripts/measure.py"
    monkeypatch.setattr(measure, "__file__", synced)
    assert measure._is_running_from_synced_plugin() is True
    # Update-safe: the resolver emits the ${CLAUDE_PLUGIN_ROOT} form, not the
    # version/sync-pinned absolute path.
    assert measure._get_measure_py_path() == (
        "${CLAUDE_PLUGIN_ROOT}/skills/token-optimizer/scripts/measure.py"
    )


def test_desktop_path_not_flagged_as_synced():
    # The real repo path has no /plugins/synced/ segment.
    assert measure._is_running_from_synced_plugin() is False


# --------------------------------------------------------------------------- #
# FIX 5 — honest platform-overhead caveat in Cowork (no fabricated number)
# --------------------------------------------------------------------------- #

def _minimal_snapshot():
    return {
        "label": "test",
        "timestamp": "2026-08-14T00:00:00",
        "components": {
            "core_system": {"tokens": 12900, "note": "desktop"},
        },
        "totals": {
            "controllable_tokens": 55400,
            "fixed_tokens": 12900,
            "estimated_total": 68300,
        },
    }


def test_report_prints_honest_caveat_in_cowork(monkeypatch, capsys):
    monkeypatch.setattr(measure, "is_cowork", lambda: True)
    measure.print_snapshot_summary(_minimal_snapshot())
    out = capsys.readouterr().out
    assert "NOTE (Cowork)" in out
    assert "not measurable from inside" in out.lower() or "NOT" in out
    assert "controllable slice only" in out
    assert "Core system (desktop est.)" in out
    # It must NOT quietly present the plain full-picture line.
    assert "Context used before typing" not in out
    # And it must NOT fabricate a Cowork-specific overhead number: the only token
    # figures printed are the real measured ones (12,900 / 68,300).
    assert "68,300" in out
    assert "12,900" in out


def test_report_desktop_path_unchanged(monkeypatch, capsys):
    monkeypatch.setattr(measure, "is_cowork", lambda: False)
    measure.print_snapshot_summary(_minimal_snapshot())
    out = capsys.readouterr().out
    assert "Context used before typing" in out
    assert "NOTE (Cowork)" not in out
    assert "Core system (fixed)" in out


def test_compact_restore_envelope_event_derives_from_hook_event_name():
    """PR #142 (@danikdanik): the compact-restore dispatch must choose the emitted
    envelope's hookEventName from the firing hook's stdin `hook_event_name`, NOT
    from is_cowork(). is_cowork() false-positives on the local CLI (hook
    subprocesses inherit the harness AI_AGENT marker), so the old static
    `event="UserPromptSubmit" if _cw else "SessionStart"` made a SessionStart:compact
    hook emit a UserPromptSubmit envelope that Claude Code rejects, discarding the
    post-compaction recovery context. Source-pin the fix so it cannot silently
    revert to the runtime-detection choice.
    """
    src = (SCRIPTS / "measure.py").read_text(encoding="utf-8")
    # The compact-restore dispatch must read the event from stdin.
    assert 'event=str(hook_input.get("hook_event_name") or "SessionStart")' in src, (
        "compact-restore must derive the envelope event from stdin hook_event_name"
    )
    # And must NOT reinstate the is_cowork-based static choice at the emit site.
    assert '_buf.getvalue(), event="UserPromptSubmit" if _cw else "SessionStart"' not in src, (
        "compact-restore reverted to the is_cowork static event choice (PR #142 regression)"
    )

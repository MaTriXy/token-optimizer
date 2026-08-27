#!/usr/bin/env python3
"""Regression guard for the consent-gate silent no-op race (v5.11.93, unit D).

The race: a SessionStart hook (ensure-health's ``last_hook_heal_check``
write, measure.py) creates ``<config>/token-optimizer/config.json`` BEFORE the
consent bootstrap writes ``enterprise_consent_shown`` / ``v5_welcome_shown``.
In that window run.py's ``_check_consent()`` must FAIL OPEN: every non-exempt
hook -- including the PreToolUse Bash compression rewrite
(``bash_hook.py --quiet``) -- must still be dispatched. Fail-closed-and-silent
there made compression silently do nothing for the whole window.

What must NOT change: a genuine explicit opt-out (``measure.py consent
--reset`` writes ``enterprise_consent_shown: false`` -- key PRESENT, value
False) stays gated, and the ensure-health/consent/v5 bootstrap exemption
keeps working under that opt-out so the user can re-grant.

Run: python3 -m pytest tests/test_consent_gate_race_window.py -q
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
HOOKS = REPO / "hooks"
RUN_PY = HOOKS / "run.py"

# The exact argv the hooks.json PreToolUse "Bash" entry dispatches through
# run.py (see hooks/hooks.json PreToolUse matcher "Bash").
BASH_HOOK_ARGV = [
    "run.py",
    "skills/token-optimizer/scripts/bash_hook.py",
    "--quiet",
]
# A bootstrap-exempt command (SessionStart ensure-health entry).
ENSURE_HEALTH_ARGV = [
    "run.py",
    "skills/token-optimizer/scripts/measure.py",
    "ensure-health",
    "--once-mark",
]


def _load_run_py():
    """Import hooks/run.py as a fresh module (it has no package-relative imports)."""
    spec = importlib.util.spec_from_file_location("race_window_run_py", RUN_PY)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def race_env(monkeypatch, tmp_path):
    """Real run.py against a tmp CLAUDE_CONFIG_DIR. Returns a helper object.

    Does NOT stub _check_consent: the whole point is driving the REAL consent
    read against a tmp config.json. Popen and signal.signal are stubbed so no
    child process spawns and pytest's handlers are untouched.
    """
    run = _load_run_py()

    claude_dir = tmp_path / "claude"
    cfg_dir = claude_dir / "token-optimizer"
    cfg_dir.mkdir(parents=True, exist_ok=True)
    cfg_path = cfg_dir / "config.json"

    # run._check_consent honors CLAUDE_CONFIG_DIR (absolute, existing,
    # non-symlink). Keep it from falling through to CLAUDE_PLUGIN_DATA /
    # CODEX_HOME / the host ~/.claude.
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(claude_dir))
    monkeypatch.delenv("CLAUDE_PLUGIN_DATA", raising=False)
    monkeypatch.delenv("CODEX_HOME", raising=False)
    monkeypatch.setenv("CLAUDE_PLUGIN_ROOT", str(REPO))
    monkeypatch.setattr(run, "_plugin_disabled_by_host", lambda: False)

    spawned = {"argv": []}

    class _FakeProc:
        def wait(self, timeout=None):
            return 0

        def poll(self):
            return 0

        def kill(self):
            pass

    def _popen(cmd, *a, **k):
        spawned["argv"].append(cmd)
        return _FakeProc()

    monkeypatch.setattr(run.subprocess, "Popen", _popen)
    monkeypatch.setattr(run.signal, "signal", lambda *_a, **_k: None)

    def write_config(obj: dict) -> None:
        cfg_path.write_text(json.dumps(obj), encoding="utf-8")

    def dispatch(argv) -> int:
        spawned["argv"].clear()
        monkeypatch.setattr(sys, "argv", argv)
        return run.main()

    class _Env:
        pass

    env = _Env()
    env.run = run
    env.cfg_path = cfg_path
    env.write_config = write_config
    env.dispatch = dispatch
    env.spawned = spawned
    return env


def test_race_window_config_exists_flags_absent_consent_fails_open(race_env):
    """THE race state: config.json EXISTS (written by ensure-health's
    last_hook_heal_check) with NO enterprise_consent_shown and NO
    v5_welcome_shown. Consent must read True (fail-open), not False."""
    race_env.write_config({"last_hook_heal_check": 1756300000})
    assert race_env.run._check_consent(REPO) is True, (
        "flags-absent race window must fail OPEN; fail-closed here is the "
        "v5.11.93 silent no-op"
    )


def test_race_window_pretooluse_bash_hook_still_fires(race_env):
    """End-to-end through run.main(): with config.json present but consent
    flags absent, the PreToolUse Bash compression hook (bash_hook.py) MUST
    reach Popen -- it must NOT return 0 silently at the consent gate."""
    race_env.write_config({"last_hook_heal_check": 1756300000})
    rc = race_env.dispatch(BASH_HOOK_ARGV)
    assert len(race_env.spawned["argv"]) == 1, (
        "PreToolUse Bash hook was silently gated off in the flags-absent "
        "window (v5.11.93 race): run.py returned before Popen"
    )
    # The dispatch must target bash_hook.py via module_runner.
    assert any("bash_hook" in part for part in race_env.spawned["argv"][0])
    assert rc == 0


def test_explicit_opt_out_still_gates_pretooluse_bash(race_env):
    """A genuine opt-out is NOT the race window: `consent --reset` writes
    enterprise_consent_shown: false (key PRESENT). That state must stay
    fail-CLOSED for non-exempt hooks -- never weakened by the race fix."""
    race_env.write_config({"enterprise_consent_shown": False})
    assert race_env.run._check_consent(REPO) is False, (
        "explicit opt-out (consent --reset) must remain gated"
    )
    rc = race_env.dispatch(BASH_HOOK_ARGV)
    assert race_env.spawned["argv"] == [], (
        "explicit opt-out must still gate the PreToolUse Bash hook"
    )
    assert rc == 0  # gate is a silent 0 by design; the point is no Popen


def test_explicit_opt_out_still_allows_bootstrap_exemption(race_env):
    """Under an explicit opt-out the ensure-health bootstrap must STILL
    dispatch (it is the only path that can re-grant consent); the fix must
    not break the ensure-health/consent/v5 exemption."""
    race_env.write_config({"enterprise_consent_shown": False})
    rc = race_env.dispatch(ENSURE_HEALTH_ARGV)
    assert len(race_env.spawned["argv"]) == 1, (
        "ensure-health bootstrap must stay consent-exempt even under an "
        "explicit opt-out, or the user can never re-grant"
    )
    assert rc == 0


def test_missing_config_still_fails_open(race_env):
    """Pre-existing invariant, kept as a guard: no config.json at all must
    fail open (fresh install before any writer runs)."""
    assert not race_env.cfg_path.exists()
    assert race_env.run._check_consent(REPO) is True
    race_env.dispatch(BASH_HOOK_ARGV)
    assert len(race_env.spawned["argv"]) == 1


def test_v5_welcome_backfill_still_grants_consent(race_env):
    """Pre-existing invariant: v5_welcome_shown present => consent True and
    enterprise_consent_shown backfilled into config.json."""
    race_env.write_config({"v5_welcome_shown": True})
    assert race_env.run._check_consent(REPO) is True
    cfg = json.loads(race_env.cfg_path.read_text(encoding="utf-8"))
    assert cfg.get("enterprise_consent_shown") is True, (
        "v5 welcome backfill must persist enterprise_consent_shown: true"
    )

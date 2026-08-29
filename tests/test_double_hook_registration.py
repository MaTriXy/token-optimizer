"""Double hook registration bug: self-heal re-adds hooks when they come from
another settings layer (--settings flag, project settings, managed policy).

ROOT CAUSE: setup_all_hooks reads ~/.claude/settings.json, sees no hooks, and
adds all 26. But the hooks may already be active from a --settings file or
project-level settings. Claude Code merges all layers and fires hooks from
each, so the session ends up with every hook firing twice.

The host does NOT expose the --settings path or the merged hook set to hook
subprocesses. No env var, no hook input field reveals the external layer. So
the detection must be indirect: if our SessionStart hook is firing
(_running_under_hook() is True) but ~/.claude/settings.json has ZERO Token
Optimizer hooks, the hooks are coming from another layer. Adding them here
would double-register.

This test reproduces the bug: settings.json has no hooks, we're running under
a hook, and self-heal adds hooks anyway (the bug). After the fix, self-heal
should skip when running under a hook with zero existing TO hooks.

Run: python3 -m pytest tests/test_double_hook_registration.py -v
"""
from __future__ import annotations

import importlib.util
import json
import os
import sys
import tempfile
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
SCRIPTS = REPO / "skills" / "token-optimizer" / "scripts"


@pytest.fixture()
def m(monkeypatch):
    """Load measure.py fresh with a tmp snapshot dir."""
    monkeypatch.setenv("TOKEN_OPTIMIZER_SNAPSHOT_DIR", tempfile.mkdtemp(prefix="to-dh-"))
    sys.path.insert(0, str(SCRIPTS))
    if "measure" in sys.modules:
        del sys.modules["measure"]
    import measure
    return measure


def _make_settings(claude_dir: Path, hooks: dict | None = None) -> Path:
    """Write a settings.json in claude_dir with the given hooks (or none)."""
    settings_path = claude_dir / "settings.json"
    settings_path.parent.mkdir(parents=True, exist_ok=True)
    # Start with a non-empty key so _read_settings_for_write's "parsed empty"
    # guard doesn't refuse to write. A real settings.json always has at least
    # one key (theme, model, etc.).
    data = {"_test_marker": True}
    if hooks is not None:
        data["hooks"] = hooks
    settings_path.write_text(json.dumps(data), encoding="utf-8")
    return settings_path


def _make_plugin_hooks_json(plugin_dir: Path) -> Path:
    """Create a minimal plugin hooks.json with one PreToolUse hook."""
    hooks_json = plugin_dir / "hooks" / "hooks.json"
    hooks_json.parent.mkdir(parents=True, exist_ok=True)
    hooks_json.write_text(json.dumps({
        "hooks": {
            "PreToolUse": [
                {"matcher": "Bash", "hooks": [
                    {"type": "command", "command": "python3 '${CLAUDE_PLUGIN_ROOT}/skills/token-optimizer/scripts/measure.py' quality-cache --warn --quiet"}
                ]}
            ],
            "SessionStart": [
                {"hooks": [
                    {"type": "command", "command": "python3 '${CLAUDE_PLUGIN_ROOT}/skills/token-optimizer/scripts/measure.py' ensure-health --hook"}
                ]}
            ]
        }
    }), encoding="utf-8")
    return hooks_json


def test_self_heal_adds_hooks_when_not_running_under_hook(m, tmp_path, monkeypatch):
    """BASELINE: when NOT running under a hook (manual `measure.py ensure-health`),
    self-heal SHOULD add missing hooks to settings.json. This is the normal
    script-install drift recovery path and must not regress."""
    claude_dir = tmp_path / "claude"
    plugin_dir = tmp_path / "plugin"
    _make_settings(claude_dir)  # no hooks
    _make_plugin_hooks_json(plugin_dir)

    monkeypatch.setattr(m, "CLAUDE_DIR", claude_dir)
    monkeypatch.setattr(m, "SETTINGS_PATH", claude_dir / "settings.json")
    monkeypatch.setattr(m, "_is_plugin_installed", lambda: False)
    monkeypatch.setattr(m, "_running_under_hook", lambda: False)
    monkeypatch.setattr(m, "_find_plugin_hooks_json", lambda: plugin_dir / "hooks" / "hooks.json")
    # Bypass the 24h throttle
    now_val = 0
    monkeypatch.setattr(m, "_read_config_flag", lambda key, default=0: now_val if key == "last_hook_heal_check" else default)
    result = m.setup_all_hooks(dry_run=False, verbose=False)
    assert result["added"] > 0, "manual ensure-health must add missing hooks"

    # Verify hooks were written
    settings = json.loads((claude_dir / "settings.json").read_text())
    assert "hooks" in settings
    assert "PreToolUse" in settings["hooks"]


def test_self_heal_skips_when_running_under_hook_with_zero_existing_hooks(m, tmp_path, monkeypatch):
    """THE BUG: when running under a hook (SessionStart fired) and
    ~/.claude/settings.json has ZERO Token Optimizer hooks, self-heal adds
    them anyway. But our hook is firing, which means hooks are registered
    in ANOTHER layer (--settings, project settings, managed policy). Adding
    them here double-registers and every hook fires twice.

    After the fix: self-heal should detect this condition and skip."""
    claude_dir = tmp_path / "claude"
    plugin_dir = tmp_path / "plugin"
    _make_settings(claude_dir)  # no hooks
    _make_plugin_hooks_json(plugin_dir)

    monkeypatch.setattr(m, "CLAUDE_DIR", claude_dir)
    monkeypatch.setattr(m, "SETTINGS_PATH", claude_dir / "settings.json")
    monkeypatch.setattr(m, "_is_plugin_installed", lambda: False)
    monkeypatch.setattr(m, "_running_under_hook", lambda: True)  # WE ARE A HOOK
    monkeypatch.setattr(m, "_find_plugin_hooks_json", lambda: plugin_dir / "hooks" / "hooks.json")

    # The guard function (to be implemented)
    has_to_hooks = m._settings_has_any_to_hooks()
    assert has_to_hooks is False, "settings.json has no TO hooks"

    # The decision: should we skip self-heal?
    should_skip, confident = m._should_skip_self_heal_hooks()
    assert should_skip is True, (
        "Running under a hook with zero TO hooks in settings.json → "
        "hooks are from another layer → self-heal must skip to avoid "
        "double registration"
    )
    assert confident is True, (
        "settings.json is readable and has zero TO hooks → confident skip"
    )


def test_self_heal_does_not_skip_when_running_under_hook_with_some_existing_hooks(m, tmp_path, monkeypatch):
    """When running under a hook AND settings.json already has SOME Token
    Optimizer hooks (but maybe not all), that's genuine drift within our
    layer. Self-heal should run to fix the missing ones."""
    claude_dir = tmp_path / "claude"
    plugin_dir = tmp_path / "plugin"
    # Settings has ONE of our hooks but is missing others
    _make_settings(claude_dir, hooks={
        "PreToolUse": [{"matcher": "Bash", "hooks": [
            {"type": "command", "command": "python3 /old/path/measure.py quality-cache --warn --quiet"}
        ]}]
    })
    _make_plugin_hooks_json(plugin_dir)

    monkeypatch.setattr(m, "CLAUDE_DIR", claude_dir)
    monkeypatch.setattr(m, "SETTINGS_PATH", claude_dir / "settings.json")
    monkeypatch.setattr(m, "_is_plugin_installed", lambda: False)
    monkeypatch.setattr(m, "_running_under_hook", lambda: True)  # WE ARE A HOOK
    monkeypatch.setattr(m, "_find_plugin_hooks_json", lambda: plugin_dir / "hooks" / "hooks.json")

    has_to_hooks = m._settings_has_any_to_hooks()
    assert has_to_hooks is True, "settings.json has a TO hook (old path)"

    should_skip, _confident = m._should_skip_self_heal_hooks()
    assert should_skip is False, (
        "Running under a hook WITH some TO hooks → genuine drift → "
        "self-heal should run to fix missing/stale hooks"
    )


def test_self_heal_does_not_skip_when_not_running_under_hook(m, tmp_path, monkeypatch):
    """When NOT running under a hook (manual ensure-health), self-heal should
    always run regardless of whether settings.json has hooks."""
    claude_dir = tmp_path / "claude"
    _make_settings(claude_dir)  # no hooks

    monkeypatch.setattr(m, "CLAUDE_DIR", claude_dir)
    monkeypatch.setattr(m, "SETTINGS_PATH", claude_dir / "settings.json")
    monkeypatch.setattr(m, "_is_plugin_installed", lambda: False)
    monkeypatch.setattr(m, "_running_under_hook", lambda: False)  # MANUAL RUN

    should_skip, _confident = m._should_skip_self_heal_hooks()
    assert should_skip is False, (
        "Manual ensure-health (not under hook) → self-heal should run"
    )


def test_double_registration_count(m, tmp_path, monkeypatch):
    """THE CORE BUG: count how many hooks end up in settings.json after
    self-heal when running under a hook with zero existing hooks.

    BEFORE FIX: setup_all_hooks adds all hooks → settings.json now has
    hooks from BOTH the --settings layer AND settings.json → double fire.

    AFTER FIX: the guard skips setup_all_hooks → settings.json stays empty
    → hooks fire only from the --settings layer → single fire.
    """
    claude_dir = tmp_path / "claude"
    plugin_dir = tmp_path / "plugin"
    _make_settings(claude_dir)  # no hooks
    _make_plugin_hooks_json(plugin_dir)

    monkeypatch.setattr(m, "CLAUDE_DIR", claude_dir)
    monkeypatch.setattr(m, "SETTINGS_PATH", claude_dir / "settings.json")
    monkeypatch.setattr(m, "_is_plugin_installed", lambda: False)
    monkeypatch.setattr(m, "_running_under_hook", lambda: True)  # WE ARE A HOOK
    monkeypatch.setattr(m, "_find_plugin_hooks_json", lambda: plugin_dir / "hooks" / "hooks.json")

    # Count hooks in settings.json BEFORE self-heal
    settings_before = json.loads((claude_dir / "settings.json").read_text())
    hooks_before = sum(
        len(group.get("hooks", []))
        for groups in settings_before.get("hooks", {}).values()
        for group in groups
    )
    assert hooks_before == 0, "settings.json starts with zero hooks"

    # If the guard says skip, self-heal should NOT call setup_all_hooks
    _skip, _confident = m._should_skip_self_heal_hooks()
    if _skip:
        pass  # skip self-heal
    else:
        m.setup_all_hooks(dry_run=False, verbose=False)  # THE BUG: adds hooks

    # Count hooks in settings.json AFTER
    settings_after = json.loads((claude_dir / "settings.json").read_text())
    hooks_after = sum(
        len(group.get("hooks", []))
        for groups in settings_after.get("hooks", {}).values()
        for group in groups
    )

    assert hooks_after == 0, (
        f"settings.json has {hooks_after} hooks after self-heal while running "
        f"under a hook with zero existing hooks. This double-registers with "
        f"the --settings layer and every hook fires twice. The guard should "
        f"have skipped self-heal."
    )


def test_uncertain_skip_on_missing_settings(m, tmp_path, monkeypatch):
    """When settings.json is MISSING and we're running under a hook, the guard
    should skip (to avoid double-registration) but NOT advance the 24h throttle
    (the condition may be transient). The skip is uncertain because we can't
    tell if the file was deleted after session start or if hooks are from
    another layer."""
    claude_dir = tmp_path / "claude"
    # Do NOT create settings.json -- it's missing
    monkeypatch.setattr(m, "CLAUDE_DIR", claude_dir)
    monkeypatch.setattr(m, "SETTINGS_PATH", claude_dir / "settings.json")
    monkeypatch.setattr(m, "_is_plugin_installed", lambda: False)
    monkeypatch.setattr(m, "_running_under_hook", lambda: True)

    should_skip, confident = m._should_skip_self_heal_hooks()
    assert should_skip is True, (
        "Missing settings.json + running under hook → skip to avoid "
        "double-registration"
    )
    assert confident is False, (
        "Missing settings.json → uncertain skip → do NOT advance 24h throttle"
    )


def test_uncertain_skip_on_malformed_settings(m, tmp_path, monkeypatch):
    """When settings.json is malformed (invalid JSON) and we're running under
    a hook, the guard should skip but NOT advance the 24h throttle."""
    claude_dir = tmp_path / "claude"
    settings_path = claude_dir / "settings.json"
    settings_path.parent.mkdir(parents=True, exist_ok=True)
    settings_path.write_text("{invalid json content", encoding="utf-8")

    monkeypatch.setattr(m, "CLAUDE_DIR", claude_dir)
    monkeypatch.setattr(m, "SETTINGS_PATH", settings_path)
    monkeypatch.setattr(m, "_is_plugin_installed", lambda: False)
    monkeypatch.setattr(m, "_running_under_hook", lambda: True)

    should_skip, confident = m._should_skip_self_heal_hooks()
    assert should_skip is True, (
        "Malformed settings.json + running under hook → skip to avoid "
        "double-registration"
    )
    assert confident is False, (
        "Malformed settings.json → uncertain skip → do NOT advance 24h throttle"
    )


def test_scanner_matches_runner_script_names(m, tmp_path, monkeypatch):
    """The scanner must match hooks that use run.py wrapper scripts
    (sessionstart_runner.py, stop_runner.py, etc.) even when the install
    path does NOT contain 'token-optimizer'. This covers the false-negative
    case where a custom install path would make the run.py hooks invisible
    to the substring scanner."""
    claude_dir = tmp_path / "claude"
    # Settings has a hook that uses run.py with a NON-token-optimizer path
    _make_settings(claude_dir, hooks={
        "SessionStart": [{"hooks": [
            {"type": "command", "command": "bash /opt/my-tools/hooks/run.py hooks/sessionstart_runner.py"}
        ]}]
    })

    monkeypatch.setattr(m, "CLAUDE_DIR", claude_dir)
    monkeypatch.setattr(m, "SETTINGS_PATH", claude_dir / "settings.json")
    monkeypatch.setattr(m, "_is_plugin_installed", lambda: False)

    has_to_hooks = m._settings_has_any_to_hooks()
    assert has_to_hooks is True, (
        "Scanner must match sessionstart_runner.py even when the install "
        "path does not contain 'token-optimizer'"
    )

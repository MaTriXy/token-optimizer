#!/usr/bin/env python3
"""Spec-lock tests for the Token Optimizer x Claude Cowork adapter.

These pin the intended POST-BUILD behavior of the Cowork adapter while the core
lane lands its concurrent edits to ``cowork_install.py`` and ``runtime_env.py``
on branch ``cowork-full-parity``. This lane does NOT edit those source files;
it only asserts against the shape they must land in.

Two surfaces are covered:

1. ``cowork_install.build_cowork_hooks`` / ``COWORK_EVENTS`` -- the Cowork
   hooks.json trim + SessionStart run-once remap. Cowork does not fire
   SessionStart, so the run-once features that lived there
   (``ensure-health`` / ``quality-cache --force`` /
   ``compact-restore --new-session-only``) move onto ``UserPromptSubmit``;
   ``keepwarm`` and the compaction-matcher hooks (``compact-restore --compact``
   / ``--clear-compacted``) are dropped as unsalvageable in Cowork.

2. ``runtime_env`` cowork detection -- ``CLAUDE_CODE_CONTAINER_ID`` marks a
   Cowork container. The runtime is still ``"claude"`` but a cowork indicator is
   exposed.

Landed-state guarding
---------------------
Both surfaces are being edited concurrently by the core lane and, at the time
this file was written, NEITHER was on disk yet (verified via git: COWORK_EVENTS
still carried "SessionStart"; runtime_env had no CLAUDE_CODE_CONTAINER_ID
handling). Where the intended behavior is not yet present, the affected test
skips with a clear reason so this file is green today and converts to real
pass/fail the instant the core lane lands.

The skip GUARDS are deliberately WEAK -- they detect only that the lane touched
the symbol at all (e.g. "SessionStart" left COWORK_EVENTS), never the full spec.
So a landed-but-WRONG implementation passes the guard and FAILS the assertion
loudly, rather than silently skipping. The assertion logic itself was verified
green against a spec-conformant reference implementation before commit.

``build/findings/core-parity.md`` was ABSENT when this was written, so the exact
public name of the runtime cowork surface could not be confirmed; it is probed
by candidate name (see ``COWORK_SIGNAL_CANDIDATES``) and the runtime-signal test
skips until one resolves.

Run: python3 -m pytest tests/test_cowork_adapter.py -q
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
SCRIPTS = REPO / "skills" / "token-optimizer" / "scripts"
HOOKS_JSON = REPO / "hooks" / "hooks.json"

if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import cowork_install  # noqa: E402

# The master hooks template every emitted Cowork payload is trimmed from.
MASTER_TEMPLATE = json.loads(HOOKS_JSON.read_text(encoding="utf-8"))


# --------------------------------------------------------------------------- #
# cowork_install: COWORK_EVENTS + build_cowork_hooks
# --------------------------------------------------------------------------- #

# WEAK landed-guard: the SessionStart->UserPromptSubmit remap is the headline
# change, and its necessary precondition is that SessionStart leaves the fired
# event set. A partial/buggy landing (SessionStart gone but PostToolUse
# forgotten, or the remap botched) still passes this guard and fails the
# assertion below -- which is the point.
_COWORK_EVENTS_LANDED = "SessionStart" not in cowork_install.COWORK_EVENTS
_needs_cowork_events = pytest.mark.skipif(
    not _COWORK_EVENTS_LANDED,
    reason=(
        "core lane not yet landed: cowork_install.COWORK_EVENTS still contains "
        "'SessionStart', so the on-disk packager predates the Cowork "
        "SessionStart->UserPromptSubmit remap. Activates once that lane lands."
    ),
)


def _commands(hooks_by_event):
    """Yield (event, command) for every hook command in a build output map."""
    for event, groups in hooks_by_event.items():
        for group in groups:
            for hook in group.get("hooks", []):
                yield event, hook.get("command", "")


def _commands_for(hooks_by_event, event):
    return [command for evt, command in _commands(hooks_by_event) if evt == event]


@_needs_cowork_events
def test_cowork_events_are_exactly_the_cowork_firing_set():
    # Spec item 1: SessionStart is NOT in it; PostToolUse IS.
    assert cowork_install.COWORK_EVENTS == (
        "UserPromptSubmit",
        "PreToolUse",
        "PostToolUse",
        "Stop",
    )
    assert "SessionStart" not in cowork_install.COWORK_EVENTS


@_needs_cowork_events
def test_build_cowork_hooks_drops_sessionstart_and_non_cowork_events():
    # Spec item 2: no SessionStart key, no compaction/session-lifecycle keys.
    hooks = cowork_install.build_cowork_hooks(MASTER_TEMPLATE)["hooks"]
    assert "SessionStart" not in hooks
    for absent in ("PreCompact", "PostCompact", "SessionEnd", "StopFailure", "CwdChanged"):
        assert absent not in hooks, f"{absent} must not survive into the Cowork payload"
    assert set(hooks) == {"UserPromptSubmit", "PreToolUse", "PostToolUse", "Stop"}


@_needs_cowork_events
def test_userpromptsubmit_carries_remapped_runonce_plus_originals():
    # Issue #139: the six UserPromptSubmit subcommands (the three originals
    # -- quality-cache --warn, prompt-continuity, verbosity-steer -- plus the
    # three run-once features remapped off SessionStart: ensure-health,
    # quality-cache --force, compact-restore --new-session-only) are consolidated
    # into ONE dispatcher entry, hooks/userpromptsubmit_runner.py, which imports
    # measure.py once and runs all six in-process. The Cowork payload is a pure
    # trim of the master, so its UserPromptSubmit is that single dispatcher.
    hooks = cowork_install.build_cowork_hooks(MASTER_TEMPLATE)["hooks"]
    ups = _commands_for(hooks, "UserPromptSubmit")

    assert ups, "UserPromptSubmit must have at least one command"
    assert any("userpromptsubmit_runner.py" in command for command in ups), (
        "consolidated UserPromptSubmit dispatcher (userpromptsubmit_runner.py) "
        "missing from Cowork payload"
    )
    # The six former per-subcommand entries must NOT survive as separate
    # commands -- they are now internal to the runner.
    for former in (
        "quality-cache --warn",
        "prompt-continuity",
        "verbosity-steer",
        "ensure-health",
        "quality-cache --force",
        "compact-restore --new-session-only",
    ):
        assert not any(former in command for command in ups), (
            f"former UserPromptSubmit subcommand {former!r} should be inside the "
            "runner, not a separate Cowork command"
        )


@_needs_cowork_events
def test_keepwarm_command_is_dropped_everywhere():
    # Spec item 2: DROP_COMMAND_MARKERS still applied -- no keepwarm anywhere.
    hooks = cowork_install.build_cowork_hooks(MASTER_TEMPLATE)["hooks"]
    offenders = [command for _evt, command in _commands(hooks) if "keepwarm" in command]
    assert offenders == [], f"keepwarm commands survived into the Cowork payload: {offenders}"
    # Guard against the marker being silently emptied out of the source.
    assert any("keepwarm" in marker for marker in cowork_install.DROP_COMMAND_MARKERS)


@_needs_cowork_events
def test_compaction_matcher_commands_are_unsalvageable_and_dropped():
    # Spec item 2: no compaction-matcher command survives.
    hooks = cowork_install.build_cowork_hooks(MASTER_TEMPLATE)["hooks"]
    for _evt, command in _commands(hooks):
        assert "compact-restore --compact" not in command, command
        assert "clear-compacted" not in command, command


@_needs_cowork_events
def test_every_emitted_command_uses_plugin_root_resolver_and_run_py():
    # Spec item 3: every command keeps the ${CLAUDE_PLUGIN_ROOT} bash-resolver
    # form and points at run.py.
    hooks = cowork_install.build_cowork_hooks(MASTER_TEMPLATE)["hooks"]
    commands = [command for _evt, command in _commands(hooks)]
    assert commands, "expected at least one emitted command"
    for command in commands:
        assert "${CLAUDE_PLUGIN_ROOT}" in command, command
        assert "run.py" in command, command


# --------------------------------------------------------------------------- #
# runtime_env: Cowork container detection
# --------------------------------------------------------------------------- #

# CLAUDE_CODE_CONTAINER_ID is set inside Cowork's local/cloud VM. The exact
# public name of the cowork indicator the core lane adds is not yet known
# (build/findings/core-parity.md was absent), so probe the likely surface.
COWORK_SIGNAL_CANDIDATES = (
    "_cowork_signal",
    "cowork_signal",
    "is_cowork",
    "is_cowork_runtime",
    "is_cowork_session",
    "in_cowork",
    "detect_cowork",
    "_is_cowork",
    "cowork_active",
)

# Other-runtime / override env that must be neutralized for deterministic
# detection, plus the cowork marker itself (reset before each subtest sets it).
_RUNTIME_ENV_TO_CLEAR = (
    "TOKEN_OPTIMIZER_RUNTIME",
    "CODEX_HOME",
    "HERMES_HOME",
    "COPILOT_HOME",
    "TOKEN_OPTIMIZER_COPILOT_HOME",
    "TOKEN_OPTIMIZER_CURSOR_HOME",
    "CURSOR_PROJECT_DIR",
    "CURSOR_VERSION",
    "OPENCODE_BIN",
    "OPENCODE_CONFIG_DIR",
    "OPENCODE_DATA_DIR",
    "OPENCODE_CONFIG",
    "OPENCODE_CLIENT",
    "CLAUDE_PLUGIN_ROOT",
    "CLAUDE_PLUGIN_DATA",
    "CLAUDE_CODE_CONTAINER_ID",
)


def _cache_clear(fn):
    clear = getattr(fn, "cache_clear", None)
    if callable(clear):
        clear()


def _reset_runtime_env(monkeypatch, runtime_env):
    for var in _RUNTIME_ENV_TO_CLEAR:
        monkeypatch.delenv(var, raising=False)
    # Disable the best-effort ps ancestor scan so detection is deterministic in CI.
    monkeypatch.setenv("TOKEN_OPTIMIZER_NO_PROC_SCAN", "1")
    _cache_clear(runtime_env.detect_runtime)


def _find_cowork_signal(runtime_env):
    for name in COWORK_SIGNAL_CANDIDATES:
        fn = getattr(runtime_env, name, None)
        if callable(fn):
            return name, fn
    return None, None


def test_cowork_container_is_still_the_claude_runtime(monkeypatch):
    # Spec item 4: a Cowork container is still the "claude" runtime, never a new
    # runtime name. This asserts unconditionally (green now, spec-lock later): if
    # the core lane ever routes CLAUDE_CODE_CONTAINER_ID to a different runtime,
    # this fails loudly.
    import runtime_env  # noqa: E402  (lazy: keeps import side-effect-free at collect)

    _reset_runtime_env(monkeypatch, runtime_env)
    monkeypatch.setenv("CLAUDECODE", "1")
    monkeypatch.setenv("CLAUDE_CODE_CONTAINER_ID", "cowork-container-abc123")
    _cache_clear(runtime_env.detect_runtime)
    try:
        assert runtime_env.detect_runtime() == "claude"
    finally:
        _cache_clear(runtime_env.detect_runtime)


def test_cowork_signal_true_under_container_id(monkeypatch):
    # Spec item 4: with CLAUDE_CODE_CONTAINER_ID set, runtime_env reports the
    # cowork signal true (and false without it). Skips until the surface lands.
    import runtime_env  # noqa: E402

    name, fn = _find_cowork_signal(runtime_env)
    if fn is None:
        pytest.skip(
            "cowork detection surface not yet landed in runtime_env "
            f"(probed: {', '.join(COWORK_SIGNAL_CANDIDATES)}). "
            "build/findings/core-parity.md was absent, so the public name could "
            "not be confirmed; extend COWORK_SIGNAL_CANDIDATES once the core "
            "lane documents it."
        )

    _reset_runtime_env(monkeypatch, runtime_env)
    _cache_clear(fn)
    assert not fn(), f"{name}() should be False without CLAUDE_CODE_CONTAINER_ID"

    monkeypatch.setenv("CLAUDE_CODE_CONTAINER_ID", "cowork-container-xyz789")
    _cache_clear(fn)
    _cache_clear(runtime_env.detect_runtime)
    assert fn(), f"{name}() should be True when CLAUDE_CODE_CONTAINER_ID is set"
    # And the runtime is still claude while the cowork signal is up.
    assert runtime_env.detect_runtime() == "claude"
    _cache_clear(runtime_env.detect_runtime)

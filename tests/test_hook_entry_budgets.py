#!/usr/bin/env python3
"""Per-event hook latency budgets (PreToolUse / PostToolUse / Stop).

Context: a sustained container workload measured TO's hooks at 10.5s
(SessionStart), 7.3s (PreToolUse:Read) and 7.0s (Stop) averages, with 25/29/84
host TIMEOUTS and 372 cancelled PostToolUse:Bash hooks -- while the same
imports cost 0.2-0.4s locally. The host's hooks.json timeout is a KILL, not a
budget. These tests pin the self-imposed deadline that makes the host timeout a
backstop instead of the mechanism: an over-budget hook exits 0 producing no
output at all.

The budgets themselves are derived from measured cold (no __pycache__)
end-to-end cost; see the table in hook_runtime._entry_budget_rules' docstring
block.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest


REPO = Path(__file__).resolve().parent.parent
SCRIPTS = REPO / "skills" / "token-optimizer" / "scripts"
HOOKS = REPO / "hooks"
MODULE_RUNNER = HOOKS / "module_runner.py"

sys.path.insert(0, str(SCRIPTS))

from hook_runtime import (  # noqa: E402
    BUDGET_POSTTOOL,
    BUDGET_POSTTOOL_MEASURE,
    BUDGET_POSTTOOL_RUNNER,
    BUDGET_PRETOOL,
    BUDGET_PRETOOL_MEASURE,
    BUDGET_STOP,
    HookDeadline,
    arm_entry_budget,
    resolve_entry_budget,
)


# Smallest host ceiling we can evidence: Codex kills a hook at 25s; the
# hooks.json entries in this repo use 5-20s. Every budget must sit well under
# the SMALLEST of those (the 5s Stop keepwarm-arm entry).
SMALLEST_HOST_CEILING = 5.0


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------


def _hooks_json_entries():
    """Yield (event, module_name, script_args, host_timeout) for every
    run.py-dispatched entry in the real hooks.json."""
    cfg = json.loads((HOOKS / "hooks.json").read_text(encoding="utf-8"))
    for event, groups in cfg["hooks"].items():
        for group in groups:
            for hook in group.get("hooks", []):
                m = re.search(r'run\.py" (\S+)((?: [^;]*?)?); done', hook["command"])
                if not m:
                    continue
                path, rest = m.group(1), m.group(2).strip()
                module = path.rsplit("/", 1)[-1][:-3]
                args = rest.split() if rest else []
                yield event, module, args, hook.get("timeout")


def _stub_tree(tmp_path, module_name, body):
    """A minimal scripts_dir holding the real hook_runtime plus one stub."""
    d = tmp_path / "scripts"
    d.mkdir(exist_ok=True)
    shutil.copy(SCRIPTS / "hook_runtime.py", d / "hook_runtime.py")
    (d / f"{module_name}.py").write_text(body, encoding="utf-8")
    return d


def _run_entry(scripts_dir, module_name, args, *, timeout=20, env=None):
    e = os.environ.copy()
    e.pop("TOKEN_OPTIMIZER_HOOK_BUDGET_MS", None)
    if env:
        e.update(env)
    started = time.monotonic()
    proc = subprocess.run(
        [sys.executable, str(MODULE_RUNNER), str(scripts_dir), module_name, *args],
        capture_output=True,
        text=True,
        timeout=timeout,
        env=e,
        input="{}",
    )
    return proc, time.monotonic() - started


BLOCK_FOREVER = """
import sys, time
# Simulate the container pathology: a hook that will never finish in budget.
# Writes only AFTER the block, so a correctly-budgeted run produces no output.
time.sleep(60)
sys.stdout.write("SHOULD-NEVER-BE-PRINTED")
"""


# --------------------------------------------------------------------------
# 1. the budget table matches the real hooks.json, and clears every ceiling
# --------------------------------------------------------------------------

EXPECTED = {
    ("PreToolUse", "read_cache", "--quiet"): BUDGET_PRETOOL,
    ("PreToolUse", "bash_hook", "--quiet"): BUDGET_PRETOOL,
    ("PreToolUse", "refetch_guard", "--quiet"): BUDGET_PRETOOL,
    ("PreToolUse", "measure", "checkpoint-trigger"): BUDGET_PRETOOL_MEASURE,
    # hooks.json now registers ONE consolidated PostToolUse dispatcher in place
    # of the five entry points below, so this is the only PostToolUse row the
    # hooks.json walk can see. The five per-entry rules are NOT gone: the runner
    # resolves them itself for its per-subcommand budgets (pinned by
    # tests/test_posttooluse_runner.py::
    # test_runner_consumes_hook_runtime_per_entry_budgets) and they still enforce
    # for a script-mode install that invokes those entry points directly (pinned
    # by the over-budget parametrization below, which is unchanged).
    ("PostToolUse", "posttooluse_runner", ""): BUDGET_POSTTOOL_RUNNER,
    ("PreCompact", "measure", "dynamic-compact-instructions"): BUDGET_POSTTOOL_MEASURE,
    ("PreCompact", "measure", "compact-capture"): BUDGET_STOP,
    ("PreCompact", "read_cache", "--clear"): BUDGET_POSTTOOL_MEASURE,
    ("PostCompact", "measure", "quality-cache"): BUDGET_POSTTOOL_MEASURE,
    ("CwdChanged", "read_cache", "--clear"): BUDGET_POSTTOOL_MEASURE,
    ("StopFailure", "measure", "compact-capture"): BUDGET_STOP,
}

# Events whose hooks.json entries are consolidated dispatchers that arm their
# own shared HookDeadline internally (sessionstart_runner, stop_runner,
# userpromptsubmit_runner). They must keep the previous behaviour
# (module_runner's 110s backstop), i.e. resolve to no entry budget.
UNBUDGETED_EVENTS = {
    "SessionStart",
    "SessionEnd",
    "Stop",
    "UserPromptSubmit",
}


def test_every_in_scope_hooks_json_entry_has_the_expected_budget():
    seen = set()
    for event, module, args, _timeout in _hooks_json_entries():
        if event in UNBUDGETED_EVENTS:
            continue
        key = (event, module, args[0] if args else "")
        assert key in EXPECTED, f"unmapped in-scope entry: {event} {module} {args}"
        secs, label = resolve_entry_budget(module, args)
        assert secs == EXPECTED[key], f"{event} {module} {args}: {secs} != {EXPECTED[key]}"
        assert label
        seen.add(key)
    assert seen == set(EXPECTED), f"hooks.json missing: {set(EXPECTED) - seen}"


def test_off_limits_events_keep_previous_behaviour_and_get_no_entry_budget():
    """SessionStart / UserPromptSubmit / SessionEnd / Stop are owned
    elsewhere; re-budgeting them as a side effect would be a regression.

    Notably `session-end-flush --trigger end` (SessionEnd, 60s ceiling,
    detached worker) must NOT collide with the Stop-scoped
    `session-end-flush --trigger stop` rule, and `compact-capture --trigger
    auto` (PreCompact) is budgeted separately from the Stop-scoped one.
    """
    for event, module, args, _timeout in _hooks_json_entries():
        if event not in UNBUDGETED_EVENTS:
            continue
        secs, label = resolve_entry_budget(module, args)
        assert secs is None and label is None, (
            f"{event} {module} {args} was re-budgeted to {secs}s; that event is "
            "owned by another engineer and must keep the 110s backstop"
        )


def test_every_budget_clears_the_smallest_host_ceiling_with_margin():
    """A budget only helps if it fires before the host kills the process."""
    for event, module, args, timeout in _hooks_json_entries():
        secs, _ = resolve_entry_budget(module, args)
        if secs is None:
            continue
        assert timeout is not None
        assert secs <= timeout / 2.0, (
            f"{event} {module} {args}: budget {secs}s is not comfortably under "
            f"its own {timeout}s host ceiling"
        )
        assert secs < SMALLEST_HOST_CEILING, (
            f"{event} {module} {args}: budget {secs}s exceeds the smallest host "
            f"ceiling ({SMALLEST_HOST_CEILING}s)"
        )


# --------------------------------------------------------------------------
# 2. enforcement: over budget -> exit 0, no output, no hang
#    THIS IS THE NEGATIVE TEST. Before the change module_runner armed a flat
#    110s for every entry point, so these stubs ran the full 60s sleep and the
#    20s subprocess timeout raised TimeoutExpired.
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "module,args,budget",
    [
        ("read_cache", ["--quiet"], BUDGET_PRETOOL),
        ("bash_hook", ["--quiet"], BUDGET_PRETOOL),
        ("refetch_guard", ["--quiet"], BUDGET_PRETOOL),
        ("bash_compress_hook", ["--quiet"], BUDGET_POSTTOOL),
        ("archive_result", ["--quiet"], BUDGET_POSTTOOL),
        ("context_intel", ["--quiet"], BUDGET_POSTTOOL),
        ("read_cache", ["--invalidate", "--quiet"], BUDGET_POSTTOOL),
        ("measure", ["quality-cache", "--quiet", "--throttle-only"], BUDGET_POSTTOOL_MEASURE),
        ("measure", ["checkpoint-trigger", "--quiet"], BUDGET_PRETOOL_MEASURE),
        ("measure", ["compact-capture", "--trigger", "stop", "--quiet"], BUDGET_STOP),
        ("measure", ["session-end-flush", "--trigger", "stop", "--quiet"], BUDGET_STOP),
        ("measure", ["keepwarm-arm", "--quiet"], BUDGET_STOP),
        ("codex_hook_bridge", ["session-start"], 4.5),
        ("codex_hook_bridge", ["user-prompt-submit"], 4.5),
        ("codex_hook_bridge", ["subagent-start"], 2.5),
        ("codex_hook_bridge", ["subagent-stop"], 2.5),
        ("measure", ["dynamic-compact-instructions", "--quiet"], BUDGET_POSTTOOL_MEASURE),
        ("measure", ["compact-capture", "--trigger", "auto", "--quiet"], BUDGET_STOP),
        ("measure", ["quality-cache", "--force", "--quiet"], BUDGET_POSTTOOL_MEASURE),
        ("read_cache", ["--clear", "--quiet"], BUDGET_POSTTOOL_MEASURE),
    ],
)
def test_over_budget_entry_exits_zero_with_no_output_and_does_not_hang(
    tmp_path, module, args, budget
):
    scripts = _stub_tree(tmp_path, module, BLOCK_FOREVER)
    proc, elapsed = _run_entry(scripts, module, args, timeout=20)

    assert proc.returncode == 0, f"expected clean exit 0, got {proc.returncode}"
    assert proc.stdout == "", f"over-budget hook wrote stdout: {proc.stdout!r}"
    assert proc.stderr == "", f"over-budget hook wrote stderr: {proc.stderr!r}"
    # Fired at the budget, not at the host ceiling and not at the 110s backstop.
    assert elapsed < budget + 3.0, f"took {elapsed:.1f}s against a {budget}s budget"
    assert elapsed >= budget * 0.5, (
        f"exited after {elapsed:.2f}s, well before its {budget}s budget -- the "
        "deadline is firing early, not on time"
    )


def test_off_limits_entry_is_not_cut_short_by_an_entry_budget(tmp_path):
    """A SessionStart-shaped entry must still be running after every in-scope
    budget would have expired. Guards against a table change that silently
    starts capping the events other engineers own."""
    body = """
import sys, time
time.sleep(6)
sys.stdout.write("STILL-ALIVE")
"""
    scripts = _stub_tree(tmp_path, "measure", body)
    proc, elapsed = _run_entry(
        scripts, "measure", ["ensure-health", "--once-mark"], timeout=25
    )
    assert proc.returncode == 0
    assert "STILL-ALIVE" in proc.stdout, (
        "an unbudgeted (off-limits) entry was cut short by an entry budget"
    )
    assert elapsed >= 5.5


# --------------------------------------------------------------------------
# 3. normal path unchanged
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "module,args",
    [
        ("read_cache", ["--quiet"]),
        ("measure", ["quality-cache", "--quiet", "--throttle-only"]),
        ("measure", ["compact-capture", "--trigger", "stop", "--quiet"]),
    ],
)
def test_under_budget_entry_keeps_its_output_and_exit_code(tmp_path, module, args):
    body = """
import sys
sys.stdout.write('{"hookSpecificOutput": {"ok": true}}')
sys.stderr.write("diagnostic\\n")
"""
    scripts = _stub_tree(tmp_path, module, body)
    proc, elapsed = _run_entry(scripts, module, args, timeout=20)
    assert proc.returncode == 0
    assert json.loads(proc.stdout)["hookSpecificOutput"]["ok"] is True
    assert "diagnostic" in proc.stderr
    assert elapsed < 5.0


def test_budget_does_not_change_how_a_raising_hook_behaves(tmp_path):
    """module_runner deliberately lets a hook exception propagate (run.py is
    the fail-open layer that turns it into exit 0). Arming a budget must not
    alter that either way, so compare budgeted against budget-disabled."""
    scripts = _stub_tree(tmp_path, "read_cache", "raise RuntimeError('boom')\n")
    budgeted, _ = _run_entry(scripts, "read_cache", ["--quiet"], timeout=20)
    unbudgeted, _ = _run_entry(
        scripts,
        "read_cache",
        ["--quiet"],
        timeout=20,
        env={"TOKEN_OPTIMIZER_HOOK_BUDGET_MS": "0"},
    )
    assert budgeted.returncode == unbudgeted.returncode
    assert "RuntimeError" in budgeted.stderr and "RuntimeError" in unbudgeted.stderr


def test_run_py_still_fails_open_when_a_budgeted_hook_raises(tmp_path):
    """End-to-end through the real dispatcher: a raising budgeted hook must
    still surface to the host as exit 0, budget or no budget."""
    scripts = _stub_tree(tmp_path, "read_cache", "raise RuntimeError('boom')\n")
    plugin_root = tmp_path / "plugin"
    (plugin_root / "hooks").mkdir(parents=True)
    shutil.copy(HOOKS / "run.py", plugin_root / "hooks" / "run.py")
    shutil.copy(MODULE_RUNNER, plugin_root / "hooks" / "module_runner.py")
    target = plugin_root / "skills" / "token-optimizer" / "scripts"
    target.mkdir(parents=True)
    for f in scripts.iterdir():
        shutil.copy(f, target / f.name)

    env = os.environ.copy()
    env["CLAUDE_PLUGIN_ROOT"] = str(plugin_root)
    env.pop("TOKEN_OPTIMIZER_HOOK_BUDGET_MS", None)
    proc = subprocess.run(
        [
            sys.executable,
            str(plugin_root / "hooks" / "run.py"),
            "skills/token-optimizer/scripts/read_cache.py",
            "--quiet",
        ],
        capture_output=True,
        text=True,
        env=env,
        input="{}",
        timeout=30,
    )
    assert proc.returncode == 0, "run.py must fail open for the host"


# --------------------------------------------------------------------------
# 4. the silent-deadline primitive
# --------------------------------------------------------------------------


def test_silent_deadline_writes_nothing_but_the_default_still_warns(tmp_path):
    """message=b"" must mean SILENT. Before the fix HookDeadline used
    `message or <default>`, so an empty message fell back to the default and
    every over-budget hot-path hook printed '[Token Optimizer] hook budget
    exceeded' onto the host's stderr."""
    env = os.environ.copy()
    env["PYTHONPATH"] = str(SCRIPTS)

    def _run(ctor):
        return subprocess.run(
            [
                sys.executable,
                "-c",
                f"from hook_runtime import HookDeadline\n{ctor}\nimport time\ntime.sleep(5)\n",
            ],
            capture_output=True,
            text=True,
            env=env,
            timeout=15,
        )

    silent = _run('HookDeadline(0.2, message=b"").start()')
    assert silent.returncode == 0
    assert silent.stdout == "" and silent.stderr == "", (
        f"silent deadline emitted output: {silent.stdout!r} {silent.stderr!r}"
    )

    default = _run("HookDeadline(0.2).start()")
    assert default.returncode == 0
    assert "hook budget exceeded" in default.stderr, (
        "the default diagnostic regressed; the 110s/60s backstop tests rely on it"
    )


def test_arm_entry_budget_returns_none_for_an_unbudgeted_entry():
    assert arm_entry_budget("measure", ["ensure-health", "--once-mark"]) is None
    assert arm_entry_budget("totally_unknown_module", ["--quiet"]) is None


def test_arm_entry_budget_arms_a_silent_deadline_for_a_budgeted_entry():
    deadline = arm_entry_budget("read_cache", ["--quiet"])
    try:
        assert isinstance(deadline, HookDeadline)
        assert deadline.message == b"", "entry budgets must be silent"
        assert deadline.seconds == BUDGET_PRETOOL
    finally:
        if deadline is not None:
            deadline.cancel()


# --------------------------------------------------------------------------
# 5. operator override
# --------------------------------------------------------------------------


def test_env_override_sets_and_disables_the_budget(monkeypatch):
    monkeypatch.setenv("TOKEN_OPTIMIZER_HOOK_BUDGET_MS", "1500")
    assert resolve_entry_budget("read_cache", ["--quiet"])[0] == 1.5

    monkeypatch.setenv("TOKEN_OPTIMIZER_HOOK_BUDGET_MS", "0")
    assert resolve_entry_budget("read_cache", ["--quiet"]) == (None, None)

    monkeypatch.setenv("TOKEN_OPTIMIZER_HOOK_BUDGET_MS", "not-a-number")
    assert resolve_entry_budget("read_cache", ["--quiet"])[0] == BUDGET_PRETOOL

    # An override must never resurrect a budget for an off-limits entry.
    monkeypatch.setenv("TOKEN_OPTIMIZER_HOOK_BUDGET_MS", "1500")
    assert resolve_entry_budget("measure", ["ensure-health", "--once-mark"]) == (
        None,
        None,
    )


def test_disabled_budget_falls_back_to_the_110s_backstop(tmp_path):
    scripts = _stub_tree(tmp_path, "read_cache", "import sys; sys.stdout.write('OK')\n")
    proc, _ = _run_entry(
        scripts,
        "read_cache",
        ["--quiet"],
        env={"TOKEN_OPTIMIZER_HOOK_BUDGET_MS": "0"},
    )
    assert proc.returncode == 0 and proc.stdout == "OK"


# --------------------------------------------------------------------------
# 6. mirrors
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "canonical,mirrors",
    [
        (
            "hooks/module_runner.py",
            [
                "plugins/token-optimizer/hooks/module_runner.py",
                "cowork/token-optimizer/hooks/module_runner.py",
            ],
        ),
        (
            "skills/token-optimizer/scripts/hook_runtime.py",
            [
                "plugins/token-optimizer/skills/token-optimizer/scripts/hook_runtime.py",
                "cowork/token-optimizer/skills/token-optimizer/scripts/hook_runtime.py",
            ],
        ),
    ],
)
def test_budget_code_is_identical_in_every_mirror(canonical, mirrors):
    want = (REPO / canonical).read_bytes()
    for mirror in mirrors:
        path = REPO / mirror
        if not path.exists():
            pytest.skip(f"{mirror} not present in this tree")
        assert path.read_bytes() == want, f"{mirror} drifted from {canonical}"

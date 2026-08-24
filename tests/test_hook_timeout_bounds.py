#!/usr/bin/env python3
"""Every hooks.json entry must carry an explicit, bounded timeout.

Claude Code's default `command` hook timeout is 600 seconds (30 for
UserPromptSubmit). A hook that omits `timeout` therefore inherits a ten-minute
stall budget: when it wedges, the user's tool call blocks for ten minutes with
no signal that a hook is responsible.

That is not hypothetical. A Windows field report covering 50 sessions showed 36
hook timeouts costing 7,148 seconds, and 6,600s of that -- 92% -- came from 11
events on hooks that had no `timeout` key at all. Every one of those hooks was
healthy at p50; they were slow only in the tail, and the tail was unbounded.

`async: true` is NOT a substitute. scripts/sync-codex-marketplace-plugin.sh
strips async from the mirrored config (Codex skips async hooks entirely), so an
async-but-untimed hook ships to Codex users as a blocking hook with the full
600s default. Both trees are checked here for that reason.

Run: python3 -m pytest tests/test_hook_timeout_bounds.py -v
"""

import json
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
HOOKS_JSON = REPO / "hooks" / "hooks.json"
MIRROR_HOOKS_JSON = REPO / "plugins" / "token-optimizer" / "hooks" / "hooks.json"

# Events that fire on every tool call or every prompt. A timeout here is paid
# repeatedly through a session, so the ceiling is tight on purpose.
HOT_PATH_EVENTS = {"PreToolUse", "PostToolUse", "UserPromptSubmit"}
HOT_PATH_CEILING = 20

# Session-lifecycle events fire once per session, compaction, or turn end.
LIFECYCLE_CEILING = 60

# Below this, a healthy hook on a slow host (Windows git-bash pays ~75ms for the
# bash spawn plus ~80ms of Python start before doing any work) would be killed
# mid-run and the feature would silently stop working.
FLOOR = 5

# Codex hard-caps SessionEnd hook timeouts at 3s (below FLOOR) and prints a
# load-time issue when a SessionEnd hook declares more, so the Codex mirror
# pre-clamps SessionEnd to this value (see scripts/sync-codex-marketplace-plugin.sh).
# Safe below the floor: that hook runs `session-end-flush --defer`, which only
# spawns a detached worker and returns in well under a second. The root tree keeps
# its Claude-tuned 60s. Exactly this (codex-mirror, SessionEnd, ==cap) entry is
# exempt from the floor and from root/mirror timeout parity.
CODEX_SESSION_END_TIMEOUT_CAP = 3


def _is_codex_clamped_session_end(tree, event, value):
    return (
        tree == "codex-mirror"
        and event == "SessionEnd"
        and value == CODEX_SESSION_END_TIMEOUT_CAP
    )


def _flatten(path):
    """Yield (event, matcher, command, hook_dict) for every hook command entry."""
    data = json.loads(path.read_text(encoding="utf-8"))
    for event, groups in data["hooks"].items():
        for group in groups:
            for hook in group["hooks"]:
                yield event, group.get("matcher"), hook.get("command", ""), hook


def _label(event, matcher, command):
    tail = command.split("scripts/")[-1].split(";")[0].strip()
    return f"{event}[{matcher}] {tail}"


def _both_trees():
    return [("root", HOOKS_JSON), ("codex-mirror", MIRROR_HOOKS_JSON)]


def test_every_hook_declares_a_timeout():
    """No hook may inherit the host's 600s default, async or not."""
    missing = []
    for tree, path in _both_trees():
        for event, matcher, command, hook in _flatten(path):
            if "timeout" not in hook:
                missing.append(f"{tree}: {_label(event, matcher, command)}")
    assert not missing, "hooks with no explicit timeout:\n  " + "\n  ".join(missing)


def test_timeouts_are_positive_integer_seconds():
    """`timeout` is seconds, not milliseconds. A 15000 here means 4+ hours."""
    bad = []
    for tree, path in _both_trees():
        for event, matcher, command, hook in _flatten(path):
            value = hook.get("timeout")
            if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
                bad.append(f"{tree}: {_label(event, matcher, command)} -> {value!r}")
    assert not bad, "timeout must be a positive int (seconds):\n  " + "\n  ".join(bad)


def test_hot_path_hooks_stay_under_the_ceiling():
    """Per-tool-call and per-prompt hooks may not book a long stall."""
    over = []
    for tree, path in _both_trees():
        for event, matcher, command, hook in _flatten(path):
            if event not in HOT_PATH_EVENTS:
                continue
            value = hook.get("timeout")
            if isinstance(value, int) and value > HOT_PATH_CEILING:
                over.append(f"{tree}: {_label(event, matcher, command)} -> {value}s")
    assert not over, (
        f"hot-path hooks over the {HOT_PATH_CEILING}s ceiling:\n  " + "\n  ".join(over)
    )


def test_lifecycle_hooks_stay_under_the_ceiling():
    """Session/compaction hooks are rarer but still must not stall for minutes."""
    over = []
    for tree, path in _both_trees():
        for event, matcher, command, hook in _flatten(path):
            if event in HOT_PATH_EVENTS:
                continue
            value = hook.get("timeout")
            if isinstance(value, int) and value > LIFECYCLE_CEILING:
                over.append(f"{tree}: {_label(event, matcher, command)} -> {value}s")
    assert not over, (
        f"lifecycle hooks over the {LIFECYCLE_CEILING}s ceiling:\n  " + "\n  ".join(over)
    )


def test_timeouts_leave_room_for_a_healthy_run():
    """A timeout so tight it kills healthy runs disables the feature silently."""
    tight = []
    for tree, path in _both_trees():
        for event, matcher, command, hook in _flatten(path):
            value = hook.get("timeout")
            if _is_codex_clamped_session_end(tree, event, value):
                continue  # Codex forces 3s here; --defer returns instantly.
            if isinstance(value, int) and value < FLOOR:
                tight.append(f"{tree}: {_label(event, matcher, command)} -> {value}s")
    assert not tight, f"timeouts below the {FLOOR}s floor:\n  " + "\n  ".join(tight)


def test_mirror_timeouts_match_the_root():
    """The Codex mirror strips async and clamps SessionEnd to Codex's 3s cap;
    every OTHER timeout must equal the root. Any other drift is a bug."""
    root = {
        (e, m, c): h.get("timeout") for e, m, c, h in _flatten(HOOKS_JSON)
    }
    mirror = {
        (e, m, c): h.get("timeout") for e, m, c, h in _flatten(MIRROR_HOOKS_JSON)
    }
    assert root.keys() == mirror.keys(), "hook sets differ between root and mirror"
    drifted = []
    for key in root:
        event = key[0]
        if root[key] == mirror[key]:
            continue
        # The one sanctioned divergence: SessionEnd clamped down to Codex's cap.
        if (
            event == "SessionEnd"
            and mirror[key] == CODEX_SESSION_END_TIMEOUT_CAP
            and isinstance(root[key], int)
            and root[key] > CODEX_SESSION_END_TIMEOUT_CAP
        ):
            continue
        drifted.append(f"{_label(*key)}: root={root[key]}s mirror={mirror[key]}s")
    assert not drifted, "timeout drift:\n  " + "\n  ".join(drifted)

#!/usr/bin/env python3
"""Runtime thrash guard: nudge-only loop prevention across turns (Track F lever 1).

Why this exists
---------------
A per-command wrapper (e.g. JFrog Boost) is exec'd once per command and keeps
no cross-turn state, so it structurally cannot see an agent quietly re-running
the same command — the failure mode Boost's own blog describes ("an agent
quietly running `ls` six times to re-establish where it is ... So we needed the
agent itself to tell us. In band.") and their issue #35 (an agent looping
"until the user interrupts", caused by Boost and undetected). Token Optimizer
is a session-stateful hook, so it can see the streak and say something.

Design (nudge-only):
- Fire only on >= 3 consecutive runs of the SAME command with BYTE-IDENTICAL
  output. Any material output change resets the streak to 1, so a command
  whose output is evolving (progress bars, growing logs) never fires.
- Never deny a tool call: the caller appends the nudge line to the output the
  agent already has. The command has already run; nothing is blocked.
- Cooldown: after a nudge at streak S, the next nudge waits until streak
  S + REPEAT_AFTER, so a long stuck loop is reminded periodically, not
  every turn.
- Staleness: a streak older than STALE_SECONDS is reset — repeats spaced
  hours apart are deliberate re-checks, not thrash.
- Fail-open everywhere: any error returns None and the output stands.

The state lives in the per-session SessionStore (command_run_streaks), so
streaks never leak across sessions.
"""

from __future__ import annotations

import os
import re
import time

# Fire once a command has produced byte-identical output this many times in
# a row (inclusive). 3 = the documented "ran `ls` six times" pattern minus
# one grace run for legitimate re-checks.
STREAK_THRESHOLD = 3
# After a nudge at streak S, the next nudge fires at streak S + REPEAT_AFTER.
REPEAT_AFTER = 3
# A streak older than this is deliberate re-checking, not thrash: reset.
STALE_SECONDS = 1800
# Outputs shorter than this are not worth a nudge line (a bare "" or "0").
MIN_OUTPUT_CHARS = 2
# Session IDs must match this pattern (alphanumeric + - and _). An invalid
# session ID would cause SessionStore to generate a fresh fallback UUID per
# call, preventing streak accumulation. Rejecting it here makes the silent
# disablement explicit rather than silently broken.
_VALID_SESSION_ID = re.compile(r"^[a-zA-Z0-9_-]+$")

_NUDGE_TEMPLATE = (
    "[Token Optimizer: `{label}` has now run {streak} times this session with "
    "byte-identical output. Re-running it will not change the result; either "
    "change the approach, or state plainly what is blocking you.]"
)


def _sanitize_label(command: str) -> str:
    """Sanitize a command for use in the nudge label.

    Strips backticks and instruction-like phrases so the nudge cannot be used
    as a prompt-injection vector via the echoed command text. The agent
    already sees the full command in the tool input; the nudge label is for
    identification, not verbatim replay.
    """
    label = command[:60]
    # Remove backticks so the label cannot break out of the template's code span.
    label = label.replace("`", "'")
    return label


def check(command: str, output: str, now: float | None = None):
    """Record this Bash run and return a nudge line when the streak warrants it.

    Returns None when there is nothing to say (the overwhelmingly common case).
    Never raises; never denies — the caller decides how to surface the nudge.
    ``now`` is injectable for tests and defaults to ``time.time()``.
    """
    try:
        if not command or not output or len(output) < MIN_OUTPUT_CHARS:
            return None
        session_id = os.environ.get("CLAUDE_SESSION_ID", "")
        if not session_id or not _VALID_SESSION_ID.match(session_id):
            return None

        from session_store import SessionStore
        from delta_diff import content_hash
        from archive_result import _redact_credentials

        stripped = command.strip()
        cmd_h = content_hash(stripped)
        out_h = content_hash(output)
        # Redact before persisting, mirroring the cross-turn dedup path
        # (archive_result._redact_credentials): the command line can carry
        # inline secrets (-pPASSWORD, an auth header, a connection string) and
        # must never reach the on-disk streak store. The label shown to the
        # agent stays on the live (unredacted) command the agent already sees.
        safe_command = _redact_credentials(stripped)[:500]
        now = time.time() if now is None else now
        store = SessionStore(session_id)
        try:
            prior = store.get_command_streak(cmd_h)
            if (
                prior
                and prior.get("output_hash") == out_h
                and now - float(prior.get("last_ts") or 0) <= STALE_SECONDS
            ):
                streak = int(prior.get("streak") or 0) + 1
                nudged_streak = prior.get("nudged_streak")
                nudged_streak = int(nudged_streak) if nudged_streak is not None else None
            else:
                # Material change (different output), a new command, or a stale
                # streak: start over. This is the "never fire when the output
                # changed materially" guarantee.
                streak = 1
                nudged_streak = None

            fire = streak >= STREAK_THRESHOLD and (
                nudged_streak is None or streak >= nudged_streak + REPEAT_AFTER
            )
            store.upsert_command_streak(
                cmd_h, safe_command, out_h, streak, streak if fire else nudged_streak, now
            )
            if not fire:
                return None
            return _NUDGE_TEMPLATE.format(label=_sanitize_label(stripped), streak=streak)
        finally:
            store.close()
    except Exception:
        return None

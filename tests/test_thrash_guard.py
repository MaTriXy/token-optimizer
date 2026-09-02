"""Runtime thrash guard: nudge-only loop prevention across turns (Track F lever 1).

A per-command wrapper (e.g. Boost) is exec'd per command with no cross-turn
state, so it cannot see an agent re-running the same command with identical
output — the exact failure mode Boost's own blog describes and their issue #35
shows causing an infinite loop. Token Optimizer is session-stateful, so the
PostToolUse Bash path records every run and nudges on a >= 3 identical-output
streak. Contracts guarded here:

1. Fires on the 3rd byte-identical run, not before; nudge is one line and
   names the command and the streak.
2. Any material output change resets the streak (never fires on change).
3. Cooldown: after a nudge at streak S, the next waits until S + REPEAT_AFTER.
4. Stale streaks (STALE_SECONDS) reset instead of firing.
5. No session id / empty output -> silent no-op (fail-open).
6. Integration: through bash_compress_hook.main(), the nudge is APPENDED to
   the original stdout in updatedToolOutput — the tool result is never
   denied, replaced with less information, or blocked.
7. The nudge path never suppresses normal compression for non-thrash output.
"""
import importlib
import json
import os
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parent.parent / "skills" / "token-optimizer" / "scripts"
BASH_COMPRESS_HOOK = SCRIPTS / "bash_compress_hook.py"


@pytest.fixture()
def guard(monkeypatch):
    tmp = tempfile.mkdtemp(prefix="to-thrash-")
    monkeypatch.setenv("TOKEN_OPTIMIZER_SNAPSHOT_DIR", tmp)
    monkeypatch.setenv("CLAUDE_SESSION_ID", "test-thrash-" + uuid.uuid4().hex[:8])
    sys.path.insert(0, str(SCRIPTS))
    for m in ("thrash_guard", "session_store", "delta_diff"):
        sys.modules.pop(m, None)
    mod = importlib.import_module("thrash_guard")
    importlib.reload(mod)
    yield mod
    sys.modules.pop("thrash_guard", None)


def test_first_two_runs_stay_silent(guard):
    assert guard.check("ls -la", "file_a\nfile_b\n") is None
    assert guard.check("ls -la", "file_a\nfile_b\n") is None


def test_third_identical_run_nudges(guard):
    out = "file_a\nfile_b\n"
    guard.check("ls -la", out)
    guard.check("ls -la", out)
    nudge = guard.check("ls -la", out)
    assert nudge is not None
    assert "ls -la" in nudge
    assert "3 times" in nudge
    assert "\n" not in nudge  # one line


def test_output_change_resets_streak(guard):
    guard.check("ls -la", "a\n")
    guard.check("ls -la", "a\n")
    # Material change: streak must restart, so the next two runs stay silent.
    assert guard.check("ls -la", "a\nb\n") is None
    assert guard.check("ls -la", "a\nb\n") is None
    assert guard.check("ls -la", "a\nb\n") is not None  # new streak reached 3


def test_cooldown_after_nudge(guard):
    out = "same\n"
    guard.check("make test", out)
    guard.check("make test", out)
    assert guard.check("make test", out) is not None          # streak 3: fire
    assert guard.check("make test", out) is None              # streak 4: cooldown
    assert guard.check("make test", out) is None              # streak 5: cooldown
    assert guard.check("make test", out) is not None          # streak 6: fire again


def test_stale_streak_resets(guard):
    out = "same\n"
    t0 = time.time()
    guard.check("git status", out, now=t0)
    guard.check("git status", out, now=t0)
    # A repeat after the stale window is a deliberate re-check: silent, and
    # the streak restarts (so the next two repeats stay silent too).
    stale = t0 + guard.STALE_SECONDS + 10
    assert guard.check("git status", out, now=stale) is None
    assert guard.check("git status", out, now=stale) is None
    assert guard.check("git status", out, now=stale) is not None


def test_no_session_is_silent(guard, monkeypatch):
    monkeypatch.delenv("CLAUDE_SESSION_ID", raising=False)
    assert guard.check("ls", "a\n") is None


def test_empty_output_is_silent(guard):
    assert guard.check("ls", "") is None
    assert guard.check("ls", "a") is None  # below MIN_OUTPUT_CHARS


def test_distinct_commands_do_not_share_streaks(guard):
    guard.check("ls -la", "x\n")
    guard.check("ls -la", "x\n")
    assert guard.check("git status", "x\n") is None
    assert guard.check("git status", "x\n") is None
    assert guard.check("git status", "x\n") is not None


def test_persisted_command_is_redacted(guard):
    cmd = "curl -sS 'https://api.example.com/x?token=FAKE_SECRET_VALUE_123'"
    out = "ok\nok\n"
    guard.check(cmd, out)
    guard.check(cmd, out)
    guard.check(cmd, out)
    from session_store import SessionStore
    from delta_diff import content_hash
    store = SessionStore(os.environ["CLAUDE_SESSION_ID"])
    row = store.get_command_streak(content_hash(cmd.strip()))
    store.close()
    assert row is not None
    assert "FAKE_SECRET_VALUE_123" not in row["command_text"], row["command_text"]


@pytest.mark.parametrize("cmd,secret", [
    ("mysql -u root -pSecretPass123 -e 'SELECT 1'", "SecretPass123"),
    ("sshpass -p mysecret ssh user@host", "mysecret"),
    ("redis-cli -a myredisauth GET foo", "myredisauth"),
    ("psql --password=hunter2 -U user", "hunter2"),
    ("psql --password hunter2 -U user", "hunter2"),
    ("mysql -p SecretPass -e 'SELECT 1'", "SecretPass"),
    ("mariadb -u root -pMypass -e 'SELECT 1'", "Mypass"),
])
def test_inline_cli_password_redacted_in_streak_store(guard, cmd, secret):
    """Inline CLI password flags must be redacted before persisting to
    command_run_streaks (the thrash guard's streak store)."""
    out = "ok\nok\n"
    guard.check(cmd, out)
    from session_store import SessionStore
    from delta_diff import content_hash
    store = SessionStore(os.environ["CLAUDE_SESSION_ID"])
    row = store.get_command_streak(content_hash(cmd.strip()))
    store.close()
    assert row is not None, "streak row must exist"
    assert secret not in row["command_text"], (
        f"secret {secret!r} leaked into streak store: {row['command_text']!r}"
    )


@pytest.mark.parametrize("cmd,secret", [
    ("mysql -u root -pSecretPass123 -e 'SELECT 1'", "SecretPass123"),
    ("sshpass -p mysecret ssh user@host", "mysecret"),
    ("redis-cli -a myredisauth GET foo", "myredisauth"),
    ("psql --password=hunter2 -U user", "hunter2"),
])
def test_inline_cli_password_redacted_in_command_outputs(guard, cmd, secret):
    """Inline CLI password flags must be redacted before persisting to
    command_outputs (the cross-turn dedup store, written by
    bash_compress_hook._crossturn_dedup)."""
    out = "ok\nok\n"
    # Run twice so the cross-turn dedup path stores the command
    guard.check(cmd, out)
    guard.check(cmd, out)
    from session_store import SessionStore
    from delta_diff import content_hash
    store = SessionStore(os.environ["CLAUDE_SESSION_ID"])
    row = store.get_command_output(content_hash(cmd.strip()))
    store.close()
    if row is not None:
        assert secret not in row["command_text"], (
            f"secret {secret!r} leaked into command_outputs: {row['command_text']!r}"
        )


# ---------------------------------------------------------------------------
# Integration: through bash_compress_hook.main() — nudge appended, never denied
# ---------------------------------------------------------------------------

def _payload(command, stdout, session_id):
    return json.dumps({
        "session_id": session_id,
        "transcript_path": "/tmp/transcript.jsonl",
        "cwd": "/Users/test/project",
        "hook_event_name": "PostToolUse",
        "tool_name": "Bash",
        "tool_input": {"command": command},
        "tool_response": {"stdout": stdout, "stderr": "",
                          "interrupted": False, "isImage": False},
    })


def _run_hook(payload: str) -> subprocess.CompletedProcess:
    env = {**os.environ}
    env.pop("CLAUDE_PLUGIN_ROOT", None)
    return subprocess.run(
        [sys.executable, str(BASH_COMPRESS_HOOK)],
        input=payload, capture_output=True, text=True,
        encoding="utf-8", errors="replace", timeout=30, env=env,
    )


def _updated_stdout(proc):
    if not proc.stdout.strip():
        return None
    try:
        return json.loads(proc.stdout)["hookSpecificOutput"]["updatedToolOutput"]["stdout"]
    except (json.JSONDecodeError, KeyError, TypeError):
        return None


def test_hook_appends_nudge_and_never_denies():
    sid = "test-thrash-int-" + uuid.uuid4().hex[:8]
    out = "Error: connection refused\n" * 3
    cmd = "pytest -q tests/test_flaky.py"
    for _ in range(2):
        proc = _run_hook(_payload(cmd, out, sid))
        assert proc.returncode == 0
    proc = _run_hook(_payload(cmd, out, sid))
    assert proc.returncode == 0
    updated = _updated_stdout(proc)
    assert updated is not None, "nudge run must emit updatedToolOutput"
    assert updated.startswith(out), "original output must be preserved verbatim"
    assert "byte-identical output" in updated
    assert "change the approach" in updated


def test_hook_stays_silent_below_threshold():
    sid = "test-thrash-int-" + uuid.uuid4().hex[:8]
    proc = _run_hook(_payload("ls -la", "a\nb\nc\n", sid))
    assert proc.returncode == 0
    assert _updated_stdout(proc) is None  # small output, first run: pass through

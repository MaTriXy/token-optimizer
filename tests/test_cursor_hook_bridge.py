"""Cursor hook bridge: payload decoding, tally lifecycle, and output contracts.

Covers the bridge behaviours that the Copilot adapter does NOT share with
Cursor, so they can't be inherited by copy:

  - snake_case payload decoding (object or JSON-string ``tool_input``) with a
    traversal-safe conversation-id sanitizer;
  - the durable tally read-modify-write under ``sessions/<id>.json``, including
    the reopen rule that flips a final/idle row back to active on new activity;
  - the observed-events ledger (KTD7/R13) and the ``rewrite_honoured`` /
    ``rewrite_ignored`` distinction the doctor and docs consume;
  - top-level Cursor output contracts (``permission``/``updated_input``,
    ``additional_context``), NOT the Copilot ``hookSpecificOutput`` envelope;
  - per-workspace restore injection keyed by ``sha1(workspace_root)``;
  - stop throttling (one rollup+dashboard per 120s) vs sessionEnd unthrottled.

All fixtures are built under tmp_path; no network and no writes outside
tmp_path (``cursor_home`` is monkeypatched to a tmp path).
"""

from __future__ import annotations

import hashlib
import json
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

REPO = Path(__file__).resolve().parent.parent
SCRIPTS = REPO / "skills" / "token-optimizer" / "scripts"
sys.path.insert(0, str(SCRIPTS))

import cursor_hook_bridge as bridge  # noqa: E402


def simple():
    return SimpleNamespace(pid=4242)


@pytest.fixture()
def cursor_dir(monkeypatch, tmp_path):
    cur = tmp_path / ".cursor"
    cur.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(bridge, "cursor_home", lambda: cur)
    return cur


def _to_dir(cursor_dir):
    return cursor_dir / "token-optimizer"


def _fake_bash(monkeypatch, *, whitelisted=True, dangerous=False):
    monkeypatch.setattr(
        bridge,
        "_bash_hook",
        SimpleNamespace(
            _is_whitelisted=lambda c: whitelisted,
            _has_dangerous_chars=lambda c: dangerous,
        ),
    )


def _read_last_observed(cursor_dir):
    path = _to_dir(cursor_dir) / "observed-events.jsonl"
    lines = path.read_text(encoding="utf-8").splitlines()
    return json.loads(lines[-1])


def _shell_payload(command, sid="composer-abc123"):
    return {
        "hook_event_name": "postToolUse",
        "tool_name": "Shell",
        "tool_input": {"command": command, "working_directory": "/Users/alex/repos/demo"},
        "conversation_id": sid,
        "cursor_version": "3.18.9",
        "workspace_roots": ["/Users/alex/repos/demo"],
    }


# ---------------------------------------------------------------------------
# decode_payload
# ---------------------------------------------------------------------------


def test_decode_snake_case_object_and_string_tool_input(cursor_dir):
    obj = _shell_payload("echo hi")
    fields = bridge.decode_payload(obj)
    assert fields["tool_name"] == "Shell"
    assert fields["tool_args"]["command"] == "echo hi"
    assert fields["conversation_id"] == "composer-abc123"

    string_input = dict(obj)
    string_input["tool_input"] = json.dumps(obj["tool_input"])
    fields = bridge.decode_payload(string_input)
    assert fields["tool_args"]["command"] == "echo hi"
    assert fields["tool_args"]["working_directory"] == "/Users/alex/repos/demo"


def test_decode_rejects_traversal_and_short_ids(cursor_dir):
    # Dots/slashes are stripped, so a traversal id cannot escape the data dir.
    out = bridge.decode_payload({"conversation_id": "../../../etc/passwd"})
    assert out["conversation_id"] == "etcpasswd"
    assert "/" not in out["conversation_id"] and ".." not in out["conversation_id"]

    # Shorter than 6 chars after sanitize -> unknown.
    out = bridge.decode_payload({"conversation_id": "ab"})
    assert out["conversation_id"] == "unknown"


# ---------------------------------------------------------------------------
# Tally lifecycle
# ---------------------------------------------------------------------------


def test_post_tool_use_writes_tally_and_reopens_idle(cursor_dir, monkeypatch):
    sdir = _to_dir(cursor_dir) / "sessions"
    sdir.mkdir(parents=True, exist_ok=True)
    (sdir / "composer-abc123.json").write_text(
        json.dumps({
            "conversation_id": "composer-abc123",
            "first_ts": time.time() - 900,
            "updated_at": time.time() - 60,
            "turns": 1,
            "tool_calls": 4,
            "final": True,
            "end_reason": "idle",
        }),
        encoding="utf-8",
    )
    _fake_bash(monkeypatch)

    bridge.handle_post_tool_use(_shell_payload("echo hi"))

    tally = json.loads((sdir / "composer-abc123.json").read_text(encoding="utf-8"))
    assert tally["tool_calls"] == 5
    assert tally["final"] is False
    assert tally["end_reason"] == ""


def test_session_end_marks_tally_final(cursor_dir, monkeypatch):
    monkeypatch.setattr(bridge, "spawn_detached", lambda argv, **k: simple())
    bridge.handle_session_end({
        "hook_event_name": "sessionEnd",
        "conversation_id": "composer-abc123",
        "reason": "user_exit",
        "cursor_version": "3.18.9",
    })
    tally = json.loads(
        (_to_dir(cursor_dir) / "sessions" / "composer-abc123.json").read_text(encoding="utf-8")
    )
    assert tally["final"] is True
    assert tally["end_reason"] == "user_exit"


# ---------------------------------------------------------------------------
# preToolUse output contract + rewrite
# ---------------------------------------------------------------------------


def test_pre_tool_use_rewrites_and_echoes_fields(cursor_dir, monkeypatch, capsys):
    _fake_bash(monkeypatch, whitelisted=True, dangerous=False)
    monkeypatch.setattr(bridge, "_COMPRESS_AVAILABLE", True)
    monkeypatch.setattr(bridge, "_COMPRESS_PATH", SCRIPTS / "bash_compress.py")

    bridge.handle_pre_tool_use(_shell_payload("git status"))

    emitted = json.loads(capsys.readouterr().out)
    assert emitted["permission"] == "allow"
    updated = emitted["updated_input"]
    assert "bash_compress.py" in updated["command"]
    # The whole-tool_input replacement must preserve the untouched fields.
    assert updated["working_directory"] == "/Users/alex/repos/demo"


def test_pre_tool_use_non_shell_emits_nothing_records_event(cursor_dir, monkeypatch, capsys):
    _fake_bash(monkeypatch)
    bridge.handle_pre_tool_use({
        "hook_event_name": "preToolUse",
        "tool_name": "Read",
        "tool_input": {"file_path": "/x"},
        "conversation_id": "composer-abc123",
        "cursor_version": "3.18.9",
    })
    assert capsys.readouterr().out == ""
    assert _read_last_observed(cursor_dir)["tool_name"] == "Read"


# ---------------------------------------------------------------------------
# postToolUse rewrite ledger
# ---------------------------------------------------------------------------


def test_post_tool_use_records_rewrite_honoured(cursor_dir, monkeypatch):
    _fake_bash(monkeypatch, whitelisted=True)
    monkeypatch.setattr(bridge, "_COMPRESS_AVAILABLE", True)
    cmd = "python3 /tmp/bash_compress.py git status"
    bridge.handle_post_tool_use(_shell_payload(cmd))
    assert _read_last_observed(cursor_dir)["rewrite"] == "honoured"


def test_post_tool_use_records_rewrite_ignored(cursor_dir, monkeypatch):
    _fake_bash(monkeypatch, whitelisted=True)
    monkeypatch.setattr(bridge, "_COMPRESS_AVAILABLE", True)
    bridge.handle_post_tool_use(_shell_payload("git status"))
    assert _read_last_observed(cursor_dir)["rewrite"] == "ignored"


def test_post_tool_use_skips_non_whitelisted_rewrite_ledger(cursor_dir, monkeypatch):
    _fake_bash(monkeypatch, whitelisted=False)
    monkeypatch.setattr(bridge, "_COMPRESS_AVAILABLE", True)
    bridge.handle_post_tool_use(_shell_payload("rm -rf /"))
    assert "rewrite" not in _read_last_observed(cursor_dir)


def test_post_tool_use_nudges_on_crossing_threshold(cursor_dir, monkeypatch, capsys):
    sdir = _to_dir(cursor_dir) / "sessions"
    sdir.mkdir(parents=True, exist_ok=True)
    (sdir / "composer-abc123.json").write_text(
        json.dumps({
            "conversation_id": "composer-abc123",
            "first_ts": time.time() - 900,
            "updated_at": time.time() - 5,
            "tool_calls": 30,
            "nudge_level": 0,
        }),
        encoding="utf-8",
    )
    _fake_bash(monkeypatch)
    bridge.handle_post_tool_use(_shell_payload("echo hi"))
    assert "additional_context" in json.loads(capsys.readouterr().out)


# ---------------------------------------------------------------------------
# Restore context (per workspace)
# ---------------------------------------------------------------------------


def test_session_start_injects_matching_workspace_restore(cursor_dir, capsys):
    root = "/Users/alex/repos/demo"
    digest = hashlib.sha1(root.encode("utf-8", "replace")).hexdigest()
    restore_dir = _to_dir(cursor_dir) / "restore-context"
    restore_dir.mkdir(parents=True, exist_ok=True)
    (restore_dir / f"{digest}.md").write_text(
        "[Token Optimizer] Continuity from your previous Cursor session", encoding="utf-8"
    )

    bridge.handle_session_start({
        "hook_event_name": "sessionStart",
        "session_id": "composer-new",
        "workspace_roots": [root],
        "cursor_version": "3.18.9",
    })

    assert "additional_context" in json.loads(capsys.readouterr().out)


def test_session_start_skips_restore_when_sibling_active(cursor_dir, capsys):
    root = "/Users/alex/repos/demo"
    digest = hashlib.sha1(root.encode("utf-8", "replace")).hexdigest()
    restore_dir = _to_dir(cursor_dir) / "restore-context"
    restore_dir.mkdir(parents=True, exist_ok=True)
    (restore_dir / f"{digest}.md").write_text("should be skipped", encoding="utf-8")

    sdir = _to_dir(cursor_dir) / "sessions"
    sdir.mkdir(parents=True, exist_ok=True)
    (sdir / "sibling.json").write_text(
        json.dumps({
            "conversation_id": "sibling",
            "updated_at": time.time(),
            "final": False,
            "workspace_roots": [root],
        }),
        encoding="utf-8",
    )

    bridge.handle_session_start({
        "hook_event_name": "sessionStart",
        "session_id": "composer-new",
        "workspace_roots": [root],
    })

    assert capsys.readouterr().out == ""


# ---------------------------------------------------------------------------
# stop / sessionEnd spawns + throttle
# ---------------------------------------------------------------------------


def test_stop_throttles_rollup_and_dashboard(cursor_dir, monkeypatch):
    calls = []
    monkeypatch.setattr(bridge, "spawn_detached",
                        lambda argv, **k: calls.append(argv) or simple())
    payload = {"hook_event_name": "stop", "conversation_id": "composer-abc123"}

    bridge.handle_stop(payload)
    assert len(calls) == 2
    kind = [("cursor-rollup" in a or "dashboard" in a) for a in calls]
    assert kind == [True, True]

    # A second stop inside the 120s window must not spawn again.
    bridge.handle_stop(payload)
    assert len(calls) == 2


def test_session_end_spawns_unthrottled(cursor_dir, monkeypatch):
    calls = []
    monkeypatch.setattr(bridge, "spawn_detached",
                        lambda argv, **k: calls.append(argv) or simple())
    monkeypatch.setattr(bridge, "_stop_rollup_due", lambda: False)  # irrelevant to sessionEnd
    payload = {"hook_event_name": "sessionEnd", "conversation_id": "composer-abc123"}

    bridge.handle_session_end(payload)
    bridge.handle_session_end(payload)
    assert len(calls) == 4


# ---------------------------------------------------------------------------
# observed-events ledger
# ---------------------------------------------------------------------------


def test_ledger_records_event_and_cursor_version(cursor_dir, monkeypatch):
    monkeypatch.setenv("CURSOR_VERSION", "3.18.9")
    bridge.handle_stop({"hook_event_name": "stop", "conversation_id": "composer-abc123"})

    entry = _read_last_observed(cursor_dir)
    assert entry["event"] == "stop"
    assert entry["cursor_version"] == "3.18.9"

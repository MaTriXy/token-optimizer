#!/usr/bin/env python3
"""Unit tests for the Cursor session normalizer (cursor_session.py).

The load-bearing contract is R16 token ordering: state.vscdb bubble tokens when
non-zero, else transcript estimate, else a zero-token tally-only row that is
never dropped. Also covers the CLI/IDE surface heuristic.

Run: python3 -m pytest tests/test_cursor_session.py -v
"""

import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
SCRIPTS = REPO / "skills" / "token-optimizer" / "scripts"
sys.path.insert(0, str(SCRIPTS))

import cursor_session  # noqa: E402


def _tally(**overrides):
    base = {
        "conversation_id": "conv-abc",
        "first_ts": 1700000000.0,
        "updated_at": 1700000600.0,
        "final": True,
        "end_reason": "sessionEnd",
        "turns": 5,
        "tool_calls": 9,
        "tool_names": {"Shell": 6, "Read": 3},
        "tool_output_bytes": 12345,
        "models": {"claude-sonnet": 7},
        "compactions": [
            {"context_tokens": 80000, "context_window_size": 200000, "context_usage_percent": 40}
        ],
        "cwd": "/repo/demo",
        "workspace_roots": ["/repo/demo"],
        "transcript_path": None,
        "cursor_version": "3.18.9",
        "nudge_level": 0,
    }
    base.update(overrides)
    return base


def test_bubble_tokens_win_when_nonzero():
    raw = _tally(
        bubble_tokens={"input_tokens": 1000, "output_tokens": 200, "model": "claude-sonnet"},
        transcript_tokens=999,
    )
    s = cursor_session.normalize_session(raw)
    assert s["token_source"] == "cursor_state_vscdb"
    assert s["estimated"] is False
    assert s["total_input_tokens"] == 1000
    assert s["total_output_tokens"] == 200
    assert s["model"] == "claude-sonnet"


def test_transcript_estimate_when_bubbles_all_zero():
    raw = _tally(
        bubble_tokens={"input_tokens": 0, "output_tokens": 0},
        transcript_tokens=400 // 4,
    )
    s = cursor_session.normalize_session(raw)
    assert s["token_source"] == "cursor_transcript_estimate"
    assert s["estimated"] is True
    assert s["total_input_tokens"] == 100


def test_tally_only_zero_tokens_never_dropped():
    raw = _tally()  # no bubble_tokens / transcript_tokens
    s = cursor_session.normalize_session(raw)
    assert s is not None
    assert s["token_source"] == "cursor_tally_only"
    assert s["estimated"] is True
    assert s["total_input_tokens"] == 0
    assert s["total_output_tokens"] == 0


def test_surface_heuristic_cli_vs_ide():
    assert cursor_session.surface_from_version("2026.08.31-4057e58") == "cli"
    assert cursor_session.surface_from_version("3.18.9") == "ide"
    assert cursor_session.surface_from_version(None) == "unknown"
    assert cursor_session.surface_from_version("") == "unknown"


def test_normalize_surface_and_canonical_fields():
    s = cursor_session.normalize_session(_tally())
    assert s["runtime"] == "cursor"
    assert s["billing_provider"] == "cursor"
    assert s["cost_source"] == "cursor_no_cost_data"
    assert s["cost_usd"] == 0.0
    assert s["incomplete"] is False
    assert s["end_reason"] == "sessionEnd"
    assert s["surface"] == "ide"
    assert s["tool_calls"]["total"] == 9
    assert s["message_count"] == 5
    assert s["compactions"] == 1
    assert s["cwd"] == "/repo/demo"
    assert s["slug"] == "conv-abc"


def test_incomplete_when_not_final():
    s = cursor_session.normalize_session(_tally(final=False, end_reason=""))
    assert s["incomplete"] is True
    assert s["end_reason"] == ""


def test_idle_end_reason_sets_incomplete_false():
    s = cursor_session.normalize_session(_tally(final=True, end_reason="idle"))
    assert s["incomplete"] is False
    assert s["end_reason"] == "idle"


def test_cwd_falls_back_to_workspace_root():
    raw = _tally(cwd=None)
    s = cursor_session.normalize_session(raw)
    assert s["cwd"] == "/repo/demo"


def test_non_dict_and_missing_id_return_none():
    assert cursor_session.normalize_session(None) is None
    assert cursor_session.normalize_session([]) is None
    assert cursor_session.normalize_session({}) is None
    assert cursor_session.normalize_session({"turns": 1}) is None


@pytest.mark.parametrize(
    "model,expected",
    [
        ("gpt-5", 128_000),
        ("gpt-4.1", 128_000),
        ("gpt-4o", 128_000),
        ("claude-sonnet-4.5", 200_000),
        ("gemini-2.5-pro", 128_000),
        ("o3", 200_000),
        ("o4-mini", 200_000),
        ("some-unknown-model", 128_000),
        ("", 128_000),
        (None, 128_000),
        ("CLAUDE-OPUS", 200_000),  # case-insensitive prefix match
    ],
)
def test_context_window_for_model(model, expected):
    assert cursor_session.context_window_for_model(model) == expected

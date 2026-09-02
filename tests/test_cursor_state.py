#!/usr/bin/env python3
"""Unit tests for the read-only Cursor session readers (cursor_state.py).

Covers the tally reader, transcript chars-over-four containment+estimate, the
read-only state.vscdb token reader, and idle finalisation. All fixtures are
built under tmp_path; no network and no writes outside tmp_path.

Run: python3 -m pytest tests/test_cursor_state.py -v
"""

import json
import sqlite3
import sys
import time
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
SCRIPTS = REPO / "skills" / "token-optimizer" / "scripts"
sys.path.insert(0, str(SCRIPTS))

import cursor_state  # noqa: E402


def _write_tally(sessions_dir: Path, cid: str, obj: dict) -> Path:
    sessions_dir.mkdir(parents=True, exist_ok=True)
    p = sessions_dir / f"{cid}.json"
    p.write_text(json.dumps(obj), encoding="utf-8")
    return p


def test_find_tallies_and_read_tally(tmp_path):
    sessions = tmp_path / "token-optimizer" / "sessions"
    _write_tally(sessions, "conv-abc", {"conversation_id": "conv-abc", "turns": 3})
    _write_tally(sessions, "conv-def", {"conversation_id": "conv-def", "turns": 7})
    # a non-json stray file is ignored by find_tallies' suffix filter
    (sessions / "stray.txt").write_text("x", encoding="utf-8")

    tallies = cursor_state.find_tallies(home=tmp_path)
    assert [p.name for p in tallies] == ["conv-abc.json", "conv-def.json"]

    data = cursor_state.read_tally(tallies[0])
    assert data["conversation_id"] == "conv-abc"
    assert data["turns"] == 3

    # corrupt JSON -> None
    bad = sessions / "bad.json"
    bad.write_text("{not json", encoding="utf-8")
    assert cursor_state.read_tally(bad) is None

    # non-dict JSON -> None
    arr = sessions / "arr.json"
    arr.write_text("[1,2,3]", encoding="utf-8")
    assert cursor_state.read_tally(arr) is None

    # missing home dir -> []
    assert cursor_state.find_tallies(home=tmp_path / "nope") == []


def test_read_tally_missing_file_returns_none(tmp_path):
    assert cursor_state.read_tally(tmp_path / "does-not-exist.json") is None


def test_transcript_estimate_chars_over_four(tmp_path):
    home = tmp_path / "home"
    projects = home / "projects"
    transcript = projects / "slug" / "agent-transcripts" / "conv-abc" / "conv-abc.jsonl"
    transcript.parent.mkdir(parents=True, exist_ok=True)
    text = '{"type":"user.message","text":"' + ("x" * 40) + '"}\n'
    transcript.write_text(text, encoding="utf-8")

    estimate = cursor_state.transcript_estimate(str(transcript), home)
    assert estimate == len(text) // 4


def test_transcript_estimate_ignores_paths_outside_projects(tmp_path):
    home = tmp_path / "home"
    outside = tmp_path / "elsewhere" / "t.jsonl"
    outside.parent.mkdir(parents=True, exist_ok=True)
    outside.write_text("x" * 40, encoding="utf-8")
    assert cursor_state.transcript_estimate(str(outside), home) is None


def test_transcript_estimate_missing_or_none(tmp_path):
    home = tmp_path / "home"
    assert cursor_state.transcript_estimate(None, home) is None
    assert cursor_state.transcript_estimate("", home) is None
    assert cursor_state.transcript_estimate(str(home / "projects" / "gone.jsonl"), home) is None


def _make_vscdb(path: Path, bubbles, composer_meta):
    conn = sqlite3.connect(str(path))
    try:
        conn.execute("PRAGMA journal_mode=DELETE")
        conn.execute("CREATE TABLE cursorDiskKV (key TEXT PRIMARY KEY, value TEXT)")
        for key, value in bubbles.items():
            conn.execute(
                "INSERT OR REPLACE INTO cursorDiskKV (key, value) VALUES (?, ?)",
                (key, json.dumps(value)),
            )
        for key, value in composer_meta.items():
            conn.execute(
                "INSERT OR REPLACE INTO cursorDiskKV (key, value) VALUES (?, ?)",
                (key, json.dumps(value)),
            )
        conn.commit()
    finally:
        conn.close()


def test_read_state_vscdb_tokens_sums_bubbles_and_model(tmp_path):
    db = tmp_path / "state.vscdb"
    _make_vscdb(
        db,
        bubbles={
            "bubbleId:c1:b1": {"type": 1, "tokenCount": {"inputTokens": 100, "outputTokens": 50}},
            "bubbleId:c1:b2": {"type": 2, "tokenCount": {"inputTokens": 40, "outputTokens": 10}},
            "bubbleId:c2:b1": {"type": 1, "tokenCount": {"inputTokens": 999, "outputTokens": 1}},
        },
        composer_meta={
            "composerData:c1": {"modelConfig": {"modelName": "claude-sonnet"}, "createdAt": 1700000000000},
        },
    )
    result = cursor_state.read_state_vscdb_tokens(["c1", "c2", "missing"], db)
    assert result["c1"]["input_tokens"] == 140
    assert result["c1"]["output_tokens"] == 60
    assert result["c1"]["model"] == "claude-sonnet"
    assert result["c1"]["created_at_ms"] == 1700000000000
    assert result["c2"]["input_tokens"] == 999
    assert "missing" not in result


def test_read_state_vscdb_tokens_all_zero_bubbles(tmp_path):
    db = tmp_path / "state.vscdb"
    _make_vscdb(
        db,
        bubbles={"bubbleId:c1:b1": {"tokenCount": {"inputTokens": 0, "outputTokens": 0}}},
        composer_meta={},
    )
    result = cursor_state.read_state_vscdb_tokens(["c1"], db)
    # still reported, but with zero totals — the caller's R16 ordering then
    # falls through to transcript/tally sources.
    assert result["c1"]["input_tokens"] == 0
    assert result["c1"]["output_tokens"] == 0


def test_read_state_vscdb_tokens_missing_db_returns_empty(tmp_path):
    assert cursor_state.read_state_vscdb_tokens(["c1"], tmp_path / "nope.vscdb") == {}


def test_idle_finalise(tmp_path):
    tally = {"conversation_id": "c1", "updated_at": 1000.0, "final": False}
    # 3h later -> idle
    out = cursor_state.idle_finalise(tally, now=1000.0 + 3 * 3600)
    assert out["final"] is True
    assert out["end_reason"] == "idle"
    # original is untouched (copy semantics)
    assert tally["final"] is False
    # 10 min later -> stays as-is
    out2 = cursor_state.idle_finalise(tally, now=1000.0 + 600)
    assert out2["final"] is False
    assert out2.get("end_reason", "") == ""
    # already final -> unchanged
    final_tally = {"conversation_id": "c1", "updated_at": 1000.0, "final": True, "end_reason": "sessionEnd"}
    out3 = cursor_state.idle_finalise(final_tally, now=1000.0 + 3 * 3600)
    assert out3["end_reason"] == "sessionEnd"


def test_state_vscdb_path_is_a_path():
    p = cursor_state.state_vscdb_path()
    assert isinstance(p, Path)
    assert str(p).endswith("state.vscdb")


def _vscdb_with_bubbles(path, bubbles):
    import sqlite3
    conn = sqlite3.connect(str(path))
    conn.execute("CREATE TABLE cursorDiskKV (key TEXT PRIMARY KEY, value TEXT)")
    for key, value in bubbles.items():
        conn.execute("INSERT INTO cursorDiskKV VALUES (?, ?)", (key, json.dumps(value)))
    conn.commit()
    conn.close()


def test_like_wildcard_underscore_does_not_bleed_tokens(tmp_path):
    """P0-4: '_' in a composer id is a LIKE single-char wildcard; querying for
    abc_def must never sum abcXdef's bubbles."""
    db = tmp_path / "state.vscdb"
    _vscdb_with_bubbles(db, {
        "bubbleId:abc_def:b1": {"tokenCount": {"inputTokens": 100, "outputTokens": 50}},
        "bubbleId:abcXdef:b1": {"tokenCount": {"inputTokens": 999, "outputTokens": 1}},
        "bubbleId:abc%def:b1": {"tokenCount": {"inputTokens": 500, "outputTokens": 0}},
    })
    res = cursor_state.read_state_vscdb_tokens(["abc_def"], db)
    assert res["abc_def"]["input_tokens"] == 100
    assert res["abc_def"]["output_tokens"] == 50


def test_percent_wildcard_does_not_bleed_tokens(tmp_path):
    db = tmp_path / "state.vscdb"
    _vscdb_with_bubbles(db, {
        "bubbleId:aaa:b1": {"tokenCount": {"inputTokens": 7, "outputTokens": 0}},
        "bubbleId:aaXb:b1": {"tokenCount": {"inputTokens": 400, "outputTokens": 0}},
    })
    res = cursor_state.read_state_vscdb_tokens(["aaa"], db)
    assert res["aaa"]["input_tokens"] == 7


def test_single_scan_not_n_plus_one(tmp_path):
    """P0-5: one query over the key prefixes, not 2 per composer id. The old
    N+1 shape fired 1000 SELECTs here (4.6s on this DB); the single scan is
    pinned by counting executed SELECT statements, with wall-clock as a loose
    smoke bound only."""
    import sqlite3
    db = tmp_path / "state.vscdb"
    conn = sqlite3.connect(str(db))
    conn.execute("CREATE TABLE cursorDiskKV (key TEXT PRIMARY KEY, value TEXT)")
    ids = [f"composer-{i:04d}-uuid" for i in range(500)]
    rows = [(f"bubbleId:{cid}:b{i}",
             json.dumps({"tokenCount": {"inputTokens": 10, "outputTokens": 5}}))
            for i, cid in enumerate(ids * 110)]
    conn.executemany("INSERT INTO cursorDiskKV VALUES (?, ?)", rows)
    conn.commit()
    conn.close()

    selects = []
    orig_connect = cursor_state.sqlite3.connect

    def counting_connect(*a, **k):
        c = orig_connect(*a, **k)
        c.set_trace_callback(lambda s: selects.append(s) if s.lstrip().upper().startswith("SELECT") else None)
        return c

    cursor_state.sqlite3.connect = counting_connect
    try:
        t0 = time.perf_counter()
        res = cursor_state.read_state_vscdb_tokens(ids, db)
        elapsed = time.perf_counter() - t0
    finally:
        cursor_state.sqlite3.connect = orig_connect

    assert len(res) == 500
    assert res[ids[0]]["input_tokens"] == 10 * 110
    assert len(selects) == 1, f"expected exactly 1 SELECT, got {len(selects)}"
    assert elapsed < 5.0, f"single-scan read took {elapsed:.2f}s"

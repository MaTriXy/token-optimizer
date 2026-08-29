"""OSRC delegation marker must only count in a HUMAN prompt, never a tool result.

Both marker tests used to search the whole record (and the raw line) for
``OSRC::PROGRESS`` / ``OSRC::DONE``. The marker therefore matched inside tool
RESULTS, so an orchestrator session that merely *watched* a delegate (running
``outsourcerer.sh status``, whose output echoes the marker) classified itself as
the delegation it was watching. On the author's machine that mislabelled 234 of
3,312 30-day rows holding 57% of all input tokens -- the longest genuine working
sessions -- and pulled them out of the human pool every cost comparison uses.

Fixing the classifier alone is not enough: ``_backfill_outsourcerer_sidechain``
is gated by a persistent ``osrc_backfill_done`` marker, so the corrected code
never revisits rows the buggy code already wrote. Hence the repair migration.

Every test here fails against the pre-fix code and passes after.

Run: python3 -m pytest tests/test_osrc_sidechain_scope.py -v
"""
import json
import sqlite3
import sys
import tempfile
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parent.parent / "skills" / "token-optimizer" / "scripts"

MARKER = "OSRC::PROGRESS#abc123 doing the thing"


@pytest.fixture()
def m(monkeypatch):
    tmp = tempfile.mkdtemp(prefix="to-osrc-scope-")
    monkeypatch.setenv("TOKEN_OPTIMIZER_SNAPSHOT_DIR", tmp)
    sys.path.insert(0, str(SCRIPTS))
    if "measure" in sys.modules:
        del sys.modules["measure"]
    import measure
    return measure


def _write(tmp_path, name, records):
    p = tmp_path / name
    p.write_text("\n".join(json.dumps(r) for r in records), encoding="utf-8")
    return str(p)


def _human_prompt(text):
    return {"type": "user", "message": {"role": "user", "content": text}}


def _tool_result(text):
    """A user record that is the transport for a tool's output, not a human turn."""
    return {
        "type": "user",
        "message": {
            "role": "user",
            "content": [
                {"type": "tool_result", "tool_use_id": "toolu_01", "content": text}
            ],
        },
    }


def _assistant(text):
    return {"type": "assistant", "message": {"id": "msg_1", "role": "assistant",
                                             "content": [{"type": "text", "text": text}],
                                             "usage": {"input_tokens": 1, "output_tokens": 1}}}


# --------------------------------------------------------------------------
# _scan_jsonl_is_sidechain
# --------------------------------------------------------------------------

def test_marker_in_tool_result_is_not_a_delegation(m, tmp_path):
    """THE BUG. Watching a delegate must not mark the watcher as the delegate."""
    fp = _write(tmp_path, "orchestrator.jsonl", [
        _human_prompt("check on the delegate please"),
        _tool_result(MARKER),          # outsourcerer.sh status output
        _assistant("it is still running"),
    ])
    assert m._scan_jsonl_is_sidechain(fp) is False


def test_marker_in_assistant_text_is_not_a_delegation(m, tmp_path):
    """Quoting the marker back to the user is not delegation either."""
    fp = _write(tmp_path, "quoted.jsonl", [
        _human_prompt("what did it say?"),
        _assistant(f"the delegate reported {MARKER}"),
    ])
    assert m._scan_jsonl_is_sidechain(fp) is False


def test_marker_in_human_prompt_is_a_delegation(m, tmp_path):
    """The real signal still fires: outsourcerer injects it INTO the prompt."""
    fp = _write(tmp_path, "delegation.jsonl", [
        _human_prompt(f"{MARKER}\nDo the task."),
        _assistant("on it"),
    ])
    assert m._scan_jsonl_is_sidechain(fp) is True


def test_explicit_issidechain_field_still_wins(m, tmp_path):
    """A genuine subagent transcript is flagged regardless of any marker."""
    fp = _write(tmp_path, "subagent.jsonl", [
        {"type": "user", "isSidechain": True,
         "message": {"role": "user", "content": "sub-task"}},
    ])
    assert m._scan_jsonl_is_sidechain(fp) is True


def test_unreadable_file_returns_none_not_a_guess(m, tmp_path):
    assert m._scan_jsonl_is_sidechain(str(tmp_path / "nope.jsonl")) is None


# --------------------------------------------------------------------------
# _parse_session_jsonl must agree with the scanner
# --------------------------------------------------------------------------

def test_collector_agrees_with_scanner_on_tool_result(m, tmp_path):
    """The two implementations must not disagree; they did before the fix."""
    fp = _write(tmp_path, "collect.jsonl", [
        _human_prompt("watch it"),
        _tool_result(MARKER),
        _assistant("still running"),
    ])
    parsed = m._parse_session_jsonl(fp)
    assert parsed is not None
    assert parsed["is_sidechain"] is False
    assert m._scan_jsonl_is_sidechain(fp) is False


def test_collector_agrees_with_scanner_on_real_delegation(m, tmp_path):
    fp = _write(tmp_path, "collect2.jsonl", [
        _human_prompt(f"{MARKER}\nDo the task."),
        _assistant("on it"),
    ])
    parsed = m._parse_session_jsonl(fp)
    assert parsed is not None
    assert parsed["is_sidechain"] is True
    assert m._scan_jsonl_is_sidechain(fp) is True


# --------------------------------------------------------------------------
# the repair migration
# --------------------------------------------------------------------------

def _seed(conn, rows):
    conn.execute(
        "CREATE TABLE IF NOT EXISTS session_log ("
        "id INTEGER PRIMARY KEY AUTOINCREMENT, jsonl_path TEXT, "
        "is_sidechain INTEGER DEFAULT 0)"
    )
    conn.executemany(
        "INSERT INTO session_log (jsonl_path, is_sidechain) VALUES (?, ?)", rows
    )
    conn.commit()


def test_repair_flips_back_only_the_misflagged(m, tmp_path):
    watcher = _write(tmp_path, "w.jsonl", [_human_prompt("watch"), _tool_result(MARKER)])
    real = _write(tmp_path, "r.jsonl", [_human_prompt(f"{MARKER}\ngo")])
    conn = sqlite3.connect(":memory:")
    _seed(conn, [(watcher, 1), (real, 1)])       # both wrongly flagged by the old code

    assert m._repair_osrc_sidechain_misflag(conn) == 1

    got = dict(conn.execute("SELECT jsonl_path, is_sidechain FROM session_log").fetchall())
    assert got[watcher] == 0, "the watcher must be returned to the human pool"
    assert got[real] == 1, "a genuine delegation must stay flagged"


def test_repair_is_one_way_never_flags_new_rows(m, tmp_path):
    """A bug in the repair must not be able to manufacture sidechains."""
    real = _write(tmp_path, "r2.jsonl", [_human_prompt(f"{MARKER}\ngo")])
    conn = sqlite3.connect(":memory:")
    _seed(conn, [(real, 0)])                     # human row that WOULD match the marker

    m._repair_osrc_sidechain_misflag(conn)

    assert conn.execute(
        "SELECT is_sidechain FROM session_log"
    ).fetchone()[0] == 0, "repair must only move 1 -> 0"


def test_repair_leaves_rows_whose_transcript_is_gone(m, tmp_path):
    """None (unreadable) is not proof of misflagging, so do not guess."""
    conn = sqlite3.connect(":memory:")
    _seed(conn, [(str(tmp_path / "vanished.jsonl"), 1)])

    assert m._repair_osrc_sidechain_misflag(conn) == 0
    assert conn.execute("SELECT is_sidechain FROM session_log").fetchone()[0] == 1


def test_repair_runs_at_most_once(m, tmp_path):
    """Ungated, it would content-scan every flagged transcript on every DB open."""
    watcher = _write(tmp_path, "w3.jsonl", [_human_prompt("watch"), _tool_result(MARKER)])
    conn = sqlite3.connect(":memory:")
    _seed(conn, [(watcher, 1)])

    assert m._repair_osrc_sidechain_misflag(conn) == 1
    assert m._repair_osrc_sidechain_misflag(conn) == 0, "gate must make it idempotent"
    assert conn.execute(
        "SELECT 1 FROM token_optimizer_meta WHERE key = 'osrc_misflag_repair_done'"
    ).fetchone() is not None


# --------------------------------------------------------------------------
# scan-depth determinism
# --------------------------------------------------------------------------

def test_classification_does_not_depend_on_scan_depth(m, tmp_path):
    """50 of the author's 234 rows had the marker only BEYOND record 200, so a
    capped scan and a full scan disagreed. With the marker scoped to the human
    prompt (which opens the transcript), the verdict is stable either way."""
    records = [_human_prompt(f"{MARKER}\ngo")]
    records += [_assistant("working") for _ in range(300)]
    fp = _write(tmp_path, "deep.jsonl", records)

    assert m._scan_jsonl_is_sidechain(fp, max_lines=10) is True
    assert m._scan_jsonl_is_sidechain(fp, max_lines=1000) is True

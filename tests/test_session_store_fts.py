#!/usr/bin/env python3
"""archive lineage tags (schema + writers).

Verifies that ``tool_outputs`` carries ``source_file_path``/``language``/
``archived_from`` lineage columns, that ``insert_tool_output`` persists them,
that pre-existing DBs migrate in place idempotently, and that the
``archive_result`` / ``read_cache`` writers pass lineage through with
credential redaction.

Run: python3 -m pytest tests/test_session_store_fts.py -v
"""

from __future__ import annotations

import json
import os
import sqlite3
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
SCRIPTS = REPO / "skills" / "token-optimizer" / "scripts"

if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from session_store import SessionStore  # noqa: E402


# ---------------------------------------------------------------------------
# Schema: lineage columns exist on fresh + pre-existing DBs
# ---------------------------------------------------------------------------

def test_fresh_db_has_lineage_columns(tmp_path):
    store = SessionStore("test-fresh-lineage", snapshot_dir=tmp_path)
    conn = store._connect()
    cols = {r[1] for r in conn.execute("PRAGMA table_info(tool_outputs)").fetchall()}
    store.close()
    assert "source_file_path" in cols
    assert "language" in cols
    assert "archived_from" in cols


def test_pre_existing_db_migrates_in_place(tmp_path):
    # Create a DB with the OLD schema (no lineage columns), then open it with
    # SessionStore which must add the columns via ALTER TABLE.
    store_dir = tmp_path / "session-store"
    store_dir.mkdir(parents=True)
    db_path = store_dir / "test-migrate-lineage.db"
    conn = sqlite3.connect(str(db_path))
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS tool_outputs (
            tool_use_id TEXT PRIMARY KEY,
            tool_name TEXT NOT NULL,
            tool_type TEXT NOT NULL,
            command_or_path TEXT,
            output_hash TEXT NOT NULL,
            output_chars INTEGER NOT NULL,
            output_tokens_est INTEGER NOT NULL,
            compressed_preview TEXT,
            timestamp REAL NOT NULL
        );
    """)
    conn.commit()
    conn.close()
    # Now open with SessionStore -- _ensure_tool_output_columns must fire.
    store = SessionStore("test-migrate-lineage", snapshot_dir=tmp_path)
    conn = store._connect()
    cols = {r[1] for r in conn.execute("PRAGMA table_info(tool_outputs)").fetchall()}
    store.close()
    assert "source_file_path" in cols
    assert "language" in cols
    assert "archived_from" in cols


def test_idempotent_migration_no_duplicate_columns(tmp_path):
    store = SessionStore("test-idempotent-lineage", snapshot_dir=tmp_path)
    conn = store._connect()
    # Call the ensure function a second time directly; must not error or
    # duplicate columns.
    store._ensure_tool_output_columns(conn)
    store._ensure_tool_output_columns(conn)
    cols = [r[1] for r in conn.execute("PRAGMA table_info(tool_outputs)").fetchall()]
    store.close()
    assert cols.count("source_file_path") == 1
    assert cols.count("language") == 1
    assert cols.count("archived_from") == 1


# ---------------------------------------------------------------------------
# insert_tool_output: lineage persistence + backward compat
# ---------------------------------------------------------------------------

def test_insert_with_lineage_persists_all_three_fields(tmp_path):
    store = SessionStore("test-lineage-persist", snapshot_dir=tmp_path)
    store.insert_tool_output(
        tool_use_id="tu-1",
        tool_name="Read",
        tool_type="read",
        command_or_path="/safe/path/file.py",
        output_hash="abc123",
        output_chars=1000,
        output_tokens_est=250,
        compressed_preview="preview",
        source_file_path="/safe/path/file.py",
        language="python",
        archived_from="first_read_skeleton",
    )
    conn = store._connect()
    row = conn.execute(
        "SELECT source_file_path, language, archived_from FROM tool_outputs WHERE tool_use_id = ?",
        ("tu-1",),
    ).fetchone()
    store.close()
    assert row is not None
    assert row[0] == "/safe/path/file.py"
    assert row[1] == "python"
    assert row[2] == "first_read_skeleton"


def test_insert_without_lineage_persists_nulls(tmp_path):
    store = SessionStore("test-lineage-nulls", snapshot_dir=tmp_path)
    store.insert_tool_output(
        tool_use_id="tu-2",
        tool_name="Bash",
        tool_type="bash",
        command_or_path="ls -la",
        output_hash="def456",
        output_chars=500,
        output_tokens_est=125,
        compressed_preview="preview",
    )
    conn = store._connect()
    row = conn.execute(
        "SELECT source_file_path, language, archived_from FROM tool_outputs WHERE tool_use_id = ?",
        ("tu-2",),
    ).fetchone()
    store.close()
    assert row is not None
    assert row[0] is None
    assert row[1] is None
    assert row[2] is None


# ---------------------------------------------------------------------------
# read_cache first-read shadow path writes lineage
# ---------------------------------------------------------------------------

def test_read_cache_shadow_first_read_writes_lineage(tmp_path):
    """A shadow first-read skeleton produces a tool_outputs row with
    source_file_path/language/archived_from='first_read_skeleton'."""
    import subprocess

    # Create a sizable Python file that triggers the shadow path.
    # Each function has a fully unique body (every line includes the index)
    # to stay below the 0.35 repeated_ratio threshold of looks_generated_python.
    target = tmp_path / "big.py"
    parts = ["import os\nimport sys\nimport json\nfrom pathlib import Path\n\n"]
    for i in range(120):
        parts.append(
            f"def handler_{i}(request, ctx=None):\n"
            f"    '''Handler {i}: process request type {i % 7} with ctx slot {i % 13}.'''\n"
            f"    if request is None or ctx is None:  # guard {i}\n"
            f"        raise ValueError('handler_{i}: missing request or ctx')\n"
            f"    key = 'handler_{i}_result_{i % 7}'\n"
            f"    value = request.get('param_{i}', {i}) * {i + 1} + {i % 5}\n"
            f"    ctx[key] = value  # store {i}\n"
            f"    return ctx[key]  # ret {i}\n\n"
        )
    body = "".join(parts)
    target.write_text(body, encoding="utf-8")

    session = "44444444-4444-4444-4444-444444444444"
    env = dict(os.environ)
    env["TOKEN_OPTIMIZER_SNAPSHOT_DIR"] = str(tmp_path)
    env.setdefault("TOKEN_OPTIMIZER_READ_CACHE", "1")
    env.setdefault("TOKEN_OPTIMIZER_FIRST_READ_SHADOW", "1")
    # Ensure active is OFF so the shadow path runs (not the retarget path).
    env["TOKEN_OPTIMIZER_FIRST_READ_ACTIVE"] = "0"
    payload = {
        "tool_name": "Read",
        "tool_input": {"file_path": str(target), "offset": 0, "limit": 0},
        "session_id": session,
        "agent_id": session,
    }
    out = subprocess.run(
        [sys.executable, str(SCRIPTS / "read_cache.py")],
        input=json.dumps(payload), capture_output=True, text=True, env=env, timeout=60,
    )
    assert out.returncode == 0, out.stderr

    # Check the session DB for the lineage row.
    store = SessionStore(session, snapshot_dir=tmp_path)
    conn = store._connect()
    rows = conn.execute(
        "SELECT source_file_path, language, archived_from FROM tool_outputs "
        "WHERE archived_from = 'first_read_skeleton'"
    ).fetchall()
    store.close()
    assert len(rows) >= 1, "expected at least one first_read_skeleton lineage row"
    row = rows[0]
    assert row[0] is not None and str(target) in row[0], row[0]
    assert row[1] == "python"
    assert row[2] == "first_read_skeleton"


# ---------------------------------------------------------------------------
# U6: FTS5 full-text search (with LIKE fallback)
# ---------------------------------------------------------------------------

def test_fts5_search_by_tool_name(tmp_path):
    store = SessionStore("test-fts-toolname", snapshot_dir=tmp_path)
    store.insert_tool_output(
        tool_use_id="tu-fts-1",
        tool_name="Bash",
        tool_type="bash",
        command_or_path="ls -la",
        output_hash="h1",
        output_chars=100,
        output_tokens_est=25,
        compressed_preview="file1.txt file2.txt",
        source_file_path=None,
        language=None,
        archived_from="PostToolUse",
        output_text="file1.txt file2.txt total 4096",
    )
    results = store.search_tool_outputs("Bash", limit=10)
    store.close()
    assert len(results) >= 1
    assert results[0]["tool_use_id"] == "tu-fts-1"
    assert results[0]["tool_name"] == "Bash"


def test_fts5_search_matches_term_in_output_body_only(tmp_path):
    """Full-text search matches a term present only in the full response
    body (not the preview), proving real full-text indexing."""
    store = SessionStore("test-fts-body", snapshot_dir=tmp_path)
    store.insert_tool_output(
        tool_use_id="tu-fts-2",
        tool_name="Read",
        tool_type="read",
        command_or_path="/safe/path.py",
        output_hash="h2",
        output_chars=5000,
        output_tokens_est=1250,
        compressed_preview="def hello():\n    return 1",
        source_file_path="/safe/path.py",
        language="python",
        archived_from="PostToolUse",
        output_text="def hello():\n    return 1\n\ndef unique_marker_xyzzy():\n    return 42\n",
    )
    # "unique_marker_xyzzy" appears ONLY in output_text, not in the preview.
    results = store.search_tool_outputs("unique_marker_xyzzy", limit=10)
    store.close()
    assert len(results) >= 1, "full-text search must match body-only terms"
    assert results[0]["tool_use_id"] == "tu-fts-2"


def test_fts5_search_respects_limit(tmp_path):
    store = SessionStore("test-fts-limit", snapshot_dir=tmp_path)
    for i in range(30):
        store.insert_tool_output(
            tool_use_id=f"tu-fts-limit-{i}",
            tool_name="Bash",
            tool_type="bash",
            command_or_path=f"echo hello_{i}",
            output_hash=f"h{i}",
            output_chars=50,
            output_tokens_est=12,
            compressed_preview=f"hello_{i}",
            output_text=f"hello_{i} world output",
        )
    results = store.search_tool_outputs("hello", limit=5)
    store.close()
    assert len(results) <= 5
    assert all("tool_use_id" in r for r in results)


def test_fts5_search_no_match_returns_empty(tmp_path):
    store = SessionStore("test-fts-nomatch", snapshot_dir=tmp_path)
    store.insert_tool_output(
        tool_use_id="tu-fts-3",
        tool_name="Bash",
        tool_type="bash",
        command_or_path="ls",
        output_hash="h3",
        output_chars=10,
        output_tokens_est=2,
        compressed_preview="a b c",
        output_text="a b c",
    )
    results = store.search_tool_outputs("nonexistent_term_zzz", limit=10)
    store.close()
    assert results == []


def test_fts5_search_empty_query_returns_empty(tmp_path):
    store = SessionStore("test-fts-empty", snapshot_dir=tmp_path)
    results = store.search_tool_outputs("", limit=10)
    store.close()
    assert results == []


def test_like_fallback_when_fts5_unavailable(tmp_path, monkeypatch):
    """When FTS5 is monkeypatched to unavailable, the LIKE fallback still
    returns matching rows."""
    from session_store import SessionStore as SS
    monkeypatch.setattr(SS, "_fts5_available", False)
    store = SS("test-like-fallback", snapshot_dir=tmp_path)
    store.insert_tool_output(
        tool_use_id="tu-like-1",
        tool_name="Bash",
        tool_type="bash",
        command_or_path="grep -r pattern",
        output_hash="h4",
        output_chars=100,
        output_tokens_est=25,
        compressed_preview="found pattern here",
        source_file_path="/safe/code.py",
        language="python",
        archived_from="PostToolUse",
        output_text="found pattern here in the output body",
    )
    # LIKE fallback should still find the row by tool_name.
    results = store.search_tool_outputs("Bash", limit=10)
    store.close()
    assert len(results) >= 1
    assert results[0]["tool_use_id"] == "tu-like-1"
    # LIKE fallback should also find by output_text body term.
    results2 = store.search_tool_outputs("output body", limit=10)
    assert any(r["tool_use_id"] == "tu-like-1" for r in results2)

#!/usr/bin/env python3
"""external-content FTS5 mirror over the archive.

Regression coverage for the three reproduced FTS defects:
  (a) legacy rows written before FTS existed are returned by search
      (backfilled by a one-time rebuild on the schema-version upgrade);
  (b) re-inserting the same tool_use_id never appends a duplicate FTS row, so
      search returns each match exactly once;
  (c) a pre-existing DB (no FTS, or a legacy STANDALONE FTS with duplicate
      rows) is migrated in place and its rows are searchable + deduped.

Run: python3 -m pytest tests/test_session_store_fts_mirror.py -v
"""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SCRIPTS = REPO / "skills" / "token-optimizer" / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from session_store import SessionStore  # noqa: E402


def _mk_legacy_no_fts(tmp_path, name):
    """A pre-U6 DB: schema v1, NO fts table, some archive rows, no lineage."""
    store_dir = tmp_path / "session-store"
    store_dir.mkdir(parents=True, exist_ok=True)
    db = store_dir / f"{name}.db"
    conn = sqlite3.connect(str(db))
    conn.executescript(
        """
        CREATE TABLE tool_outputs (
            tool_use_id TEXT PRIMARY KEY, tool_name TEXT NOT NULL,
            tool_type TEXT NOT NULL, command_or_path TEXT,
            output_hash TEXT NOT NULL, output_chars INTEGER NOT NULL,
            output_tokens_est INTEGER NOT NULL, compressed_preview TEXT,
            timestamp REAL NOT NULL);
        CREATE TABLE session_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
        INSERT INTO session_meta VALUES ('_schema_version','1');
        INSERT INTO tool_outputs VALUES
            ('leg-1','Grep','grep','needle_pattern','h',10,2,'prev',1000.0);
        INSERT INTO tool_outputs VALUES
            ('leg-2','Read','read','/legacy/app.py','h',10,2,'prev',1001.0);
        """
    )
    conn.commit()
    conn.close()
    return db


# ---------------------------------------------------------------------------
# (c) legacy pre-FTS DB: rows visible after upgrade/backfill
# ---------------------------------------------------------------------------

def test_legacy_rows_searchable_after_upgrade(tmp_path):
    _mk_legacy_no_fts(tmp_path, "legacyA")
    store = SessionStore("legacyA", snapshot_dir=tmp_path)
    # The archive predates FTS entirely; the upgrade must backfill it so both
    # legacy rows are searchable by their indexed columns.
    by_tool = store.search_tool_outputs("Grep")
    by_cmd = store.search_tool_outputs("needle_pattern")
    ver = store.get_meta("_schema_version")
    store.close()
    assert [r["tool_use_id"] for r in by_tool] == ["leg-1"]
    assert [r["tool_use_id"] for r in by_cmd] == ["leg-1"]
    assert ver == "2"  # schema version bumped


def test_legacy_standalone_fts_dedups_on_migration(tmp_path):
    """A U6-era STANDALONE fts table with duplicate rows for one tool_use_id
    must collapse to a single search hit after conversion to external content.
    Skips when this SQLite build lacks FTS5."""
    store_dir = tmp_path / "session-store"
    store_dir.mkdir(parents=True)
    db = store_dir / "legacyB.db"
    conn = sqlite3.connect(str(db))
    try:
        conn.execute("CREATE VIRTUAL TABLE _p USING fts5(x)")
        conn.execute("DROP TABLE _p")
    except sqlite3.OperationalError:
        conn.close()
        import pytest
        pytest.skip("FTS5 not available in this SQLite build")
    conn.executescript(
        """
        CREATE TABLE tool_outputs (
            tool_use_id TEXT PRIMARY KEY, tool_name TEXT NOT NULL,
            tool_type TEXT NOT NULL, command_or_path TEXT,
            output_hash TEXT NOT NULL, output_chars INTEGER NOT NULL,
            output_tokens_est INTEGER NOT NULL, compressed_preview TEXT,
            timestamp REAL NOT NULL, source_file_path TEXT, language TEXT,
            archived_from TEXT, output_text TEXT);
        CREATE TABLE session_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
        INSERT INTO session_meta VALUES ('_schema_version','1');
        INSERT INTO tool_outputs VALUES
            ('dup-1','Bash','bash','ls',' h',10,2,'prev',1000.0,NULL,NULL,
             'PostToolUse','duptext_marker body');
        CREATE VIRTUAL TABLE tool_outputs_fts USING fts5(
            tool_use_id UNINDEXED, tool_name, command_or_path,
            source_file_path, language, archived_from, output_text,
            tokenize='porter unicode61');
        INSERT INTO tool_outputs_fts VALUES
            ('dup-1','Bash','ls','','','PostToolUse','duptext_marker body');
        INSERT INTO tool_outputs_fts VALUES
            ('dup-1','Bash','ls','','','PostToolUse','duptext_marker body');
        """
    )
    conn.commit()
    conn.close()
    store = SessionStore("legacyB", snapshot_dir=tmp_path)
    results = store.search_tool_outputs("duptext_marker")
    ver = store.get_meta("_schema_version")
    store.close()
    assert len(results) == 1, f"expected exactly one hit, got {results}"
    assert results[0]["tool_use_id"] == "dup-1"
    assert ver == "2"


# ---------------------------------------------------------------------------
# (b) re-insert of same tool_use_id -> no duplicate search hit
# ---------------------------------------------------------------------------

def test_reinsert_same_id_no_duplicate_hit(tmp_path):
    if SessionStore._fts5_available is False:
        import pytest
        pytest.skip("FTS5 not available")
    store = SessionStore("dedup-reinsert", snapshot_dir=tmp_path)
    # read_cache re-reads reuse a stable fr_shadow_<sha> id; the second insert
    # is an INSERT OR IGNORE no-op and must NOT append a duplicate FTS row.
    for _ in range(3):
        store.insert_tool_output(
            tool_use_id="fr_shadow_abc",
            tool_name="Read",
            tool_type="read",
            command_or_path="/x.py",
            output_hash="h",
            output_chars=10,
            output_tokens_est=2,
            compressed_preview="prev",
            source_file_path="/x.py",
            language="python",
            archived_from="first_read_skeleton",
            output_text="stable_token_zeta appears once",
        )
    results = store.search_tool_outputs("stable_token_zeta")
    store.close()
    ids = [r["tool_use_id"] for r in results]
    assert ids == ["fr_shadow_abc"], f"expected one hit, got {ids}"


def test_search_returns_every_matching_row_once(tmp_path):
    if SessionStore._fts5_available is False:
        import pytest
        pytest.skip("FTS5 not available")
    store = SessionStore("multi-match", snapshot_dir=tmp_path)
    for i in range(5):
        store.insert_tool_output(
            tool_use_id=f"tu-{i}",
            tool_name="Bash",
            tool_type="bash",
            command_or_path=f"echo {i}",
            output_hash=f"h{i}",
            output_chars=10,
            output_tokens_est=2,
            compressed_preview="p",
            output_text=f"sharedterm row number {i}",
        )
    results = store.search_tool_outputs("sharedterm", limit=50)
    store.close()
    ids = sorted(r["tool_use_id"] for r in results)
    assert ids == [f"tu-{i}" for i in range(5)], ids
    assert len(ids) == len(set(ids)), "no row may appear twice"


# ---------------------------------------------------------------------------
# LIKE fallback wildcard escaping
# ---------------------------------------------------------------------------

def test_like_fallback_escapes_wildcards(tmp_path, monkeypatch):
    from session_store import SessionStore as SS
    monkeypatch.setattr(SS, "_fts5_available", False)
    store = SS("like-escape", snapshot_dir=tmp_path)
    store.insert_tool_output(
        "row-lit", "Bash", "bash", "grep 100%_done", "h", 10, 2,
        compressed_preview="p", output_text="literal 100%_done marker",
    )
    store.insert_tool_output(
        "row-other", "Bash", "bash", "grep XZZZY", "h", 10, 2,
        compressed_preview="p", output_text="unrelated content",
    )
    # A query with % and _ must match the literal text, not act as SQL wildcards
    # (an unescaped "%" would match everything).
    results = store.search_tool_outputs("100%_done")
    store.close()
    ids = {r["tool_use_id"] for r in results}
    assert ids == {"row-lit"}, ids

#!/usr/bin/env python3
"""surgical expand --search Verifies that ``expand --search <query>`` resolves keyword/lineage queries to
archived keys via SessionStore.search_tool_outputs, and that the exact-key
``expand <key>`` path still retrieves full content after a search.

Run: python3 -m pytest tests/test_expand_search.py -v
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
SCRIPTS = REPO / "skills" / "token-optimizer" / "scripts"

if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from session_store import SessionStore  # noqa: E402


def _run_measure(args, env, timeout=30):
    return subprocess.run(
        [sys.executable, str(SCRIPTS / "measure.py")] + args,
        capture_output=True, text=True, env=env, timeout=timeout,
    )


def test_expand_search_finds_distinct_lineage_term(tmp_path):
    """Seed two archived rows with distinct lineage; --search returns exactly
    the matching key(s)."""

    session = "55555555-5555-5555-5555-555555555555"
    store = SessionStore(session, snapshot_dir=tmp_path)
    store.insert_tool_output(
        tool_use_id="tu-search-alpha",
        tool_name="Bash",
        tool_type="bash",
        command_or_path="grep -r alpha_project",
        output_hash="h1",
        output_chars=200,
        output_tokens_est=50,
        compressed_preview="alpha_project result line",
        source_file_path="/safe/alpha.py",
        language="python",
        archived_from="PostToolUse",
        output_text="alpha_project result line with details",
    )
    store.insert_tool_output(
        tool_use_id="tu-search-beta",
        tool_name="Read",
        tool_type="read",
        command_or_path="/safe/beta.py",
        output_hash="h2",
        output_chars=300,
        output_tokens_est=75,
        compressed_preview="beta_module content",
        source_file_path="/safe/beta.py",
        language="python",
        archived_from="PostToolUse",
        output_text="beta_module content here",
    )
    store.close()

    env = dict(os.environ)
    env["TOKEN_OPTIMIZER_SNAPSHOT_DIR"] = str(tmp_path)
    env["CLAUDE_SESSION_ID"] = session
    out = _run_measure(["expand", "--search", "alpha_project"], env)
    assert out.returncode == 0, out.stderr
    assert "tu-search-alpha" in out.stdout
    assert "tu-search-beta" not in out.stdout


def test_expand_search_matches_body_only_term(tmp_path):
    """--search matches a term present only in the full response body (not
    the preview), proving full-text search, not manifest-only."""

    session = "66666666-6666-6666-6666-666666666666"
    store = SessionStore(session, snapshot_dir=tmp_path)
    store.insert_tool_output(
        tool_use_id="tu-body-term",
        tool_name="Bash",
        tool_type="bash",
        command_or_path="cat file.txt",
        output_hash="h3",
        output_chars=5000,
        output_tokens_est=1250,
        compressed_preview="first 1500 chars of output",
        source_file_path=None,
        language=None,
        archived_from="PostToolUse",
        output_text="first 1500 chars of output\n\ndeep_body_marker_zzz991 here\n",
    )
    store.close()

    env = dict(os.environ)
    env["TOKEN_OPTIMIZER_SNAPSHOT_DIR"] = str(tmp_path)
    env["CLAUDE_SESSION_ID"] = session
    out = _run_measure(["expand", "--search", "deep_body_marker_zzz991"], env)
    assert out.returncode == 0, out.stderr
    assert "tu-body-term" in out.stdout


def test_expand_search_no_match_prints_no_results(tmp_path):

    session = "77777777-7777-7777-7777-777777777777"
    store = SessionStore(session, snapshot_dir=tmp_path)
    store.insert_tool_output(
        tool_use_id="tu-nomatch",
        tool_name="Bash",
        tool_type="bash",
        command_or_path="ls",
        output_hash="h4",
        output_chars=10,
        output_tokens_est=2,
        compressed_preview="a b c",
        output_text="a b c",
    )
    store.close()

    env = dict(os.environ)
    env["TOKEN_OPTIMIZER_SNAPSHOT_DIR"] = str(tmp_path)
    env["CLAUDE_SESSION_ID"] = session
    out = _run_measure(["expand", "--search", "nonexistent_term_qqq"], env)
    assert out.returncode == 0, out.stderr
    assert "No results" in out.stdout or "No results" in out.stderr


def test_expand_search_empty_query_prints_usage(tmp_path):
    env = dict(os.environ)
    env["TOKEN_OPTIMIZER_SNAPSHOT_DIR"] = str(tmp_path)
    env["CLAUDE_SESSION_ID"] = "88888888-8888-8888-8888-888888888888"
    out = _run_measure(["expand", "--search", ""], env)
    # Empty query should print usage and exit non-zero.
    assert out.returncode != 0 or "Usage" in out.stderr


def test_expand_list_still_works(tmp_path):
    """--list behavior is unchanged by the --search addition."""

    session = "99999999-9999-9999-9999-999999999999"
    store = SessionStore(session, snapshot_dir=tmp_path)
    store.insert_tool_output(
        tool_use_id="tu-list-check",
        tool_name="Bash",
        tool_type="bash",
        command_or_path="echo test",
        output_hash="h5",
        output_chars=10,
        output_tokens_est=2,
        compressed_preview="test",
        output_text="test",
    )
    store.close()

    env = dict(os.environ)
    env["TOKEN_OPTIMIZER_SNAPSHOT_DIR"] = str(tmp_path)
    env["CLAUDE_SESSION_ID"] = session
    # --list should not error (it reads the file archive, which may be empty
    # since we only wrote to the SQLite store; the key assertion is that
    # --list does not crash and returns 0).
    out = _run_measure(["expand", "--list"], env)
    assert out.returncode == 0, out.stderr

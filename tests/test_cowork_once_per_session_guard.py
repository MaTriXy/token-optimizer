#!/usr/bin/env python3
"""Unit tests for the Cowork run-once-per-session guard in measure.py.

The run-once SessionStart features (ensure-health / quality-cache --force /
compact-restore --new-session-only) are wired onto UserPromptSubmit for Cowork
parity, where UserPromptSubmit fires every prompt. ``_ran_once_this_session``
must let the FIRST fire of a session through and no-op every later fire of the
SAME session, while a DIFFERENT session runs fresh. That guarantee is what
protects existing native Claude Code users from a double-fire (SessionStart sets
the marker; the UserPromptSubmit copies then no-op).

Run: python3 -m pytest tests/test_cowork_once_per_session_guard.py -q
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
SCRIPTS = REPO / "skills" / "token-optimizer" / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import measure  # noqa: E402


@pytest.fixture()
def cache_dir(tmp_path, monkeypatch):
    """Point the per-session marker dir at a temp dir (the engine uses
    QUALITY_CACHE_DIR; the guard writes ``once-<tag>-<sid>.json`` there)."""
    monkeypatch.setattr(measure, "QUALITY_CACHE_DIR", tmp_path)
    return tmp_path


def test_first_call_runs_then_same_session_noops(cache_dir):
    # First fire of the session: guard returns False (i.e. "run").
    assert measure._ran_once_this_session("ensure-health", "sess-A") is False
    # Every later fire of the same session: True (i.e. "already ran, skip").
    assert measure._ran_once_this_session("ensure-health", "sess-A") is True
    assert measure._ran_once_this_session("ensure-health", "sess-A") is True


def test_different_session_runs_fresh(cache_dir):
    assert measure._ran_once_this_session("ensure-health", "sess-A") is False
    assert measure._ran_once_this_session("ensure-health", "sess-A") is True
    # A different session_id is independent -> runs.
    assert measure._ran_once_this_session("ensure-health", "sess-B") is False
    assert measure._ran_once_this_session("ensure-health", "sess-B") is True


def test_tags_are_independent(cache_dir):
    # Each run-once feature keys on its own tag, so warming the cache does not
    # suppress the health check (or vice versa) within one session.
    assert measure._ran_once_this_session("quality-cache-force", "sess-A") is False
    assert measure._ran_once_this_session("compact-restore-new-session", "sess-A") is False
    assert measure._ran_once_this_session("ensure-health", "sess-A") is False
    # ...and each is now latched for that session.
    assert measure._ran_once_this_session("quality-cache-force", "sess-A") is True
    assert measure._ran_once_this_session("compact-restore-new-session", "sess-A") is True
    assert measure._ran_once_this_session("ensure-health", "sess-A") is True


def test_missing_session_id_fails_open(cache_dir):
    # No usable session_id -> cannot key a marker -> fail open (always run),
    # never crash, never latch.
    assert measure._ran_once_this_session("ensure-health", None) is False
    assert measure._ran_once_this_session("ensure-health", None) is False
    assert measure._ran_once_this_session("ensure-health", "") is False


def test_session_id_is_sanitized(cache_dir):
    # A hostile session_id must not escape the marker dir; the guard still works.
    weird = "../../etc/pwn ;rm -rf"
    assert measure._ran_once_this_session("ensure-health", weird) is False
    assert measure._ran_once_this_session("ensure-health", weird) is True
    # Marker landed inside the temp cache dir, not at a traversed path.
    markers = list(cache_dir.glob("once-ensure-health-*.json"))
    assert len(markers) == 1
    assert markers[0].parent == cache_dir


def test_once_mark_always_runs_and_latches_ups(cache_dir):
    # SessionStart carries --once-mark, which RUNS the work and
    # (re)writes the marker but NEVER reports "already ran". So a second
    # SessionStart of the same session (resume/compact keep the session_id) still
    # runs -- quality-cache --force re-warms after auto-compaction, the resume
    # checkpoint pointer + forced warm are no longer suppressed. The marker it
    # writes still latches the UserPromptSubmit (--once-per-session) copies so
    # native Claude Code sees zero double-fire.
    measure._mark_ran_this_session("quality-cache-force", "sess-A")
    # Second SessionStart of the SAME session: the mark path never skips.
    measure._mark_ran_this_session("quality-cache-force", "sess-A")
    # ...and the UserPromptSubmit copy is latched out by the marker.
    assert measure._ran_once_this_session("quality-cache-force", "sess-A") is True
    # Exactly one marker file exists (refresh overwrites, never accumulates).
    markers = list(cache_dir.glob("once-quality-cache-force-*.json"))
    assert len(markers) == 1


def test_once_mark_missing_session_id_is_safe(cache_dir):
    # No usable session_id -> mark is a no-op, never raises, never latches, so a
    # later UserPromptSubmit check still fails open (runs).
    measure._mark_ran_this_session("ensure-health", None)
    measure._mark_ran_this_session("ensure-health", "")
    assert not list(cache_dir.glob("once-ensure-health-*.json"))
    assert measure._ran_once_this_session("ensure-health", None) is False

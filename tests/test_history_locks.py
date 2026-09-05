"""U6 — real-history test harness: resume-lock + fresh-lock + incident fixtures.

Locks both directions against real sessions. A scorer change that
drops resume recall fails CI; a change that surfaces on fresh fails CI.

Fixtures: tests/fixtures/history/openings_and_checkpoints.json -- sanitized real
openings (derived from real Claude Code session first-prompts) + topic-faithful
checkpoint sidecars + incident fixtures (stale-pool, cross-project, own-session, non-UTF-8, CJK).

Three lock types:
  1. Resume-direction: each resume opening -> the RIGHT checkpoint wins AND
     clears CHECKPOINT_RELEVANCE_THRESHOLD.
  2. Fresh-direction: each fresh opening -> NO checkpoint clears threshold
     (EMPTY return). This is the test the old blind-recency path failed on.
  3. Incident: each incident -> its expected outcome (no_match / match /
     no_match_or_handled).
"""
from __future__ import annotations

import importlib
import json
import sys
from datetime import datetime, timedelta
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
SCRIPTS = REPO / "skills" / "token-optimizer" / "scripts"
RESUME_SCRIPTS = REPO / "skills" / "resume-checkpoint" / "scripts"
FIXTURES = REPO / "tests" / "fixtures" / "history" / "openings_and_checkpoints.json"
for p in (str(SCRIPTS), str(RESUME_SCRIPTS)):
    if p not in sys.path:
        sys.path.insert(0, str(p))


def _load_fixtures():
    return json.loads(FIXTURES.read_text(encoding="utf-8"))


FIXTURE = _load_fixtures()


@pytest.fixture
def m(monkeypatch, tmp_path):
    monkeypatch.setenv("TOKEN_OPTIMIZER_SNAPSHOT_DIR", str(tmp_path))
    if "measure" in sys.modules:
        del sys.modules["measure"]
    mod = importlib.import_module("measure")
    importlib.reload(mod)
    yield mod
    if "measure" in sys.modules:
        del sys.modules["measure"]


@pytest.fixture
def pull(m, monkeypatch, tmp_path):
    cp_dir = tmp_path / "checkpoints"
    cp_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(m, "CHECKPOINT_DIR", cp_dir, raising=True)
    if "pull_checkpoint" in sys.modules:
        del sys.modules["pull_checkpoint"]
    mod = importlib.import_module("pull_checkpoint")
    importlib.reload(mod)
    yield mod
    if "pull_checkpoint" in sys.modules:
        del sys.modules["pull_checkpoint"]


def _cp_from_spec(tmp_path, spec):
    """Build a checkpoint dict (with .md + .json sidecar) from a fixture spec."""
    filename = spec["filename"]
    cp_path = tmp_path / filename
    if spec.get("corrupt_body"):
        cp_path.write_bytes(b"# Session State Checkpoint\n\xff\xfe\x00bad\n")
    else:
        task = spec.get("active_task") or ""
        cp_path.write_text(
            f"# Session State Checkpoint\n# Generated: test\nbody: {task}\n",
            encoding="utf-8")
    if not spec.get("no_sidecar"):
        sidecar = {
            "version": 1, "generated": "test", "trigger": spec.get("trigger", "stop"),
            "session_id": "src-sid",
            "active_task": spec.get("active_task"),
            "decisions": spec.get("decisions", []),
            "modified_files": [{"path": p, "action": "edit", "range": None}
                               for p in spec.get("modified_files", [])],
            "recent_reads": [],
        }
        (tmp_path / cp_path.name.replace(".md", ".json")).write_text(
            json.dumps(sidecar), encoding="utf-8")
    return {
        "filename": filename,
        "path": str(cp_path),
        "created": datetime.now() - timedelta(seconds=spec.get("age_seconds", 60)),
        "trigger": spec.get("trigger", "stop"),
    }


def _build_pool(tmp_path, checkpoints_spec):
    return [_cp_from_spec(tmp_path, c) for c in checkpoints_spec]


def _best_checkpoint_id(m, pull, prompt, pool, cwd=None, session_id=None):
    """Return the active_task of the winning checkpoint, or None for no-match."""
    out = pull.pull_checkpoint(prompt, session_id=session_id, cwd=cwd,
                               checkpoints=pool)
    if "No relevant checkpoint found" in out:
        return None
    # The winning checkpoint's active_task appears in the sidecar summary.
    for cp in pool:
        try:
            sc = m._read_checkpoint_sidecar(cp["path"])
            if sc and sc.get("active_task", "") in out:
                return cp["filename"]
        except Exception:
            continue
    return "unknown"


# =========================================================================
# RESUME-DIRECTION LOCK: each resume opening -> the right checkpoint wins
# =========================================================================

@pytest.mark.parametrize("opening", FIXTURE["resume_openings"], ids=[o["id"] for o in FIXTURE["resume_openings"]])
def test_resume_finds_right_checkpoint(m, pull, tmp_path, opening):
    pool = _build_pool(tmp_path, FIXTURE["checkpoints"])
    winner = _best_checkpoint_id(m, pull, opening["prompt"], pool)
    expected_cp = next(c for c in FIXTURE["checkpoints"]
                       if c["id"] == opening["expected_checkpoint_id"])
    assert winner == expected_cp["filename"], (
        f"resume opening {opening['id']!r} should match checkpoint "
        f"{expected_cp['id']!r} ({expected_cp['filename']!r}), "
        f"but got {winner!r}. prompt={opening['prompt']!r}")


# =========================================================================
# FRESH-DIRECTION LOCK: each fresh opening -> NO checkpoint clears threshold
# =========================================================================

@pytest.mark.parametrize("opening", FIXTURE["fresh_openings"], ids=[o["id"] for o in FIXTURE["fresh_openings"]])
def test_fresh_opening_yields_no_match(m, pull, tmp_path, opening):
    pool = _build_pool(tmp_path, FIXTURE["checkpoints"])
    winner = _best_checkpoint_id(m, pull, opening["prompt"], pool)
    assert winner is None, (
        f"fresh opening {opening['id']!r} must NOT match any checkpoint, "
        f"but matched {winner!r}. prompt={opening['prompt']!r}")


# =========================================================================
# INCIDENT LOCKS: each incident -> its expected outcome
# =========================================================================

@pytest.mark.parametrize("incident", FIXTURE["incidents"], ids=[i["id"] for i in FIXTURE["incidents"]])
def test_incident_expected_outcome(m, pull, tmp_path, incident):
    pool = _build_pool(tmp_path, incident["checkpoints"])
    cwd = incident.get("cwd")
    sid = incident.get("session_id")
    winner = _best_checkpoint_id(m, pull, incident["prompt"], pool,
                                 cwd=cwd, session_id=sid)
    expected = incident["expected"]
    if expected == "no_match":
        assert winner is None, (
            f"incident {incident['id']!r} (#{incident.get('issue')}) "
            f"expected no_match, got {winner!r}")
    elif expected == "match":
        assert winner is not None, (
            f"incident {incident['id']!r} (#{incident.get('issue')}) "
            f"expected a match, got None")
    elif expected == "no_match_or_handled":
        # Non-UTF-8: either no match (score 0.0) or a handled match that
        # doesn't crash. The key is it never raises.
        assert winner is None or winner == "unknown", (
            f"incident {incident['id']!r} expected no_match or handled, "
            f"got {winner!r}")

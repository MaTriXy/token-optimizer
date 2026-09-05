"""U7 — replay benchmark over historical first-prompts (anti-overfit gate).

Every future scorer tweak is measured, not vibed. Guards against overfitting to
the competitor's fresh-only slice (R7): a pull-only-style silence-on-resume
(recall drop) FAILS; a fresh-direction false-positive rise FAILS.

Three test scenarios:
  1. Baseline run produces a stable metrics file matching the committed baseline.
  2. A deliberately over-tightened threshold (drops resume recall) -> FAILS.
  3. A deliberately loosened threshold (fresh false positives) -> FAILS.
"""
from __future__ import annotations

import importlib
import json
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
SCRIPTS = REPO / "scripts"
TO_SCRIPTS = REPO / "skills" / "token-optimizer" / "scripts"
RESUME_SCRIPTS = REPO / "skills" / "resume-checkpoint" / "scripts"
BASELINE = REPO / "tests" / "baselines" / "replay-metrics.json"
for p in (str(SCRIPTS), str(TO_SCRIPTS), str(RESUME_SCRIPTS)):
    if p not in sys.path:
        sys.path.insert(0, str(p))


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
def replay(m, monkeypatch, tmp_path):
    cp_dir = tmp_path / "checkpoints"
    cp_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(m, "CHECKPOINT_DIR", cp_dir, raising=True)
    if "pull_checkpoint" in sys.modules:
        del sys.modules["pull_checkpoint"]
    import pull_checkpoint
    importlib.reload(pull_checkpoint)
    if "replay_benchmark" in sys.modules:
        del sys.modules["replay_benchmark"]
    mod = importlib.import_module("replay_benchmark")
    importlib.reload(mod)
    yield mod
    for name in ("replay_benchmark", "pull_checkpoint"):
        if name in sys.modules:
            del sys.modules[name]


# --- T1: baseline run produces a stable metrics file ---

def test_baseline_matches_committed(replay, tmp_path):
    metrics = replay.run_benchmark(tmp_path=str(tmp_path / "replay"))
    baseline = json.loads(BASELINE.read_text(encoding="utf-8"))
    # The key metrics must match the committed baseline. The threshold and
    # bonus are also checked so a calibration change is caught.
    for key in ("resume_recall", "fresh_precision", "incident_pass_rate",
                "mix_weighted_expected_tokens", "threshold", "resume_intent_bonus"):
        assert metrics[key] == baseline[key], (
            f"metric {key!r} drifted: baseline={baseline[key]!r} "
            f"current={metrics[key]!r}")


# --- T2: over-tightened threshold drops resume recall -> FAILS ---

def test_over_tightened_threshold_fails(replay, m, monkeypatch, tmp_path):
    """A threshold so high that resume prompts can't clear it must FAIL the
    regression bars (silence-on-resume is a failing regression, R7)."""
    monkeypatch.setattr(m, "CHECKPOINT_RELEVANCE_THRESHOLD", 0.99, raising=True)
    metrics = replay.run_benchmark(tmp_path=str(tmp_path / "replay"))
    passed, failures = replay.check_regression(metrics)
    assert not passed, (
        f"over-tightened threshold must fail the regression bars; "
        f"resume_recall={metrics['resume_recall']:.2f}")
    assert any("resume_recall" in f for f in failures), (
        f"the failure must cite resume_recall, got: {failures}")


# --- T3: loosened threshold causes fresh false positives -> FAILS ---

def test_loosened_threshold_fails(replay, m, monkeypatch, tmp_path):
    """A threshold so low that fresh prompts match must FAIL the regression
    bars (fresh false-positive rise is a failing regression, R7)."""
    monkeypatch.setattr(m, "CHECKPOINT_RELEVANCE_THRESHOLD", 0.001, raising=True)
    metrics = replay.run_benchmark(tmp_path=str(tmp_path / "replay"))
    passed, failures = replay.check_regression(metrics)
    assert not passed, (
        f"loosened threshold must fail the regression bars; "
        f"fresh_precision={metrics['fresh_precision']:.2f}")
    assert any("fresh_precision" in f for f in failures), (
        f"the failure must cite fresh_precision, got: {failures}")


# --- T4: mix-weighted expected tokens is negative (net savings) ---

def test_mix_weighted_expected_tokens_is_net_savings(replay, tmp_path):
    """With perfect resume recall and fresh precision, the mix-weighted
    expected tokens must be negative (net savings, not net cost)."""
    metrics = replay.run_benchmark(tmp_path=str(tmp_path / "replay"))
    assert metrics["mix_weighted_expected_tokens"] < 0, (
        f"the mix-weighted expected tokens must be net savings (negative); "
        f"got {metrics['mix_weighted_expected_tokens']}")


# --- T5: the benchmark scans REAL historical first-prompts, not only fixtures ---

def _write_transcript(path, first_user_prompt):
    """A minimal Claude Code transcript whose first user entry is the prompt."""
    lines = [
        json.dumps({"type": "user", "isMeta": True,
                    "message": {"content": "<meta bootstrap>"}}),
        json.dumps({"type": "user",
                    "message": {"content": first_user_prompt}}),
        json.dumps({"type": "assistant",
                    "message": {"content": [{"type": "text", "text": "ok"}]}}),
    ]
    path.write_text("\n".join(lines), encoding="utf-8")


def test_scans_historical_first_prompts(replay, tmp_path):
    """U7 minor: the benchmark must be able to scan the FIRST user prompt of real
    session transcripts and classify each as resume vs fresh -- not just replay
    the 17 curated fixtures."""
    hist_root = tmp_path / "projects"
    (hist_root / "proj-a").mkdir(parents=True)
    (hist_root / "proj-b").mkdir(parents=True)
    _write_transcript(hist_root / "proj-a" / "sess1.jsonl",
                      "continue where we left off on the payment gateway")
    _write_transcript(hist_root / "proj-b" / "sess2.jsonl",
                      "write me a haiku about the ocean")

    # Empty/None root -> opt-out, nothing scanned (keeps the fixture baseline pure).
    assert replay.scan_historical_first_prompts(None) == {}
    assert replay.scan_historical_first_prompts(str(tmp_path / "does-not-exist")) == {}

    out = replay.scan_historical_first_prompts(str(hist_root))
    assert out["historical_scanned"] == 2, f"must scan both transcripts; got {out}"
    assert out["historical_resume_intent"] == 1, (
        f"the 'continue where we left off' opening must classify as resume; got {out}")
    assert out["historical_fresh"] == 1, (
        f"the haiku opening must classify as fresh; got {out}")


# --- T6: a "stale" spec produces a genuinely stale file (staleness exercised) ---

def test_stale_spec_is_actually_stale_on_disk(replay, m, tmp_path):
    """The stale-pool incident must exercise staleness for real: the
    relevance scorer's recency bonus reads the checkpoint FILE mtime, so a spec
    labelled stale must age the file on disk, not only a dict field. A genuinely
    stale checkpoint must therefore lose the recency bonus a fresh one gets."""
    import os as _os
    import time as _time
    d = tmp_path / "cps"
    d.mkdir()
    # Longer task + a partial-overlap prompt with NO resume cue, so the score sits
    # below the 1.0 cap and the 0.05 recency delta is observable (a saturated
    # score would hide it).
    task = ("token optimizer checkpoint injection targeting fix mirror sync "
            "parity relevance scorer statusline pointer")
    fresh = replay._cp_from_spec(d, {
        "id": "f", "filename": "aaaa1111-20260811-120000-checkpoint.md",
        "active_task": task, "modified_files": ["measure.py"], "age_seconds": 60})
    stale = replay._cp_from_spec(d, {
        "id": "s", "filename": "bbbb2222-20260811-120000-checkpoint.md",
        "active_task": task, "modified_files": ["measure.py"], "age_seconds": 43200})

    age_min = (_time.time() - _os.path.getmtime(stale["path"])) / 60
    assert age_min > 180, (
        f"a stale fixture must be older than the recency window on disk; got {age_min:.0f}min")

    prompt = "checkpoint injection targeting"
    s_fresh = m.checkpoint_relevance_score(prompt, fresh["path"], pool=[fresh])
    s_stale = m.checkpoint_relevance_score(prompt, stale["path"], pool=[stale])
    assert s_stale < s_fresh, (
        "a genuinely stale checkpoint must lose the recency bonus the fresh one "
        f"gets (staleness exercised); fresh={s_fresh} stale={s_stale}")

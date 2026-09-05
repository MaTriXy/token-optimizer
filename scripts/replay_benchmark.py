#!/usr/bin/env python3
"""U7 — replay benchmark over historical first-prompts (anti-overfit gate).

Replays the curated U6 first-prompt corpus (resume + fresh + incidents) through
the relevance scorer and emits a diffable metrics report, and ADDITIONALLY scans
"all historical first-prompts" from real session transcripts when a history root
is provided (``--history-root`` / ``TOKEN_OPTIMIZER_HISTORY_ROOT``) so the resume
detector is measured against whatever users actually typed, not only the 17
fixtures. Guards against overfitting to the competitor's fresh-only slice (R7): a
pull-only-style silence-on-resume (recall drop) FAILS; a fresh-direction
false-positive rise FAILS.

Metrics:
  - resume_recall: fraction of resume openings that matched the right checkpoint
  - fresh_precision: fraction of fresh openings that correctly yielded no match
  - incident_pass_rate: fraction of incidents with the expected outcome
  - mix_weighted_expected_tokens: expected token cost across the real resume/fresh
    mix (resume hit saves ~1500 tok, resume miss wastes ~1500 tok, fresh FP
    wastes ~300 tok, fresh correct rejection costs 0)

Usage:
  python3 scripts/replay_benchmark.py                    # print metrics
  python3 scripts/replay_benchmark.py --json             # emit JSON
  python3 scripts/replay_benchmark.py --baseline <path>  # compare to baseline
  python3 scripts/replay_benchmark.py --write-baseline <path>  # write baseline

Exit code: 0 if metrics meet the regression bars, 1 otherwise.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timedelta
from pathlib import Path

# Bootstrap: measure.py lives in the sibling token-optimizer skill's scripts dir.
_HERE = Path(__file__).resolve().parent
_TO_SCRIPTS = _HERE.parent / "skills" / "token-optimizer" / "scripts"
if str(_TO_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_TO_SCRIPTS))
_RESUME_SCRIPTS = _HERE.parent / "skills" / "resume-checkpoint" / "scripts"
if str(_RESUME_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_RESUME_SCRIPTS))

import measure
import pull_checkpoint

FIXTURES = _HERE.parent / "tests" / "fixtures" / "history" / "openings_and_checkpoints.json"

# Token cost model (conservative, from the plan's incident data):
#  - Resume hit (correct checkpoint matched): model saves ~1500 tokens (efficient
#    resume via pointer instead of re-deriving context from scratch).
#  - Resume miss (no match when one should match): model wastes ~1500 tokens
#    (re-deriving context that a checkpoint already held).
#  - Fresh false positive: model wastes ~300 tokens (an unnecessary pull tool
#    call or pointer injection that yields nothing useful).
#  - Fresh correct rejection: 0 tokens.
_RESUME_HIT_SAVINGS = 1500
_RESUME_MISS_COST = 1500
_FRESH_FP_COST = 300

# Regression bars (the benchmark FAILS if any of these are not met):
_RESUME_RECALL_BAR = 1.0      # every resume opening must match
_FRESH_PRECISION_BAR = 1.0    # every fresh opening must be rejected
_INCIDENT_PASS_BAR = 1.0      # every incident must pass


def _load_fixtures():
    return json.loads(FIXTURES.read_text(encoding="utf-8"))


def _extract_first_user_prompt(jsonl_path):
    """Return the FIRST real user prompt text from a Claude Code transcript, or
    None. Skips meta/system entries; handles both string and content-block
    message shapes. Never raises."""
    try:
        with open(jsonl_path, encoding="utf-8", errors="replace") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                except (json.JSONDecodeError, ValueError):
                    continue
                if entry.get("type") != "user" or entry.get("isMeta"):
                    continue
                msg = entry.get("message") or {}
                content = msg.get("content")
                if isinstance(content, str):
                    text = content
                elif isinstance(content, list):
                    text = " ".join(
                        c.get("text", "") for c in content
                        if isinstance(c, dict) and c.get("type") == "text")
                else:
                    text = ""
                text = text.strip()
                if text:
                    return text
    except OSError:
        return None
    return None


def scan_historical_first_prompts(root=None, limit=1000):
    """Scan REAL session transcripts for their first user prompt and classify
    each as resume-intent vs fresh via ``measure._resume_intent``.

    This is the "all historical first-prompts" corpus (U7): unlike the 17 curated
    fixtures, it exercises the resume detector against whatever users actually
    typed, so a regex/scorer change that silently reclassifies real openings is
    caught. Opt-in and deterministic: when ``root`` is falsy nothing is scanned
    (so the committed fixture baseline is never perturbed and no private
    transcripts are read during CI). Returns a counts dict, or ``{}`` when there
    is nothing to scan. Never raises.
    """
    if not root:
        return {}
    try:
        root = Path(root)
        if not root.exists():
            return {}
        prompts = []
        for p in sorted(root.rglob("*.jsonl"))[:limit]:
            text = _extract_first_user_prompt(p)
            if text:
                prompts.append(text)
        if not prompts:
            return {}
        resume = sum(1 for t in prompts if measure._resume_intent(t))
        return {
            "historical_scanned": len(prompts),
            "historical_resume_intent": resume,
            "historical_fresh": len(prompts) - resume,
        }
    except Exception:
        return {}


def _cp_from_spec(tmp_path, spec):
    filename = spec["filename"]
    cp_path = tmp_path / filename
    if spec.get("corrupt_body"):
        cp_path.write_bytes(b"# Session State Checkpoint\n\xff\xfe\x00bad\n")
    else:
        task = spec.get("active_task") or ""
        cp_path.write_text(
            f"# Session State Checkpoint\n# Generated: test\nbody: {task}\n",
            encoding="utf-8")
    sidecar_path = None
    if not spec.get("no_sidecar"):
        sidecar = {
            "version": 1, "trigger": spec.get("trigger", "stop"),
            "session_id": "src-sid",
            "active_task": spec.get("active_task"),
            "decisions": spec.get("decisions", []),
            "modified_files": [{"path": p, "action": "edit", "range": None}
                               for p in spec.get("modified_files", [])],
            # Honor the spec's recent_reads (real checkpoints carry project
            # identity in read paths too). Defaults to [] when absent, so older
            # specs are unchanged.
            "recent_reads": list(spec.get("recent_reads", [])),
        }
        sidecar_path = tmp_path / cp_path.name.replace(".md", ".json")
        sidecar_path.write_text(json.dumps(sidecar), encoding="utf-8")
    # Age the file on DISK so a "stale" spec is genuinely stale. The relevance
    # scorer's recency bonus reads the checkpoint file's mtime, NOT the ``created``
    # dict field below -- so without this a spec labelled stale still got the
    # recency bonus (fresh mtime), and the stale-pool incident passed for the
    # wrong reason (bare "continue" scores 0 regardless). Now age_seconds is
    # authoritative for both mtime and ``created``.
    age_seconds = spec.get("age_seconds", 60)
    old_ts = (datetime.now() - timedelta(seconds=age_seconds)).timestamp()
    try:
        os.utime(cp_path, (old_ts, old_ts))
        if sidecar_path is not None:
            os.utime(sidecar_path, (old_ts, old_ts))
    except OSError:
        pass
    return {
        "filename": filename, "path": str(cp_path),
        "created": datetime.now() - timedelta(seconds=age_seconds),
        "trigger": spec.get("trigger", "stop"),
    }


def _winner_filename(prompt, pool, cwd=None, session_id=None):
    """Return the winning checkpoint's filename, or None for no-match."""
    out = pull_checkpoint.pull_checkpoint(
        prompt, session_id=session_id, cwd=cwd, checkpoints=pool)
    if "No relevant checkpoint found" in out:
        return None
    for cp in pool:
        try:
            sc = measure._read_checkpoint_sidecar(cp["path"])
            if sc and sc.get("active_task", "") and sc["active_task"] in out:
                return cp["filename"]
        except Exception:
            continue
    return "unknown"


def run_benchmark(tmp_path=None):
    """Run the replay benchmark and return a metrics dict."""
    import tempfile
    if tmp_path is None:
        tmp_path = Path(tempfile.mkdtemp(prefix="to-replay-"))
    else:
        tmp_path = Path(tmp_path)
    tmp_path.mkdir(parents=True, exist_ok=True)

    fixture = _load_fixtures()
    pool = [_cp_from_spec(tmp_path, c) for c in fixture["checkpoints"]]

    # Resume direction
    resume_hits = 0
    resume_total = 0
    for o in fixture["resume_openings"]:
        resume_total += 1
        winner = _winner_filename(o["prompt"], pool)
        expected_cp = next(c for c in fixture["checkpoints"]
                           if c["id"] == o["expected_checkpoint_id"])
        if winner == expected_cp["filename"]:
            resume_hits += 1

    # Fresh direction
    fresh_correct_rejections = 0
    fresh_total = 0
    fresh_fps = []
    for o in fixture["fresh_openings"]:
        fresh_total += 1
        winner = _winner_filename(o["prompt"], pool)
        if winner is None:
            fresh_correct_rejections += 1
        else:
            fresh_fps.append(o["id"])

    # Incidents
    incident_passes = 0
    incident_total = 0
    for inc in fixture["incidents"]:
        incident_total += 1
        inc_pool = [_cp_from_spec(tmp_path, c) for c in inc["checkpoints"]]
        winner = _winner_filename(
            inc["prompt"], inc_pool,
            cwd=inc.get("cwd"), session_id=inc.get("session_id"))
        expected = inc["expected"]
        if expected == "no_match":
            if winner is None:
                incident_passes += 1
        elif expected == "match":
            if winner is not None:
                incident_passes += 1
        elif expected == "no_match_or_handled":
            if winner is None or winner == "unknown":
                incident_passes += 1

    resume_recall = resume_hits / resume_total if resume_total else 0.0
    fresh_precision = fresh_correct_rejections / fresh_total if fresh_total else 0.0
    incident_pass_rate = incident_passes / incident_total if incident_total else 0.0

    # Mix-weighted expected tokens: assume a 60/40 resume/fresh mix (from the
    # real history sample). Negative = net savings (good).
    resume_ratio = 0.6
    fresh_ratio = 0.4
    resume_miss_rate = 1.0 - resume_recall
    fresh_fp_rate = 1.0 - fresh_precision
    mix_weighted_expected_tokens = int(
        resume_ratio * (resume_miss_rate * _RESUME_MISS_COST - resume_recall * _RESUME_HIT_SAVINGS)
        + fresh_ratio * (fresh_fp_rate * _FRESH_FP_COST)
    )

    metrics = {
        "resume_recall": round(resume_recall, 4),
        "fresh_precision": round(fresh_precision, 4),
        "incident_pass_rate": round(incident_pass_rate, 4),
        "mix_weighted_expected_tokens": mix_weighted_expected_tokens,
        "resume_hits": resume_hits,
        "resume_total": resume_total,
        "fresh_correct_rejections": fresh_correct_rejections,
        "fresh_total": fresh_total,
        "fresh_false_positives": fresh_fps,
        "incident_passes": incident_passes,
        "incident_total": incident_total,
        "threshold": measure.CHECKPOINT_RELEVANCE_THRESHOLD,
        "resume_intent_bonus": measure._RELEVANCE_RESUME_INTENT_BONUS,
    }

    # "All historical first-prompts" (U7): additionally scan REAL session
    # transcripts when a history root is provided (env override; unset in CI so
    # the fixture baseline stays reproducible and no private transcripts are read
    # by default). The extra keys only appear when something was actually scanned.
    hist = scan_historical_first_prompts(os.environ.get("TOKEN_OPTIMIZER_HISTORY_ROOT"))
    if hist:
        metrics.update(hist)
    return metrics


def check_regression(metrics):
    """Return (passed, failures) for the regression bars."""
    failures = []
    if metrics["resume_recall"] < _RESUME_RECALL_BAR:
        failures.append(
            f"resume_recall {metrics['resume_recall']:.2f} < {_RESUME_RECALL_BAR:.2f} "
            f"(silence-on-resume regression)")
    if metrics["fresh_precision"] < _FRESH_PRECISION_BAR:
        failures.append(
            f"fresh_precision {metrics['fresh_precision']:.2f} < {_FRESH_PRECISION_BAR:.2f} "
            f"(fresh false-positive rise)")
    if metrics["incident_pass_rate"] < _INCIDENT_PASS_BAR:
        failures.append(
            f"incident_pass_rate {metrics['incident_pass_rate']:.2f} < {_INCIDENT_PASS_BAR:.2f}")
    return (len(failures) == 0, failures)


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Replay benchmark over historical first-prompts (U7, R7).")
    parser.add_argument("--json", action="store_true",
                        help="Emit metrics as JSON instead of human-readable.")
    parser.add_argument("--baseline", default=None,
                        help="Compare metrics to a baseline JSON file.")
    parser.add_argument("--write-baseline", default=None,
                        help="Write the current metrics as a baseline JSON file.")
    parser.add_argument("--history-root", default=None,
                        help="Scan real session transcripts under this dir for "
                             "their first user prompt (all historical first-prompts).")
    args = parser.parse_args(argv)

    if args.history_root:
        os.environ["TOKEN_OPTIMIZER_HISTORY_ROOT"] = args.history_root

    metrics = run_benchmark()

    if args.write_baseline:
        p = Path(args.write_baseline)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(metrics, indent=2) + "\n", encoding="utf-8")
        print(f"Baseline written to {p}")
        return 0

    passed, failures = check_regression(metrics)

    if args.baseline:
        baseline = json.loads(Path(args.baseline).read_text(encoding="utf-8"))
        if metrics != baseline:
            print("METRICS DRIFTED from baseline:")
            for k in sorted(set(list(metrics.keys()) + list(baseline.keys()))):
                if metrics.get(k) != baseline.get(k):
                    print(f"  {k}: baseline={baseline.get(k)!r} current={metrics.get(k)!r}")
            return 1
        else:
            print("Metrics match baseline.")
            return 0

    if args.json:
        print(json.dumps(metrics, indent=2))
    else:
        print(f"Replay Benchmark (threshold={metrics['threshold']}, bonus={metrics['resume_intent_bonus']})")
        print(f"  resume_recall:       {metrics['resume_recall']:.2f} ({metrics['resume_hits']}/{metrics['resume_total']})")
        print(f"  fresh_precision:     {metrics['fresh_precision']:.2f} ({metrics['fresh_correct_rejections']}/{metrics['fresh_total']})")
        if metrics["fresh_false_positives"]:
            print(f"  fresh_false_positives: {metrics['fresh_false_positives']}")
        print(f"  incident_pass_rate:  {metrics['incident_pass_rate']:.2f} ({metrics['incident_passes']}/{metrics['incident_total']})")
        print(f"  mix_weighted_expected_tokens: {metrics['mix_weighted_expected_tokens']}")
        if "historical_scanned" in metrics:
            print(f"  historical_first_prompts:  {metrics['historical_scanned']} scanned "
                  f"({metrics['historical_resume_intent']} resume-intent, "
                  f"{metrics['historical_fresh']} fresh)")
        if not passed:
            print("  REGRESSION:")
            for f in failures:
                print(f"    - {f}")

    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())

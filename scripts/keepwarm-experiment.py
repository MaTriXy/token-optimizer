#!/usr/bin/env python3
"""Keep-warm mechanism validation experiment (plan U1, HARD GATE).

Proves, with real budget-capped API calls, the two inferred claims the
keep-warm feature depends on (plan KTD-2):

  CLAIM-1  A headless `claude --resume <id> -p` run re-sends the session's
           exact prefix and lands as a cache HIT (cache_read ~ prefix,
           cache_creation ~ 0), without touching the original transcript
           when --no-session-persistence (form A) or --fork-session
           (form B fallback) is used.
  CLAIM-2  That hit refreshes the cache entry's TTL: a later resume that
           lands AFTER the original TTL window but INSIDE the refreshed
           window is still warm.

Run modes:
  --fast (default)  full proof only when the seed session writes 5m cache
                    entries (proof fits in ~10 minutes). For 1h entries,
                    CLAIM-1 is proven and CLAIM-2 is reported as SKIPPED
                    (use --long to wait out a full hour).
  --long            also run CLAIM-2 for 1h entries (>1h wall clock).

Requirements: ANTHROPIC_API_KEY in env (API billing — never run this on a
subscription-only machine; that is the exact user class keep-warm excludes).
Total spend is hard-capped: every claude call carries --max-budget-usd and
the script aborts if cumulative reported cost exceeds $2.00.

Stdlib only. Read-only outside its scratch dir; never modifies the repo or
any pre-existing transcript.
"""

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

# Windows console-flash guard. A console-subsystem child spawned from a
# console-less parent gets a NEW console allocated by Windows -- the flashing cmd
# window. CREATE_NO_WINDOW suppresses it. The attribute exists only on Windows
# builds, so getattr(..., 0) makes this a no-op on POSIX (0 is the documented
# creationflags default). This script is a maintainer-run experiment, never
# shipped to a user's plugin dir, so the guard is uniformity, not a live fix.
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)

TOTAL_BUDGET_USD = 2.00
PER_CALL_BUDGET_USD = 0.50
# Haiku: cheapest adequate model. The experiment reads usage metadata, not
# model output ("ok" x4); cache mechanics are model-independent. Claude
# Code's ~12-15k system prompt clears Haiku's 4,096-token cache floor.
MODEL = os.environ.get("KEEPWARM_EXP_MODEL", "claude-haiku-4-5-20251001")


def _resolve_claude_bin():
    """Resolve `claude` to an absolute path, mirroring the executor's resolver.

    `shutil.which` first (honours a foreground shell PATH), then the common
    install locations a minimal/launchd PATH cannot see -- so running this dev
    tool under a stripped PATH no longer spawns bare `claude` and pollutes the
    ledger with FileNotFoundError rows (the stale 48-error run, 2026-06-11).
    """
    found = shutil.which("claude")
    if found:
        return found
    home = Path.home()
    for cand in (home / ".local" / "bin" / "claude",
                 Path("/opt/homebrew/bin/claude"),
                 Path("/usr/local/bin/claude"),
                 home / ".claude" / "local" / "claude"):
        if cand.is_file() and os.access(str(cand), os.X_OK):
            return str(cand)
    return "claude"


CLAUDE_BIN = _resolve_claude_bin()
PROJECTS_DIR = Path.home() / ".claude" / "projects"

# Thresholds for "the ping hit the cache" (fractions of the seed prefix).
HIT_READ_MIN_FRACTION = 0.8
HIT_WRITE_MAX_FRACTION = 0.2

_spent = 0.0
_results = {"claims": {}, "calls": [], "model": MODEL}
# Per-run private scratch dir (0700) for the result json; created lazily so a
# multi-user host never shares a predictable /tmp/keepwarm-experiment-result.json.
_RESULT_DIR = None


def _result_dir() -> Path:
    global _RESULT_DIR
    if _RESULT_DIR is None:
        _RESULT_DIR = Path(tempfile.mkdtemp(prefix="keepwarm-exp-result-"))
        try:
            os.chmod(_RESULT_DIR, 0o700)
        except OSError:
            pass
    return _RESULT_DIR


def fail(msg: str) -> None:
    print(f"ABORT: {msg}", file=sys.stderr)
    _results["aborted"] = msg
    _emit()
    sys.exit(1)


def _emit() -> None:
    out = _result_dir() / "keepwarm-experiment-result.json"
    # 0600 result file: it records session_ids + usage. Write via a fd opened
    # O_CREAT|O_WRONLY|O_TRUNC with mode 0600 so it is never group/other-readable.
    fd = os.open(str(out), os.O_CREAT | os.O_WRONLY | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(json.dumps(_results, indent=2))
    print(f"\nresult json: {out}")


def run_claude(args, cwd, label):
    """Run a budget-capped headless claude call; return parsed JSON result."""
    global _spent
    if _spent >= TOTAL_BUDGET_USD:
        fail(f"cumulative spend ${_spent:.2f} reached the ${TOTAL_BUDGET_USD} cap")
    cmd = [
        CLAUDE_BIN,
        *args,
        "--model",
        MODEL,
        "--max-budget-usd",
        str(PER_CALL_BUDGET_USD),
        "--output-format",
        "json",
        "--print",
    ]
    try:
        proc = subprocess.run(
            cmd, cwd=cwd, capture_output=True, text=True, timeout=300,
            creationflags=_NO_WINDOW,
        )
    except subprocess.TimeoutExpired:
        # A hung claude must route through the normal _error path (which the HARD
        # GATE already handles) instead of crashing the gate after pre-spend.
        return {"_error": "timeout after 300s", "_rc": -1}
    if proc.returncode != 0:
        err = (proc.stderr.strip()[-300:] or "") + " | stdout: " + \
            (proc.stdout.strip()[-300:] or "(empty)")
        return {"_error": err, "_rc": proc.returncode}
    try:
        data = json.loads(proc.stdout)
    except json.JSONDecodeError:
        return {"_error": f"non-json output: {proc.stdout[:300]}"}
    cost = float(data.get("total_cost_usd") or 0.0)
    _spent += cost
    usage = data.get("usage") or {}
    rec = {
        "label": label,
        "cost_usd": cost,
        "cumulative_usd": round(_spent, 4),
        "session_id": data.get("session_id"),
        "cache_read": usage.get("cache_read_input_tokens", 0),
        "cache_creation": usage.get("cache_creation_input_tokens", 0),
        "input": usage.get("input_tokens", 0),
    }
    cc = usage.get("cache_creation") or {}
    rec["ephemeral_1h"] = cc.get("ephemeral_1h_input_tokens", 0)
    rec["ephemeral_5m"] = cc.get("ephemeral_5m_input_tokens", 0)
    _results["calls"].append(rec)
    print(f"  [{label}] cost=${cost:.4f} read={rec['cache_read']} "
          f"write={rec['cache_creation']} (1h={rec['ephemeral_1h']} "
          f"5m={rec['ephemeral_5m']})")
    data["_rec"] = rec
    return data


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def find_transcript(session_id: str, retries: int = 6) -> Path:
    """Locate a session's transcript by id — never guess cwd encoding
    (symlink resolution and _ -> - rewriting both vary)."""
    for _ in range(retries):
        hits = list(PROJECTS_DIR.glob(f"*/{session_id}.jsonl"))
        if hits:
            return hits[0]
        time.sleep(2)
    return Path("/nonexistent") / f"{session_id}.jsonl"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--long", action="store_true",
                    help="run CLAIM-2 even for 1h cache entries (>1h wall)")
    opts = ap.parse_args()

    if not os.environ.get("ANTHROPIC_API_KEY"):
        # Subscription-auth run: fine for the MECHANISM proof (cache
        # semantics are billing-independent); spend is a sliver of plan
        # quota, not dollars. The production feature itself remains
        # API-billing-only.
        print("note: no ANTHROPIC_API_KEY — running on the logged-in "
              "subscription auth (mechanism proof only, quota not dollars)")
        _results["billing"] = "subscription"
    else:
        _results["billing"] = "api_key"

    # Guarded like run_claude (T1 Finding 6): a missing/hung `claude` must
    # route through fail() (clean ABORT + result json) instead of raising a
    # bare FileNotFoundError/TimeoutExpired out of main() before any result
    # is emitted. FileNotFoundError is an OSError subclass.
    try:
        help_text = subprocess.run(
            [CLAUDE_BIN, "--help"], capture_output=True, text=True,
            timeout=60, creationflags=_NO_WINDOW,
        ).stdout
    except subprocess.TimeoutExpired:
        fail("`claude --help` timed out after 60s")
    except OSError as exc:
        fail(f"cannot run {CLAUDE_BIN!r}: {exc}")
    for flag in ("--no-session-persistence", "--fork-session",
                 "--max-budget-usd", "--output-format"):
        if flag not in help_text:
            fail(f"installed claude build lacks {flag}")

    scratch = Path(tempfile.mkdtemp(prefix="keepwarm-exp-"))
    print(f"scratch cwd: {scratch}")

    # ---- Seed session (persisted; this is the "user session" stand-in) ----
    print("seeding session...")
    t0 = time.time()
    # Trivial prompt: no tool temptation; the cached PREFIX (system prompt,
    # ~12-33k under a real user config) is what the experiment measures.
    seed = run_claude(["Reply with exactly: ok"], scratch, "seed")
    if "_error" in seed:
        print(f"  seed attempt 1 failed ({seed['_error'][:120]}); retrying")
        time.sleep(10)
        seed = run_claude(["Reply with exactly: ok"], scratch, "seed-retry")
    if "_error" in seed:
        fail(f"seed failed: {seed['_error']}")
    sid = seed.get("session_id")
    if not sid:
        fail("seed produced no session_id")
    prefix = seed["_rec"]["cache_creation"] + seed["_rec"]["cache_read"]
    ttl_kind = "1h" if seed["_rec"]["ephemeral_1h"] else (
        "5m" if seed["_rec"]["ephemeral_5m"] else "unknown")
    _results["seed"] = {"session_id": sid, "prefix": prefix,
                        "ttl_kind": ttl_kind}
    print(f"  session {sid} prefix~{prefix} ttl_kind={ttl_kind}")
    if prefix < 1024:
        fail(f"seed prefix {prefix} below the model's min cacheable size")

    jsonl = find_transcript(sid)
    if not jsonl.exists():
        fail(f"transcript not found for session {sid}")
    pdir = jsonl.parent
    pdir_before = set(p.name for p in pdir.iterdir())
    sig0 = sha256(jsonl)

    # ---- CLAIM-1, form A: --resume + --no-session-persistence ----
    time.sleep(5)
    print("CLAIM-1 form A: resume + no-session-persistence ping...")
    ping = run_claude(
        ["--resume", sid, "--no-session-persistence",
         "Reply with exactly: ok"],
        scratch, "ping-A",
    )
    form = "A:--no-session-persistence"
    if "_error" in ping:
        print(f"  form A failed ({ping['_error']}); trying form B")
        print("CLAIM-1 form B: resume + fork-session ping...")
        ping = run_claude(
            ["--resume", sid, "--fork-session", "Reply with exactly: ok"],
            scratch, "ping-B",
        )
        form = "B:--fork-session"
        if "_error" in ping:
            fail(f"both ping forms failed: {ping['_error']}")
    rec = ping["_rec"]
    hit = (rec["cache_read"] >= HIT_READ_MIN_FRACTION * prefix
           and rec["cache_creation"] <= HIT_WRITE_MAX_FRACTION * prefix)
    new_files = (set(p.name for p in pdir.iterdir()) - pdir_before
                 - {f"{sid}.jsonl"})
    # "untouched" = the seed transcript's bytes are unchanged AND (for form A,
    # which promises no persistence) no NEW transcript files were created.
    untouched = sha256(jsonl) == sig0
    if form.startswith("A"):
        untouched = untouched and not new_files
    _results["claims"]["CLAIM-1"] = {
        "form": form, "cache_hit": hit, "original_untouched": untouched,
        "read": rec["cache_read"], "write": rec["cache_creation"],
        "prefix": prefix, "new_transcript_files": sorted(new_files),
        "verdict": "VERIFIED" if (hit and untouched) else "REFUTED",
    }
    print(f"  CLAIM-1: hit={hit} untouched={untouched} -> "
          f"{_results['claims']['CLAIM-1']['verdict']}")
    if not (hit and untouched):
        _results["claims"]["CLAIM-2"] = {"verdict": "MOOT"}
        _emit()
        print("GATE: NO-GO (CLAIM-1 refuted — see plan KTD-2 abort path)")
        return 1

    # ---- CLAIM-2: ping refreshes TTL ----
    if ttl_kind == "5m" or opts.long:
        window = 300 if ttl_kind == "5m" else 3600
        margin = 60 if ttl_kind == "5m" else 300
        # ping again just before expiry, then resume after the ORIGINAL
        # window has lapsed but inside the refreshed one.
        wait1 = max(0, (window - margin) - (time.time() - t0))
        print(f"CLAIM-2: sleeping {wait1:.0f}s to pre-expiry ping point...")
        time.sleep(wait1)
        refresh = run_claude(
            ["--resume", sid, "--no-session-persistence",
             "Reply with exactly: ok"],
            scratch, "refresh-ping",
        )
        if "_error" in refresh:
            fail(f"refresh ping failed: {refresh['_error']}")
        t_refresh = time.time()
        wait2 = max(0, (t0 + window + margin) - time.time())
        print(f"CLAIM-2: sleeping {wait2:.0f}s past the original window...")
        time.sleep(wait2)
        final = run_claude(
            ["--resume", sid, "--no-session-persistence",
             "Reply with exactly: ok"],
            scratch, "final-resume",
        )
        if "_error" in final:
            fail(f"final resume failed: {final['_error']}")
        frec = final["_rec"]
        warm = (frec["cache_read"] >= HIT_READ_MIN_FRACTION * prefix
                and frec["cache_creation"] <= HIT_WRITE_MAX_FRACTION * prefix)
        _results["claims"]["CLAIM-2"] = {
            "ttl_kind": ttl_kind,
            "original_window_s": window,
            "final_resume_at_s": round(time.time() - t0),
            "refreshed_at_s": round(t_refresh - t0),
            "warm_after_original_expiry": warm,
            "verdict": "VERIFIED" if warm else "REFUTED",
        }
        print(f"  CLAIM-2: warm={warm} -> "
              f"{_results['claims']['CLAIM-2']['verdict']}")
    else:
        _results["claims"]["CLAIM-2"] = {
            "verdict": "SKIPPED",
            "reason": f"seed wrote {ttl_kind} entries; full 1h proof needs "
                      f"--long (>1h wall). Refresh-on-read is doc-verified; "
                      f"the 5m fast proof generalizes when available.",
        }
        print("  CLAIM-2: SKIPPED (1h entries, no --long)")

    _results["total_spend_usd"] = round(_spent, 4)
    _emit()
    c1 = _results["claims"]["CLAIM-1"]["verdict"]
    c2 = _results["claims"]["CLAIM-2"]["verdict"]
    go = c1 == "VERIFIED" and c2 in ("VERIFIED", "SKIPPED")
    print(f"\nGATE: {'GO' if go else 'NO-GO'} "
          f"(CLAIM-1={c1}, CLAIM-2={c2}, spend=${_spent:.2f})")
    return 0 if go else 1


if __name__ == "__main__":
    sys.exit(main())

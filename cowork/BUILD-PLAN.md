# Token Optimizer Upgrades — Build Plan (2026-08-27)

**Base:** worktree off origin/main @ `00e7bc9` = **v5.12.4**. Branch `upgrades/compression-2026-08-27`.
**Orchestrator:** alexgreenshpun-a2 (Claude). **Fleet:** GLM-5.2 (Devin) build, DeepSeek Flash (Devin) reviews, DeepSeek V4 Pro (droid/warp) hard design, Fable final review. **No Sonnet.**
**Source of truth for evidence:** tbench head-to-head benchmark, `.../benchmarks/tbench-headtohead-2026-08-27/harness/DEBUG_FINDINGS_fable.md`.

## Why these upgrades (measured, not vibes)
Liveness proof in Harbor (Haiku 4.5, both arms reward 1.0):
- `jq . big.json` 54.4KB → TO **468** vs baseline 2,210 (TO wins on structured data)
- `ls -la /usr/bin` 41.1KB → TO 10,131 vs baseline 2,206 (**TO LOSES** — CC already stubs >30KB to ~2.2KB)
- Bash eligibility on real agent commands: **2 of 153 (1.3%)** — 140 excluded for shell metachars, 11 non-whitelisted
- SessionStart injects ~0.7KB/session tax; 594 chars of it is a useless `[Error] systemctl --user is not reachable`

## Levers → Units

### Unit A — Kill the SessionStart context tax (Lever 2) · GLM/Devin · LOW risk
`measure.py:23694` prints `[Error] systemctl --user is not reachable...` (594 chars) into agent context on hook/headless/container runs. Suppress daemon/systemctl diagnostic noise from *injected context* when running as a non-interactive hook; keep it only in interactive dashboard/diagnostic output. Target: ~0.7KB → ~0.1KB per session.
**Eval:** measure.py self-test; assert SessionStart injection < 150 chars in headless mode; liveness gate still green.

### Unit B — Expand Bash compression eligibility (Lever 1 / "Battle A") · DeepSeek Pro design + GLM impl · HIGH value, HIGH care
`bash_hook.py` rewrites the command pre-exec to re-run through `bash_compress.py`; it refuses `;|&\`$(){}><` (140/153). Design a **post-execution** output-compression path (PostToolUse or a safe raw-exec wrapper) that compresses pipeline/metachar command *output* WITHOUT unsafe command reconstruction, preserving all read-only/safety guarantees. This is the biggest win; must not weaken security posture.
**Eval:** eligibility re-measured on the 153-command corpus (target ≫ 1.3%); full existing bash_hook/bash_compress test suite green; security review (DeepSeek Flash + Fable); no non-read-only command ever double-executed.

### Unit C — Beat CC's 30KB stub (Lever 4) · GLM/Devin · MED risk
CC 2.1.247 truncates >~30KB outputs to a ~2.2KB `<persisted-output>` stub, so TO's 10KB compressed `ls` LOSES. Make TO detect the stub threshold and guarantee its compressed result is smaller than the stub (or defer to CC's stub when it can't beat it). TO must never be larger than baseline on huge outputs.
**Eval:** synthetic >30KB fixtures; assert TO result ≤ baseline stub size for every case; liveness gate green.

### Unit D — Consent-gate race hardening (Lever 3) · GLM/Devin · LOW risk (may be partly fixed in 5.12.4)
`run.py:113 _check_consent` already claims fail-open. Verify the SessionStart race (config.json exists, flags not yet written → all hooks silently no-op) is actually closed in 5.12.4; add a regression test that reproduces the race and proves hooks still fire.
**Eval:** new regression test red→green; existing run.py tests green.

## Gates (every unit, no exceptions)
1. Unit's own new tests green.
2. Existing test suite for touched modules green (no regression).
3. Liveness gate `harness/scripts/liveness_check.sh` green (TO arm still compresses in-container).
4. Version alignment (plugin.json/marketplace.json/README/tag) — via version-master before any ship.

## Sequence
A (prove the loop, fast) → D (quick verify) → C → B (biggest, most care) → Fable review of the full diff → torture-room gauntlet (5 GLM/Devin batches) → version-master → report to Alex.

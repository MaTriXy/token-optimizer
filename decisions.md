# Cowork Full-Parity — Decisions (cowork-full-parity branch)

## Design Decisions
- **In-place parity, not a second plugin.** The main `token-optimizer` plugin becomes
  Cowork-native by adding the 3 run-once SessionStart features (ensure-health,
  quality-cache --force, compact-restore --new-session-only) into the MASTER
  `hooks/hooks.json` **UserPromptSubmit** group, each wrapped in a once-per-session guard.
  Rationale: SessionStart does not fire in Cowork; UserPromptSubmit does. The guard makes
  the UserPromptSubmit copies no-op in regular Claude Code (SessionStart already ran them
  and set the marker), so zero behavior change for existing users and no duplicated skills/
  tree. "Install the normal plugin in Cowork" = full parity.
- **Proven firing events (cloud Cowork, CC 2.1.231):** UserPromptSubmit, PreToolUse,
  PostToolUse, Stop, SubagentStop. `COWORK_EVENTS` corrected to the 4 that carry features
  (SubagentStop has no master hooks to ride — not fabricated).
- **build_cowork_hooks() stays a pure trim** (org-console ZIP path). Because master
  UserPromptSubmit already carries the run-once commands, no special remap/injection is
  needed in the packager.
- **Once-per-session guard** keyed on sanitized session_id, stored in the engine's existing
  per-session state dir (no new top-level location). Protects every existing CC user from
  double-fire — this is the one correctness-critical piece.

## Deviations
- COWORK_BUILD_PLAN.md item 1-2 described editing cowork_install.py to remap events. The
  remap moved UP into master hooks.json instead (cleaner, fixes both distribution paths at
  once). Packager change reduced to the COWORK_EVENTS constant.

## Tradeoffs
- Adding guarded commands to master UserPromptSubmit = 3 extra marker-check subprocess
  spawns on the first prompt of every regular Claude Code session. Negligible, fail-open.
  Accepted in exchange for a single source of truth and no repo bloat.

## Open Questions
- compaction-restore on native trigger stays degraded in Cowork (PreCompact/PostCompact/
  SessionStart:compact all dead). compact-capture on Stop still saves state; fresh-session
  compact-restore reads it back. Documented, not solved.
- Version bump + alignment (plugin.json / marketplace.json / README badge / tag) at package
  step; not pushed to main until torture-room green + review.

---

# Docs Coverage: Cowork + platform-parity gaps (docs/cowork-platform-gap branch)

Audit of docs vs. the code that actually ships. Base: origin/main @ 72b2f41.

## Gap list (found in audit)
1. **Comparison "Multi-platform" row omits Cowork.** comparison.mdx:67 and README.md:236
   both list 7 platforms (Claude Code, VS Code, Codex, OpenClaw, OpenCode, Hermes, Copilot)
   and not Cowork, though `cowork_install.py`, `cowork_doctor.py`, `is_cowork()`, a
   `cowork` routing row, and a committed `cowork/token-optimizer/` plugin all ship. FIXED.
2. **No `platforms/cowork.mdx`.** Every other runtime has a platform page; Cowork had none.
   FIXED (new page, grounded in cowork_install.py / cowork_doctor.py / runtime_env.py /
   routing_advisor.py / measure.py / cowork README + MISSING.md).
3. **`platforms/overview.mdx` support table omits Cowork.** FIXED (added Experimental row +
   a clarifying paragraph; Cowork = same engine in a VM, org-console distribution).
4. **`reference/capability-matrix.mdx` never mentions Cowork.** FIXED with an honest pointer
   section rather than a fabricated per-cell column (see Open Questions).
5. **Sidebar (astro.config.mjs) had no Cowork platform entry.** FIXED.

## Design Decisions
- **Cowork listed as a platform, but labeled Experimental.** It is a Claude Code refinement
  (`detect_runtime()` returns "claude"; `is_cowork()` refines), so the comparison cell just
  appends "Cowork" next to Claude Code / VS Code. Consistent with how Hermes/Copilot (also
  beta) appear plainly in the same cell. The dedicated page carries the full caveat.
- **Every page claim traces to code**, not to the cowork/README.md prose (which is stale on
  the event set). Hook set taken from `COWORK_EVENTS` in cowork_install.py = UserPromptSubmit,
  PreToolUse, PostToolUse, Stop. Detection signals from `is_cowork()`. Footprint caveat from
  measure.py `context` (FIX 5). Degraded compaction from the cowork_install.py docstring.
- **No full capability-matrix column for Cowork.** Its hook firing is confirmed by probe, not
  by code (MISSING.md: "NOT verifiable from code"), so a per-cell grid would overstate what
  is verified. Added a prose section pointing to the platform page instead.

## Deviations
- Task named comparison.mdx + README as the required edits. Also touched overview.mdx,
  capability-matrix.mdx, and astro.config.mjs because Cowork's absence there is the same
  discoverability gap; all are platform-coverage surfaces, not unrelated content.

## Tradeoffs
- Chose an honest "not gridded yet" note over a speculative capability-matrix column.
  Loses at-a-glance parity comparison; keeps every claim verifiable. Right call given the
  unverified-live status and the "do not fabricate" constraint.

## Open Questions
- **cowork/README.md "hook set" prose is stale**: it says "(SessionStart, UserPromptSubmit,
  PreToolUse, Stop)" but the code's COWORK_EVENTS dropped SessionStart and added PostToolUse.
  Left untouched (dev-facing README, outside the docs-site scope of this PR). Worth a
  follow-up fix.
- **No `install/cowork.mdx`** to mirror the other per-platform install pages. Install steps
  are embedded in platforms/cowork.mdx instead, since Cowork "install" is org-console
  distribution, not a local install. Add a dedicated install page if desired for symmetry.
- **Full capability-matrix Cowork column** deferred until the to-hook-probe confirms the
  fire matrix on a live build. Then the per-cell coverage can be asserted, not inferred.
- **Overview intro still says "seven coding surfaces"** and capability-matrix says "nine
  surfaces"; these pre-existing counts were left as-is (Cowork added as an experimental
  adapter, not folded into the native-surface count). Revisit once Cowork is live-verified.

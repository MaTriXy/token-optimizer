# Orchestrator Decisions — TO Upgrades (2026-08-27)

Orchestrator: alexgreenshpun-a2 (Claude). Live log of routing + interpretation decisions.

## Design Decisions
- **Base tree:** worktree off origin/main @ 00e7bc9 = v5.12.4 (Alex: "the most latest version on main"). NOT the local diverged 5.11.93 line (546 ahead / 616 behind). Cowork line preserved at branch cowork-line-backup-2026-08-27.
- **Isolation:** one git worktree per unit off the 5.12.4 base, so 4 agents never collide. Base worktree = integration line (Unit A + merge target); wt-B/C/D = unit branches, merged in after their gate.
- **Lane routing (Alex, locked):** GLM→Devin · DeepSeek Flash→Devin · Kimi K3→Droid · DeepSeek V4 Pro→Droid · Fable=final review. Everything interactive. NO OpenRouter, NO Sonnet.
  - A (SessionStart tax) = GLM/Devin
  - B (Bash eligibility, design-first) = DeepSeek V4 Pro (Max)/Droid
  - C (beat 30KB stub) = DeepSeek Flash/Devin
  - D (consent race) = Kimi K3/Droid
- **Droid autonomy:** Auto(Low) = edits + read-only auto, ask on risky. md5/read commands approved "always in project" to reduce babysitting.

## Deviations
- Unit A found a THIRD measure.py mirror at cowork/token-optimizer/skills/... (I briefed "two copies"). Agent instructed to keep all copies in sync — correct behavior, note for merge.
- Briefly started GLM on the OpenRouter/cc lane (model z-ai/glm-5.2) before Alex corrected to Devin GLM. Reverted immediately. OpenRouter is banned for this work.

## Tradeoffs
- Devin's model ids differ from droid's: Devin wants `deepseek-v4-flash` (not `-0731`); droid exposes display-name models ("DeepSeek V4 Pro (Droid Core)") selectable only via the /model picker, NOT the `-m` CLI flag (the flag gets treated as a chat message). Resolved by driving the picker via tmux.
- OpenRouter GLM in the cc harness rejected the bare alias `glm` and the id `z-ai/glm-5.2` ("no access") — another reason Devin is the right GLM lane here.

## Open Questions
- Merge strategy for the 4 unit branches: sequential fast-forward + conflict resolve by orchestrator, then one integration branch → Fable review → gauntlet. (Assuming this unless told otherwise.)
- Unit B: awaiting the agent's design note (approach a/b/c) before it implements — will sanity-check the safety argument before greenlighting code.

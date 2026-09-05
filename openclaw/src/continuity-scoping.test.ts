/**
 * Per-project scoping for OpenClaw continuity injection.
 *
 * A two-project checkpoint leaks project A's Key Decisions into project B's
 * hint because the checkpoint's session-wide fields are dumped verbatim once
 * the checkpoint clears the same-project gate. The fix is a set-overlap
 * keep/drop rule (``keepRecoveredItem``) applied to each recovered item BEFORE
 * the existing slices, with one disclosure line emitted only when something
 * is dropped.
 *
 * The parity fixture (``PARITY_FIXTURE``) is loaded from a single shared JSON
 * file (``tests/fixtures/keep_recovered_parity.json``) that is also consumed
 * by the Python and OpenCode TS scoping tests, so all three runtimes' keep/drop
 * decisions are asserted against the SAME token inputs with no copy drift.
 */
import { test, expect } from "bun:test";
import {
  keepRecoveredItem,
  crossProjectFileDrop,
  neutralizeRecoveredBody,
  buildResumeLeanBlock,
  buildContinuityHint,
  type CheckpointEntry,
  type ContinuityCandidate,
} from "./continuity.js";
import parityFixtureJson from "../../tests/fixtures/keep_recovered_parity.json";
import dropFixtureJson from "../../tests/fixtures/cross_project_file_drop_parity.json";

// ---------------------------------------------------------------------------
// Shared parity fixture — single source of truth.
// Loaded from tests/fixtures/keep_recovered_parity.json, consumed by all 3
// suites (Python, OpenClaw TS, OpenCode TS). (item_text, keep_tokens, expected_keep)
// ---------------------------------------------------------------------------

const PARITY_FIXTURE: Array<[string, Set<string>, boolean]> = (parityFixtureJson as Array<{item_text: string; keep_tokens: string[]; expected_keep: boolean}>)
  .map((row) => [row.item_text, new Set(row.keep_tokens), row.expected_keep] as [string, Set<string>, boolean]);

// ---------------------------------------------------------------------------
// Parity: the decision function on a shared token fixture
// ---------------------------------------------------------------------------

test("keepRecoveredItem matches the shared parity fixture exactly", () => {
  for (const [itemText, keepTokens, expected] of PARITY_FIXTURE) {
    const got = keepRecoveredItem(itemText, keepTokens);
    expect(got).toBe(expected);
  }
});

test("keepRecoveredItem is purely set-overlap, no float threshold", () => {
  expect(keepRecoveredItem("alpha beta gamma delta epsilon zeta", new Set())).toBe(false);
  expect(keepRecoveredItem("alpha beta", new Set())).toBe(true);
});

// ---------------------------------------------------------------------------
// crossProjectFileDrop parity — path normalization
// Loaded from tests/fixtures/cross_project_file_drop_parity.json, consumed by
// all 3 suites. Covers backslash, UNC, trailing separator, mixed separators,
// case mismatch (casefold on Darwin/Win32), relative, and cwd-absent.
// ---------------------------------------------------------------------------

const _CASEFOLD = process.platform === "win32" || process.platform === "darwin";

interface DropRow {
  comment: string;
  path: string;
  cwd: string;
  expected_drop: boolean;
  expected_drop_casefold?: boolean;
}

test("crossProjectFileDrop matches the shared path-normalization fixture exactly", () => {
  for (const row of dropFixtureJson as DropRow[]) {
    const expected =
      _CASEFOLD && row.expected_drop_casefold !== undefined
        ? row.expected_drop_casefold
        : row.expected_drop;
    const got = crossProjectFileDrop(row.path, row.cwd);
    expect(got).toBe(expected);
  }
});

// ---------------------------------------------------------------------------
// buildResumeLeanBlock — mixed A/B checkpoint queried from B
// ---------------------------------------------------------------------------

const PROJ_B = "/home/u/beta";
const PROJ_A = "/home/u/gamma";

function makeEntry(): CheckpointEntry {
  return {
    path: "/tmp/checkpoints/test-session.md",
    sessionDirName: "testsession",
    trigger: "auto",
    createdAt: Date.now(),
  };
}

function mixedAbCheckpointMd(): string {
  // OpenClaw checkpoint .md format: blockquote header + ## sections with
  // "- " bullet items parsed by parseCheckpointSections.
  return [
    "> Quality: B (82/100)",
    "> Fill: 70%",
    "",
    "## Key Decisions",
    "- Ship the beta feature behind a feature flag",
    "- Refactor the gamma delta epsilon module for project alpha",
    "- Wire beta_router into the request pipeline",
    "",
    "## File Changes",
    `- ${PROJ_B}/src/beta_router.py`,
    `- ${PROJ_A}/src/gamma_engine.py`,
    "",
    "## Recent Messages",
    "### User",
    "work on the beta feature",
    "",
  ].join("\n");
}

test("buildResumeLeanBlock drops A-only decisions and emits one disclosure", () => {
  const entry = makeEntry();
  const content = mixedAbCheckpointMd();
  const block = buildResumeLeanBlock(entry, content, 3500, "continue the beta work", PROJ_B);

  // B-overlapping decisions kept:
  expect(block).toContain("beta feature behind a feature flag");
  expect(block).toContain("beta_router");
  // A-only DECISION dropped:
  expect(block).not.toContain("gamma delta epsilon");
  // A-only FILE path dropped (cross-project absolute path not under cwd):
  expect(block).not.toContain("gamma_engine");
  // Exactly one disclosure line. Both the A-only decision AND the A-only
  // file path are dropped, so the disclosure reports both categories:
  const disclosureCount = (block.match(/- Omitted \(scoped to current project\):/g) || []).length;
  expect(disclosureCount).toBe(1);
  expect(block).toContain("- Omitted (scoped to current project): 1 decision(s), 1 file(s)");
});

test("buildResumeLeanBlock single-project checkpoint emits NO disclosure", () => {
  const entry = makeEntry();
  const content = [
    "> Quality: A (95/100)",
    "",
    "## Key Decisions",
    "- Ship the beta feature behind a feature flag",
    "- Wire beta_router into the request pipeline",
    // Names NO project token: would be dropped by the token-overlap rule
    // alone, but the mixture gate keeps it (single-project checkpoint).
    "- Switched from REST polling to websocket push",
    "",
    "## File Changes",
    `- ${PROJ_B}/src/beta_router.py`,
    `- ${PROJ_B}/src/beta_core.py`,
    "",
    "## Recent Messages",
    "### User",
    "work on the beta feature",
    "",
  ].join("\n");
  const block = buildResumeLeanBlock(entry, content, 3500, "continue the beta work", PROJ_B);

  expect(block).toContain("beta feature");
  expect(block).toContain("beta_router");
  // The non-basename decision is kept (single-project -> no filtering):
  expect(block).toContain("Switched from REST polling to websocket push");
  expect(block).not.toContain("- Omitted");
});

test("buildResumeLeanBlock no filter when cwd absent (backward compat)", () => {
  const entry = makeEntry();
  const content = mixedAbCheckpointMd();
  // Legacy call: no promptText/cwd -> unfiltered, A-only items survive, no
  // disclosure line.
  const block = buildResumeLeanBlock(entry, content);

  expect(block).toContain("gamma delta epsilon");
  expect(block).not.toContain("- Omitted");
});

test("buildResumeLeanBlock no filter when promptText present but cwd absent (AND gate)", () => {
  const entry = makeEntry();
  const content = mixedAbCheckpointMd();
  // AND gate: promptText alone (no cwd) -> no filtering, no fabricated
  // disclosure. The OR gate would have filtered on prompt tokens alone.
  const block = buildResumeLeanBlock(entry, content, 3500, "beta feature");

  expect(block).toContain("gamma delta epsilon");
  expect(block).not.toContain("- Omitted");
});

// ---------------------------------------------------------------------------
// buildContinuityHint — filtered rebuild replaces raw 800-char excerpt
// ---------------------------------------------------------------------------

test("buildContinuityHint drops A-only decisions and emits disclosure", () => {
  const entry = makeEntry();
  const content = mixedAbCheckpointMd();
  const candidate: ContinuityCandidate = {
    entry,
    score: 0.9,
    content,
  };
  const hint = buildContinuityHint(candidate, "continue the beta work", PROJ_B);

  // B-overlapping decision kept in the filtered rebuild:
  expect(hint).toContain("beta feature behind a feature flag");
  // A-only DECISION dropped:
  expect(hint).not.toContain("gamma delta epsilon");
  // A-only FILE path dropped (cross-project absolute path not under cwd):
  expect(hint).not.toContain("gamma_engine");
  // Exactly one disclosure line. Both the A-only decision AND the A-only
  // file path are dropped, so the disclosure reports both categories:
  const disclosureCount = (hint.match(/- Omitted \(scoped to current project\):/g) || []).length;
  expect(disclosureCount).toBe(1);
  expect(hint).toContain("- Omitted (scoped to current project): 1 decision(s), 1 file(s)");
});

test("buildContinuityHint single-project checkpoint emits NO disclosure", () => {
  const entry = makeEntry();
  const content = [
    "> Quality: A (95/100)",
    "",
    "## Key Decisions",
    "- Ship the beta feature behind a feature flag",
    // Names NO project token: would be dropped by the token-overlap rule
    // alone, but the mixture gate keeps it (single-project checkpoint).
    "- Switched from REST polling to websocket push",
    "",
    "## File Changes",
    `- ${PROJ_B}/src/beta_router.py`,
    "",
    "## Recent Messages",
    "### User",
    "work on the beta feature",
    "",
  ].join("\n");
  const candidate: ContinuityCandidate = {
    entry,
    score: 0.9,
    content,
  };
  // Non-resume prompt so the lightweight hint path runs (not the lean block).
  const hint = buildContinuityHint(candidate, "beta feature", PROJ_B);

  expect(hint).toContain("beta feature");
  // The non-basename decision is kept (single-project -> no filtering):
  expect(hint).toContain("Switched from REST polling to websocket push");
  expect(hint).not.toContain("- Omitted");
});

test("buildContinuityHint no filter when promptText present but cwd absent (AND gate)", () => {
  const entry = makeEntry();
  const content = mixedAbCheckpointMd();
  const candidate: ContinuityCandidate = {
    entry,
    score: 0.9,
    content,
  };
  // AND gate: promptText alone (no cwd) -> no filtering, no fabricated
  // disclosure. The OR gate would have filtered on prompt tokens alone.
  const hint = buildContinuityHint(candidate, "beta feature");

  expect(hint).toContain("gamma delta epsilon");
  expect(hint).not.toContain("- Omitted");
});

// ---------------------------------------------------------------------------
// The disclosure line must survive the 800-char body slice
// ---------------------------------------------------------------------------

test("buildContinuityHint disclosure survives when kept body exceeds the 800-char slice", () => {
  const entry = makeEntry();
  // Each kept item is capped by safeRecoveredScalar (120 for decisions, 140
  // for files), so to exceed the 800-char body slice we fill ALL four kept
  // decision slots and ALL six kept file slots with max-length items, plus
  // one cross-project FILE that is dropped (so a disclosure is emitted).
  // 4*~130 + 6*~150 ~= 1420 chars of kept body -> _safeSlice(., 800) truncates
  // it. Before C4 the disclosure was pushed into the body and the whole
  // joined body was sliced, so the disclosure (appended last) was cut off.
  // After C4 the item body is sliced first and the disclosure is appended
  // outside the truncated region.
  const longDecision = (i: number) => `beta ${i} ` + "z".repeat(110);
  const longInProjectFile = (i: number) => `${PROJ_B}/src/${"p".repeat(110)}${i}.py`;
  const content = [
    "> Quality: B (82/100)",
    "",
    "## Key Decisions",
    `- ${longDecision(1)}`,
    `- ${longDecision(2)}`,
    `- ${longDecision(3)}`,
    `- ${longDecision(4)}`,
    "",
    "## File Changes",
    `- ${longInProjectFile(1)}`,
    `- ${longInProjectFile(2)}`,
    `- ${longInProjectFile(3)}`,
    `- ${longInProjectFile(4)}`,
    `- ${longInProjectFile(5)}`,
    `- ${longInProjectFile(6)}`,
    `- ${PROJ_A}/src/gamma_engine.py`,
    "",
    "## Recent Messages",
    "### User",
    "work on the beta feature",
    "",
  ].join("\n");
  const candidate: ContinuityCandidate = { entry, score: 0.9, content };
  const hint = buildContinuityHint(candidate, "continue the beta work", PROJ_B);

  // The kept item body is truncated by the 800-char slice:
  expect(hint).toContain("[... truncated]");
  // ...but the disclosure STILL renders (it sits outside the truncated region):
  expect(hint).toContain("- Omitted (scoped to current project): 1 file(s)");
  // And it renders AFTER the truncation marker, inside the fence:
  const truncIdx = hint.indexOf("[... truncated]");
  const discIdx = hint.indexOf("- Omitted (scoped to current project): 1 file(s)");
  expect(discIdx).toBeGreaterThan(truncIdx);
});

// ---------------------------------------------------------------------------
// NeutralizeRecoveredBody must strip CR (\x0d). The old regex kept CR,
// so Windows \r\n line endings survived and CR could be used for terminal
// injection. The fix adds \x0d to the strip class.
// ---------------------------------------------------------------------------

test("neutralizeRecoveredBody strips CR (\\x0d)", () => {
  const text = "line1\r\nline2\rXoverwritten";
  const result = neutralizeRecoveredBody(text);
  expect(result).not.toContain("\r");
  // LF is preserved (body structure):
  expect(result).toContain("\n");
});

test("neutralizeRecoveredBody strips all C0 controls except tab and LF", () => {
  for (let code = 0x00; code < 0x20; code++) {
    if (code === 0x09 || code === 0x0a) continue; // tab, LF
    const ch = String.fromCharCode(code);
    const result = neutralizeRecoveredBody(`before${ch}after`);
    expect(result).not.toContain(ch);
  }
  // Tab and LF are preserved:
  expect(neutralizeRecoveredBody("a\tb")).toContain("\t");
  expect(neutralizeRecoveredBody("a\nb")).toContain("\n");
});

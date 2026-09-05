/**
 * Per-project scoping for the lean resume block.
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
 * by the Python and OpenClaw TS scoping tests, so all three runtimes' keep/drop
 * decisions are asserted against the SAME token inputs with no copy drift.
 */
import { test, expect } from "bun:test";
import { buildLeanResumeContext, crossProjectFileDrop, keepRecoveredItem, type CheckpointRow } from "./resume-lean.js";
import parityFixtureJson from "../../../tests/fixtures/keep_recovered_parity.json";
import dropFixtureJson from "../../../tests/fixtures/cross_project_file_drop_parity.json";

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
  // A huge item with zero overlap still drops; a tiny item with zero overlap
  // still keeps. No score is computed.
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
// buildLeanResumeContext — mixed A/B checkpoint queried from B
// ---------------------------------------------------------------------------

function makeCheckpointRow(overrides: Partial<CheckpointRow> = {}): CheckpointRow {
  return {
    session_id: "abcdefgh1234",
    trigger: "manual",
    dbPath: "/tmp/test.db",
    created_at: Math.floor(Date.now() / 1000),
    active_files: JSON.stringify([]),
    decisions: JSON.stringify([]),
    content: "## Topic Summary\nwork on the beta feature\n",
    mode: "code",
    quality_score: 85,
    fill_pct: 70,
    ...overrides,
  };
}

const PROJ_B = "/home/u/beta";
const PROJ_A = "/home/u/gamma";

function mixedAbCheckpoint(): CheckpointRow {
  return makeCheckpointRow({
    active_files: JSON.stringify([
      `${PROJ_B}/src/beta_router.py`,
      `${PROJ_A}/src/gamma_engine.py`,
    ]),
    decisions: JSON.stringify([
      // B-overlapping: names beta (cwd basename) -> keep
      "Ship the beta feature behind a feature flag",
      // A-only: names only gamma/alpha (project A) -> drop
      "Refactor the gamma delta epsilon module for project alpha",
      // B-overlapping: names beta_router (B file stem) -> keep
      "Wire beta_router into the request pipeline",
    ]),
    content: "## Topic Summary\nwork on the beta feature\n",
  });
}

test("buildLeanResumeContext drops A-only decisions and emits one disclosure", () => {
  const cp = mixedAbCheckpoint();
  const block = buildLeanResumeContext(cp, "abcdefgh1234", 3500, "continue the beta work", PROJ_B);

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

test("buildLeanResumeContext single-project checkpoint emits NO disclosure", () => {
  const cp = makeCheckpointRow({
    active_files: JSON.stringify([
      `${PROJ_B}/src/beta_router.py`,
      `${PROJ_B}/src/beta_core.py`,
    ]),
    decisions: JSON.stringify([
      "Ship the beta feature behind a feature flag",
      "Wire beta_router into the request pipeline",
      // Names NO project token: would be dropped by the token-overlap rule
      // alone, but the mixture gate keeps it (single-project checkpoint).
      "Switched from REST polling to websocket push",
    ]),
  });
  const block = buildLeanResumeContext(cp, "abcdefgh1234", 3500, "continue the beta work", PROJ_B);

  expect(block).toContain("beta feature");
  expect(block).toContain("beta_router");
  // The non-basename decision is kept (single-project -> no filtering):
  expect(block).toContain("Switched from REST polling to websocket push");
  expect(block).not.toContain("- Omitted");
});

test("buildLeanResumeContext no filter when cwd absent (backward compat)", () => {
  const cp = mixedAbCheckpoint();
  // Legacy call: no promptText/cwd -> unfiltered, A-only items survive, no
  // disclosure line.
  const block = buildLeanResumeContext(cp, "abcdefgh1234");

  expect(block).toContain("gamma delta epsilon");
  expect(block).not.toContain("- Omitted");
});

test("buildLeanResumeContext no filter when promptText present but cwd absent (AND gate)", () => {
  const cp = mixedAbCheckpoint();
  // AND gate: promptText alone (no cwd) -> no filtering, no fabricated
  // disclosure. The OR gate would have filtered on prompt tokens alone.
  const block = buildLeanResumeContext(cp, "abcdefgh1234", 3500, "beta feature");

  expect(block).toContain("gamma delta epsilon");
  expect(block).not.toContain("- Omitted");
});

// ---------------------------------------------------------------------------
// .filter(Boolean) on decisions/files prevents empty-string slots in the
// rendered body. safeScalar returns "" for null/undefined or whitespace-only
// entries; without .filter(Boolean) the join produces "dec1; ; dec3".
// ---------------------------------------------------------------------------

test("buildLeanResumeContext filters empty decisions and files from the rendered body", () => {
  const cp = makeCheckpointRow({
    active_files: JSON.stringify([
      `${PROJ_B}/src/beta_router.py`,
      "",            // empty string -> safeScalar returns ""
      "   ",         // whitespace-only -> safeScalar returns ""
      `${PROJ_B}/src/beta_core.py`,
    ]),
    decisions: JSON.stringify([
      "Ship the beta feature behind a feature flag",
      "",            // empty string
      "   \t  ",     // whitespace-only
      null as unknown as string,  // null -> safeScalar returns ""
      "Wire beta_router into the request pipeline",
    ]),
  });
  const block = buildLeanResumeContext(cp, "abcdefgh1234", 3500, "continue the beta work", PROJ_B);

  // The two real decisions are present:
  expect(block).toContain("beta feature behind a feature flag");
  expect(block).toContain("beta_router");
  // No empty slots in the decisions join (JSON.stringify("") is "", so an
  // empty slot produces '"; ""' or '""; "' in the joined output):
  expect(block).not.toContain('""');
  // The two real files are present:
  expect(block).toContain("beta_router.py");
  expect(block).toContain("beta_core.py");
});

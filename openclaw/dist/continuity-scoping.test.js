"use strict";
var __importDefault = (this && this.__importDefault) || function (mod) {
    return (mod && mod.__esModule) ? mod : { "default": mod };
};
Object.defineProperty(exports, "__esModule", { value: true });
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
const bun_test_1 = require("bun:test");
const continuity_js_1 = require("./continuity.js");
const keep_recovered_parity_json_1 = __importDefault(require("../../tests/fixtures/keep_recovered_parity.json"));
const cross_project_file_drop_parity_json_1 = __importDefault(require("../../tests/fixtures/cross_project_file_drop_parity.json"));
// ---------------------------------------------------------------------------
// Shared parity fixture — single source of truth.
// Loaded from tests/fixtures/keep_recovered_parity.json, consumed by all 3
// suites (Python, OpenClaw TS, OpenCode TS). (item_text, keep_tokens, expected_keep)
// ---------------------------------------------------------------------------
const PARITY_FIXTURE = keep_recovered_parity_json_1.default
    .map((row) => [row.item_text, new Set(row.keep_tokens), row.expected_keep]);
// ---------------------------------------------------------------------------
// Parity: the decision function on a shared token fixture
// ---------------------------------------------------------------------------
(0, bun_test_1.test)("keepRecoveredItem matches the shared parity fixture exactly", () => {
    for (const [itemText, keepTokens, expected] of PARITY_FIXTURE) {
        const got = (0, continuity_js_1.keepRecoveredItem)(itemText, keepTokens);
        (0, bun_test_1.expect)(got).toBe(expected);
    }
});
(0, bun_test_1.test)("keepRecoveredItem is purely set-overlap, no float threshold", () => {
    (0, bun_test_1.expect)((0, continuity_js_1.keepRecoveredItem)("alpha beta gamma delta epsilon zeta", new Set())).toBe(false);
    (0, bun_test_1.expect)((0, continuity_js_1.keepRecoveredItem)("alpha beta", new Set())).toBe(true);
});
// ---------------------------------------------------------------------------
// crossProjectFileDrop parity — path normalization (GAUNTLET C2)
// Loaded from tests/fixtures/cross_project_file_drop_parity.json, consumed by
// all 3 suites. Covers backslash, UNC, trailing separator, mixed separators,
// case mismatch (casefold on Darwin/Win32), relative, and cwd-absent.
// ---------------------------------------------------------------------------
const _CASEFOLD = process.platform === "win32" || process.platform === "darwin";
(0, bun_test_1.test)("crossProjectFileDrop matches the shared path-normalization fixture exactly", () => {
    for (const row of cross_project_file_drop_parity_json_1.default) {
        const expected = _CASEFOLD && row.expected_drop_casefold !== undefined
            ? row.expected_drop_casefold
            : row.expected_drop;
        const got = (0, continuity_js_1.crossProjectFileDrop)(row.path, row.cwd);
        (0, bun_test_1.expect)(got).toBe(expected);
    }
});
// ---------------------------------------------------------------------------
// buildResumeLeanBlock — mixed A/B checkpoint queried from B
// ---------------------------------------------------------------------------
const PROJ_B = "/home/u/beta";
const PROJ_A = "/home/u/gamma";
function makeEntry() {
    return {
        path: "/tmp/checkpoints/test-session.md",
        sessionDirName: "testsession",
        trigger: "auto",
        createdAt: Date.now(),
    };
}
function mixedAbCheckpointMd() {
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
(0, bun_test_1.test)("buildResumeLeanBlock drops A-only decisions and emits one disclosure", () => {
    const entry = makeEntry();
    const content = mixedAbCheckpointMd();
    const block = (0, continuity_js_1.buildResumeLeanBlock)(entry, content, 3500, "continue the beta work", PROJ_B);
    // B-overlapping decisions kept:
    (0, bun_test_1.expect)(block).toContain("beta feature behind a feature flag");
    (0, bun_test_1.expect)(block).toContain("beta_router");
    // A-only DECISION dropped:
    (0, bun_test_1.expect)(block).not.toContain("gamma delta epsilon");
    // A-only FILE path dropped (cross-project absolute path not under cwd):
    (0, bun_test_1.expect)(block).not.toContain("gamma_engine");
    // Exactly one disclosure line. Both the A-only decision AND the A-only
    // file path are dropped, so the disclosure reports both categories:
    const disclosureCount = (block.match(/- Omitted \(scoped to current project\):/g) || []).length;
    (0, bun_test_1.expect)(disclosureCount).toBe(1);
    (0, bun_test_1.expect)(block).toContain("- Omitted (scoped to current project): 1 decision(s), 1 file(s)");
});
(0, bun_test_1.test)("buildResumeLeanBlock single-project checkpoint emits NO disclosure", () => {
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
    const block = (0, continuity_js_1.buildResumeLeanBlock)(entry, content, 3500, "continue the beta work", PROJ_B);
    (0, bun_test_1.expect)(block).toContain("beta feature");
    (0, bun_test_1.expect)(block).toContain("beta_router");
    // The non-basename decision is kept (single-project -> no filtering):
    (0, bun_test_1.expect)(block).toContain("Switched from REST polling to websocket push");
    (0, bun_test_1.expect)(block).not.toContain("- Omitted");
});
(0, bun_test_1.test)("buildResumeLeanBlock no filter when cwd absent (backward compat)", () => {
    const entry = makeEntry();
    const content = mixedAbCheckpointMd();
    // Legacy call: no promptText/cwd -> unfiltered, A-only items survive, no
    // disclosure line.
    const block = (0, continuity_js_1.buildResumeLeanBlock)(entry, content);
    (0, bun_test_1.expect)(block).toContain("gamma delta epsilon");
    (0, bun_test_1.expect)(block).not.toContain("- Omitted");
});
(0, bun_test_1.test)("buildResumeLeanBlock no filter when promptText present but cwd absent (AND gate)", () => {
    const entry = makeEntry();
    const content = mixedAbCheckpointMd();
    // AND gate: promptText alone (no cwd) -> no filtering, no fabricated
    // disclosure. The OR gate would have filtered on prompt tokens alone.
    const block = (0, continuity_js_1.buildResumeLeanBlock)(entry, content, 3500, "beta feature");
    (0, bun_test_1.expect)(block).toContain("gamma delta epsilon");
    (0, bun_test_1.expect)(block).not.toContain("- Omitted");
});
// ---------------------------------------------------------------------------
// buildContinuityHint — filtered rebuild replaces raw 800-char excerpt
// ---------------------------------------------------------------------------
(0, bun_test_1.test)("buildContinuityHint drops A-only decisions and emits disclosure", () => {
    const entry = makeEntry();
    const content = mixedAbCheckpointMd();
    const candidate = {
        entry,
        score: 0.9,
        content,
    };
    const hint = (0, continuity_js_1.buildContinuityHint)(candidate, "continue the beta work", PROJ_B);
    // B-overlapping decision kept in the filtered rebuild:
    (0, bun_test_1.expect)(hint).toContain("beta feature behind a feature flag");
    // A-only DECISION dropped:
    (0, bun_test_1.expect)(hint).not.toContain("gamma delta epsilon");
    // A-only FILE path dropped (cross-project absolute path not under cwd):
    (0, bun_test_1.expect)(hint).not.toContain("gamma_engine");
    // Exactly one disclosure line. Both the A-only decision AND the A-only
    // file path are dropped, so the disclosure reports both categories:
    const disclosureCount = (hint.match(/- Omitted \(scoped to current project\):/g) || []).length;
    (0, bun_test_1.expect)(disclosureCount).toBe(1);
    (0, bun_test_1.expect)(hint).toContain("- Omitted (scoped to current project): 1 decision(s), 1 file(s)");
});
(0, bun_test_1.test)("buildContinuityHint single-project checkpoint emits NO disclosure", () => {
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
    const candidate = {
        entry,
        score: 0.9,
        content,
    };
    // Non-resume prompt so the lightweight hint path runs (not the lean block).
    const hint = (0, continuity_js_1.buildContinuityHint)(candidate, "beta feature", PROJ_B);
    (0, bun_test_1.expect)(hint).toContain("beta feature");
    // The non-basename decision is kept (single-project -> no filtering):
    (0, bun_test_1.expect)(hint).toContain("Switched from REST polling to websocket push");
    (0, bun_test_1.expect)(hint).not.toContain("- Omitted");
});
(0, bun_test_1.test)("buildContinuityHint no filter when promptText present but cwd absent (AND gate)", () => {
    const entry = makeEntry();
    const content = mixedAbCheckpointMd();
    const candidate = {
        entry,
        score: 0.9,
        content,
    };
    // AND gate: promptText alone (no cwd) -> no filtering, no fabricated
    // disclosure. The OR gate would have filtered on prompt tokens alone.
    const hint = (0, continuity_js_1.buildContinuityHint)(candidate, "beta feature");
    (0, bun_test_1.expect)(hint).toContain("gamma delta epsilon");
    (0, bun_test_1.expect)(hint).not.toContain("- Omitted");
});
// ---------------------------------------------------------------------------
// C4: the disclosure line must survive the 800-char body slice
// ---------------------------------------------------------------------------
(0, bun_test_1.test)("buildContinuityHint disclosure survives when kept body exceeds the 800-char slice", () => {
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
    const longDecision = (i) => `beta ${i} ` + "z".repeat(110);
    const longInProjectFile = (i) => `${PROJ_B}/src/${"p".repeat(110)}${i}.py`;
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
    const candidate = { entry, score: 0.9, content };
    const hint = (0, continuity_js_1.buildContinuityHint)(candidate, "continue the beta work", PROJ_B);
    // The kept item body is truncated by the 800-char slice:
    (0, bun_test_1.expect)(hint).toContain("[... truncated]");
    // ...but the disclosure STILL renders (it sits outside the truncated region):
    (0, bun_test_1.expect)(hint).toContain("- Omitted (scoped to current project): 1 file(s)");
    // And it renders AFTER the truncation marker, inside the fence:
    const truncIdx = hint.indexOf("[... truncated]");
    const discIdx = hint.indexOf("- Omitted (scoped to current project): 1 file(s)");
    (0, bun_test_1.expect)(discIdx).toBeGreaterThan(truncIdx);
});
// ---------------------------------------------------------------------------
// C10: neutralizeRecoveredBody must strip CR (\x0d). The old regex kept CR,
// so Windows \r\n line endings survived and CR could be used for terminal
// injection. The fix adds \x0d to the strip class.
// ---------------------------------------------------------------------------
(0, bun_test_1.test)("neutralizeRecoveredBody strips CR (\\x0d)", () => {
    const text = "line1\r\nline2\rXoverwritten";
    const result = (0, continuity_js_1.neutralizeRecoveredBody)(text);
    (0, bun_test_1.expect)(result).not.toContain("\r");
    // LF is preserved (body structure):
    (0, bun_test_1.expect)(result).toContain("\n");
});
(0, bun_test_1.test)("neutralizeRecoveredBody strips all C0 controls except tab and LF", () => {
    for (let code = 0x00; code < 0x20; code++) {
        if (code === 0x09 || code === 0x0a)
            continue; // tab, LF
        const ch = String.fromCharCode(code);
        const result = (0, continuity_js_1.neutralizeRecoveredBody)(`before${ch}after`);
        (0, bun_test_1.expect)(result).not.toContain(ch);
    }
    // Tab and LF are preserved:
    (0, bun_test_1.expect)((0, continuity_js_1.neutralizeRecoveredBody)("a\tb")).toContain("\t");
    (0, bun_test_1.expect)((0, continuity_js_1.neutralizeRecoveredBody)("a\nb")).toContain("\n");
});
//# sourceMappingURL=continuity-scoping.test.js.map
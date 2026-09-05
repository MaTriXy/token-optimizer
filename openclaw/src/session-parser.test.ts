/**
 * Regression test for the OpenClaw nested-role session format.
 *
 * OpenClaw writes conversation lines as { type: "message", message: { role } }
 * with the role NESTED, plus type:"session"/"compaction"/"reset" markers
 * (verified against openclaw/skills/session-logs/SKILL.md, whose own jq recipe
 * filters on `.message.role`). The parser previously read a top-level
 * type:"user"/"assistant" (the Claude-Code shape), so every OpenClaw line
 * missed the gate, messageCount stayed 0, parseSession returned null, and the
 * audit reported 0 sessions across an entire OpenClaw install.
 *
 * These tests are load-bearing: before normalizedType(), every "parses the
 * OpenClaw format" assertion below returned null / [] / 0. They also pin the
 * legacy Claude-Code top-level-type path so the fix stays backward compatible.
 */
import { test, expect, beforeEach, afterEach } from "bun:test";
import * as fs from "fs";
import * as path from "path";
import * as os from "os";

import {
  parseSession,
  parseSessionTurns,
  extractCostlyPrompts,
} from "./session-parser";

let dir: string;

beforeEach(() => {
  dir = fs.mkdtempSync(path.join(os.tmpdir(), "oc-parser-"));
});

afterEach(() => {
  fs.rmSync(dir, { recursive: true, force: true });
});

function writeSession(name: string, lines: unknown[]): string {
  const p = path.join(dir, name);
  fs.writeFileSync(p, lines.map((l) => JSON.stringify(l)).join("\n") + "\n");
  return p;
}

// Real OpenClaw transcript: a session header, a nested-role user turn, and a
// nested-role assistant turn carrying usage under message.usage.
const OPENCLAW_LINES: unknown[] = [
  { type: "session", id: "sess-1", cwd: "/tmp/proj", timestamp: "2026-08-01T10:00:00.000Z", version: "2026.6.6" },
  {
    type: "message",
    timestamp: "2026-08-01T10:00:01.000Z",
    id: "m1",
    parentId: "sess-1",
    message: { role: "user", content: [{ type: "text", text: "Refactor the auth module for me please" }] },
  },
  {
    type: "message",
    timestamp: "2026-08-01T10:00:05.000Z",
    id: "m2",
    parentId: "m1",
    message: {
      role: "assistant",
      model: "claude-opus-4-8",
      content: [{ type: "text", text: "Done. Here is the refactor with tests and a summary of the changes." }],
      usage: { inputTokens: 4200, outputTokens: 900, cacheReadInputTokens: 1500 },
    },
  },
];

test("parseSession reads the OpenClaw nested-role format (regression)", () => {
  const p = writeSession("sess-1.jsonl", OPENCLAW_LINES);
  const run = parseSession(p, "agent-a", dir);

  // Pre-fix this was null (messageCount stayed 0 -> early return).
  expect(run).not.toBeNull();
  expect(run!.system).toBe("openclaw");
  expect(run!.messageCount).toBe(2); // the session header must NOT count
  expect(run!.tokens.input).toBe(4200);
  expect(run!.tokens.output).toBe(900);
  expect(run!.tokens.cacheRead).toBe(1500);
});

test("non-message marker lines (session/compaction/reset) are not counted as turns", () => {
  const p = writeSession("sess-markers.jsonl", [
    { type: "session", id: "s", timestamp: "2026-08-01T10:00:00.000Z" },
    { type: "compaction", firstKeptEntryId: "m1", timestamp: "2026-08-01T10:00:02.000Z" },
    { type: "reset", firstKeptEntryId: "m1", timestamp: "2026-08-01T10:00:03.000Z" },
    ...OPENCLAW_LINES.slice(1), // the two real messages
  ]);
  const run = parseSession(p, "agent-a", dir);
  expect(run).not.toBeNull();
  expect(run!.messageCount).toBe(2);
});

test("parseSessionTurns pairs a user->assistant turn from the nested-role format", () => {
  const p = writeSession("sess-1.jsonl", OPENCLAW_LINES);
  const turns = parseSessionTurns(p, dir);
  // Pre-fix this was [] (no record matched the user/assistant gate).
  expect(turns.length).toBeGreaterThanOrEqual(1);
  const withOutput = turns.find((t) => t.outputTokens > 0);
  expect(withOutput).toBeDefined();
  expect(withOutput!.outputTokens).toBe(900);
});

test("extractCostlyPrompts surfaces the nested-role user prompt text", () => {
  const p = writeSession("sess-1.jsonl", OPENCLAW_LINES);
  const prompts = extractCostlyPrompts(p, 5, dir);
  // Pre-fix this was [] (the user gate never matched).
  expect(prompts.length).toBeGreaterThanOrEqual(1);
  expect(prompts[0].text).toContain("Refactor the auth module");
});

test("legacy Claude-Code top-level-type sessions still parse (backward compat)", () => {
  const legacy: unknown[] = [
    {
      type: "user",
      timestamp: "2026-08-01T10:00:01.000Z",
      message: { role: "user", content: [{ type: "text", text: "hello there" }] },
    },
    {
      type: "assistant",
      timestamp: "2026-08-01T10:00:05.000Z",
      message: {
        role: "assistant",
        model: "claude-opus-4-8",
        content: [{ type: "text", text: "Hi! How can I help with your project today?" }],
        usage: { inputTokens: 300, outputTokens: 120 },
      },
    },
  ];
  const p = writeSession("legacy.jsonl", legacy);
  const run = parseSession(p, "agent-a", dir);
  expect(run).not.toBeNull();
  expect(run!.messageCount).toBe(2);
  expect(run!.tokens.output).toBe(120);
});

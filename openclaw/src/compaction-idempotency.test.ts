/**
 * Mock-harness test: proves two successive after_compaction events on a
 * session where sessionKey === sessionId and the host omits previousSessionId,
 * compactedCount, AND messageCount produce DISTINCT idempotency keys and BOTH
 * enqueue (a high-severity ordering bug).
 *
 * Before the fix, every compaction yielded the identical key
 * `checkpoint-restore:SID:SID:0`, so the 2nd+ got enqueued:false and the
 * checkpoint restore was silently dropped.
 *
 * After the fix, the per-session monotonic compaction counter (`seq:N`)
 * makes each key unique while remaining stable for retries of the same
 * compaction.
 */
import { test, expect, beforeEach, afterEach } from "bun:test";
import * as fs from "fs";
import * as path from "path";
import * as os from "os";

import pluginEntry from "./index";

interface MockApi {
  handlers: Map<string, (...args: unknown[]) => unknown | Promise<unknown>>;
  enqueued: { sessionKey: string; text: string; idempotencyKey?: string }[];
  logger: {
    info: (msg: string, ...args: unknown[]) => void;
    warn: (msg: string, ...args: unknown[]) => void;
    error: (msg: string, ...args: unknown[]) => void;
    debug?: (msg: string, ...args: unknown[]) => void;
  };
  registerService: (svc: unknown) => void;
  on: (event: string, handler: (...args: unknown[]) => unknown | Promise<unknown>) => void;
  session: {
    workflow: {
      enqueueNextTurnInjection: (input: {
        sessionKey: string;
        text: string;
        idempotencyKey?: string;
        placement?: string;
      }) => Promise<{ enqueued: boolean }>;
    };
  };
}

function createMockApi(): MockApi {
  const handlers = new Map<string, (...args: unknown[]) => unknown | Promise<unknown>>();
  const enqueued: { sessionKey: string; text: string; idempotencyKey?: string }[] = [];
  return {
    handlers,
    enqueued,
    logger: {
      info: () => {},
      warn: () => {},
      error: () => {},
      debug: () => {},
    },
    registerService: () => {},
    on: (event, handler) => {
      handlers.set(event, handler);
    },
    session: {
      workflow: {
        enqueueNextTurnInjection: async (input) => {
          // Simulate the host's idempotency dedup: if we've seen this key
          // before, return enqueued:false.
          const seen = enqueued.some((e) => e.idempotencyKey === input.idempotencyKey);
          if (seen) {
            return { enqueued: false };
          }
          enqueued.push(input);
          return { enqueued: true };
        },
      },
    },
  };
}

const HOME = process.env.HOME ?? process.env.USERPROFILE ?? "";
const CHECKPOINT_ROOT = path.join(HOME, ".openclaw", "token-optimizer", "checkpoints");

let testSessionId: string;
let sessionCheckpointDir: string;

beforeEach(() => {
  testSessionId = `test-compact-${Date.now()}-${Math.random().toString(36).slice(2, 8)}`;
  sessionCheckpointDir = path.join(CHECKPOINT_ROOT, testSessionId);
  fs.mkdirSync(sessionCheckpointDir, { recursive: true, mode: 0o700 });

  // Write a checkpoint file + manifest entry so restoreCheckpoint finds it.
  const checkpointContent = "## Checkpoint\n- Task: fix the bug\n- Files: src/index.ts";
  const checkpointFile = path.join(sessionCheckpointDir, `progressive-50-${Date.now()}.md`);
  fs.writeFileSync(checkpointFile, checkpointContent, "utf-8");

  const manifestEntry = JSON.stringify({
    file: checkpointFile,
    trigger: "progressive-50",
    createdAt: new Date().toISOString(),
  });
  fs.writeFileSync(path.join(sessionCheckpointDir, "manifest.jsonl"), manifestEntry + "\n", "utf-8");
});

afterEach(() => {
  // Clean up the test session's checkpoint directory
  try {
    fs.rmSync(sessionCheckpointDir, { recursive: true, force: true });
  } catch {
    // best-effort
  }
});

test("two successive compactions with omitted host fields produce distinct keys and both enqueue", async () => {
  const sessionId = testSessionId;
  const sessionKey = sessionId; // sessionKey === sessionId (the common case)

  const mockApi = createMockApi();

  try {
    pluginEntry.register(mockApi as unknown as Parameters<typeof pluginEntry.register>[0]);
  } catch {
    // register may fail if findOpenClawDir returns null; the after_compaction
    // handler is still registered via safeOn before any openclawDir-dependent logic.
  }

  const afterCompactionHandler = mockApi.handlers.get("after_compaction");
  expect(afterCompactionHandler).toBeDefined();

  // Fire first after_compaction: NO previousSessionId, NO compactedCount,
  // NO messageCount (the bug scenario). sessionKey === sessionId.
  await afterCompactionHandler!(
    {}, // event: all host fields omitted
    { sessionId, sessionKey, trigger: "compaction" }
  );

  // Fire second after_compaction: same session, same omitted fields.
  await afterCompactionHandler!(
    {}, // event: all host fields omitted again
    { sessionId, sessionKey, trigger: "compaction" }
  );

  // Both should have enqueued (not been deduped)
  expect(mockApi.enqueued.length).toBe(2);

  // The idempotency keys must be distinct
  const key1 = mockApi.enqueued[0].idempotencyKey;
  const key2 = mockApi.enqueued[1].idempotencyKey;
  expect(key1).toBeDefined();
  expect(key2).toBeDefined();
  expect(key1).not.toEqual(key2);

  // Both keys should contain the sessionKey
  expect(key1).toContain(sessionKey);
  expect(key2).toContain(sessionKey);

  // The keys should use the seq: discriminator (since messageCount is absent)
  expect(key1).toContain("seq:");
  expect(key2).toContain("seq:");

  // The seq values should be different (1 and 2)
  expect(key1).toContain("seq:1");
  expect(key2).toContain("seq:2");
});

test("retry of the same compaction (same messageCount) produces the same key and does not re-enqueue", async () => {
  const sessionId = testSessionId;
  const sessionKey = sessionId;

  const mockApi = createMockApi();
  try {
    pluginEntry.register(mockApi as unknown as Parameters<typeof pluginEntry.register>[0]);
  } catch {
    // May fail if findOpenClawDir returns null; handlers are still registered.
  }

  const afterCompactionHandler = mockApi.handlers.get("after_compaction");
  expect(afterCompactionHandler).toBeDefined();

  // Fire first after_compaction WITH messageCount=42
  await afterCompactionHandler!(
    { messageCount: 42 },
    { sessionId, sessionKey, trigger: "compaction" }
  );

  // Fire retry: same messageCount=42 (same compaction, retried by host)
  await afterCompactionHandler!(
    { messageCount: 42 },
    { sessionId, sessionKey, trigger: "compaction" }
  );

  // Only the first should enqueue; the retry is deduped
  expect(mockApi.enqueued.length).toBe(1);

  // The key should use the msg: discriminator
  const key = mockApi.enqueued[0].idempotencyKey;
  expect(key).toBeDefined();
  expect(key).toContain("msg:42");
});

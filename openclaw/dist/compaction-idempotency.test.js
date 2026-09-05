"use strict";
var __createBinding = (this && this.__createBinding) || (Object.create ? (function(o, m, k, k2) {
    if (k2 === undefined) k2 = k;
    var desc = Object.getOwnPropertyDescriptor(m, k);
    if (!desc || ("get" in desc ? !m.__esModule : desc.writable || desc.configurable)) {
      desc = { enumerable: true, get: function() { return m[k]; } };
    }
    Object.defineProperty(o, k2, desc);
}) : (function(o, m, k, k2) {
    if (k2 === undefined) k2 = k;
    o[k2] = m[k];
}));
var __setModuleDefault = (this && this.__setModuleDefault) || (Object.create ? (function(o, v) {
    Object.defineProperty(o, "default", { enumerable: true, value: v });
}) : function(o, v) {
    o["default"] = v;
});
var __importStar = (this && this.__importStar) || (function () {
    var ownKeys = function(o) {
        ownKeys = Object.getOwnPropertyNames || function (o) {
            var ar = [];
            for (var k in o) if (Object.prototype.hasOwnProperty.call(o, k)) ar[ar.length] = k;
            return ar;
        };
        return ownKeys(o);
    };
    return function (mod) {
        if (mod && mod.__esModule) return mod;
        var result = {};
        if (mod != null) for (var k = ownKeys(mod), i = 0; i < k.length; i++) if (k[i] !== "default") __createBinding(result, mod, k[i]);
        __setModuleDefault(result, mod);
        return result;
    };
})();
var __importDefault = (this && this.__importDefault) || function (mod) {
    return (mod && mod.__esModule) ? mod : { "default": mod };
};
Object.defineProperty(exports, "__esModule", { value: true });
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
const bun_test_1 = require("bun:test");
const fs = __importStar(require("fs"));
const path = __importStar(require("path"));
const index_1 = __importDefault(require("./index"));
function createMockApi() {
    const handlers = new Map();
    const enqueued = [];
    return {
        handlers,
        enqueued,
        logger: {
            info: () => { },
            warn: () => { },
            error: () => { },
            debug: () => { },
        },
        registerService: () => { },
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
let testSessionId;
let sessionCheckpointDir;
(0, bun_test_1.beforeEach)(() => {
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
(0, bun_test_1.afterEach)(() => {
    // Clean up the test session's checkpoint directory
    try {
        fs.rmSync(sessionCheckpointDir, { recursive: true, force: true });
    }
    catch {
        // best-effort
    }
});
(0, bun_test_1.test)("two successive compactions with omitted host fields produce distinct keys and both enqueue", async () => {
    const sessionId = testSessionId;
    const sessionKey = sessionId; // sessionKey === sessionId (the common case)
    const mockApi = createMockApi();
    try {
        index_1.default.register(mockApi);
    }
    catch {
        // register may fail if findOpenClawDir returns null; the after_compaction
        // handler is still registered via safeOn before any openclawDir-dependent logic.
    }
    const afterCompactionHandler = mockApi.handlers.get("after_compaction");
    (0, bun_test_1.expect)(afterCompactionHandler).toBeDefined();
    // Fire first after_compaction: NO previousSessionId, NO compactedCount,
    // NO messageCount (the bug scenario). sessionKey === sessionId.
    await afterCompactionHandler({}, // event: all host fields omitted
    { sessionId, sessionKey, trigger: "compaction" });
    // Fire second after_compaction: same session, same omitted fields.
    await afterCompactionHandler({}, // event: all host fields omitted again
    { sessionId, sessionKey, trigger: "compaction" });
    // Both should have enqueued (not been deduped)
    (0, bun_test_1.expect)(mockApi.enqueued.length).toBe(2);
    // The idempotency keys must be distinct
    const key1 = mockApi.enqueued[0].idempotencyKey;
    const key2 = mockApi.enqueued[1].idempotencyKey;
    (0, bun_test_1.expect)(key1).toBeDefined();
    (0, bun_test_1.expect)(key2).toBeDefined();
    (0, bun_test_1.expect)(key1).not.toEqual(key2);
    // Both keys should contain the sessionKey
    (0, bun_test_1.expect)(key1).toContain(sessionKey);
    (0, bun_test_1.expect)(key2).toContain(sessionKey);
    // The keys should use the seq: discriminator (since messageCount is absent)
    (0, bun_test_1.expect)(key1).toContain("seq:");
    (0, bun_test_1.expect)(key2).toContain("seq:");
    // The seq values should be different (1 and 2)
    (0, bun_test_1.expect)(key1).toContain("seq:1");
    (0, bun_test_1.expect)(key2).toContain("seq:2");
});
(0, bun_test_1.test)("retry of the same compaction (same messageCount) produces the same key and does not re-enqueue", async () => {
    const sessionId = testSessionId;
    const sessionKey = sessionId;
    const mockApi = createMockApi();
    try {
        index_1.default.register(mockApi);
    }
    catch {
        // May fail if findOpenClawDir returns null; handlers are still registered.
    }
    const afterCompactionHandler = mockApi.handlers.get("after_compaction");
    (0, bun_test_1.expect)(afterCompactionHandler).toBeDefined();
    // Fire first after_compaction WITH messageCount=42
    await afterCompactionHandler({ messageCount: 42 }, { sessionId, sessionKey, trigger: "compaction" });
    // Fire retry: same messageCount=42 (same compaction, retried by host)
    await afterCompactionHandler({ messageCount: 42 }, { sessionId, sessionKey, trigger: "compaction" });
    // Only the first should enqueue; the retry is deduped
    (0, bun_test_1.expect)(mockApi.enqueued.length).toBe(1);
    // The key should use the msg: discriminator
    const key = mockApi.enqueued[0].idempotencyKey;
    (0, bun_test_1.expect)(key).toBeDefined();
    (0, bun_test_1.expect)(key).toContain("msg:42");
});
//# sourceMappingURL=compaction-idempotency.test.js.map
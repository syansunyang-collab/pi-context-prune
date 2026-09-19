/**
 * Wiring checks against the built bundle (`dist/index.js`) with a fake Pi runtime that keeps the
 * session file and the running agent's message list apart, as Pi does: messages the agent
 * produces go to both, `sessionManager.appendCustomMessageEntry` writes the session only,
 * `pi.sendMessage` writes both, and the `context` hook receives the agent's list. Pi rebuilds
 * that list from the session only on reload. `npm test` builds the bundle first (`pretest`).
 */
import test from "node:test";
import assert from "node:assert/strict";
import { mkdtempSync, mkdirSync, writeFileSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";

// Mirrored from src/types.ts, which cannot be imported here: it pulls in value imports written
// with `.js` specifiers that node's type stripping does not resolve to `.ts`.
const CUSTOM_TYPE_SUMMARY = "context-prune-summary";
const CUSTOM_TYPE_INDEX = "context-prune-index";

// config.ts resolves its settings path from the home directory at module load, so redirect the
// home directory before importing the bundle. Nothing under the real ~/.pi is read or written.
const fakeHome = mkdtempSync(join(tmpdir(), "pcp-live-"));
const settingsDir = join(fakeHome, ".pi", "agent", "context-prune");
mkdirSync(settingsDir, { recursive: true });
const writeSettings = (pruneOn: string) =>
  writeFileSync(
    join(settingsDir, "settings.json"),
    JSON.stringify({
      enabled: true,
      pruneOn,
      batchingMode: "agent-message",
      showStartupNotice: false,
      showPruneStatusLine: true,
    }),
  );
writeSettings("agent-message");
process.env.HOME = fakeHome;
process.env.USERPROFILE = fakeHome;

// @ts-expect-error the bundle ships no declaration file; its shape is asserted below.
const extension = (await import("../dist/index.js")).default as (pi: any) => void;

// ── fake Pi runtime ────────────────────────────────────────────────────────

const usage = {
  input: 10,
  output: 5,
  cacheRead: 0,
  cacheWrite: 0,
  totalTokens: 15,
  cost: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0, total: 0 },
};

function makeHarness() {
  const branch: any[] = []; // session file
  const agent: any[] = []; // the running agent's message list
  const handlers = new Map<string, (event: any, ctx: any) => Promise<any>>();
  const notifications: string[] = [];
  let clock = Date.parse("2026-09-19T00:00:00Z");
  const tick = () => (clock += 1000);

  // What Pi does at message_end for a message the agent produced.
  const record = (message: any) => {
    message.timestamp = tick();
    agent.push(message);
    branch.push({ type: "message", message, timestamp: new Date(message.timestamp).toISOString() });
  };

  const stream = () => {
    const partial = { content: [{ type: "text", text: "- did a thing" }] };
    return {
      async *[Symbol.asyncIterator]() {
        yield { type: "text_end", partial };
      },
      async result() {
        return { content: partial.content, usage, stopReason: "stop" };
      },
    };
  };

  const pi = {
    on(name: string, handler: any) {
      handlers.set(name, handler);
    },
    registerCommand() {},
    registerMessageRenderer() {},
    registerTool() {},
    getActiveTools: () => [] as string[],
    setActiveTools() {},
    appendEntry(customType: string, data: unknown) {
      branch.push({ type: "custom", customType, data });
      return "id";
    },
    // Pi's own custom-message path writes the agent's list and the session.
    sendMessage(message: any) {
      const timestamp = tick();
      agent.push({ role: "custom", ...message, timestamp });
      branch.push({ type: "custom_message", ...message, timestamp: new Date(timestamp).toISOString() });
    },
  };

  const ctx = {
    signal: new AbortController().signal,
    model: { provider: "fake", id: "model" },
    modelRegistry: {
      find: () => ctx.model,
      getAvailable: () => [],
      async getApiKeyAndHeaders() {
        return { ok: true, apiKey: "k", headers: {}, env: {} };
      },
      getProvider: () => ({ stream }),
    },
    sessionManager: {
      getBranch: () => branch,
      getSessionId: () => "session",
      appendCustomEntry(customType: string, data: unknown) {
        branch.push({ type: "custom", customType, data });
        return "id";
      },
      // The session only: the running agent's list is not touched.
      appendCustomMessageEntry(customType: string, content: string, display: boolean, details: unknown) {
        branch.push({ type: "custom_message", customType, content, display, details, timestamp: new Date(tick()).toISOString() });
        return "id";
      },
    },
    ui: {
      notify: (message: string) => notifications.push(message),
      setStatus() {},
      setWidget() {},
    },
  };

  extension(pi as any);

  let runTurnIndex = 0;
  return {
    branch,
    agent,
    notifications,
    async sessionStart() {
      await handlers.get("session_start")!({}, ctx);
    },
    /** A new user prompt: Pi restarts `event.turnIndex` at 0 for every agent run. */
    prompt(text: string) {
      record({ role: "user", content: [{ type: "text", text }] });
      runTurnIndex = 0;
    },
    /** One assistant turn with one tool call per id. */
    async turn(ids: string[]) {
      const message = {
        role: "assistant",
        content: ids.map((id) => ({ type: "toolCall", id, name: "read", arguments: { path: id } })),
        stopReason: "toolUse",
      };
      record(message);
      const toolResults = ids.map((id) => ({
        role: "toolResult",
        toolCallId: id,
        toolName: "read",
        content: [{ type: "text", text: `${id}:${"x".repeat(800)}` }],
        isError: false,
      }));
      for (const result of toolResults) record(result);
      await handlers.get("turn_end")!({ message, toolResults, turnIndex: runTurnIndex++ }, ctx);
    },
    /** A final text-only reply: the agent-message trigger. Pi persists it after the extension hooks ran. */
    async finalReply(text = "done") {
      const message: any = { role: "assistant", content: [{ type: "text", text }], stopReason: "stop", timestamp: tick() };
      agent.push(message);
      await handlers.get("message_end")!({ message }, ctx);
      branch.push({ type: "message", message, timestamp: new Date(message.timestamp).toISOString() });
    },
    async checkpoint() {
      await handlers.get("tool_execution_end")!({ toolName: "context_checkpoint" }, ctx);
    },
    /** One LLM request: the message list the model receives. Pi deep-clones the list first. */
    async request() {
      const messages = structuredClone(agent);
      const out = await handlers.get("context")!({ messages }, ctx);
      return (out?.messages ?? messages) as any[];
    },
    /** What Pi rebuilds from the session file on reload. */
    reload() {
      agent.length = 0;
      for (const entry of branch) {
        if (entry.type === "message") agent.push(entry.message);
        if (entry.type === "custom_message") {
          const { type: _type, timestamp, ...rest } = entry;
          agent.push({ role: "custom", ...rest, timestamp: Date.parse(timestamp) });
        }
      }
    },
    indexedIds: () =>
      branch
        .filter((e) => e.type === "custom" && e.customType === CUSTOM_TYPE_INDEX)
        .flatMap((e: any) => e.data.toolCalls.map((tc: any) => tc.toolCallId)),
  };
}

const settle = () => new Promise((r) => setTimeout(r, 10));
const summariesIn = (messages: any[]) =>
  messages.filter((m) => m.role === "custom" && m.customType === CUSTOM_TYPE_SUMMARY);
const isStub = (m: any) => m.role === "toolResult" && m.content[0].text.startsWith("[pi-context-prune]");

async function prunedFirstPrompt() {
  writeSettings("agent-message");
  const h = makeHarness();
  await h.sessionStart();
  h.prompt("read two files");
  await h.turn(["a0"]);
  await h.turn(["a1"]);
  await h.finalReply();
  await settle();
  return h;
}

// ── tests ──────────────────────────────────────────────────────────────────

test.after(() => rmSync(fakeHome, { recursive: true, force: true }));

test("a summary written to the session reaches the running agent's next request", async () => {
  const h = await prunedFirstPrompt();
  assert.equal(summariesIn(h.agent).length, 0, "precondition: the flush wrote the session only");

  h.prompt("next task");
  const messages = await h.request();
  assert.deepEqual(messages.filter(isStub).map((m) => m.toolCallId), ["a0", "a1"]);

  const summaries = summariesIn(messages);
  assert.equal(summaries.length, 1, "the model must receive the summary its stubs point at");
  const at = messages.indexOf(summaries[0]);
  assert.equal(messages[at - 1].toolCallId, "a1", "placed right after the last result it covers");
  assert.equal(messages[at + 1].role, "assistant");
});

test("the added summary stays at the same place on later requests", async () => {
  const h = await prunedFirstPrompt();
  h.prompt("next task");
  const first = await h.request();
  await h.turn(["b0"]);
  const second = await h.request();
  assert.deepEqual(second.slice(0, first.length), first, "earlier messages must not move between requests");
});

test("a summary already in the agent's list is not added again", async () => {
  const h = await prunedFirstPrompt();
  h.reload();
  await h.sessionStart();
  h.prompt("next task");
  assert.equal(summariesIn(await h.request()).length, 1);
});

test("a summary with array content already in the list is not added again", async () => {
  writeSettings("agent-message");
  const h = makeHarness();
  await h.sessionStart();
  h.prompt("task");
  await h.turn(["z0"]);
  // appendCustomMessageEntry also accepts TextContent[]; the list the hook sees is a deep clone.
  h.branch.push({
    type: "custom",
    customType: CUSTOM_TYPE_INDEX,
    data: { toolCalls: [{ toolCallId: "z0", toolName: "read", args: {}, resultText: "z0", isError: false, turnIndex: 0, timestamp: 0 }] },
  });
  h.branch.push({
    type: "custom_message",
    customType: CUSTOM_TYPE_SUMMARY,
    content: [{ type: "text", text: "saved summary" }],
    display: false,
    details: { toolCallIds: ["z0"] },
    timestamp: new Date(0).toISOString(),
  });
  h.reload();
  await h.sessionStart();
  assert.equal(summariesIn(await h.request()).length, 1);
});

test("a runtime-delivered summary is not duplicated", async () => {
  writeSettings("on-context-tag");
  const h = makeHarness();
  await h.sessionStart();
  h.prompt("task");
  await h.turn(["c0"]);
  await h.checkpoint();
  await settle();
  assert.deepEqual(h.indexedIds(), ["c0"]);
  assert.equal(summariesIn(await h.request()).length, 1);
});

test("the first turn of a later prompt is queued in agent-message mode", async () => {
  writeSettings("agent-message");
  const h = makeHarness();
  await h.sessionStart();
  h.prompt("first");
  await h.turn(["a0"]);
  await h.turn(["a1"]);
  await h.turn(["a2"]);
  await h.finalReply();
  await settle();
  h.notifications.length = 0;

  h.prompt("second");
  await h.turn(["b0"]);
  assert.ok(
    h.notifications.some((n) => n.startsWith("pruner: 1 turn queued")),
    `expected a queue notice, got ${JSON.stringify(h.notifications)}`,
  );
});

test("every-turn mode prunes the turns of a later prompt", async () => {
  writeSettings("every-turn");
  const h = makeHarness();
  await h.sessionStart();
  h.prompt("first");
  await h.turn(["a0"]);
  await h.turn(["a1"]);
  await h.turn(["a2"]);
  await settle();
  assert.deepEqual(h.indexedIds(), ["a0", "a1", "a2"]);

  h.prompt("second");
  await h.turn(["b0"]);
  await settle();
  assert.ok(h.indexedIds().includes("b0"), "b0 must be summarized at its own turn_end");
});

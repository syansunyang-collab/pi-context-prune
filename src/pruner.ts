import type { ToolCallIndexer } from "./indexer.js";
import { CUSTOM_TYPE_SUMMARY } from "./types.js";
import { normalizeSummaryToolCallRefs } from "./summary-refs.js";

export const PRUNED_RESULT_MARKER = "[pi-context-prune]";

/**
 * Maps the `context` event message array.
 * ToolResultMessage entries whose toolCallId is in the index are replaced by a short stub
 * that points at the pruner-summary message and at `context_tree_query`.
 * Keeps ALL other messages including AssistantMessages with tool-call blocks.
 *
 * The stub is kept (rather than dropping the message) because every provider needs a result
 * for each tool call: pi-ai's transformMessages fills a missing one with an *error* result
 * ("No result provided", isError: true), and providers that bypass that transform send the
 * tool call unpaired. Both make the model believe the tool failed; a benign stub does not.
 */
export function pruneMessages(messages: any[], indexer: ToolCallIndexer): any[] {
  let changed = false;
  const out = messages.map((msg) => {
    if (msg.role !== "toolResult" || !indexer.isSummarized(msg.toolCallId)) return msg;
    changed = true;
    return {
      ...msg,
      isError: false,
      content: [
        {
          type: "text",
          text:
            `${PRUNED_RESULT_MARKER} This ${msg.toolName ?? "tool"} result was summarized and pruned from context; ` +
            `see the pruner-summary message that follows. Full output: context_tree_query with id "${msg.toolCallId}".`,
        },
      ],
    };
  });
  return changed ? out : messages;
}

/**
 * Adds the summary messages that the running agent's message list lacks.
 *
 * agent-message and every-turn flushes persist their summary with
 * `sessionManager.appendCustomMessageEntry`, which writes the session file but not the agent's
 * in-memory message list; Pi reads the session back only on reload. Until then the model would
 * see stubs pointing at a summary it never receives.
 *
 * Each missing summary goes right after the tool results of the last call it covers, where a
 * steer message would have landed, so it sits at the same place on every request and the cached
 * prefix holds. Summaries already in the list (runtime delivery, or a list rebuilt on reload) are
 * left alone.
 *
 * @param branch  the session branch (`ctx.sessionManager.getBranch()`)
 */
export function injectSummaryMessages(messages: any[], branch: any[]): any[] {
  // Pi deep-clones the list before the `context` hook, so content is compared by value; an
  // array of content blocks (which appendCustomMessageEntry also accepts) never matches by identity.
  const contentKey = (content: unknown) => (typeof content === "string" ? content : JSON.stringify(content));
  const present = new Set(
    messages
      .filter((m) => m?.role === "custom" && m.customType === CUSTOM_TYPE_SUMMARY)
      .map((m) => contentKey(m.content)),
  );
  const inserts = new Map<number, any[]>();
  for (const entry of branch) {
    if (entry?.type !== "custom_message" || entry.customType !== CUSTOM_TYPE_SUMMARY) continue;
    if (present.has(contentKey(entry.content))) continue;

    const ids = new Set(normalizeSummaryToolCallRefs(entry.details).map((ref) => ref.toolCallId));
    let at = -1;
    messages.forEach((m, i) => {
      if (m?.role === "toolResult" && ids.has(m.toolCallId)) at = i;
    });
    if (at < 0) continue;
    while (messages[at + 1]?.role === "toolResult") at++;

    // Same shape as Pi's createCustomMessage when it rebuilds the list from the session.
    const list = inserts.get(at) ?? [];
    list.push({
      role: "custom",
      customType: entry.customType,
      content: entry.content,
      display: entry.display,
      details: entry.details,
      timestamp: new Date(entry.timestamp).getTime(),
    });
    inserts.set(at, list);
  }
  if (inserts.size === 0) return messages;

  const out: any[] = [];
  messages.forEach((m, i) => {
    out.push(m);
    out.push(...(inserts.get(i) ?? []));
  });
  return out;
}

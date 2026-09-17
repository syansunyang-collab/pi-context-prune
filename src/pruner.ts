import type { ToolCallIndexer } from "./indexer.js";

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
            `see the pruner-summary message above. Full output: context_tree_query with id "${msg.toolCallId}".`,
        },
      ],
    };
  });
  return changed ? out : messages;
}

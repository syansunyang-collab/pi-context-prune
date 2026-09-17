# Changelog

## 1.5.1 — 2026-09-17

### Fixed

- Stop now works while the pruner is summarizing. Pi awaits the `turn_end`, `tool_execution_end` and `message_end` hooks, so a summarizer call made from a hook kept the run active and `abort()` waited for it. Hook-triggered flushes are now bound to the run's abort signal (`ctx.signal`); an aborted flush keeps its batches and the next trigger summarizes them.
- Hook-triggered flushes give up after 180 s, so a stalled summarizer stream can no longer hold the session. The batches are kept and a warning is shown.
- `npm publish` works from Windows (`check-package` and `build` scripts no longer depend on `npm` being spawnable without a shell or on `rm`).

## 1.5.0 — 2026-09-17

First release of the fork (`@syansunyang/pi-context-prune`), based on upstream `pi-context-prune` 1.4.0 (championswimmer/pi-context-prune@626f270).

### Fixed

- Pruned tool results are replaced by a short stub instead of being deleted. Deleting them left the assistant's tool call unpaired: pi-ai's `transformMessages` then inserted a synthetic `No result provided` error result, and providers that bypass that transform sent the call with no result at all. Either way the model saw failed tool calls.
- Assistant turns that ended with `stopReason` `error` or `aborted` are no longer treated as the final reply of the turn, so a provider error followed by a retry no longer prunes the results the retry still needs.
- The prune index, stats and frontier are rebuilt from the session on demand inside the `context` hook. The first request after a reload is pruned even when another extension triggers a turn before this extension's `session_start` runs.
- The summarizer stream call sends the OpenCode session headers.

### Added

- `summarizerMaxCharsPerResult` config key and `/pruner max-chars <n>` command (default 2000, as before).

### Packaging

- `dist/` is tracked in git so `pi install git:…` works without a build step.
- `peerDependencies` removed; the Pi extension loader provides `@earendil-works/pi-*` and typebox.

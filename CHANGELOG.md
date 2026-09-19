# Changelog

## 1.5.3 - 2026-09-19

Both fixes concern behaviour inherited from upstream 1.4.0.

### Fixed

- Summaries now reach the model within the same session. `agent-message` and `every-turn` flushes store the summary with `sessionManager.appendCustomMessageEntry`, which writes the session file but not the running agent's message list; Pi reads the file back only on reload. Until then every later request carried stubs pointing at a summary the model never received. The `context` hook now adds any summary the list lacks, right after the tool results it covers (where a steer message would land), so it keeps the same position on every request. Summaries already in the list (runtime delivery, or a list rebuilt on reload) are not added again.
- Tool turns of later prompts are no longer dropped. `turn_end` numbered its batch with Pi's `event.turnIndex`, which restarts at 0 on every prompt, while the prune frontier numbers every assistant message on the branch. From the second prompt on, early turns looked already attempted: `every-turn` skipped their prune and `agent-message` stopped queueing them. `turn_end` now uses the branch-wide number.
- The stub now says the summary follows the pruned results instead of preceding them. The stub text changes once, so the first request after upgrading re-reads previously cached context.

### Added

- `npm test`: builds the bundle, then runs wiring tests against it with a fake Pi runtime that keeps the session file and the agent's message list apart and deep-clones the list before the `context` hook, as Pi does.

## 1.5.2 - 2026-09-17

Documentation and packaging only; no behaviour change.

### Changed

- `description` now matches the repository's: what the extension does, and that it is a fork of upstream 1.4.0.
- README: a Chinese translation ([README.zh-CN.md](README.zh-CN.md)) and a language switcher; the fork notice covers the 1.5.1 abort fix.
- Sections written by the upstream project are labelled as such: "Follow-up ideas" is upstream's roadmap, not this fork's plan, and PRUNING.md carries an authorship note.
- The limitation about summarizer latency now states the 1.5.1 abort binding and the 180 s deadline.

### Added

- [`bench/`](bench/README.md): method, per-run metrics for 29 runs at thinking medium plus three at high, the A/B harness and the isolation scanner. The measured quality cost is stated, not just the token saving.

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

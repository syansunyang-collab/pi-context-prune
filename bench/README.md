# Benchmark

What pruning actually costs and saves, measured on real agent sessions rather than on a synthetic context.

## Setup

One Pi session solves 17 [SWE-bench Verified](https://openai.com/index/introducing-swe-bench-verified/) Django tasks back to back, so context accumulates across tasks the way it does in a long working session. One such session is a *run*; each arm was repeated for six rounds.

| | |
|---|---|
| Model | `openai-codex/gpt-5.6-luna`, thinking medium (plus one round at high) |
| Tasks | 17 Django instances, identical list and order in every run |
| Work tree | a single-commit, history-free copy of the repo at the task's base commit, outside any other checkout |
| Network | blocked except the model provider |
| Scoring | the hidden test patch is applied after the agent finishes; fail-to-pass and pass-to-pass both have to hold |

The agent is given no hint that a benchmark is running. Every session file was scanned afterwards for tool calls that read outside its own work tree, touched another run's directory, or looked at the task metadata. Zero hits; the scanner is [`scan_escapes.py`](scan_escapes.py).

History-free work trees matter more than they sound. An earlier batch had to be thrown away entirely: with the full Django history present, the agent found the real upstream fix with `git log -S` in most tasks, and a 99/99 score measured nothing.

## Arms

| Arm | What ran |
|---|---|
| A | stock Pi, no pruning |
| B | pruner with the upstream default: summarizer sees 2000 chars per tool result |
| B8 | same, cap 8000 |
| B0 | same, no cap |
| C | [SoL-Pi](https://github.com/NVlabs/SoL-Pi) alone, all four mechanisms |
| D | pruner (cap 8000) plus SoL-Pi |

Token counts include everything the pruner itself spends: the summarizer's input and output are added to the session's context tokens. A pruner that saved context by burning the same tokens elsewhere would show no gain here.

## Results, thinking medium

| Arm | Rounds | Context tokens, median | vs A | Resolved /17, mean | Peak context, median | Native compactions |
|---|---|---|---|---|---|---|
| A | 6 | 26.0M | — | 12.7 | 244k | 6 |
| B | 6 | 9.6M | −63% | 11.3 | 81k | 0 |
| B8 | 6 | 9.7M | −63% | 11.7 | 65k | 0 |
| B0 | 1 | 10.6M | −59% | 12.0 | 80k | 0 |
| C | 5 | 23.6M | −9% | 12.2 | 145k | 5 |
| D | 5 | 12.1M | −54% | 11.8 | 77k | 2 |

The token saving held in every single round, never below 53% and never above 72%. The unpruned arm hit Pi's native compaction threshold in all six rounds; the pruned arms never did, which matters because native compaction rewrites history and cannot be undone, while pruning keeps every original output retrievable.

## The quality cost, stated plainly

Pruning is not free at this reasoning level. The pruned arms resolve about one task fewer per 17 with the 2000 cap, and roughly half that with 8000.

The loss is concentrated: three tasks account for nearly all of it, and the other 14 behave identically across arms. Reading all 30 sessions for those three:

- **It is not missing information.** In one of them, the pruned sessions had the decisive line in their own grep output just as often as the unpruned ones and still wrote the wrong fix. Nothing relevant from earlier tasks had been pruned away.
- **It looks behavioural.** On another, the pruned arms did noticeably less investigating before their first edit, roughly 4 to 9 tool calls against 14 to 24 for the unpruned arm, in four of six rounds. Shallow fixes fail the hidden test. This is a correlation on a small sample; it cannot be separated from the model's own run-to-run noise here.
- **It is not stale file contents.** `edit` failure rates are the same in every arm, 12 to 16%.

## Same tasks at thinking high

One round, arms A / B / B8: **14 / 14 / 13** resolved. All three tasks the pruned arms kept losing at medium were solved by every arm. Token reduction unchanged at −64% and −63%. The single miss in B8 was a runaway `edit` call, the model looping on whitespace until the harness timed out, not a wrong fix.

## What to take from this

With a model that sits comfortably above the task, the token saving is close to free. With a model that is marginal on the task, budget for a small quality hit, prefer the larger summarizer cap, and consider raising the reasoning effort before blaming the pruner; here that removed the gap entirely.

## Files

| File | Contents |
|---|---|
| [`metrics-medium.json`](metrics-medium.json) | per-run metrics for all 29 medium runs: context tokens, cache hit rate, summarizer spend, peak context, call count, compactions, resolved count |
| [`metrics-high.json`](metrics-high.json) | the same for the three thinking-high runs |
| [`live_ab.py`](live_ab.py) | the harness that produced them |
| [`scan_escapes.py`](scan_escapes.py) | the after-the-fact check for tool calls that left the work tree |

The harness is published as evidence, not as a turnkey tool: its paths are specific to the machine it ran on, and it needs a SWE-bench Verified task list and per-task test baselines that are not part of this repository. Raw session transcripts are not published; they are large and contain local paths.

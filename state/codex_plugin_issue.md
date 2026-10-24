## Summary

Long Codex sessions crash the Stop hook with a V8 heap OOM (`plugins/tracing`), because every traced generation rebuilds its full input from the entire session history — O(turns × history) memory — and all of those inputs are alive at once until the export flush. This is the plugin-side root cause of langfuse/langfuse#18995 (same reporter, V8 heap OOM + traces stop uploading after long sessions).

## Root cause (verified against `main`, commit 586910c)

1. `trace.ts:646-653` — `historyPrefixes` accumulates the **full** message list per turn:
   ```ts
   historyPrefixes.push([...seen]);
   seen.push(...turnHistoryMessages(turn));
   ```
2. `trace.ts:719/756` — each `emitTurn` receives `historyPrefix: historyPrefixes[turnIndex]` (everything before that turn).
3. `trace.ts:370-386` (`generationInput`) — spreads the full prefix into a fresh array for **every generation**, plus per-step copies inside the turn loop (`trace.ts:381-384`).

Net effect: O(turns × history) transient allocations, and (with the observation queue holding inputs until flush) O(generations × history) live retention in the hook process. On a long session the heap dies before the first export.

## Why I'm opening an issue instead of a PR

The trace input is *supposed* to carry the full context the model saw — any bound changes trace fidelity, so the right shape is a maintainer call, not a drive-by default change. Options as I see them:

1. **Config cap** — `maxHistoryMessages` (or chars) with a generous default; truncation noted in generation metadata so fidelity loss is visible.
2. **Incremental emit** — flush each generation's observation before building the next input, so only one full input is alive at a time (no fidelity loss; needs the emit path to not retain inputs).
3. **Shared subarray/copy-on-write** — keep one backing array and pass offset views; smallest allocation churn, but Langfuse payloads still serialize the full content per generation.

Happy to implement whichever direction you prefer (1 is smallest; 2 is the true fix if the queue is the retainer).

## Reproduction shape

A session with N turns where each turn adds ~M messages: generation k builds ~(k×M)-message inputs, N generations alive per flush → N²M/2 messages in memory. With `historyPrefix` reaching tens of MB on long agentic sessions, the default Node heap OOMs in the Stop hook — matching langfuse/langfuse#18995 exactly.

Found while investigating langfuse/langfuse#18995 for a potential fix; happy to reference this from there once triaged.

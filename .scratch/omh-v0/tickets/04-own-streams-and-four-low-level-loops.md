# 04 — Own streams and all four low-level loops

**What to build:** Give public callers both selected carriers for the same no-Tool Run semantics: EventStream-returning `agentLoop`/`agentLoopContinue` and awaited-sink `runAgentLoop`/`runAgentLoopContinue`. Every operation is lazy, owner-managed, single-settlement, cancellable only through its owner, and observable through one consumer plus independent result waiters.

**Blocked by:** 02 — Complete Message values and canonical bytes.

**Status:** ready-for-agent

- [ ] All four low-level entries are public, require an explicit Model and stream function, and share one prompt/continuation Run implementation rather than duplicating semantics.
- [ ] Calling a lazy coroutine or stream factory performs no admission, task scheduling, state change, event, or external effect before first awaited use.
- [ ] EventStream activates exactly once, has one consuming-iterator right, retains a lossless FIFO for that consumer, and permits multiple result observers without consuming events.
- [ ] A second consumer fails before receiving an event; a late sole consumer can drain the full retained sequence through the terminal event.
- [ ] Stream terminal event, `result()`, and the corresponding awaited-sink result carry the identical terminal value for equivalent Runs.
- [ ] Early close, context exit, consumer cancellation, and cancellation of an operation-owning waiter request cancellation, shield cleanup, and do not leave a detached producer.
- [ ] `AbortSignal` is factory-produced and read-only; waiter cancellation affects only that observer, and normal Run settlement leaves a retained signal un-aborted.
- [ ] Event-sink calls are awaited barriers, and sink failure settles owned work before raising the selected lifecycle carrier with no partial tuple result.
- [ ] Deterministic tests use explicit barriers for activation and cancellation races, and Matrix/corpus rows cover both carrier pairs after the accepted async normalization.

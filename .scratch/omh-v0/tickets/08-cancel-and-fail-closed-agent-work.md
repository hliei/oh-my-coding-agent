# 08 — Cancel and fail closed Agent work

**What to build:** Complete Agent-owned cancellation and lifecycle-failure settlement. Cancellation during Model or Tool work retains confirmed history, stops later effects, correlates every Tool Call, and returns the Agent to reusable idle only when all owned work is confirmed stopped; callback or cleanup failure instead follows its typed fail-closed carrier.

**Blocked by:** 07 — Make Agent stateful and reusable.

**Status:** ready-for-agent

- [ ] `Agent.abort()` requests cancellation without blocking, `waitForIdle()` observes only the captured Run, and operation-task cancellation settles the same Run before re-raising.
- [ ] Model cancellation finalizes the latest legal cumulative Assistant value as aborted and emits one ordinary terminal Run trace after cleanup.
- [ ] During Tool cancellation, completed Outcomes remain real, every remaining Call receives one source-ordered cancelled Tool Result, and no effect begins after the cutoff.
- [ ] A synthetic aborted Assistant tail is appended only when needed after Tool projection and never requires another Model effect.
- [ ] Listener snapshot members are all attempted after the first listener error; later events/effects stop and ordered callback causes precede cleanup causes.
- [ ] Event-sink failure emits no synthetic AgentEnd and yields no partial result tuple; terminal-listener failure changes only the awaiting carrier after committed terminal history.
- [ ] If cleanup errors occur but all work is confirmed stopped, the typed lifecycle failure is raised and idle runtime state is normalized without fabricating aborted history.
- [ ] If any work or resource remains unconfirmed, the Agent remains active/non-idle with the aborted signal and rejects new work, mutation, and reset until real settlement.
- [ ] Explicit barrier tests cover all cancellation cutoffs and failure races without wall-clock sleeps; Matrix/corpus cases distinguish cancellation, listener, sink, cleanup, and ordinary Model error.

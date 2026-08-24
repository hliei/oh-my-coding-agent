# 08 — Cancel and fail closed Agent work

**What to build:** Complete Agent-owned cancellation and lifecycle-failure settlement. Cancellation during Model or Tool work retains confirmed history, stops later effects, correlates every Tool Call, and returns the Agent to reusable idle only when all owned work is confirmed stopped; callback or cleanup failure instead follows its typed fail-closed carrier.

**Blocked by:** 07 — Make Agent stateful and reusable.

**Status:** resolved

- [x] `Agent.abort()` requests cancellation without blocking, `waitForIdle()` observes only the captured Run, and operation-task cancellation settles the same Run before re-raising.
- [x] Model cancellation finalizes the latest legal cumulative Assistant value as aborted and emits one ordinary terminal Run trace after cleanup.
- [x] During Tool cancellation, completed Outcomes remain real, every remaining Call receives one source-ordered cancelled Tool Result, and no effect begins after the cutoff.
- [x] A synthetic aborted Assistant tail is appended only when needed after Tool projection and never requires another Model effect.
- [x] Listener snapshot members are all attempted after the first listener error; later events/effects stop and ordered callback causes precede cleanup causes.
- [x] Event-sink failure emits no synthetic AgentEnd and yields no partial result tuple; terminal-listener failure changes only the awaiting carrier after committed terminal history.
- [x] If cleanup errors occur but all work is confirmed stopped, the typed lifecycle failure is raised and idle runtime state is normalized without fabricating aborted history.
- [x] If any work or resource remains unconfirmed, the Agent remains active/non-idle with the aborted signal and rejects new work, mutation, and reset until real settlement.
- [x] Explicit barrier tests cover all cancellation cutoffs and failure races without wall-clock sleeps; Matrix/corpus cases distinguish cancellation, listener, sink, cleanup, and ordinary Model error.

## Comments

- 2026-08-24 — Implemented Agent-owned Run cancellation, captured-Run idle observation, cancellation-resistant settlement, Model/Tool cleanup barriers, source-ordered cancelled Tool projection, synthetic aborted tails, and typed listener/event-sink/cleanup carriers. Tests use explicit `asyncio.Event` barriers and no wall-clock sleeps.
- 2026-08-24 — Added Ticket 08 obligation-matrix and reference-corpus cases plus an installed-wheel scenario covering clean cancellation and distinct listener, event-sink, cleanup, unconfirmed-cleanup, and ordinary Model-error carriers.
- 2026-08-24 — Fixed-point review from `27cd05ee41a3f7d18617dc0a36152d351bf17ccb` reached Standards PASS and Spec PASS after red-green regressions for terminal commit, Tool-start lifecycle typing/pairing, repeated abort, listener cutoff, and terminal-Model/EOF cancellation.
- 2026-08-24 — Verification: focused Tickets 04/06/07/08 `60 passed`; full suite `202 passed`; `uv run --locked mypy src tests`; `uv run --locked python -m compileall -q src tests`; `uv lock --check`; and `git diff --check` all passed.

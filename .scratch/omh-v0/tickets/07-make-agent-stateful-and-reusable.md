# 07 — Make Agent stateful and reusable

**What to build:** Expose the stateful `Agent` as the sole owner of a reusable sequence of Runs. Public callers can prompt, continue, subscribe, inspect state, wait for idle, mutate only idle configuration, reset history, observe an ordinary Model error, and start another Run without bypassing the shared low-level Core semantics.

**Blocked by:** 06 — Settle deterministic Tool batches.

**Status:** resolved

- [x] Agent construction requires valid owned initial state, Model, and stream function and publishes only the selected state and operation members.
- [x] `prompt()` handles text, one Message, and nonempty Message sequences; `continue_()` adds no seed and admits only a valid existing User/Tool Result tail.
- [x] Empty sequences, invalid effective tails, carrier misuse, and busy operations reject before Run identity, event, state mutation, timestamp, or effect.
- [x] Agent owns one live state identity; seed/configuration fields may be atomically replaced only while idle and lifecycle fields remain read-only.
- [x] State reduction precedes each listener, uses immutable snapshots/fresh containers, and leaves `isStreaming` true through terminal listener barriers before waking idle waiters.
- [x] Each subscription is an independent ordered record; snapshot dispatch, idempotent unsubscribe, duplicate callable registration, and listener mutation behavior match the accepted contract.
- [x] Idle reset clears Messages and error text while preserving system prompt, Model, and Tools; busy reset or assignment fails without mutation.
- [x] Ordinary Model/stream failure commits one error Assistant value, performs no automatic retry, returns normally, and permits a fresh caller-started Run.
- [x] Agent history is derived only through event reduction and ends with a value-equal Run suffix; installed tests prove low-level/Agent semantic identity and Matrix coverage.

## Comments

- TDD: construction and public members failed first; text/message/sequence prompt and `continue_()` tails next; empty/invalid/busy admission then mutated state or called the Provider; idle assignment/reset passed before busy guards; reduction-before-listener and `isStreaming` through `agent_end` required installing the Run before the loop and clearing it only after terminal listeners; duplicate subscribe records and ordinary Model-error reuse closed the loop.
- Abort publishes a member only: idle is a no-op and busy requests cancellation. Full abort settlement stays with ticket 08. `continue_` is the sole spelling; Python rejects `agent.continue()`.
- Two-axis review: Standards required an `omhExpectation` on `ABD:owned-agent-state-mutation` and flagged Feature Envy on `AgentState` idle checks as a judgement call. Spec asked for busy assignment on every seed field, Tool Result `continue_()`, idle `abort()`, and the `continue()` syntax probe; those were added. Private `ModelsError` wrapping remains the existing loop redaction (`Model stream failed`) rather than a new public type.
- Conformance: Matrix/corpus rows cover the Agent public surface (`PA:python-illegal-identifier`), strict admission, owned-state mutation, low-level/Agent identity, distinct listener registrations, and ordinary Model error through the installed wheel.
- Verification: `uv run --locked pytest -q` (179 passed), `uv run --locked mypy src tests`, `uv run --locked python -m compileall -q src tests`, `uv lock --check`, JSON parsing for both conformance authorities, and `git diff --check`.

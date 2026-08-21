# 07 — Make Agent stateful and reusable

**What to build:** Expose the stateful `Agent` as the sole owner of a reusable sequence of Runs. Public callers can prompt, continue, subscribe, inspect state, wait for idle, mutate only idle configuration, reset history, observe an ordinary Model error, and start another Run without bypassing the shared low-level Core semantics.

**Blocked by:** 06 — Settle deterministic Tool batches.

**Status:** ready-for-agent

- [ ] Agent construction requires valid owned initial state, Model, and stream function and publishes only the selected state and operation members.
- [ ] `prompt()` handles text, one Message, and nonempty Message sequences; `continue_()` adds no seed and admits only a valid existing User/Tool Result tail.
- [ ] Empty sequences, invalid effective tails, carrier misuse, and busy operations reject before Run identity, event, state mutation, timestamp, or effect.
- [ ] Agent owns one live state identity; seed/configuration fields may be atomically replaced only while idle and lifecycle fields remain read-only.
- [ ] State reduction precedes each listener, uses immutable snapshots/fresh containers, and leaves `isStreaming` true through terminal listener barriers before waking idle waiters.
- [ ] Each subscription is an independent ordered record; snapshot dispatch, idempotent unsubscribe, duplicate callable registration, and listener mutation behavior match the accepted contract.
- [ ] Idle reset clears Messages and error text while preserving system prompt, Model, and Tools; busy reset or assignment fails without mutation.
- [ ] Ordinary Model/stream failure commits one error Assistant value, performs no automatic retry, returns normally, and permits a fresh caller-started Run.
- [ ] Agent history is derived only through event reduction and ends with a value-equal Run suffix; installed tests prove low-level/Agent semantic identity and Matrix coverage.

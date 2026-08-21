# 13 — Prompt one durable no-Tool Product Session

**What to build:** Drive one deterministic no-Tool prompt through the public Product Session seam and make durability precede every visible fact. The caller observes the expanded User Message and final Assistant history, ordered Session events, `AgentSettled`, idle completion, disposal, and exact-id recovery of the complete settled conversation.

**Blocked by:** 12 — Create, dispose, and recover an empty Product Session.

**Status:** ready-for-agent

- [ ] `AgentSession.prompt()` is lazy, accepts text plus the closed PromptOptions carrier, and rejects carrier/busy/closing/disposed misuse before durable input or effect.
- [ ] Run admission durably records unsettled state and the exact timestamped User Message before Agent events, Model transport, or other effects.
- [ ] Every finalized Message becomes durable before Session state and its public event; visible `messages` is always the largest confirmed durable prefix.
- [ ] Session events project the selected Core lifecycle with immutable snapshots and add one `AgentSettled` at the accepted boundary.
- [ ] AgentEnd listeners settle while the Session remains streaming/non-idle; only the final settled marker commit publishes idle and wakes prompt/idle observers.
- [ ] The prompt returns `None` after ordinary completion and exposes complete linear Session history while AgentEnd carries only the identical Run suffix.
- [ ] Session subscriptions are independent ordered snapshot barriers with idempotent per-registration removal and state-before-listener observation.
- [ ] Successful disposal retains the settled Image, and exact-id recovery restores value-equal complete history with no streaming, pending, or error runtime state.
- [ ] A crash/incomplete fixture before the settled marker is not recoverable and never rolls back to or redispatches an older effect boundary.
- [ ] Installed tests use deterministic transport plus real storage/lease resources and add durable-order/recovery cases to Matrix/corpus evidence.

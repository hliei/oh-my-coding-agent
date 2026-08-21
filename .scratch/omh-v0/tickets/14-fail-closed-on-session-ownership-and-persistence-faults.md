# 14 — Fail closed on Session ownership and persistence faults

**What to build:** Complete Product Session failure ownership around the durable boundary. Concurrent ownership, recovery reads, initial/admission writes, later Message barriers, settled-marker writes, rollback, and cleanup each stop at their defined cutoff, preserve only confirmed history, and never guess that incomplete work is resumable or idle.

**Blocked by:** 13 — Prompt one durable no-Tool Product Session.

**Status:** ready-for-agent

- [ ] A second process cannot acquire an active Session id; contention, lease-substrate failure, and release failure use the selected busy/cleanup carriers without takeover or timeout.
- [ ] Recovery read failures differ from invalid/nonresumable Images, retain private causes, and never mutate the prior Image or create a replacement identity.
- [ ] Initial Image publication is atomic: pre-commit failure leaves no Image, while post-commit pre-delivery cancellation removes or marks only the new identity incomplete.
- [ ] An admitted-record write failure occurs before Run/state/event/effect and leaves the existing Session idle for caller retry.
- [ ] A later persistence barrier failure requests cancellation, forbids later effects, gives all observers one retained failure, and exposes only confirmed durable Messages.
- [ ] Persistence failure after admission permanently closes the Session to new prompts and permits disposal only to mark incomplete/abandoned state and release resources.
- [ ] Settled-marker failure never publishes idle or permits recovery, and no rollback, repair, migration, replay, or automatic storage retry is attempted.
- [ ] Cleanup attempts every independent owned action, preserves failure precedence/order, and retains ownership when unresolved work requires a later retry.
- [ ] Deterministic fault injection is private; assertions use public Session errors/state and real store/lease effects through the installed artifact.
- [ ] Matrix cases cover each failure phase, effect cutoff, retained durable prefix, later operation admission, and crash-recovery classification.

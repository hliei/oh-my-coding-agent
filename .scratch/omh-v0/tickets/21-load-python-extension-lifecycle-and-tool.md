# 21 — Load a Python Extension with lifecycle and Tool contribution

**What to build:** Admit one trusted project Python Extension as a fresh Session-private module generation. Its fixed entrypoint registers one Tool and lifecycle handlers before publication, its handlers receive immutable snapshot Context after durable/state reduction, and startup, failure, shutdown, collision, cancellation, and arbitrary side-effect boundaries follow one fail-closed owner lifecycle.

**Blocked by:** 20 — Load Prompt Resources into the fixed system prompt.

**Status:** ready-for-agent

- [ ] Untrusted construction performs zero Extension probing; trusted discovery accepts only direct nonsymlink `.py` resources under the closed name grammar and deterministic Unicode order.
- [ ] The complete source set is strict-UTF-8 decoded and structurally validated before execution; invalid path/name/symlink/source/syntax/entrypoint rejects under the selected public carrier.
- [ ] Each Session construction executes independent private module generations with ordinary installed-distribution imports and no project package-path injection or shared public module cache.
- [ ] The sole entrypoint is `extension(api)` and may only register event handlers and AgentTools during initialization; registrations freeze after settlement.
- [ ] Built-in Tool-name collision rejects the complete Session, while distinct Extension Tools follow canonical built-ins in deterministic file/registration order.
- [ ] Extension handlers receive immutable per-invocation snapshot Context, run serially after durable/state reduction and before public listeners/later effects, and cannot retain a live Session capability.
- [ ] Handler failure stops later progression, settles owned work, preserves committed history, publishes no synthetic repair event, and raises the selected redacted hook lifecycle failure.
- [ ] `session_start` runs forward before prompt finalization/Image commit/publication; `session_shutdown` runs reverse/all-attempted after active settlement and retries only unresolved handlers.
- [ ] Initialization failure/cancellation exposes no partial Session, registry, fresh Image, or recovery mutation; omh-owned resources roll back while arbitrary in-process Extension effects remain explicitly nonrollbackable/non-sandboxed.
- [ ] Installed Product Session tests exercise a real project Extension Tool plus every discovery/lifecycle/failure boundary and complete the Extension PA/ABD Matrix/corpus rows.

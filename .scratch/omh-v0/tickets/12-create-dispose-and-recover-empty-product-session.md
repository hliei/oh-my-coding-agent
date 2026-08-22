# 12 — Create and dispose an empty SessionManager-backed Product Session

**What to build:** Publish the first `AgentSession` without running a prompt around the accepted caller-visible `SessionManager`. A caller may supply a persistent or in-memory manager, observes that identical manager plus its empty tree and fixed Model, and disposes live resources without forcing a JSONL flush or adding persistence ownership.

**Blocked by:** 07 — Make Agent stateful and reusable; 09 — Stream DeepSeek text through deterministic transport.

**Status:** ready-for-agent

- [ ] The installed public surface exposes the accepted `SessionManager`, version-3 Session carriers, factories, and exact camelCase member allowlists without a storage Adapter, read-only manager wrapper, free parser/migration helpers, or snake_case aliases.
- [ ] `createAgentSession()` accepts the selected frozen options carrier, starts validation/effects on first await, and returns one final factory-produced Session; `sessionManager` omission uses `SessionManager.create(cwd)` and a supplied manager is exposed unchanged by object identity.
- [ ] The accepted cwd/path precedence and lexical rules distinguish operational `AgentSession` cwd from manager header cwd; default and explicit Session directories and candidate `.jsonl` paths follow ticket 08 exactly without an existence or same-cwd consistency gate.
- [ ] `NewSessionOptions.id` is admitted for `create`, `inMemory`, `forkFrom`, and `newSession`; an omitted id generates UUIDv7 while an explicit admitted id is preserved exactly and need not be a UUID.
- [ ] A new persistent manager owns an in-memory version-3 header, empty entry/index/tree state, candidate path, and `flushed=False`; it creates the required Session directory but no Session file or discovery result before the first Assistant Message.
- [ ] `SessionManager.inMemory()` exposes the same empty header/tree/leaf and identity semantics with no Session directory/file I/O, no discovery row, and `isPersisted() == False`.
- [ ] Pre-publication validation, authentication, Extension/resource initialization, failure, or cancellation publishes no `AgentSession` and completely settles only the live resources acquired by construction; it promises no Session-file rollback.
- [ ] `AgentSession` projects the identical manager's id/file/name and empty active-path messages; no private settled-state envelope, continuing file lock, or owner-busy admission exists.
- [ ] Successful disposal settles live work/resources and retains the manager's accepted persistent or in-memory state unchanged; it neither flushes an empty Session nor releases a Session-file lease.
- [ ] Installed public-seam tests use real temporary directories plus in-memory managers and record the empty/unflushed, supplied-manager identity, caller-id, and no-lease obligations in the Matrix.

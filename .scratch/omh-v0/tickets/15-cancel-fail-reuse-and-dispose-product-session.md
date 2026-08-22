# 15 — Cancel, fail, reuse, and dispose a Product Session

**What to build:** Complete the public Product Session operation barriers across active Model and Tool work. Callers can abort, wait for the captured attempt, observe ordinary Model error or clean cancellation, reuse the same Session afterward, and dispose concurrently or after failure without creating a second cancellation or cleanup policy.

**Blocked by:** 11 — Classify DeepSeek failure, cancellation, and reuse; 14 — Preserve SessionManager persistence and recovery failure semantics.

**Status:** ready-for-agent

- [ ] `abort()` lazily captures only the current Run, shares one cancellation/settlement attempt across callers, and cannot affect a later Run or rewrite a committed terminal class.
- [ ] `waitForIdle()` captures only the current Run/disposal attempt, returns immediately when already idle/disposed, and never waits for future work.
- [ ] Clean Model- and Tool-phase cancellation retains the selected aborted Agent history and appends the corresponding Session entries under the manager's in-memory-first rules before prompt/idle observers complete; no settled marker is added.
- [ ] A deterministic Model error returns normally from prompt, remains distinct from cancellation/lifecycle failure, and permits a later successful prompt in the same Session.
- [ ] Caller-task cancellation requests the same owner cancellation, shields complete settlement, then re-raises the original `CancelledError`.
- [ ] `dispose()` closes admission, joins active abort/settlement, disconnects subscriptions, and shuts live resources only after every owned obligation settles; it owns no Session-file lease, flush, rollback, or recoverability transition.
- [ ] Concurrent disposal calls share one attempt; cancellation of a disposal waiter does not abandon cleanup; a later call retries only unresolved cleanup actions.
- [ ] Closing/disposed admission, retained final reads, async-context-manager behavior, and idempotent abort/wait/dispose observations match the closed public surface.
- [ ] Manual and automatic threshold/overflow compaction share the existing abort/settlement barriers; cancellation or failure appends no compaction result, while successful overflow compaction may internally continue the same prompt exactly once.
- [ ] Explicit barrier tests cover Model, Tool, listener, manager append, compaction, and disposal races without sleeps, and Matrix/corpus evidence distinguishes all terminal/failure carriers without expecting fail-closed Session persistence.

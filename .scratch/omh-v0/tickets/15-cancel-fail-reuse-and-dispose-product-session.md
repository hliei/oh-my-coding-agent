# 15 — Cancel, fail, reuse, and dispose a Product Session

**What to build:** Complete the public Product Session operation barriers across active Model and Tool work. Callers can abort, wait for the captured attempt, observe ordinary Model error or clean cancellation, reuse the same Session afterward, and dispose concurrently or after failure without creating a second cancellation or cleanup policy.

**Blocked by:** 11 — Classify DeepSeek failure, cancellation, and reuse; 14 — Fail closed on Session ownership and persistence faults.

**Status:** ready-for-agent

- [ ] `abort()` lazily captures only the current Run, shares one cancellation/settlement attempt across callers, and cannot affect a later Run or rewrite a committed terminal class.
- [ ] `waitForIdle()` captures only the current Run/disposal attempt, returns immediately when already idle/disposed, and never waits for future work.
- [ ] Clean Model- and Tool-phase cancellation durably commits the selected aborted history and settled marker before prompt/idle observers complete.
- [ ] A deterministic Model error returns normally from prompt, remains distinct from cancellation/lifecycle failure, and permits a later successful prompt in the same Session.
- [ ] Caller-task cancellation requests the same owner cancellation, shields complete settlement, then re-raises the original `CancelledError`.
- [ ] `dispose()` closes admission, joins active abort/settlement, disconnects subscriptions, shuts resources, and releases the lease only after every owned obligation settles.
- [ ] Concurrent disposal calls share one attempt; cancellation of a disposal waiter does not abandon cleanup; a later call retries only unresolved cleanup actions.
- [ ] Closing/disposed admission, retained final reads, async-context-manager behavior, and idempotent abort/wait/dispose observations match the closed public surface.
- [ ] Explicit barrier tests cover Model, Tool, listener, persistence, and disposal races without sleeps, and Matrix/corpus evidence distinguishes all terminal/failure carriers.

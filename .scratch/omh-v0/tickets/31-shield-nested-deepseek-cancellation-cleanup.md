# 31 — Shield nested DeepSeek cancellation cleanup

**What to build:** Repair the real DeepSeek cancellation cleanup path exposed by the failed Live Provider Gate. Once active-stream cancellation begins, every Provider resource owned by that operation must settle completely despite later cancellation delivery or repeated Product Session aborts; clean cancellation must retain the accepted aborted terminal and same-Session reuse, while a genuine cleanup failure remains a fail-closed lifecycle error.

**Blocked by:** 15 — Cancel, fail, reuse, and dispose a Product Session.

**Status:** ready-for-agent

- [ ] A deterministic public-seam regression reproduces active cancellation while nested DeepSeek response, client, transport, and TLS-equivalent cleanup is still pending; it must not reduce the scenario to one synthetic close barrier.
- [ ] Cancellation becomes observable before cleanup begins, and every resource owned by the Model operation is closed and drained exactly once before the operation settles.
- [ ] Later cancellation delivery, repeated `abort()`, cancelled waiters, and concurrent `waitForIdle()` or `dispose()` calls cannot interrupt, duplicate, or abandon the shared cleanup attempt.
- [ ] Successful cleanup produces exactly one aborted terminal, no `LifecycleError`, complete idle settlement, and a fresh successful Run in the same Product Session.
- [ ] A genuine resource-close failure still produces `LifecycleError(code="cleanup")`, never forges an aborted terminal or idle/reusable state, and remains retryable only where the existing disposal contract explicitly permits cleanup retry.
- [ ] Direct `streamSimple` and `completeSimple` cancellation still re-raises the caller's original `CancelledError` only after cleanup, while owner-managed `stream` and `complete` retain their single aborted-terminal contract.
- [ ] The fix performs no automatic request retry or reconnect, starts no replacement Model operation, and exposes no credential, authenticated request bytes, raw Provider response, natural-language content, or internal cleanup traceback through public events or errors.
- [ ] Barrier-driven tests cover cancellation before, during, and after each cleanup cutoff without timing sleeps or busy polling, and installed-wheel conformance exercises the repaired public Product Session path.
- [ ] Candidate commit `079ae559d314ce09a32f4b4d20d2a7e711bea234`, wheel SHA-256 `308e435fbb663db415a978411a5139699ad68e3fcd9265f29b2033735c737260`, its Release Rows, and streaming evidence SHA-256 `040d771437ca6d3c0d4c12fb0ecc46d6f7871c109a77ad26dd910b64a3e2309c` remain permanently unauthorized and cannot be reused.
- [ ] After the fix, a new reproducible Candidate Wheel is built and both macOS 26 arm64 CPython 3.12/3.13 Release Rows pass against its exact bytes before Ticket 29 restarts from its first live case with a newly issued credential and fresh evidence.

## Comments

- 2026-08-27 — Created from the second failed Ticket 29 Live Provider Gate. The deterministic cancellation coverage accepted by Tickets 11 and 15 controls a single synthetic close barrier, but the real Candidate reached nested HTTPX/httpcore/AnyIO TLS cleanup where cancellation interrupted resource close and surfaced `LifecycleError("DeepSeek cleanup failed")`. The historical tickets remain resolved; this ticket owns the newly observed public-seam regression and complete downstream Candidate invalidation.

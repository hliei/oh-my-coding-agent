# 11 — Classify DeepSeek failure, cancellation, and reuse

**What to build:** Make every non-successful DeepSeek operation settle under the selected public Model semantics. Authentication, Provider, wire/stream, Usage, finish-reason, and cancellation paths preserve legal partial state, reveal no secret or raw Provider cause, make no retry, and allow a caller-started fresh Run only after complete ownership settlement.

**Blocked by:** 08 — Cancel and fail closed Agent work; 10 — Finalize DeepSeek Tool Calls and Usage.

**Status:** resolved

- [x] Missing credentials and HTTP authentication rejection classify privately as auth; other HTTP, rate, connection, and host timeout failures classify as Provider failures.
- [x] Malformed SSE/JSON, invalid Tool delta, missing/unknown terminal data, unexpected reasoning content, and invalid Usage classify as stream failures.
- [x] Failure before Assistant start emits only the terminal error; failure after start preserves the latest legal cumulative content and response identity.
- [x] Every public helper exposes one redacted error Assistant terminal value rather than leaking a producer exception or raw response.
- [x] Credential values, authenticated headers/bodies, Provider prose, arbitrary exceptions, and private causes are absent from events, Messages, logs, history, and evidence.
- [x] Each activated operation attempts transport at most once across Models, Core, Agent, and Product Session consumers; no layer retries, reconnects, or continues automatically.
- [x] Owner cancellation maps to aborted, drains the response/client/transport, and allows a fresh successful operation only after settlement.
- [x] Environment key rotation and removal are observed on later activated calls without using stale material.
- [x] Deterministic public-seam tests cover every failure class, redaction canary, cancellation race, zero-retry assertion, and post-failure reuse in Matrix/corpus evidence.

## Comments

- Implemented in public-seam TDD slices: private auth/Provider/stream classification and redacted terminal Assistants; strict wire, finish, reasoning, and Usage failures with latest legal partial preservation; then zero-retry reuse, environment-key reread, cancellation settlement, installed-wheel execution, and Matrix/corpus linkage. Product Session construction remains ticket 12 scope and will consume the same shared Models request path without a separate retry policy.
- Two-axis review finished clean after fixes. Standards review's duplicated terminal, Agent fixture, and managed-close shapes were centralized. Spec review's Models-level aborted settlement, cleanup-failure carrier, and direct Simple caller-cancellation provenance gaps were reproduced with deterministic barriers and fixed; final Standards and Spec re-reviews found no remaining findings.
- Verification: `uv run --locked pytest -q` (297 passed), `uv run --locked mypy src tests`, `uv run --locked python -m compileall -q src tests`, `uv lock --check`, JSON syntax checks, and `git diff --check` all pass.

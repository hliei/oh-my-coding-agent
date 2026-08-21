# 11 — Classify DeepSeek failure, cancellation, and reuse

**What to build:** Make every non-successful DeepSeek operation settle under the selected public Model semantics. Authentication, Provider, wire/stream, Usage, finish-reason, and cancellation paths preserve legal partial state, reveal no secret or raw Provider cause, make no retry, and allow a caller-started fresh Run only after complete ownership settlement.

**Blocked by:** 08 — Cancel and fail closed Agent work; 10 — Finalize DeepSeek Tool Calls and Usage.

**Status:** ready-for-agent

- [ ] Missing credentials and HTTP authentication rejection classify privately as auth; other HTTP, rate, connection, and host timeout failures classify as Provider failures.
- [ ] Malformed SSE/JSON, invalid Tool delta, missing/unknown terminal data, unexpected reasoning content, and invalid Usage classify as stream failures.
- [ ] Failure before Assistant start emits only the terminal error; failure after start preserves the latest legal cumulative content and response identity.
- [ ] Every public helper exposes one redacted error Assistant terminal value rather than leaking a producer exception or raw response.
- [ ] Credential values, authenticated headers/bodies, Provider prose, arbitrary exceptions, and private causes are absent from events, Messages, logs, history, and evidence.
- [ ] Each activated operation attempts transport at most once across Models, Core, Agent, and Product Session consumers; no layer retries, reconnects, or continues automatically.
- [ ] Owner cancellation maps to aborted, drains the response/client/transport, and allows a fresh successful operation only after settlement.
- [ ] Environment key rotation and removal are observed on later activated calls without using stale material.
- [ ] Deterministic public-seam tests cover every failure class, redaction canary, cancellation race, zero-retry assertion, and post-failure reuse in Matrix/corpus evidence.

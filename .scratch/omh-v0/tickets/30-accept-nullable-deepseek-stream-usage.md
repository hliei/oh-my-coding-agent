# 30 — Accept nullable DeepSeek streaming Usage

**What to build:** Repair the real DeepSeek Chat Completions stream boundary exposed by the failed Live Provider Gate. Ordinary SSE chunks may carry explicit `usage: null`; they must remain legal zero-Usage partials, while exactly one non-null terminal Usage payload is still required, strictly validated, and bound to the final Assistant value. Close the deterministic test gap, invalidate the failed Candidate Wheel, and reproduce all downstream release evidence before Ticket 29 resumes.

**Blocked by:** 11 — Classify DeepSeek failure, cancellation, and reuse.

**Status:** ready-for-agent

- [ ] A deterministic public-seam regression reproduces the official DeepSeek streaming shape with explicit `usage: null` on ordinary chunks and observes a normal terminal rather than a redacted stream failure.
- [ ] Explicit null Usage is ignored only as a non-terminal placeholder: it does not set `usage_seen`, alter partial zero Usage, suppress later content, or satisfy the required terminal Usage obligation.
- [ ] Exactly one non-null Usage mapping remains required before `[DONE]`; missing, malformed, duplicate, inconsistent, or post-Usage payloads continue to fail closed through the existing redacted stream classification.
- [ ] Both accepted terminal layouts remain covered: Usage accompanying the finish-reason chunk and a separate Usage-only chunk with empty `choices` immediately before `[DONE]`.
- [ ] All four Models helpers retain identical non-thinking request bytes, ordered public events, terminal object identity, complete Usage/cost, zero automatic retry, credential redaction, and fresh-operation reuse.
- [ ] Installed-wheel conformance covers the regression without adding a public test hook, weakening strict Usage finalization, retaining raw authenticated traffic, or changing the accepted Provider/API/Model identity.
- [ ] Candidate commit `c7ca2c7f7ce4ba30d19989e8099c36ef2bdd7e0b` and wheel SHA-256 `13254467a3787519f7e5343801de642da83689b0adb98aa1381b3a9e39c71124` remain permanently unauthorized; their Release Row results and failed live attempts cannot be reused.
- [ ] After the fix, a new reproducible Candidate Wheel is built and both macOS 26 arm64 CPython 3.12/3.13 Release Rows pass against its exact bytes before Ticket 29 returns to human execution with a newly issued credential.

## Comments

- 2026-08-27 — Created from the failed Ticket 29 Live Provider Gate. DeepSeek's current Chat Completions contract permits `usage: null` on ordinary streaming chunks when `stream_options.include_usage` is enabled, but the Candidate treats every present `usage` field as the unique non-null terminal mapping. Existing deterministic fixtures omit the field on ordinary chunks and therefore did not exercise the live shape. The live stream started and then failed before `done`; publication remained unauthorized.

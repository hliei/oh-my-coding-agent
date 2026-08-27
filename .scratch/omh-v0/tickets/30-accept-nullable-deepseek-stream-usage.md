# 30 — Accept nullable DeepSeek streaming Usage

**What to build:** Repair the real DeepSeek Chat Completions stream boundary exposed by the failed Live Provider Gate. Ordinary SSE chunks may carry explicit `usage: null`; they must remain legal zero-Usage partials, while exactly one non-null terminal Usage payload is still required, strictly validated, and bound to the final Assistant value. Close the deterministic test gap, invalidate the failed Candidate Wheel, and reproduce all downstream release evidence before Ticket 29 resumes.

**Blocked by:** 11 — Classify DeepSeek failure, cancellation, and reuse.

**Status:** resolved

- [x] A deterministic public-seam regression reproduces the official DeepSeek streaming shape with explicit `usage: null` on ordinary chunks and observes a normal terminal rather than a redacted stream failure.
- [x] Explicit null Usage is ignored only as a non-terminal placeholder: it does not set `usage_seen`, alter partial zero Usage, suppress later content, or satisfy the required terminal Usage obligation.
- [x] Exactly one non-null Usage mapping remains required before `[DONE]`; missing, malformed, duplicate, inconsistent, or post-Usage payloads continue to fail closed through the existing redacted stream classification.
- [x] Both accepted terminal layouts remain covered: Usage accompanying the finish-reason chunk and a separate Usage-only chunk with empty `choices` immediately before `[DONE]`.
- [x] All four Models helpers retain identical non-thinking request bytes, ordered public events, terminal object identity, complete Usage/cost, zero automatic retry, credential redaction, and fresh-operation reuse.
- [x] Installed-wheel conformance covers the regression without adding a public test hook, weakening strict Usage finalization, retaining raw authenticated traffic, or changing the accepted Provider/API/Model identity.
- [x] Candidate commit `c7ca2c7f7ce4ba30d19989e8099c36ef2bdd7e0b` and wheel SHA-256 `13254467a3787519f7e5343801de642da83689b0adb98aa1381b3a9e39c71124` remain permanently unauthorized; their Release Row results and failed live attempts cannot be reused.
- [x] After the fix, a new reproducible Candidate Wheel is built and both macOS 26 arm64 CPython 3.12/3.13 Release Rows pass against its exact bytes before Ticket 29 returns to human execution with a newly issued credential.

## Comments

- 2026-08-27 — Created from the failed Ticket 29 Live Provider Gate. DeepSeek's current Chat Completions contract permits `usage: null` on ordinary streaming chunks when `stream_options.include_usage` is enabled, but the Candidate treats every present `usage` field as the unique non-null terminal mapping. Existing deterministic fixtures omit the field on ordinary chunks and therefore did not exercise the live shape. The live stream started and then failed before `done`; publication remained unauthorized.
- 2026-08-27 — Public-seam TDD reproduced the official nullable shape across `streamSimple`, `completeSimple`, `stream`, and `complete`: ordinary null placeholders preserved later text and zero-Usage partials, while the separate non-null Usage-only chunk produced the shared normal terminal with exact Usage/cost and no credential exposure. The implementation ignores only explicit null before terminal Usage; existing missing, malformed, inconsistent, duplicate, and post-Usage cases remain redacted stream failures. Focused DeepSeek conformance passed (94), strict mypy passed, and Standards/Spec review reported zero findings.
- 2026-08-27 — Two isolated builds from candidate commit `079ae559d314ce09a32f4b4d20d2a7e711bea234` produced byte-identical `omh-0.1.0-py3-none-any.whl` SHA-256 `308e435fbb663db415a978411a5139699ad68e3fcd9265f29b2033735c737260`. macOS 26.3.1 (25D771280a) arm64 CPython 3.12.13 and 3.13.8 each installed that exact wheel offline from its lock-resolved only-binary wheelhouse outside the checkout and passed the complete no-skip installed suite with platform evidence. Binding both fresh row results set `readyForReleaseEvidenceBundle: true` and retained `publishRight: false`; no prior Candidate, row result, or failed live attempt was reused. Final verification passed the full locked suite (563), strict mypy, compileall, lock validation, and `git diff --check`. Ticket 29 may resume only with a newly issued human-owned credential.

# 29 — Run the Live Provider Gate and authorize publication

**What to build:** Perform the human-owned final release gate for the exact Candidate Wheel. A named human knowingly injects a newly issued revocable real DeepSeek credential on the selected host, reviews three live cases and secret handling, revokes the credential, completes the immutable Release Evidence Bundle, and grants the Publish Right only for the already verified wheel through the bound annotated tag.

**Blocked by:** 28 — Prove both Release Rows; 30 — Accept nullable DeepSeek streaming Usage.

**Status:** ready-for-human

- [ ] The gate runs on the accepted macOS 26 arm64 host with isolated CPython 3.12, the exact Candidate Wheel, and its locked wheelhouse after all deterministic Release Rows pass.
- [ ] A named human issues and temporarily injects a new `DEEPSEEK_API_KEY`, acknowledges that built-in shell execution has ordinary host/environment authority, and keeps unrelated secrets/resources outside the controlled fixture.
- [ ] One live non-thinking no-Tool stream proves ordered public events, normal terminal classification, and complete valid Usage without asserting natural-language bytes.
- [ ] One live active-stream cancellation settles completely and is followed by a fresh successful Run in the same Product Session.
- [ ] One live programmatic Product Journey uses `projectTrusted=False` in a disposable repository to inspect, mutate, complete Tool round trips, run a specifically named successful check after the final mutation, and report truthfully.
- [ ] Each Model operation makes no automatic retry; Provider/account/rate/service failure blocks release and cannot be replaced by Faux, deterministic fixtures, or prior live evidence.
- [ ] The credential is absent from every event, Tool output, transcript, error, log, persistent JSONL or in-memory Session entry, raw retained response, and evidence record; any disclosure fails the gate.
- [ ] The named human revokes the credential, deletes the disposable repository, and signs the redacted candidate-bound live evidence.
- [ ] The Release Evidence Bundle binds candidate commit/version, exact wheel filename/hash, lock, Matrix/corpus hashes, reproducible build, both Release Rows, live evidence, zero unresolved gaps/orphans, and named approval.
- [ ] After successful review, the named human creates the bound annotated release tag and publication uploads only the already verified wheel; any changed commit, wheel, lock, authority, contract, or evidence requires the complete gate again.

## Comments

- 2026-08-27 — Named human `hliei` began the gate for Candidate commit `c7ca2c7f7ce4ba30d19989e8099c36ef2bdd7e0b`, wheel `omh-0.1.0-py3-none-any.whl`, SHA-256 `13254467a3787519f7e5343801de642da83689b0adb98aa1381b3a9e39c71124`, on macOS 26.3.1 (25D771280a) arm64 with isolated CPython 3.12.13. The first non-thinking no-Tool streaming attempt failed closed with an initially over-redacted `GateFailure`; its newly issued key was revoked. A second complete attempt with a different newly issued key reached public `start` but no `done` and failed `stream.done_missing`; that key was also revoked. No automatic retry occurred.
- 2026-08-27 — The failed live shape exposed a release-blocking deterministic gap now owned by Ticket 30: current DeepSeek Chat Completions streams may include explicit `usage: null` on ordinary chunks, while the Candidate rejects any present non-mapping `usage` immediately after `start`; existing fixtures omit ordinary-chunk Usage instead of representing the official nullable field. Both attempts produced zero live evidence, the disposable repository was clean and moved to Trash, the repository remained clean, annotated tag `v0.1.0` was absent, and bound Release Row evidence retained `publishRight: false`. This Candidate and all of its prior build/row evidence remain unauthorized and cannot resume Ticket 29.

# 22 — Complete the programmatic v0 Product Journey

**What to build:** Demonstrate the canonical v0 Product Journey through public `AgentSession` and `SessionManager` in a controlled existing repository. The Agent inspects context, performs a bounded mutation with built-in Tools, runs the applicable named check after the final mutation, and truthfully reports the result while the public JSONL tree, recovery, branch/fork, compaction, failure, cancellation, and trust contracts remain observable.

**Blocked by:** 15 — Cancel, fail, reuse, and dispose a Product Session; 20 — Load Prompt Resources into the fixed system prompt.

**Status:** resolved

- [x] A deterministic local transport drives one complete programmatic journey with the nominal DeepSeek V4 Flash identity and no external network access.
- [x] The Session operates in an existing disposable repository, resolves its logical Workspace/instructions, and can complete the core journey with `projectTrusted=False` and no Skill/Extension dependency.
- [x] The Agent uses built-in inspection and mutation capabilities, then runs the specifically applicable named verification check after the final mutation.
- [x] A successful final report states what changed, which named check ran, and that it passed; normal Run completion or an unrelated successful command cannot substitute.
- [x] No-check, unavailable-check, and failed-check cases report unverified or failed verification and never claim a successful modification.
- [x] A recoverable Tool failure can return to the same Run, lead to a correction, and eventually produce a verified result without becoming a Provider failure.
- [x] Deterministic Provider failure, clean cancellation, busy admission, and later Session reuse remain observably distinct from the golden result.
- [x] Completion, disposal, path/id reopening from the parseable JSONL prefix, and a later correction prompt preserve the active-path conversation and rebind current resources/authentication without replaying interrupted effects.
- [x] Public tree/leaf reads retain abandoned history; one in-file branch and one independently appendable fork preserve their source entries and prove divergent continuation without source rewrite.
- [x] Explicit and automatic compaction preserve the complete tree while rebuilding active Model context from summary/retained tail; successful overflow continuation occurs at most once and is not a general retry.
- [x] The same public seam proves an in-memory Session with no file/discovery effect and no arbitrary persistence Adapter.
- [x] Companion project Skill behavior is independently visible under trusted construction without becoming required by the core journey.
- [x] Matrix/corpus journey rows connect every public observation/effect to its decision authority and run only through the installed `AgentSession`/`SessionManager` seams.

## Comments

- Added deterministic source-tree and installed-wheel conformance scenarios through public `createAgentSession`, `AgentSession`, and `SessionManager` seams; the private HTTP transport is used only to control nondeterminism and never reaches the network.
- Covered the golden mutation/check/report journey, the complete named-check truthfulness matrix, failure/cancellation/busy/reuse, recovery/disposal, branch/fork, explicit/threshold/overflow compaction, in-memory isolation, and trusted-only Skill visibility.
- Recorded each journey in the obligation matrix and observation corpus. Composite journey rows are closed local-release evidence with explicit Reference non-applicability; the named-check rule remains its governing ABD.
- Verification: focused Ticket 22 source/installed tests, the full locked pytest suite (472 passed), locked mypy, compileall, lock validation, JSON parsing, and diff checks passed. Standards and Spec review findings were fixed before final re-review.

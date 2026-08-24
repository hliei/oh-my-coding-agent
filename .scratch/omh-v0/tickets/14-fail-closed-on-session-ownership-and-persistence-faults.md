# 14 — Preserve SessionManager persistence and recovery failure semantics

**What to build:** Complete the ticket-08 local JSONL failure model without imposing atomic settled-state or continuing-lock guarantees. Lazy first flush, in-memory-first append, limited direct rewrites, best-effort line parsing, parsed-prefix recovery, and multiple manager snapshots each preserve their exact observable effects when file work succeeds or fails.

**Blocked by:** 13 — Prompt one durable no-Tool Product Session.

**Status:** resolved

- [x] Failure while exclusively creating or writing the lazy first header-plus-entry prefix propagates the underlying file failure without fabricating a settled marker, incomplete/abandoned record, rollback guarantee, or automatic retry; observable target bytes match the actual failed operation.
- [x] After first flush, every append mutates `fileEntries`, indexes/labels, and the manager-local leaf before file append; append failure propagates raw and retains those in-memory mutations even when the new entry or its parent is absent on disk.
- [x] A live manager may continue from its divergent in-memory leaf after append failure; it is not permanently closed, reclassified as cleanup failure, or restricted to a confirmed-durable prefix, and later open sees only parseable persisted entries.
- [x] Whole-file rewrite occurs only for an existing empty explicit path, registered v1-to-v2/v2-to-v3 local migration, or selected-path materialization; it directly truncates/writes the target, may leave partial bytes, and never rewrites the source Session.
- [x] Physical-line parsing independently skips blank and malformed-JSON lines, continues to later values, accepts a complete final value without LF, and skips a crash-partial final line without rewriting the file.
- [x] A physically nonempty file whose first parsed value is not a header with a string id fails unchanged; later syntactically valid entries receive only the accepted operation-specific validation rather than a complete load-time schema gate.
- [x] Recovery rebuilds indexes, labels, the complete append-only tree, and leaf from parsed file order, including an incomplete Run suffix; it loses only unpersisted bytes and never replays interrupted Model/Tool work, repairs history, or restores busy/queue/retry state.
- [x] Multiple `SessionManager` instances and processes may open and append to one path without an exclusive lease or owner-busy rejection. Cases expose stale manager snapshots and parseable physical ordering while making no safe-multi-writer or deterministic cross-process-order promise.
- [x] `open`, `setSessionFile`, `continueRecent`, `list`, and `listAll` preserve their distinct accepted absent/empty/invalid/probe/top-level failure behavior without substituting a private storage error taxonomy.
- [x] Deterministic fault injection remains private; installed-artifact tests use real files and explicit concurrency barriers to cover every write/rewrite/read/parse cutoff, in-memory versus persisted divergence, later admission, and recovery observation in Matrix/corpus evidence.

## Comments

- Implemented the complete local JSONL persistence/recovery surface: raw lazy-first-flush and post-flush append failure continuity, limited direct rewrites and migrations, best-effort physical-line recovery, operation-specific open/selection/discovery behavior, tree/label/leaf reconstruction, branch/fork materialization, and stale multi-manager/process append semantics. Implementation and evidence commits: `4b89dd1`, `4d43c7a`.
- Two-axis review from baseline `05f8c90` completed with zero final Standards findings and zero final Spec findings. Review fixes consolidated JSONL-line encoding and entry indexing, absorbed top-level discovery enumeration failure, and added installed-wheel direct-rewrite/read cutoff evidence.
- Verification: `uv run --locked pytest -q` (334 passed), `uv run --locked mypy src tests`, `uv run --locked python -m compileall -q src tests`, `uv lock --check`, focused ticket-14/installed-wheel checks (31 passed), scenario/corpus comparison, and `git diff --check` all pass.

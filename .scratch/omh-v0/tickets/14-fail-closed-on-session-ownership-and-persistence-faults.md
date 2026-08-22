# 14 — Preserve SessionManager persistence and recovery failure semantics

**What to build:** Complete the ticket-08 local JSONL failure model without imposing atomic settled-state or continuing-lock guarantees. Lazy first flush, in-memory-first append, limited direct rewrites, best-effort line parsing, parsed-prefix recovery, and multiple manager snapshots each preserve their exact observable effects when file work succeeds or fails.

**Blocked by:** 13 — Prompt one durable no-Tool Product Session.

**Status:** ready-for-agent

- [ ] Failure while exclusively creating or writing the lazy first header-plus-entry prefix propagates the underlying file failure without fabricating a settled marker, incomplete/abandoned record, rollback guarantee, or automatic retry; observable target bytes match the actual failed operation.
- [ ] After first flush, every append mutates `fileEntries`, indexes/labels, and the manager-local leaf before file append; append failure propagates raw and retains those in-memory mutations even when the new entry or its parent is absent on disk.
- [ ] A live manager may continue from its divergent in-memory leaf after append failure; it is not permanently closed, reclassified as cleanup failure, or restricted to a confirmed-durable prefix, and later open sees only parseable persisted entries.
- [ ] Whole-file rewrite occurs only for an existing empty explicit path, registered v1-to-v2/v2-to-v3 local migration, or selected-path materialization; it directly truncates/writes the target, may leave partial bytes, and never rewrites the source Session.
- [ ] Physical-line parsing independently skips blank and malformed-JSON lines, continues to later values, accepts a complete final value without LF, and skips a crash-partial final line without rewriting the file.
- [ ] A physically nonempty file whose first parsed value is not a header with a string id fails unchanged; later syntactically valid entries receive only the accepted operation-specific validation rather than a complete load-time schema gate.
- [ ] Recovery rebuilds indexes, labels, the complete append-only tree, and leaf from parsed file order, including an incomplete Run suffix; it loses only unpersisted bytes and never replays interrupted Model/Tool work, repairs history, or restores busy/queue/retry state.
- [ ] Multiple `SessionManager` instances and processes may open and append to one path without an exclusive lease or owner-busy rejection. Cases expose stale manager snapshots and parseable physical ordering while making no safe-multi-writer or deterministic cross-process-order promise.
- [ ] `open`, `setSessionFile`, `continueRecent`, `list`, and `listAll` preserve their distinct accepted absent/empty/invalid/probe/top-level failure behavior without substituting a private storage error taxonomy.
- [ ] Deterministic fault injection remains private; installed-artifact tests use real files and explicit concurrency barriers to cover every write/rewrite/read/parse cutoff, in-memory versus persisted divergence, later admission, and recovery observation in Matrix/corpus evidence.

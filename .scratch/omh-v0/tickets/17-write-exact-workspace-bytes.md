# 17 — Write exact Workspace bytes

**What to build:** Let the Agent create or replace one literal Workspace target through the fixed `write` built-in. The Tool reports actual UTF-8 bytes and every recognized operational negative truthfully, serializes conflicting mutations, and settles parent creation or a started overwrite before cancellation without promising atomic replacement or rollback.

**Blocked by:** 16 — Establish the built-in registry and inspect with read.

**Status:** ready-for-agent

- [ ] `write` accepts exactly literal path and Unicode-scalar content strings, including empty content, with no hidden size cap or normalization.
- [ ] Relative and absolute identity follows the shared Workspace path contract and actual I/O follows ordinary host symlink and permission semantics.
- [ ] Missing parents are created recursively inside the mutation queue before one ordinary create-or-truncate strict-UTF-8 write.
- [ ] Success reports the actual encoded byte count and canonically quoted original path without adding BOM, newline, or Unicode normalization.
- [ ] Recognized not-found, invalid-path, parent-not-directory, target-directory, not-writable, and storage-full conditions return the selected code/phase/effect Tool Outcomes.
- [ ] Error observations distinguish no effect, parents may exist, and target may be partially or completely changed without overstating rollback.
- [ ] Same-effective-target write/edit mutations serialize under one process-local key while different targets, reads, and shell effects remain concurrent.
- [ ] Cancellation holds the queue until active parent/write operations and handles settle, starts no later phase, publishes no success, and performs no rollback.
- [ ] Installed Product Session cases verify exact file bytes and public Tool events/results across success, negative, concurrency, and cancellation scenarios and update Matrix/corpus evidence.

# 17 — Write exact Workspace bytes

**What to build:** Let the Agent create or replace one literal Workspace target through the fixed `write` built-in. The Tool reports actual UTF-8 bytes and every recognized operational negative truthfully, serializes conflicting mutations, and settles parent creation or a started overwrite before cancellation without promising atomic replacement or rollback.

**Blocked by:** 16 — Establish the built-in registry and inspect with read.

**Status:** resolved

- [x] `write` accepts exactly literal path and Unicode-scalar content strings, including empty content, with no hidden size cap or normalization.
- [x] Relative and absolute identity follows the shared Workspace path contract and actual I/O follows ordinary host symlink and permission semantics.
- [x] Missing parents are created recursively inside the mutation queue before one ordinary create-or-truncate strict-UTF-8 write.
- [x] Success reports the actual encoded byte count and canonically quoted original path without adding BOM, newline, or Unicode normalization.
- [x] Recognized not-found, invalid-path, parent-not-directory, target-directory, not-writable, and storage-full conditions return the selected code/phase/effect Tool Outcomes.
- [x] Error observations distinguish no effect, parents may exist, and target may be partially or completely changed without overstating rollback.
- [x] Same-effective-target write/edit mutations serialize under one process-local key while different targets, reads, and shell effects remain concurrent.
- [x] Cancellation holds the queue until active parent/write operations and handles settle, starts no later phase, publishes no success, and performs no rollback.
- [x] Installed Product Session cases verify exact file bytes and public Tool events/results across success, negative, concurrency, and cancellation scenarios and update Matrix/corpus evidence.

## Comments

- Implemented operational `write` at the Product Session seam: lexical Workspace paths, recursive parent creation inside a process-local mutation queue, ordinary create-or-truncate UTF-8 overwrite, actual byte counts with canonical JSON path quoting, closed Outcome codes/phase/effect, and cancellation that publishes no success and does not roll back. `edit` still uses the later ticket's execute path but will share this queue.
- Review follow-up: queue acquire waits out cancellation with `uncancel()` so a queued writer cannot drop the lock; installed/corpus rows cover all six Outcome codes plus a control-bearing path.

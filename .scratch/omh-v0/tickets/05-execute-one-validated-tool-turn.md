# 05 — Execute one validated Tool Turn

**What to build:** Complete one narrow Tool-calling tracer through the installed low-level Core seam. A Faux Assistant requests one valid Tool, Runtime preflights and executes it, public Tool events and a Tool Result Message are projected, and the next Faux Model response ends the same Run.

**Blocked by:** 03 — Complete Tool schemas and callable values; 04 — Own streams and all four low-level loops.

**Status:** resolved

- [x] A Tool-calling Assistant response enters one Turn whose Tool Call is looked up, prepared, converted, and validated before its callable starts.
- [x] ToolExecutionStart carries the original immutable id, name, and arguments; accepted cumulative updates are snapshotted and delivered in same-Tool FIFO order.
- [x] The callable receives fresh validated parameters, the Run's read-only signal, and a synchronous update callback, and returns one validated final AgentToolResult.
- [x] A valid returned negative domain result remains a Tool Outcome with `isError=False`; it is not converted into a Tool Failure.
- [x] Missing/ambiguous Tool, preparation failure, schema failure, raise, non-awaitable return, invalid update, and invalid final result each produce the selected redacted recoverable Tool Failure without leaking private data.
- [x] Every started attempt has a causally ordered start/update/end lifecycle, and its Tool Result Message is projected only after the attempt and accepted updates settle.
- [x] A nonterminating Tool Outcome causes a later Model Turn, and the Run returns the prompt seed plus Assistant, Tool Result, and final Assistant Messages in the selected order.
- [x] EventStream, awaited-sink, and terminal AgentEnd projections agree on the immutable Run suffix, and input Context remains unchanged.
- [x] Installed public-seam tests and Matrix/corpus cases cover the successful round trip plus every Runtime Tool Failure template owned by this slice.

## Comments

- TDD: the public `runAgentLoop` tracer first failed because the loop ended after the Tool-calling Assistant; start/update/end, converted params, and a later Model Turn then went green. Failure templates next failed as uncaught lookup/prepare/execute errors before the shared redacted Tool Failure path.
- Two-axis review: Standards found `_ToolAttempt` missing `kw_only=True` (fixed). Spec found updates flushed only after `execute` returned, failure corpus C hardcoded rather than measured, and sync `execute` invalid-update misclassified; concurrent FIFO drain, measured later Turns, and irreversible invalid-update classification now cover those.
- Verification: `uv run --locked pytest -q` (155 passed), `uv run --locked mypy src tests`, `uv run --locked python -m compileall -q src tests`, `uv lock --check`, and `git diff --check`.
- Scope boundary: empty/duplicate Tool Call ids, sequential/parallel batches, length-truncated calls, unanimous `terminate=True`, and cancellation remain with tickets 06 and 08.

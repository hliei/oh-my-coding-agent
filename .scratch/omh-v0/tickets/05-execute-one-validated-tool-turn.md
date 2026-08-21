# 05 — Execute one validated Tool Turn

**What to build:** Complete one narrow Tool-calling tracer through the installed low-level Core seam. A Faux Assistant requests one valid Tool, Runtime preflights and executes it, public Tool events and a Tool Result Message are projected, and the next Faux Model response ends the same Run.

**Blocked by:** 03 — Complete Tool schemas and callable values; 04 — Own streams and all four low-level loops.

**Status:** ready-for-agent

- [ ] A Tool-calling Assistant response enters one Turn whose Tool Call is looked up, prepared, converted, and validated before its callable starts.
- [ ] ToolExecutionStart carries the original immutable id, name, and arguments; accepted cumulative updates are snapshotted and delivered in same-Tool FIFO order.
- [ ] The callable receives fresh validated parameters, the Run's read-only signal, and a synchronous update callback, and returns one validated final AgentToolResult.
- [ ] A valid returned negative domain result remains a Tool Outcome with `isError=False`; it is not converted into a Tool Failure.
- [ ] Missing/ambiguous Tool, preparation failure, schema failure, raise, non-awaitable return, invalid update, and invalid final result each produce the selected redacted recoverable Tool Failure without leaking private data.
- [ ] Every started attempt has a causally ordered start/update/end lifecycle, and its Tool Result Message is projected only after the attempt and accepted updates settle.
- [ ] A nonterminating Tool Outcome causes a later Model Turn, and the Run returns the prompt seed plus Assistant, Tool Result, and final Assistant Messages in the selected order.
- [ ] EventStream, awaited-sink, and terminal AgentEnd projections agree on the immutable Run suffix, and input Context remains unchanged.
- [ ] Installed public-seam tests and Matrix/corpus cases cover the successful round trip plus every Runtime Tool Failure template owned by this slice.

# 06 — Settle deterministic Tool batches

**What to build:** Extend the single-Tool tracer to complete deterministic multi-Tool Turns. Runtime correlates every Call before effects, applies the selected sequential/parallel policy, preserves per-Tool causality, waits for all started work, and projects final Tool Results in Assistant source order before either continuing or terminating the Run.

**Blocked by:** 05 — Execute one validated Tool Turn.

**Status:** ready-for-agent

- [ ] One effect-free source-order scan rejects empty Tool Call ids and every occurrence of a duplicated id while leaving unrelated unique Calls eligible.
- [ ] Complete lookup, preparation, conversion, and validation preflight finishes for the batch before any approved Tool callable starts.
- [ ] Omitted execution mode runs approved Tools concurrently; explicit global sequential mode or any called Tool's sequential declaration serializes the complete batch.
- [ ] Same-Tool start/update/end causality is fixed while parallel cross-Tool updates and ends may interleave according to observed execution.
- [ ] Every started Tool and accepted update settles before Tool Result Messages project in original Call order and before any later Model effect.
- [ ] A length-truncated Assistant with Tool Calls starts no Tool effects and receives the selected recoverable truncated-call Results.
- [ ] A nonempty batch terminates only when every finalized Tool Outcome is successful and explicitly `terminate=True`; any rejection, failure, false, or absent intent continues.
- [ ] Tool Call ids may be reused in a later Assistant Message after the earlier Turn has settled.
- [ ] Cancellation races use explicit pause/release barriers rather than sleeps, and Matrix/corpus cases cover mixed valid/invalid batches in both execution modes.

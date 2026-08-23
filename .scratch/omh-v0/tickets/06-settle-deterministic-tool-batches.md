# 06 — Settle deterministic Tool batches

**What to build:** Extend the single-Tool tracer to complete deterministic multi-Tool Turns. Runtime correlates every Call before effects, applies the selected sequential/parallel policy, preserves per-Tool causality, waits for all started work, and projects final Tool Results in Assistant source order before either continuing or terminating the Run.

**Blocked by:** 05 — Execute one validated Tool Turn.

**Status:** resolved

- [x] One effect-free source-order scan rejects empty Tool Call ids and every occurrence of a duplicated id while leaving unrelated unique Calls eligible.
- [x] Complete lookup, preparation, conversion, and validation preflight finishes for the batch before any approved Tool callable starts.
- [x] Omitted execution mode runs approved Tools concurrently; explicit global sequential mode or any called Tool's sequential declaration serializes the complete batch.
- [x] Same-Tool start/update/end causality is fixed while parallel cross-Tool updates and ends may interleave according to observed execution.
- [x] Every started Tool and accepted update settles before Tool Result Messages project in original Call order and before any later Model effect.
- [x] A length-truncated Assistant with Tool Calls starts no Tool effects and receives the selected recoverable truncated-call Results.
- [x] A nonempty batch terminates only when every finalized Tool Outcome is successful and explicitly `terminate=True`; any rejection, failure, false, or absent intent continues.
- [x] Tool Call ids may be reused in a later Assistant Message after the earlier Turn has settled.
- [x] Cancellation races use explicit pause/release barriers rather than sleeps, and Matrix/corpus cases cover mixed valid/invalid batches in both execution modes.

## Comments

- TDD: correlation scanning first failed because every id prepared/executed; default parallelism then failed behind the first paused Tool; truncation next executed Tools; and unanimous termination requested an extra Model response. Each public `runAgentLoop`/`agentLoop` slice went red before the narrow batch implementation.
- Cancellation evidence uses only explicit events: parallel cancellation retains a completed Outcome, waits for suppressed-cancellation cleanup, rejects post-cutoff updates, and projects a cancelled peer; sequential cancellation waits cleanup, starts no later Tool or Model effect, and re-raises caller-task cancellation only after the aborted trace settles. Callback/projection cutoff ownership remains with ticket 08.
- Two-axis review: Standards found no hard violation and accepted standalone installed-scenario duplication. Spec found missing active-batch cancellation and a rejected sequential Call losing its whole-batch override; both were fixed. Follow-up also narrowed mode selection to one uniquely matched called Tool so an ambiguous definition cannot serialize unrelated approved Calls.
- Conformance: Matrix/corpus rows now cover strict empty/duplicate correlation in parallel and sequential modes plus deterministic truncation and unanimous termination through the installed wheel.
- Verification: `uv run --locked pytest -q` (167 passed), `uv run --locked mypy src tests`, `uv run --locked python -m compileall -q src tests`, `uv lock --check`, JSON parsing for both conformance authorities, and `git diff --check`.

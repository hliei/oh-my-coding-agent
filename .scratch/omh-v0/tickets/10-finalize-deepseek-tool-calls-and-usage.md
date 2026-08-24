# 10 — Finalize DeepSeek Tool Calls and Usage

**What to build:** Complete DeepSeek's successful text-and-Tool protocol. Fragmented Provider deltas finalize exact Tool Calls, selected request options fail or pass before effect, terminal Usage and cost are validated, and the resulting Assistant value can drive the existing Core Tool Turn without a second Adapter behavior.

**Blocked by:** 05 — Execute one validated Tool Turn; 09 — Stream DeepSeek text through deterministic transport.

**Status:** resolved

- [x] One private request constructor serves stream, complete, Simple, Core, Agent, and Session consumers with the same canonical request semantics.
- [x] Tools are sent only when present; temperature and maxTokens are omitted or passed under the accepted DeepSeek names and ranges without clamping.
- [x] Invalid Provider-specific Model/options reject with the selected local error before transport construction or network effect.
- [x] Fragmented Tool Call ids, names, and JSON argument deltas produce cumulative partials and one exact finalized ToolCall object without repair.
- [x] Stop, length, and tool-call finish reasons map to the selected stop reasons, and Tool Call output drives a complete Core Tool round trip.
- [x] Terminal Usage validates every required count and relationship, preserves reported total tokens, and maps cache/input/output categories exactly.
- [x] Usage cost applies the fixed v0 DeepSeek Flash component rates and preserves the unrounded exact component sum.
- [x] Streaming partials carry all-zero Usage until the unique terminal payload; the terminal stream result is the identical Assistant object used by Core.
- [x] Deterministic installed tests cover boundary options, fragmented text/Tool streams, Usage/cost, request bytes, and the two DeepSeek ABD records in Matrix/corpus evidence.

## Comments

- Implemented in vertical public-seam slices: `StreamOptions` and four-helper/Core request preflight; fragmented Tool Call cumulative events and strict JSON finalization; required terminal Usage relationships and exact fixed-rate cost; then the installed Core Tool round trip and Matrix/corpus rows.
- Two-axis review found no Standards hard violation. Its Tool-state Feature Envy judgement call was resolved by moving fragment accumulation/finalization onto the private state object; installed-scenario fixture duplication remains intentional for `python -I` wheel isolation. Spec review found three correctness/evidence gaps—required cache-hit zero-fill, missing-arguments `{}` fallback, and incomplete four-helper preflight rows—and all three were reproduced and fixed.
- Verification: `uv run --locked pytest -q` (234 passed), `uv run --locked mypy src tests`, `uv run --locked python -m compileall -q src tests`, `uv lock --check`, JSON syntax/linkage checks, and `git diff --check` all pass.

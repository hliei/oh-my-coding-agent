# 10 — Finalize DeepSeek Tool Calls and Usage

**What to build:** Complete DeepSeek's successful text-and-Tool protocol. Fragmented Provider deltas finalize exact Tool Calls, selected request options fail or pass before effect, terminal Usage and cost are validated, and the resulting Assistant value can drive the existing Core Tool Turn without a second Adapter behavior.

**Blocked by:** 05 — Execute one validated Tool Turn; 09 — Stream DeepSeek text through deterministic transport.

**Status:** ready-for-agent

- [ ] One private request constructor serves stream, complete, Simple, Core, Agent, and Session consumers with the same canonical request semantics.
- [ ] Tools are sent only when present; temperature and maxTokens are omitted or passed under the accepted DeepSeek names and ranges without clamping.
- [ ] Invalid Provider-specific Model/options reject with the selected local error before transport construction or network effect.
- [ ] Fragmented Tool Call ids, names, and JSON argument deltas produce cumulative partials and one exact finalized ToolCall object without repair.
- [ ] Stop, length, and tool-call finish reasons map to the selected stop reasons, and Tool Call output drives a complete Core Tool round trip.
- [ ] Terminal Usage validates every required count and relationship, preserves reported total tokens, and maps cache/input/output categories exactly.
- [ ] Usage cost applies the fixed v0 DeepSeek Flash component rates and preserves the unrounded exact component sum.
- [ ] Streaming partials carry all-zero Usage until the unique terminal payload; the terminal stream result is the identical Assistant object used by Core.
- [ ] Deterministic installed tests cover boundary options, fragmented text/Tool streams, Usage/cost, request bytes, and the two DeepSeek ABD records in Matrix/corpus evidence.

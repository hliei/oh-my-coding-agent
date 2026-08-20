# Choose the Python Extension lifecycle

Type: grilling
Status: open
Blocked by: 01, 03, 04, 05, 07, 08

## Question

What discovery order, module contract, registration interface, event set, dependency access, cache policy, reload lifecycle, stale-context rule, trust gate, and error isolation must dynamically loaded `.py` extensions provide in v0? This ticket owns the Python entrypoint corresponding to the Reference Extension default-export factory and, if that mapping differs inside an envelope, must write a complete separately named PA; it may not reuse `PA:python-illegal-identifier`.

## Comments

- 2026-08-20 — Upstream [Choose the AgentSession surface](08-choose-agent-session-surface.md) fixes one exclusively owned durable Product Session and transactional private construction. Ticket 09's trust field must be the same gate used by project Prompt Resources; Extension discovery/load/initialization must complete before a fresh Session Image commit and result publication, while exact-id recovery must re-load currently admitted Extensions without serializing their code/instances into the Image. Extension effects and their pre-publication rollback remain solely this ticket's authority. Any accepted system-prompt contribution may only be a controlled append after ticket 08's fixed six-section base; it cannot replace/reorder the base, introduce arbitrary append/config/context inputs, rebuild mid-Session, or claim `ABD:fixed-session-system-prompt`. Ticket 09 may add only its reserved options/result fields and must preserve ticket 08's lease, durable ordering, failure, disposal, and fixed-resource lifetime boundaries.
- 2026-08-20 — Upstream [Choose the Agent loop and Tool lifecycle](07-choose-agent-loop-and-tool-lifecycle.md) excludes the public `beforeToolCall` / `afterToolCall` hook group and requires expected public Tool failures to return validated `AgentToolResult` values while raised exceptions are redacted. Extension registration/events may not reopen those Core hooks, bypass Tool correlation/preflight/cancellation barriers, or inject arbitrary exception text into Agent history.
- 2026-08-18 — Upstream [Choose the async, stream, and cancellation contract](04-choose-async-stream-cancellation-contract.md) requires `createAgentSession()` to publish a Session only after all selected initialization succeeds and to clean every live resource on pre-publication failure or cancellation. This decision must classify Extension discovery/load/initialization side effects and define their rollback or retained-effects policy; the async contract alone guarantees no live task or resource leak.

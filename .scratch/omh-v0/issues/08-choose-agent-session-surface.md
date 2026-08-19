# Choose the AgentSession surface

Type: grilling
Status: open
Blocked by: 01, 03, 04, 05, 07

## Question

Which coding-agent responsibilities must `AgentSession` own in v0 across session history, persistence, branching, compaction, retries, skills, prompts, model selection, and orchestration around `Agent`, and which remain later-version work?

## Comments

- 2026-08-20 — Upstream [Choose the Agent loop and Tool lifecycle](07-choose-agent-loop-and-tool-lifecycle.md) fixes one immutable-Context Agent Run machine, source-ordered complete Tool history, a synthetic aborted tail after Tool-phase cancellation, no steering/follow-up/retry/hidden Turn limit, and lifecycle failures that may truncate events without rolling back published history. This decision must define Session persistence and Session events around those facts without rewriting Agent outcomes or exposing a second control plane.
- 2026-08-18 — Upstream [Choose the async, stream, and cancellation contract](04-choose-async-stream-cancellation-contract.md) requires `createAgentSession()` to publish a Session only after all selected initialization succeeds and to clean every live resource on pre-publication failure or cancellation. This decision must define whether and how session-store writes made during construction commit or roll back; the async contract alone promises no live resource leak, not persistent-state rollback.

# Choose the v0 REPL and run modes

Type: grilling
Status: open
Blocked by: 01, 06, 07, 08, 09, 10

## Question

What observable input, streaming output, tool display, cancellation, queueing, session, shutdown, configuration, and non-interactive behaviour must the simple v0 REPL and any other selected run modes provide without becoming a reusable TUI framework?

## Comments

- 2026-08-20 — Upstream [Choose the Agent loop and Tool lifecycle](07-choose-agent-loop-and-tool-lifecycle.md) supplies closed Agent events, source-ordered Tool Result history, stable redacted Tool errors, explicit busy rejection, no steering/follow-up queue, and a synthetic aborted Assistant tail after Tool-phase cancellation. This decision must choose the REPL projection of those existing observations without changing their classification, retrying, or adding a second queue/stop policy.

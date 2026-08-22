# Specify the omh v0 Release Surface

Type: wayfinder:map

## Destination

Produce the complete decisions needed to hand omh v0 to `/to-spec`: the first usable vertical Python release across `oh_my_llm`, `oh_my_core`, and `oh_my_coding_agent`, reproducing an explicitly selected subset of the Reference Revision's observable behaviour, including its public SessionManager, local append-only JSONL Session tree, branch/fork, compaction, best-effort recovery, and ephemeral mode.

Define the v0 user journey, public interfaces, Python semantic adaptations, model and tool execution, session and extension behaviour, simple terminal experience, and conformance/release evidence. Do not specify or implement later-version completion of the full Target Compatibility Surface.

## Notes

- This map produces decisions, not a specification, implementation tickets, scaffolding, tests, or product code. When the map is clear, hand it to `/to-spec` for a separate read-only scope/seam review.
- Advance exactly one frontier decision at a time. Record a decision only after explicit human acceptance.
- Use `codebase-design` and `domain-modeling` while resolving grilling tickets. Keep `CONTEXT.md` as glossary only; a decision's detail belongs in its ticket.
- The Reference Revision is the committed tree `0e6909f050eeb15e8f6c05185511f3788357ddb3` in `/Users/andyli/Documents/pi-learn/pi`. Inspect it through revision-pinned Git reads, not the mutable worktree, and ignore the source repository's HEAD.
- Evidence, research, compatibility matrices, source citations, and licensing or provenance documents may name Pi directly when factual. The Pi naming restriction applies to omh product identity, runtime code, public namespaces, configuration, and concrete implementation names; never weaken or obscure evidence merely to avoid the word.
- The product, CLI, configuration, runtime modules, and public names use `omh`; Python import packages use `oh_my_llm`, `oh_my_core`, and `oh_my_coding_agent`; product configuration lives under `.omh/`.
- The long-term Target Compatibility Surface covers the Reference Revision's `ai`, the non-Harness runtime path in `agent`, and `coding-agent` observable behaviour. It excludes every `harness/**` export, `AgentHarness`, the standalone reusable `tui` package interface, and the experimental `orchestrator` package.
- `AgentSession` directly composes `Agent` and a caller-visible `SessionManager`; coding-agent owns compaction, Sessions, and Skills. v0 persistence and recovery follow the pinned Reference Revision's local JSONL SessionManager behaviour rather than the former private settled-only Session Image contract.
- Persistent Sessions expose local JSONL paths, append-only trees, branch/fork, explicit and automatic compaction, recent and caller-id selectors, parsed-entry recovery including incomplete Runs, and no exclusive live-owner lease. `SessionManager.inMemory()` and `omh --no-session` provide the ephemeral mode.
- The reopened Product Journey is upstream scope authority. Downstream decisions, the specification, implementation tickets, dependency guidance, and the live glossary are reconciled to this public-`SessionManager` contract; dated Comments and superseded research remain historical evidence.
- Terminal capability required by v0 is a simple REPL implemented inside `oh_my_coding_agent`; omh does not publish an independent TUI package.
- Extensions are Python modules loaded dynamically from `.py` sources, including project-local `.omh/extensions/`; TypeScript extension source compatibility is not a goal.
- The Reference Revision already contains coding-agent telemetry and update-check behaviour. v0 excludes them deliberately; telemetry is not assumed to be HEAD-only.

## Decisions so far

- [Choose the v0 product journey](issues/01-choose-v0-product-journey.md) — Anchor v0 on a verified local code change backed by public SessionManager, local append-only JSONL trees, branch/fork, compaction, best-effort recovery, Reference-style selectors, no live-owner lease, and ephemeral Sessions.
- [Choose the v0 Behavioral Parity policy](issues/02-choose-v0-parity-policy.md) — Require closed five-dimensional semantic parity and a decision-ticket-owned Ledger whose Session revision adds `ABD:omh-user-session-root` and retires six conflicting Session ABDs.
- [Choose the public Python interface](issues/03-choose-public-python-interface.md) — Publish one atomic distribution with three closed import seams, a caller-visible `SessionManager` and typed Session carriers, explicit Agent interfaces, four low-level loops, and the sole `continue_` naming adaptation.
- [Choose the async, stream, and cancellation contract](issues/04-choose-async-stream-cancellation-contract.md) — Use lazy owner-managed asyncio operations with single-consumer managed streams, explicit cancellation and settlement barriers, ordered callback failure handling, deterministic Tool scheduling, and fail-closed Product Session cleanup.
- [Choose the values, schemas, and errors contract](issues/05-choose-values-schemas-errors-contract.md) — Use immutable constructor-validated values, a strict JSON and Tool-schema domain, closed event protocols, type-preserving canonical serialization, and typed failure carriers.
- [Choose the model, provider, and authentication surface](issues/06-choose-model-provider-auth-surface.md) — Ship one static non-thinking DeepSeek V4 Flash Adapter with environment-only authentication, strict request/Usage validation, zero retries, and deterministic plus live release evidence.
- [Choose the Agent loop and Tool lifecycle](issues/07-choose-agent-loop-and-tool-lifecycle.md) — Use one closed Run/Turn state machine with immutable Context/results, deterministic Agent events and Tool outcomes, fail-closed ownership/correlation, explicit cancellation settlement, and five narrow ABDs.
- [Choose the AgentSession surface](issues/08-choose-agent-session-surface.md) — Compose `AgentSession` with a caller-visible `SessionManager` over local JSONL and in-memory trees, with branch/fork, compaction, parsed-prefix recovery including incomplete Runs, no exclusive lease, and fixed Prompt Resources/system prompt.
- [Choose the Python Extension lifecycle](issues/09-choose-extension-lifecycle.md) — Use explicitly trusted deterministic project-only Python modules with fixed registration, snapshot contexts, fail-closed event barriers, and independent per-Session generations.
- [Choose the coding Tools and Workspace contract](issues/10-choose-coding-tools-and-workspace-contract.md) — Use four fixed parallel built-ins over a logical non-sandbox Workspace, literal text/file semantics, same-file mutation serialization, managed shell execution, actionable outcomes, and fail-closed Extension collisions.
- [Choose the v0 REPL and run modes](issues/11-choose-repl-and-run-modes.md) — Expose explicit interactive and one-shot text Command Modes with Reference-style path/recent/exact-id and ephemeral Session selection, safe append-only projection, deterministic controls, shutdown, status, and redacted diagnostics.
- [Define the v0 conformance and release gate](issues/12-define-v0-conformance-and-release-gate.md) — Require a closed authority matrix, reproducible fixed-Reference corpus and universal wheel, four installed-artifact rows, a human-owned live DeepSeek gate, and hash-bound human release authorization.
- [Choose the v0 Python dependencies](issues/13-choose-v0-python-dependencies.md) — Use a locked CPython 3.12–3.13 Distribution with three narrowly owned Runtime dependencies, public SessionManager/local JSONL persistence without SQLite or a lease, local canonical JSON ownership, and release-gate-owned platform evidence.

## Not yet specified

- None.

## Out of scope

- omh product code, tests, implementation tickets, source layout details, or implementation estimates.
- `AgentHarness`, all `harness/**` exports, a standalone `oh_my_tui` package, and the experimental orchestrator.
- Rich terminal UI beyond the simple v0 REPL, including theme systems, reusable terminal widgets, and advanced interactive selectors.
- Telemetry, update checks, and any moving-HEAD capability for v0.
- TypeScript extension source compatibility or loading `.ts` files in omh.
- Image input and generation, public thinking or reasoning surfaces, and in-session model discovery or switching.
- Multiple real Providers in v0.
- In-flight steering or follow-up queues; Session import, export, or sharing.
- Automatic replay or resumption of an in-flight Model request, Tool execution, or other unpersisted effect after process failure. Recovery from the parseable persisted Session prefix remains in scope.
- Mutually opening Pi Session files as an omh product promise; Pi remains pinned comparison evidence, not a shared storage-format authority.
- Planning releases after v0 or silently pulling later-version work into the v0 specification.

# Specify the omh v0 Release Surface

Type: wayfinder:map

## Destination

Produce the complete decisions needed to hand omh v0 to `/to-spec`: the first usable vertical Python release across `oh_my_ai`, `oh_my_agent_core`, and `oh_my_coding_agent`, reproducing an explicitly selected subset of the Reference Revision's observable behaviour.

Define the v0 user journey, public interfaces, Python semantic adaptations, model and tool execution, session and extension behaviour, simple terminal experience, and conformance/release evidence. Do not specify or implement later-version completion of the full Target Compatibility Surface.

## Notes

- This map produces decisions, not a specification, implementation tickets, scaffolding, tests, or product code. When the map is clear, hand it to `/to-spec` for a separate read-only scope/seam review.
- Advance exactly one frontier decision at a time. Record a decision only after explicit human acceptance.
- Use `codebase-design` and `domain-modeling` while resolving grilling tickets. Keep `CONTEXT.md` as glossary only; a decision's detail belongs in its ticket.
- The Reference Revision is the committed tree `0e6909f050eeb15e8f6c05185511f3788357ddb3` in `/Users/andyli/Documents/pi-learn/pi`. Inspect it through revision-pinned Git reads, not the mutable worktree, and ignore the source repository's HEAD.
- Evidence, research, compatibility matrices, source citations, and licensing or provenance documents may name Pi directly when factual. The Pi naming restriction applies to omh product identity, runtime code, public namespaces, configuration, and concrete implementation names; never weaken or obscure evidence merely to avoid the word.
- The product, CLI, configuration, runtime modules, and public names use `omh`; Python import packages use `oh_my_ai`, `oh_my_agent_core`, and `oh_my_coding_agent`; product configuration lives under `.omh/`.
- The long-term Target Compatibility Surface covers the Reference Revision's `ai`, the non-Harness runtime path in `agent`, and `coding-agent` observable behaviour. It excludes every `harness/**` export, `AgentHarness`, the standalone reusable `tui` package interface, and the experimental `orchestrator` package.
- `AgentSession` directly composes `Agent`; coding-agent owns compaction, sessions, and skills. Both callback/list-returning loop functions and both `EventStream`-returning loop functions remain candidates for the v0 public interface.
- Terminal capability required by v0 is a simple REPL implemented inside `oh_my_coding_agent`; omh does not publish an independent TUI package.
- Extensions are Python modules loaded dynamically from `.py` sources, including project-local `.omh/extensions/`; TypeScript extension source compatibility is not a goal.
- The Reference Revision already contains coding-agent telemetry and update-check behaviour. v0 excludes them deliberately; telemetry is not assumed to be HEAD-only.

## Decisions so far

- [Choose the v0 product journey](issues/01-choose-v0-product-journey.md) — Anchor v0 on a verified local code change through the REPL and programmatic session, with explicit companion paths and a bounded later-version surface.
- [Choose the v0 Behavioral Parity policy](issues/02-choose-v0-parity-policy.md) — Require closed five-dimensional semantic parity, narrow Python adaptations, and a decision-ticket-owned Ledger seeded with five accepted records.

## Not yet specified

- Concrete Python libraries and provider SDKs; select them only after the required semantics and provider surface are known.
- Whether any semantic decision needs a throwaway prototype rather than conversational resolution.

## Out of scope

- omh product code, tests, implementation tickets, source layout details, or implementation estimates.
- `AgentHarness`, all `harness/**` exports, a standalone `oh_my_tui` package, and the experimental orchestrator.
- Rich terminal UI beyond the simple v0 REPL, including theme systems, reusable terminal widgets, and advanced interactive selectors.
- Telemetry, update checks, and any moving-HEAD capability for v0.
- TypeScript extension source compatibility or loading `.ts` files in omh.
- Image input and generation, public thinking or reasoning surfaces, and in-session model discovery or switching.
- In-flight steering or follow-up queues; session branching, cloning, compaction, import, export, or sharing.
- Recovery of in-flight model, Tool, or other incomplete effects after process failure.
- Planning releases after v0 or silently pulling later-version work into the v0 specification.

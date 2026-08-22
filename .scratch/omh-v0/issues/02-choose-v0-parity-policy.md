# Choose the v0 Behavioral Parity policy

Type: grilling
Status: resolved
Blocked by: 01

## Question

For the capabilities selected into the v0 Release Surface, which observations must match the Reference Revision exactly, which Python-language adaptations are permitted, and how must every accepted deviation be named and evidenced?

## Answer

v0 uses **closed semantic equivalence**, not source, structure, transcript-byte, or outcome-only similarity. For the same canonical input at a named public interface, omh must match the Reference Revision within that interface's declared **observation envelope** after applying only the closed mappings recorded below.

The five observation dimensions are:

- **A — Admission**: whether and when an operation, input, or resource is accepted or rejected.
- **L — Lifecycle**: the causally ordered public observations produced while the operation runs.
- **T — Terminal**: terminal classification, returned values, and failure ownership.
- **E — Effects**: externally visible state changes and effect results.
- **C — Continuity**: what the same public object or persisted session may do after settlement.

Every selected public interface must declare which of A/L/T/E/C its envelope covers and the concrete observations in each covered dimension. A difference inside that envelope is a Parity Gap unless an applicable Python Adaptation or Accepted Behavioral Deviation in the closed Parity Ledger permits it. An observation outside the envelope carries no v0 parity promise. Wall-clock timing, scheduler interleavings, raw provider chunk boundaries, concrete UUIDs or timestamps, stack traces, error prose, token counts, and provider metadata are outside by default; an owning decision may promote one explicitly.

### Difference classes

A **Python Adaptation (PA)** changes only how a Reference behaviour is represented through Python. It may not change any covered A/L/T/E/C observation or merge distinctions exposed by the Reference Revision. Permitted adaptation classes are closed:

1. **Naming**: only independent product identity (`oh_my_*` import roots and product-owned module filenames), syntax that Python cannot express, and a further difference individually named by the owning public-interface ticket. `runAgentLoop`, `Agent`, `AgentSession`, event `type` strings, and envelope fields such as `toolCallId` remain one-to-one by default; PEP 8 does not authorize a bulk rename.
2. **Values and types**: an explicit one-to-one mapping from a selected JS/TS value or type to its Python representation.
3. **Async and resource mechanics**: an explicit mapping among Promise, callback, async iterable, resource-lifecycle, awaitable, async iterator, and context-manager forms.
4. **Error and cancellation carriers**: an explicit Python carrier mapping that preserves classification, ownership, ordering, and terminal meaning.

No adaptation may merge Reference distinctions including `StopReason` cases, `isError`, `aborted`, Provider `error`, and recoverable Tool failure.

An **Accepted Behavioral Deviation (ABD)** deliberately changes an observation inside an envelope. A **Surface Exclusion** removes a capability from the v0 Release Surface and creates no v0 parity promise; exclusions never enter the Ledger. Any other difference inside an envelope is a **Parity Gap**.

### Parity Ledger schema and authority

The Ledger is closed. Every PA or ABD is a complete record with seven required fields:

1. stable `PA:<kebab>` or `ABD:<kebab>` key and readable name;
2. capability, named public interface, canonical input, and affected A/L/T/E/C dimensions;
3. fixed Reference Revision, precise source or test citations, and Reference observation;
4. omh mapping or different observation, classification, and reason;
5. exact comparison or normalization rule;
6. executable conformance row, or release-gate evidence plus the reason deterministic verification is impossible; and
7. the accepting decision-ticket link.

The accepting decision ticket owns and contains the complete record. `/to-spec` only compiles those authoritative records. A pure naming PA may record all five dimensions as `none`. Surface Exclusions do not enter the Ledger. An unrecorded difference inside an envelope is a Parity Gap.

A later accepted decision may withdraw a Ledger record. Withdrawal removes the record from the active closed Ledger immediately: it authorizes no normalization or conformance expectation, its stable key becomes a non-reusable historical tombstone, and its former complete record remains only as decision history. If the replacement still differs from the Reference Revision, it requires a new stable key and complete record; if the replacement restores Reference behaviour, no inverse PA or ABD is created.

All Reference evidence below is pinned to `0e6909f050eeb15e8f6c05185511f3788357ddb3`.

### Initial Parity Ledger

#### `PA:omh-python-import-roots` — omh Python import roots

1. **Scope**: Python Public Import Roots. Canonical input loads the three package roots and accesses the v0-selected symbols. A/L/T/E/C: `none`.
2. **Reference evidence**: `packages/ai/package.json#L2-L20` and `packages/ai/src/index.ts#L1-L30` expose `@earendil-works/pi-ai`; `packages/agent/package.json#L2-L17` and `packages/agent/src/index.ts#L1-L46` expose `@earendil-works/pi-agent-core`; `packages/coding-agent/package.json#L2-L21` and `packages/coding-agent/src/index.ts#L1-L25,#L190-L245` expose `@earendil-works/pi-coding-agent`.
3. **omh rule and reason**: the corresponding package identities are `oh-my-llm`/`oh_my_llm`, `oh-my-core`/`oh_my_core`, and `oh-my-coding-agent`/`oh_my_coding_agent` because omh has an independent product identity. This record authorizes only the root-identity mapping; it authorizes neither child-symbol renaming nor additional exports.
4. **Comparator**: compare the explicit three-root mapping, layer ownership, and public reachability of only the symbols selected by their owning interface decisions. Do not compare root strings. Separately assert that omh public identity does not leak Reference product naming.
5. **Verification**: installed-distribution import and selected-public-export conformance rows.
6. **Authority**: [Choose the v0 Behavioral Parity policy](02-choose-v0-parity-policy.md).

#### `ABD:omh-project-config-root` — `.omh/` project configuration root

1. **Scope**: Project Configuration Root. Canonical fixtures place conflicting product-owned configuration or resources under both `.pi/` and `.omh/`. A/E/C are affected; L/T are not.
2. **Reference evidence**: `packages/coding-agent/package.json#L6-L10` fixes `configDir` to `.pi`; `packages/coding-agent/src/config.ts#L487-L491,#L510-L560`, `core/settings-manager.ts#L188-L197`, and `core/package-manager.ts#L2336-L2418` use that product-owned namespace for configuration and resources.
3. **omh rule and reason**: omh uses `.omh/` for its project-owned configuration and resources and does not treat `.pi/` as omh configuration by default, preserving independent product identity. This record does not cover `.agents/skills`, and it does not choose the eventual user-global directory shape.
4. **Comparator**: in a fixture containing both namespaces, the Reference reads only the applicable `.pi/` side and omh reads only the applicable `.omh/` side. Compare later discovery, trust, and continuity semantics after this explicit logical-path normalization.
5. **Verification**: deterministic, isolated HOME/cwd dual-directory conformance rows for each product-owned path selected by its owning decision.
6. **Authority**: [Choose the v0 Behavioral Parity policy](02-choose-v0-parity-policy.md).

#### `ABD:reject-missing-model-at-session-creation` — reject an unresolved model at Product Session creation

1. **Scope**: Product Session Creation. Canonical cases provide neither an explicit nor default resolvable model, or provide an explicit identity that cannot validly resolve. A/T/C are affected; L is not; E is deliberately not locked by this record.
2. **Reference evidence**: `packages/coding-agent/src/core/sdk.ts#L192-L222,#L294-L300,#L385-L406` still returns a session plus fallback information; `core/agent-session.ts#L1139-L1154` defers the failure to prompt preflight.
3. **omh rule and reason**: omh rejects at Product Session creation and returns no usable session, keeping an invalid Product Session outside the public lifecycle instead of delaying failure.
4. **Comparator**: compare the rejection's lifecycle position and classification, not error wording. The canonical case performs no model call. Creation-side effect cutoff remains for the owning storage or session decision because E is not locked here.
5. **Verification**: deterministic creation rows for an empty model registry, no valid default, and an invalid explicit identity.
6. **Authority**: [Choose the v0 product journey](01-choose-v0-product-journey.md), with its classification and evidence recorded here.

#### `ABD:repl-reject-busy-ordinary-message` — reject a busy ordinary REPL submission

1. **Scope**: REPL Busy Submission. Canonical input submits an ordinary second Enter while a deterministic first Run is blocked. A/L/T/E/C are affected.
2. **Reference evidence**: `packages/coding-agent/src/modes/interactive/interactive-mode.ts#L2787-L2807` sends an ordinary busy Enter with `streamingBehavior: "steer"`, accepting and queuing it. In contrast, programmatic `Agent.prompt()` at `packages/agent/src/agent.ts#L334-L345` and plain `AgentSession.prompt(text)` at `packages/coding-agent/src/core/agent-session.ts#L1120-L1126` already reject while busy and are exact parity, not part of this ABD.
3. **omh rule and reason**: the v0 REPL immediately busy-rejects the ordinary second submission, creates no second-message queue, lifecycle, or effect, leaves the original Run unaffected, and accepts a fresh submission after settlement. v0 excludes in-flight steering and follow-up queues.
4. **Comparator**: the Reference interactive trace accepts and queues the second message; the omh REPL trace rejects it and contains no second-message lifecycle or effect. Both traces must preserve the original Run, and omh must prove post-settlement reuse.
5. **Verification**: a deterministic blocked-model plus REPL-driver row, including absence of a second effect and successful submission after settlement.
6. **Authority**: [Choose the v0 product journey](01-choose-v0-product-journey.md), narrowed and recorded here.

#### `ABD:require-named-check-for-success` — require the applicable named check before a modification-success claim

1. **Scope**: Modification Success Claim. Canonical inputs name a verification command either explicitly in the user request or through the resolved applicable repository convention. The seed fixture uses an explicit `CHECK`. A is not affected; L/T/E/C are affected.
2. **Reference evidence**: the default `packages/coding-agent/src/core/system-prompt.ts#L88-L147` and `packages/agent/src/agent-loop.ts#L192-L224,#L259-L274` provide no general named-check gate; `packages/agent/test/agent-loop.test.ts#L491-L522` permits an edit followed directly by `"done"` and normal completion.
3. **omh rule and reason**: after the final mutation, omh may claim that the modification succeeded only after observing the applicable named check's successful Tool Result. Otherwise it reports unverified or failed verification. This gives the success claim user- or repository-relevant evidence.
4. **Comparator**: match the applicable command identity, its position after the final mutation, the Tool Result success, and the final claim. A normal Run end or an unrelated successful command cannot substitute. The no-check and failed-check traces must not produce a modification-success claim; the same session may continue to correct and verify.
5. **Verification**: deterministic rows for no check, failed check, and a successful applicable check after the final mutation. The seed uses an explicitly named `CHECK`; repository-convention resolution receives its own owning-interface row.
6. **Authority**: [Choose the v0 product journey](01-choose-v0-product-journey.md), with its classification and evidence recorded here.

### Session persistence and recovery Ledger revision

Effective 2026-08-22, the re-accepted [Choose the AgentSession surface](08-choose-agent-session-surface.md) Answer supersedes every earlier Session persistence or recovery Ledger record that conflicts with its Reference-style public `SessionManager`, local append-only JSONL tree, best-effort parsed-prefix recovery, caller-chosen identity, and no-lease contract.

The revision adds one active Session persistence ABD:

- `ABD:omh-user-session-root` remains the complete record owned by [Choose the AgentSession surface](08-choose-agent-session-surface.md). It authorizes only the user-level `~/.pi/agent/sessions` to `~/.omh/agent/sessions` product-identity mapping. Explicit `sessionDir` and `SessionManager.open(path)` receive no normalization from it.

The following keys are withdrawn tombstones and are not members of the active Parity Ledger:

- `ABD:durable-session-commit-order` — withdrawn because omh now follows Reference lazy first flush, in-memory-first append, and the absence of a durable settled marker or durable-before-visible boundary.
- `ABD:atomic-session-image-publication` — withdrawn because v0 has no Session Image, atomic Session publication, settled-only recovery, rollback marker, or complete-Run recovery gate.
- `ABD:fail-closed-session-persistence` — withdrawn because an append failure propagates without rolling back the already-mutated manager state, closing the Session, or restricting continuity to a confirmed-durable prefix.
- `ABD:strict-session-cwd-admission` — withdrawn because v0 now follows the accepted Reference path carrier, expansion, lexical resolution, `cwd` precedence, and absence of an existence or manager/operational-`cwd` consistency gate.
- `ABD:exclusive-product-session-ownership` — withdrawn because opening or recovering a Session acquires no continuing exclusive lease or owner-busy admission; per-`AgentSession` single-active-Run ownership remains separate.
- `ABD:exact-id-cli-recovery` — withdrawn because recovery-only canonical-UUID selection conflicts with the accepted Session identity and selection authority. Reference-style existing-id selection and absent-id creation require no inverse ABD. Its owning command-mode ticket is not otherwise reopened or amended here.

This audit leaves `ABD:atomic-session-construction` and `ABD:managed-session-disposal` active for their non-persistence live-resource publication and disposal differences. It also leaves `ABD:reject-missing-model-at-session-creation`, `ABD:fixed-session-system-prompt`, `ABD:fixed-session-prompt-resources`, and `ABD:strict-prompt-resource-invocation` active and unchanged. No non-Session PA or ABD is reopened by this revision.

### Reserved downstream adaptation ownership

This ticket reserves `PA:python-illegal-identifier` but creates no Ledger record for it. [Choose the public Python interface](03-choose-public-python-interface.md) owns the key and may instantiate it only if v0 selects a Reference public identifier that Python syntax cannot express. The known candidate is public `Agent.continue()` at `packages/agent/src/agent.ts#L347-L375`; if selected, that ticket must lock the exact one-to-one Python spelling, such as `continue_`, and provide all seven Ledger fields. The reserved key may not be reused as a general naming license.

The Reference Extension `default factory` is a JavaScript module default-export contract, not an illegal identifier. [Choose the Python Extension lifecycle](09-choose-extension-lifecycle.md) owns the Python module entrypoint decision and must create a separately named PA if its selected mapping differs inside an envelope. It may not reuse `PA:python-illegal-identifier`.

## Comments

- 2026-08-22 — Reopened resolution accepted the Session persistence and recovery Ledger revision above. It adds the withdrawal rule, deactivates the six conflicting Session keys as non-reusable tombstones, recognizes `ABD:omh-user-session-root` as the sole new persistence ABD, preserves the explicitly named non-conflicting Session-adjacent records, and leaves every non-Session record and owning ticket unchanged.

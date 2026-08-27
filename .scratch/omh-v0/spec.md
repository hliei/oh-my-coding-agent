# omh v0 Release Surface

Type: spec
Status: ready-for-agent

## Problem Statement

Python users need a small but genuinely usable coding-agent product rather than a loose collection of translated modules. The first release must complete a bounded, verified code change in an existing local repository through either a simple terminal interaction or a programmatic session, while preserving the selected observable behavior of the fixed Reference Revision.

That promise is difficult to make safely because model streaming, Tool execution, durable session state, cancellation, project-owned executable resources, terminal projection, and release evidence all cross ownership boundaries. Ambiguous admission, mutable aliases, incomplete cleanup, replay after a crash, secret-bearing diagnostics, or a test suite that reaches around public interfaces would make the release behavior impossible to audit. The v0 Release Surface therefore needs one closed contract for public names, value carriers, lifecycle ordering, persistence, extensions, built-in Tools, Command Modes, dependencies, Behavioral Parity, and publication authority.

## Solution

Ship one versioned `omh` Distribution for CPython 3.12 and 3.13. It installs the `omh` command and the separately callable `oh_my_llm`, `oh_my_core`, and `oh_my_coding_agent` Public Import Packages.

The product journey is a bounded code change in an existing repository. A Product Session uses a fixed DeepSeek V4 Flash Model, streams observable Agent and Tool activity, performs built-in inspection and mutation, runs an applicable named verification check after the final mutation, and truthfully reports the result. The same outcome is available through an interactive plain-text REPL and the programmatic `AgentSession` seam; one-shot `omh --print` provides an additional single-Run Command Mode.

Product Sessions compose one `AgentSession` with a caller-visible `SessionManager`. They use either a local append-only JSONL Session tree or an in-memory carrier, support branch/fork and compaction, and recover the parseable persisted prefix even when the prior Run was incomplete. Lazy owner-managed asyncio operations, immutable public values, closed event protocols, explicit cancellation settlement, deterministic Extension loading, actionable Tool Outcomes, and fail-closed live-resource cleanup keep every lifecycle fact under one owner without inventing exclusive ownership of a Session file.

Behavioral Parity is measured as closed A/L/T/E/C semantic equivalence against the immutable Reference Revision. The Deterministic Conformance Suite exercises a single behavioral boundary: the installed Candidate Wheel through its three Public Import Surfaces and two `omh` Command Modes. It uses private controls for nondeterminism without adding public test hooks. A fixed Reference Observation Corpus, closed Conformance Obligation Matrix, two Release Rows, reproducible build, human-owned Live Provider Gate, and hash-bound release approval authorize publication of exactly one verified wheel.

## User Stories

1. As a coding-agent user, I want omh to make a bounded change in an existing repository, so that v0 solves a real development task.
2. As a coding-agent user, I want omh to inspect relevant repository context before editing, so that changes follow the repository's conventions.
3. As a coding-agent user, I want omh to use built-in file and shell capabilities without requiring a Skill or Extension, so that the core journey works in an ordinary repository.
4. As a coding-agent user, I want omh to run an applicable named check after the final mutation, so that a success claim has relevant evidence.
5. As a coding-agent user, I want omh to report an unavailable or failed named check as unverified or failed, so that normal Run completion is not mistaken for verified work.
6. As a coding-agent user, I want a failed Tool attempt to remain observable and recoverable inside the Run, so that the Agent can correct its work.
7. As a coding-agent user, I want a valid negative domain result to remain a Tool Outcome, so that expected operational negatives are not misclassified as Runtime failures.
8. As a coding-agent user, I want user cancellation to be distinct from Model failure and ordinary completion, so that I know why a Run ended.
9. As a coding-agent user, I want the same Product Session to accept another Run after clean cancellation, so that cancellation does not discard the conversation.
10. As a coding-agent user, I want deterministic Provider failure to be distinct from authentication failure, Tool failure, and cancellation, so that remediation is clear.
11. As a coding-agent user, I want a Product Session to remain usable after an ordinary Model error settles, so that a later explicit prompt can continue the work.
12. As a coding-agent user, I want project Skills to influence Agent behavior only when project resources are trusted, so that instructional resources are admitted explicitly.
13. As a coding-agent user, I want project Python Extensions to contribute observable behavior only when project resources are trusted, so that executable resources are admitted explicitly.
14. As a coding-agent user, I want Project Resource Trust to remain separate from built-in Tool and filesystem authority, so that trusting resources does not imply a sandbox or permission grant.
15. As a terminal user, I want `omh` to start a simple interactive REPL, so that I can work with one selected Product Session over multiple Runs.
16. As a terminal user, I want `omh --print` to perform one text Run and exit, so that scripts can request a single settled result.
17. As a terminal user, I want Command Mode selection to be explicit, so that TTY or pipe state does not silently change semantics.
18. As a terminal user, I want the REPL to show incremental Assistant text and selected Tool observations in an append-only transcript, so that activity is understandable without a rich TUI.
19. As a terminal user, I want Tool outcomes and Tool failures labeled differently, so that a valid negative result is not shown as a Runtime failure.
20. As a terminal user, I want terminal-control and bidirectional-format characters rendered visibly, so that model or Tool output cannot execute terminal control sequences.
21. As a terminal user, I want an ordinary message entered during a busy Run rejected immediately, so that v0 never creates an implicit steering or follow-up queue.
22. As a terminal user, I want Ctrl-C during a Run to request cancellation and await settlement, so that the same Session can return safely to idle.
23. As a terminal user, I want idle Ctrl-C and empty-line Ctrl-D to have deterministic local behaviors, so that terminal controls never introduce a force-exit race.
24. As a terminal user, I want a visible new-or-recovered Session identity before a Run starts, so that I know which persistent or in-memory Session the Run will use.
25. As a terminal user, I want one-shot stdout to contain only settled Assistant text, so that diagnostics and lifecycle facts remain on stderr.
26. As a terminal user, I want stable process statuses for usage errors, Model errors, signals, lifecycle failures, and success, so that shell automation can classify outcomes.
27. As a terminal user, I want `--help` and `--version` to be pure pre-construction actions, so that they never touch credentials, trust, storage, or project resources.
28. As a terminal user, I want explicit path/id, recent, exact-id open-or-create, and ephemeral selectors with closed precedence, so that Session selection is deliberate without requiring a rich picker.
29. As a terminal user, I want project trust decided anew for each interactive construction, so that it is never inherited from recovery state.
30. As a terminal user, I want one-shot mode never to prompt for trust, so that noninteractive execution remains deterministic.
31. As a Python caller, I want one atomic Distribution with three distinct Public Import Packages, so that layers are installed together without collapsing their interfaces.
32. As a Python caller, I want a closed Public Import Surface, so that technically importable implementation modules create no compatibility promise.
33. As a Python caller, I want retained public names to remain one-to-one with the Reference unless a named adaptation permits otherwise, so that broad style renames do not obscure parity.
34. As an LLM-layer caller, I want an explicit `Models` collection, so that Provider registration and Model lookup have no package-global state.
35. As an LLM-layer caller, I want a minimal deterministic Faux Adapter, so that text and Tool streaming can be tested without real Provider access.
36. As an LLM-layer caller, I want Faux kept out of Product Session Model selection and live release claims, so that deterministic fixtures cannot masquerade as real Provider evidence.
37. As an LLM-layer caller, I want text, Tool Call, Usage, Message, and stream values to use closed nominal records, so that invalid or unknown variants fail at construction.
38. As an LLM-layer caller, I want strict incremental Tool Call JSON finalization, so that malformed arguments are never repaired silently.
39. As an LLM-layer caller, I want one read-only `AbortSignal`, so that observers can await cancellation without gaining cancellation authority.
40. As an LLM-layer caller, I want each EventStream to have one consumer and multiple independent result observers, so that events are neither partitioned nor consumed by result retrieval.
41. As an LLM-layer caller, I want streams to activate only on first awaited use, so that creating a carrier performs no hidden effect.
42. As an LLM-layer caller, I want early stream close to cancel and settle owned work, so that no detached producer survives its owner.
43. As a Provider user, I want one static DeepSeek V4 Flash Model selected by default, so that v0 has no discovery or switching ambiguity.
44. As a Provider user, I want credentials read from `DEEPSEEK_API_KEY` for each Provider effect, so that rotation is visible and secrets are not persisted.
45. As a Provider user, I want missing credentials to reject Session publication, so that omh never falls back silently to a fake Model.
46. As a Provider user, I want invalid request options rejected before network effects, so that inputs are never silently clamped.
47. As a Provider user, I want at most one request attempt per activated operation, so that retry, backoff, and reconnect behavior is never hidden.
48. As a Provider user, I want invalid terminal Usage to fail closed, so that cost and token observations are not fabricated.
49. As a Provider user, I want Provider and stream errors to use stable redacted classifications, so that secret-bearing response data cannot enter public history.
50. As a Core caller, I want the four low-level loop entries to share one Run and Turn state machine, so that carrier choice does not fork semantics.
51. As a Core caller, I want prompt and continuation to differ only by their seed, so that Run results and lifecycle observations remain consistent.
52. As a Core caller, I want low-level loops to copy their input Context, so that a supplied history never doubles as an undocumented output channel.
53. As a Core caller, I want `AgentEnd`, EventStream result, and coroutine result to carry the same immutable Run suffix, so that terminal projections cannot disagree.
54. As an Agent user, I want invalid prompt seeds, busy prompts, and invalid continuation tails rejected before Run ownership, so that rejected work has no events or effects.
55. As an Agent user, I want Agent state updated before listener delivery, so that every event and state observation describes one consistent generation.
56. As an Agent user, I want lifecycle listeners awaited sequentially from a fixed snapshot, so that their ordering and effect cutoffs are deterministic.
57. As an Agent user, I want Tool Calls preflighted completely before effects start, so that invalid correlation or arguments cannot race with valid effects.
58. As an Agent user, I want parallel Tool execution to preserve source-ordered Tool Result projection, so that concurrency does not reorder conversation history.
59. As an Agent user, I want every approved Tool Call to settle before the next Model effect, so that no Tool work leaks across Turns.
60. As an Agent user, I want Tool Runtime errors converted to stable redacted Tool Failures, so that private exception text never reaches public events or durable history.
61. As an Agent user, I want no hidden Turn, token, or cost limit, so that only explicit terminal conditions end a continuing Run.
62. As an Agent user, I want cancellation to preserve already confirmed history and synthesize missing cancelled Tool Results in source order, so that the terminal trace remains correlated.
63. As an Agent user, I want unconfirmed cleanup to leave the Agent non-idle, so that ownership is never released on a guessed terminal state.
64. As a Product Session caller, I want lazy `createAgentSession()` construction to publish either one fully initialized Session or none, so that partial live resources never escape.
65. As a Product Session caller, I want to supply and recover the identical public `SessionManager` through `AgentSession`, so that persistence, tree, and leaf ownership stay caller-visible.
66. As a Product Session caller, I want both local JSONL and in-memory Session modes, so that durable and ephemeral work share one tree model without an arbitrary storage Adapter.
67. As a Product Session caller, I want omitted ids to generate UUIDv7 while admitted caller ids are preserved exactly, so that identity selection is explicit without a UUID-only restriction.
68. As a Product Session caller, I want one append-only entry tree with a caller-visible path and active leaf, so that history and abandoned branches remain inspectable.
69. As a Product Session caller, I want branch and fork operations that preserve source history, so that divergent continuation never rewrites the old path.
70. As a Product Session caller, I want explicit and automatic compaction that preserves the complete tree, so that Model context can shrink without deleting history.
71. As a Product Session caller, I want recovery from the parseable JSONL prefix, including incomplete Runs, so that persisted facts survive without replaying interrupted effects.
72. As a Product Session caller, I want append failures to propagate without rollback, automatic retry, or fabricated durable continuity, so that manager memory and the persisted prefix remain honestly observable.
73. As a Product Session caller, I want no exclusive Session-file lease or owner-busy promise, so that v0 matches the selected shared-file append behavior without claiming safe multi-writer coordination.
74. As a Product Session caller, I want `waitForIdle()`, `abort()`, and `dispose()` to settle only live work and resources, so that they never invent persistence rollback or affect a later Run.
75. As a Product Session caller, I want a fixed system prompt and Prompt Resource snapshot for one `AgentSession` construction, with missing command-shaped resources rejected before entry append or effects, so that behavior cannot drift mid-instance.
76. As an Extension author, I want deterministic direct project Extension discovery, so that module order does not depend on filesystem enumeration.
77. As an Extension author, I want a fixed `extension(api)` registration entrypoint, so that Python Extension admission has one explicit shape.
78. As an Extension author, I want registrations frozen before Session publication, so that the Tool and handler set cannot mutate during a Session.
79. As an Extension author, I want immutable per-invocation Extension Context snapshots, so that handlers cannot retain a stale live Session capability.
80. As an Extension author, I want handler failures to stop later progression and settle owned work, so that Extension errors fail closed.
81. As an Extension author, I want each Product Session construction to execute a fresh module generation, so that project modules do not share hidden Session state.
82. As an Extension author, I want startup and reverse-order retryable shutdown events, so that Session-lifetime resources have explicit ownership.
83. As a Tool user, I want exactly `read`, `bash`, `edit`, and `write` built in, so that the coding surface is small and predictable.
84. As a Tool user, I want relative paths based on the logical Workspace without treating it as a sandbox, so that path meaning and authority remain distinct.
85. As a Tool user, I want `read` limited to bounded strict UTF-8 regular-file text, so that binary or oversized data is reported actionably.
86. As a Tool user, I want `edit` to apply unique non-overlapping literal replacements from one original snapshot, so that changes are precise and deterministic.
87. As a Tool user, I want `write` to report actual UTF-8 bytes and possible partial effects truthfully, so that mutation outcomes are not overstated.
88. As a Tool user, I want `bash` to preserve the host environment and logical Workspace while clearly disclaiming sandboxing, so that shell behavior is honest.
89. As a Tool user, I want large shell output truncated predictably with a private full-output spill path, so that useful output remains bounded without being lost.
90. As a Tool user, I want timeout, signal, nonzero exit, spawn denial, and output-infrastructure failures classified actionably, so that the Agent can respond appropriately.
91. As a Tool user, I want same-file `edit` and `write` operations serialized while unrelated effects may run concurrently, so that mutations do not race on one target.
92. As an Extension author, I want built-in Tool names to be non-overridable, so that project code cannot shadow product behavior.
93. As a release engineer, I want every parity, adaptation, deviation, journey, public surface, and platform obligation indexed by one Conformance Obligation Matrix, so that release coverage fails closed.
94. As a release engineer, I want exact-parity observations captured from the fixed Reference Revision, so that moving source or current omh output cannot redefine success.
95. As a release engineer, I want deterministic tests to drive only the installed Candidate Wheel through public seams, so that source-tree reachability cannot hide packaging defects.
96. As a release engineer, I want concurrency tests to use explicit barriers rather than sleeps or flaky retries, so that race evidence is repeatable.
97. As a release engineer, I want real JSONL filesystem, concurrent-manager, process-tree, signal, and terminal resources used on each Release Row, so that platform behavior is actually proven.
98. As a release engineer, I want one byte-reproducible universal Candidate Wheel installed offline on every Release Row, so that all evidence binds to identical artifact bytes.
99. As a release engineer, I want a separate named-human Live Provider Gate, so that real DeepSeek compatibility and credential handling are proven without putting secrets in ordinary CI.
100. As a release engineer, I want publication authorized by a hash-bound evidence bundle and annotated tag, so that no rebuilt or substituted artifact can inherit approval.

## Implementation Decisions

### Scope, authority, and parity

- The v0 Release Surface is the complete contract compiled from the 13 resolved Wayfinder decisions. The fixed Reference Revision is immutable comparison evidence; the local omh decisions and this specification are product authority.
- Behavioral Parity compares canonical inputs at named public interfaces across Admission, causally ordered Lifecycle, Terminal classification and returned values, External effects, and post-settlement Continuity. Each interface declares its concrete observation envelope.
- Differences inside an observation envelope are permitted only by a complete Python Adaptation or Accepted Behavioral Deviation in the closed Parity Ledger. Surface Exclusions make no v0 parity promise. Every other difference is a Parity Gap.
- Python Adaptations may represent a selected behavior differently but may not merge semantic distinctions. There is no general PEP 8 renaming permission. Accepted Behavioral Deviations deliberately change covered behavior and remain narrowly scoped.
- The closed Python Adaptation keys are `PA:omh-python-import-roots`, `PA:python-illegal-identifier`, `PA:python-async-resource-carriers`, `PA:readonly-abort-signal`, `PA:frozen-public-value-records`, `PA:none-for-absent-optional-field`, `PA:json-schema-mapping`, `PA:strict-python-tool-callable`, `PA:type-preserving-canonical-json`, `PA:closed-lifecycle-error-carrier`, and `PA:python-extension-entrypoint`.
- The closed active Accepted Behavioral Deviation keys are `ABD:omh-project-config-root`, `ABD:omh-user-session-root`, `ABD:reject-missing-model-at-session-creation`, `ABD:repl-reject-busy-ordinary-message`, `ABD:require-named-check-for-success`, `ABD:atomic-session-construction`, `ABD:distinct-listener-registrations`, `ABD:managed-event-stream`, `ABD:managed-lifecycle-callbacks`, `ABD:managed-run-cancellation`, `ABD:managed-session-disposal`, `ABD:redacted-deterministic-tool-validation-errors`, `ABD:strict-toolcall-json-finalization`, `ABD:deepseek-request-preflight`, `ABD:strict-deepseek-usage-finalization`, `ABD:immutable-loop-context`, `ABD:owned-agent-state-mutation`, `ABD:redacted-tool-runtime-errors`, `ABD:strict-agent-run-admission`, `ABD:strict-tool-call-correlation`, `ABD:fixed-session-prompt-resources`, `ABD:fixed-session-system-prompt`, `ABD:strict-prompt-resource-invocation`, `ABD:atomic-extension-initialization`, `ABD:deterministic-extension-discovery`, `ABD:explicit-project-resource-trust`, `ABD:fail-closed-extension-handlers`, `ABD:fixed-extension-registration`, `ABD:session-scoped-extension-modules`, `ABD:snapshot-extension-context`, `ABD:actionable-built-in-outcomes`, `ABD:literal-edit-text`, `ABD:literal-tool-paths`, `ABD:non-overridable-built-in-tools`, `ABD:precomputed-edit-result`, `ABD:private-bash-output-spill`, `ABD:strict-edit-arguments`, `ABD:strict-read-pagination`, `ABD:strict-text-file-read`, `ABD:truthful-bash-signal-termination`, `ABD:truthful-write-result`, `ABD:unmodified-host-shell-environment`, `ABD:deterministic-one-shot-terminal-carrier`, `ABD:explicit-command-mode-admission`, `ABD:plain-repl-transcript`, `ABD:redacted-command-diagnostics`, `ABD:repl-control-inputs`, and `ABD:repl-signal-status`. The withdrawn keys `ABD:durable-session-commit-order`, `ABD:atomic-session-image-publication`, `ABD:fail-closed-session-persistence`, `ABD:strict-session-cwd-admission`, `ABD:exclusive-product-session-ownership`, and `ABD:exact-id-cli-recovery` are non-reusable tombstones rather than active Ledger members.
- The resolved decision owning each Ledger key remains authority for its complete seven-field record, fixed Reference citations, comparator, normalization, and executable evidence. The specification neither broadens nor duplicates a key.

### Distribution and public interfaces

- v0 ships one atomic, versioned `omh` Distribution. It installs the `omh` console command and the separately importable `oh_my_llm`, `oh_my_core`, and `oh_my_coding_agent` Public Import Packages. They are not separately versioned or installable.
- The Public Import Surface is a closed allowlist. `oh_my_core` and `oh_my_coding_agent` have root-only public paths. `oh_my_llm` has its root and only the selected DeepSeek Provider child path. Implementation modules and cross-layer re-exports are not public.
- Retained class, method, field, parameter, and event names remain one-to-one. `Agent.continue_()` is the sole Python-illegal-identifier spelling adaptation and has no `continue` alias.
- `oh_my_llm` exposes an explicit `Models`/`MutableModels` collection rather than a package-global registry. `Models` owns Provider and Model lookup, authentication observation, stream, completion, and Simple operations; `MutableModels` adds only Provider registration.
- `Provider` is a closed factory-produced handle with read-only identity. v0 exposes no Adapter-authoring interface, custom transport configuration, or dynamic Provider catalog.
- The public LLM value and stream surface is limited to text, Tool Calls, standard Messages, Usage, selected options, closed Assistant Message events, owned EventStreams, read-only cancellation observation, Tool validation, and the deterministic Faux helpers.
- Faux supports deterministic text and Tool Call scenarios through the public `Models` seam. It has no public custom catalog or Adapter escape and cannot be selected as a Product Session Model.
- `oh_my_core` exposes `agentLoop`, `agentLoopContinue`, `runAgentLoop`, and `runAgentLoopContinue`. All four use one Run/Turn implementation; they differ only in EventStream versus awaited event-sink carrier and prompt versus continuation seed.
- `Agent` exposes state, active signal observation, subscription, prompt, `continue_`, abort, idle settlement, and reset. Construction requires explicit valid initial Model and stream function dependencies.
- `oh_my_coding_agent` exposes lazy `createAgentSession()`, frozen creation options, a result wrapper, the final factory-produced `AgentSession`, caller-visible `SessionManager`, typed Session carriers, closed Session events/listeners, and prompt options. It exposes no internal Agent, arbitrary persistence Adapter, registry, resource loader, or Extension runner.
- `CreateAgentSessionOptions.sessionManager` accepts an existing manager; omission creates `SessionManager.create(cwd)`. `AgentSession.sessionManager` exposes that identical manager together with its projected Session id, file, name, active-path messages, tree-navigation, and compaction controls.
- `SessionManager` exposes the accepted create/open/recent/in-memory/fork/list factories; JSONL path, identity, metadata, entry/tree/leaf/context reads; append, branch/reset, label, path-extraction, and new-Session operations. Its public typed carriers fix `CURRENT_SESSION_VERSION = 3`; free persistence, parsing, migration, indexing, and compaction-algorithm helpers remain private.

### Public values, schemas, and errors

- Public Value Records are frozen, slotted, keyword-only nominal records with constructor-time recursive validation and owned immutable containers. Tagged variants fix their discriminator; unions and finite domains are closed.
- Equality and hashing preserve carrier distinctions, including integer versus float and positive versus negative float zero. Invalid carriers raise `TypeError`; admitted carriers violating domain invariants raise `ValueError` with a deterministic field path.
- `JSONValue` admits only null, bool, Unicode strings, safe integers, finite binary64 floats, tuples, and read-only string-keyed mappings. Mutable aliases, non-string keys, cycles, unsafe numbers, surrogate code points, bytes, and arbitrary objects are rejected.
- The private canonical codec is type-preserving and byte-canonical: strict UTF-8, Unicode-code-point key order, minimal escaping, safe integer spelling, shortest round-trip float spelling, and explicit signed zero. It rejects duplicate keys, noncanonical bytes, unknown fields, invalid tags, and invalid numeric carriers.
- Public content is closed to `TextContent` and `ToolCall`; public Messages are exactly User, Assistant, and Tool Result Messages. Stop reasons remain distinct for stop, length, Tool use, error, and abort.
- Assistant Message streaming uses a closed start/delta/end/done/error protocol with cumulative immutable partials. Tool Call argument deltas must finalize as one strict JSON object; no repair or permissive parser is allowed.
- Tool schemas use a closed direct-mapping subset of JSON Schema Draft 2020-12 with a linear-time ECMAScript/RE2 Pattern Subset. Unknown or misplaced keywords, recursive/reference schemas, and unsupported vocabularies fail admission.
- Tool validation is pure and returns a fresh mutable working tree after deterministic conversion. Failures aggregate stable redacted pointer/keyword issues and never expose argument values or schema text.
- `AgentTool` is a final identity-bearing executable Adapter. Its effect-free argument preparation precedes validation; execution receives exactly the Tool Call id, fresh validated parameters, active `AbortSignal`, and synchronous update callback.
- `AgentToolResult` contains text-only content, required JSON details, and optional termination intent. It has no error bit. A valid returned result is always a normal Tool Outcome; Runtime rejection, raise, invalid return/update, or cancellation produces the separate Tool Failure projection.
- Model/Provider failures use a closed `ModelsError` classification. Lifecycle ownership failures use a closed `LifecycleError` classification with ordered private causes where required. Real caller task cancellation remains `asyncio.CancelledError` after owned cleanup.

### Async, streams, callbacks, and cancellation

- Selected asynchronous operations are lazy. Calling a coroutine-returning operation performs no admission, state change, or effect; its first await activates it. Stream factories are synchronous and effect-free until first entry, iteration, or result wait.
- The module creating an operation owns its task, mutable cancellation source, terminal classification, and cleanup barrier. Public callers receive only selected awaitable, EventStream, listener, or read-only signal carriers.
- An EventStream has one producer, one consuming-iterator right, an unbounded lossless FIFO, one terminal settlement, and multiple independent result observers. A second consumer fails before receiving an event.
- Stream close, early context exit, consumer cancellation, or cancellation of an operation-owning result waiter requests cancellation and shields cleanup to settlement. Normal exhaustion releases buffered history; unconfirmed cleanup never fabricates terminal state.
- One active Run supplies the same read-only `AbortSignal` to Agent observation, listeners, the stream function, and Tool execution. Only the owner may transition it to aborted. Cancelling a signal waiter affects only that waiter.
- Listeners and event sinks are awaited sequentially in registration order from a fixed dispatch snapshot after public state reduction. Every selected callback in the snapshot is attempted; callback failure stops later events/effects and settles owned work.
- Each subscription call creates an independent record even for the same callable. Unsubscribe is idempotent and removes only that record.
- Owner cancellation imposes a hard no-later-effect cutoff. It waits for every started Tool and accepted update to settle and creates cancelled Tool Results for remaining Calls as required for correlation.
- Agent and Product Session cleanup is fail-closed. If work cannot be confirmed stopped, the owner remains non-idle/closing and rejects reuse rather than publishing a synthetic terminal classification.

### Agent Run, Turn, Tool, and state lifecycle

- A Run is one admitted Agent-loop invocation from one `agent_start` to ordinary `agent_end` or a lifecycle-carrier failure. A Turn is one Assistant response and its complete Tool batch and source-ordered Tool Results.
- New-prompt Run results contain seed Messages plus newly produced Messages. Continuation results contain only newly produced Messages. The supplied Context is copied and never mutated.
- EventStream result, awaited low-level result, and `AgentEnd.messages` are the identical immutable Run suffix. Stateful Agent history is derived only by reducing events.
- Prompt and continuation admission validates carriers, nonempty seeds, effective tail roles, and busy state before Run ownership, timestamps, events, state mutation, or effects.
- Agent owns one live state identity. Seed/configuration fields may be replaced atomically only while idle; lifecycle fields are read-only. Reset while idle clears history/error state but preserves Model, Tools, and system prompt.
- Agent events form a closed immutable protocol for Agent, Turn, Message, and Tool execution start/update/end observations. State reduction precedes delivery. During terminal listeners the Run remains streaming; idle is committed only after those barriers settle.
- Model/Provider/stream failure becomes one ordinary error Assistant terminal value, produces no retry, and permits a fresh caller-started Run after settlement.
- Every Tool batch performs effect-free correlation, lookup, argument-preparation, and validation preflight in source order before any approved effect begins. Empty or duplicate Tool Call ids and ambiguous Tool lookup fail predictably without invoking affected Tools.
- Omitted global Tool execution defaults to parallel. Explicit sequential mode or any called Tool requiring sequential execution serializes the complete batch. Parallel updates may interleave, but final Tool Result Messages always project in original Call order.
- Valid Tool updates are snapshotted into a per-Tool FIFO. Invalid updates or final values become Tool Failures. Raw exception text, arguments, schemas, paths, and stack traces never enter public Tool events or history.
- A nonempty batch ends the Run only when every finalized Outcome is successful and explicitly requests termination. There is no hidden Turn, token, cost, or retry limit.
- Clean cancellation preserves confirmed history and outcomes. It settles unconfirmed Tool Calls with correlated cancelled results, completes the Turn, appends an aborted Assistant tail when needed, and ends normally only after owned cleanup succeeds.

### Product Sessions, durability, and recovery

- `createAgentSession()` is the sole programmatic construction seam. It is lazy and privately owns every acquired live resource until one fully initialized result is published. Pre-publication failure or cancellation performs reverse-order shielded live-resource cleanup and exposes no partial `AgentSession`; it does not promise persistence rollback.
- `oh_my_coding_agent.SessionManager` is the sole public persistence seam. Supplying `CreateAgentSessionOptions.sessionManager` preserves object identity, while omission creates `SessionManager.create(cwd)`. `AgentSession.sessionManager` exposes that same live manager rather than copying, wrapping, hiding, or exclusively transferring it.
- A manager uses either local JSONL persistence or in-memory persistence. `SessionManager.inMemory()` performs no Session-directory or file I/O and never appears in discovery; both modes retain the same header, entry tree, leaf, branch, and compaction semantics. v0 exposes no arbitrary persistence Adapter.
- Path carriers follow the accepted tilde/file-URL and lexical-resolution rules. The default persistent location is `~/.omh/agent/sessions/--<encoded-cwd>--/<timestamp>_<session-id>.jsonl`; explicit `sessionDir` and `open(path)` bypass that layout. Operational `AgentSession` cwd may differ from a supplied manager's header cwd.
- `NewSessionOptions.id` accepts an admitted caller-chosen id for `create`, `inMemory`, `forkFrom`, and `newSession`; omission generates UUIDv7. Explicit ids are preserved exactly, are not restricted to UUIDs, and are not promised globally unique across projects or custom Session directories.
- One Session file contains a version-3 header followed by immutable typed entries with stable `id`, `parentId`, and `timestamp`. Parent links form the complete append-only tree; a manager-local leaf chooses the active root-to-leaf path. Credentials, Tool callables, Extension instances/code, Prompt Resource bytes, effective system prompt, and live handles are not persisted.
- A new persistent manager creates its directory, identity, header, candidate path, indexes, tree, and `flushed=False` state in memory without creating the file. Initial Model/Thinking entries and the first User Message may remain in memory. The first Assistant Message exclusively creates the file and writes the complete header-plus-entry prefix.
- After first flush, append mutates manager entries, indexes, labels, and leaf before appending one JSON line. A write failure propagates without rollback, closing, incomplete marker, automatic retry, or a confirmed-durable-prefix projection; the live manager may continue from state absent on disk.
- Whole-file rewrite is limited to initializing an existing empty explicit path, registered local-format migration, and materializing a selected tree path as a new Session. It directly truncates and writes the target and may leave a partial file on failure. Ordinary append, branch, compaction, open, and recovery do not rewrite or clean history, and omh promises no Pi-file compatibility.
- Physical-line recovery skips blank and JSON-syntax-malformed lines, continues to later parseable lines, accepts a complete final JSON value without LF, and skips a crash-partial final line. The first parsed value must be a Session header with a string id; otherwise a physically nonempty file fails unchanged. Recovery rebuilds indexes, labels, tree, and leaf from the parsed sequence, including persisted entries from an incomplete Run.
- Recovery has no settled marker, complete-Run gate, effect replay, repair, or rollback. Persisted entries survive, an unpersisted tail is lost, and interrupted Model or Tool work is never redispatched; continued work requires a fresh caller prompt.
- Opening or sharing a Session path acquires no continuing exclusive lease and has no owner-busy error. Multiple managers may hold stale snapshots and append to one path. v0 promises neither safe multi-writer coordination nor deterministic cross-process ordering; later recovery follows parseable physical file order.
- Branch/reset moves only the manager-local leaf; the next append records divergence without deleting old entries. Path extraction and `forkFrom` create independent Sessions while preserving source history. Labels and Session metadata remain ordinary append-only entries.
- Compaction appends a summary, retained-tail boundary, and pre-compaction token count without deleting full history. Manual and automatic threshold/overflow compaction rebuild active Model context; only successful overflow compaction may internally continue the same prompt once. Cancellation or failure appends no compaction result and adds no general retry, queue, or steering policy.
- `AgentSession.prompt()` expands at most one explicit Prompt Resource, appends the User Message through its manager, and drives one Agent Run. `messages` is the compaction-aware active-path Agent context rather than the complete tree. Session events project Core events and add compaction observations plus `AgentSettled`; Agent-end messages remain only the Run suffix.
- `abort()` and `waitForIdle()` capture only current live work. `dispose()` closes admission, settles active work, and cleans live resources; it owns no Session-file lease, rollback, flush, or recoverability guarantee. Concurrent callers share an attempt and later calls retry only unresolved live-resource cleanup.

### Prompt Resources and Python Extensions

- Project Resource Trust is a construction-local boolean defaulting false. False performs no project Skill, Prompt Template, or Python Extension enumeration. Trust is never persisted or inherited and grants no Tool/filesystem authority.
- Trusted Prompt Resource discovery is limited to direct project-owned Skill and Prompt Template entries under the omh configuration namespace. Selected directories and entries may not be symlinks. Discovery uses Unicode-code-point name order.
- The complete Prompt Resource set is strict-UTF-8 decoded, schema-validated, and snapshotted atomically per construction. Invalid selected resources reject the complete Session. Mid-Session file changes do not alter behavior; recovery creates a fresh admitted snapshot.
- Skill and Prompt Template invocation has one closed command grammar. Missing or malformed command-shaped resources reject before Session-entry append or effects. Expansion is deterministic, nonrecursive, performs no shell/environment evaluation, and persists only resulting User Message text.
- One private deterministic builder creates the fixed six-section Product Session system prompt from omh identity, Tool summaries, working/verification guidance, admitted Skill descriptors, creation date, and normalized cwd. It runs once per construction; recovery rebuilds from current admitted resources.
- Trusted Python Extension discovery is limited to direct nonsymlink `.py` entries whose names satisfy the accepted closed grammar. The entire set is validated and snapshotted in deterministic order before module execution.
- Every Session construction executes a fresh private module generation from strict-UTF-8 source. The sole entrypoint is `extension(api)`, which may register handlers and Tools only during initialization.
- `ExtensionAPI` exposes only fixed event registration and Tool registration. Registrations freeze after factory settlement. Built-in Tool-name collisions reject complete Session construction; distinct Extension Tools follow deterministic discovery/registration order.
- Extension handlers receive immutable per-invocation snapshot Context and run serially after manager/state reduction but before public listeners or the next effect. Handler failure stops progression and raises a redacted hook lifecycle failure after cleanup.
- `session_start` handlers run forward before system-prompt finalization or `AgentSession` publication. `session_shutdown` handlers run in reverse order after active Run settlement; all are attempted, successes are remembered, and later disposal retries only unresolved handlers.
- Extension initialization is atomic only for omh-owned modules, registrations, live tasks, and resources. Extension code runs in-process with user permissions and no sandbox; arbitrary external side effects performed by Extension code cannot be rolled back, and SessionManager persistence retains its separately accepted non-rollback behavior.

### Built-in Tools and Workspace

- Every Product Session owns exactly four built-ins in canonical order: `read`, `bash`, `edit`, and `write`. All are always active. Extension Tools follow them; there is no Tool selection, allow/deny, approval, sandbox, remote-operation, or override configuration.
- Workspace is the normalized logical Product Session cwd. Relative paths and initial shell cwd use it, but absolute paths, `..`, symlinks, shell `cd`, child processes, and host permissions remain available. Workspace is not a containment boundary.
- File Tool paths are nonempty NUL-free literal strings with lexical normalization only. No trimming, tilde/environment/URL expansion, alternate probing, `realpath` rewrite, case folding, or Unicode normalization changes caller identity.
- Built-ins participate in parallel Tool batches. `edit` and `write` additionally share a private process-local queue for the same effective target; unrelated files, reads, and shell commands remain concurrent.
- `read` accepts a path and optional positive safe line offset/limit. It reads only regular-file strict-UTF-8 text, preserves accepted bytes, uses one-based logical-line addressing, and returns at most 2,000 complete lines or 51,200 UTF-8 bytes. Expected file and range negatives are actionable Tool Outcomes.
- `write` accepts a path and Unicode-scalar content, creates missing parents, and performs an ordinary create-or-truncate host write. It truthfully reports actual UTF-8 bytes and any possible partial-effect phase. It provides no atomic replace, backup, fsync, rollback, or retry promise.
- `edit` accepts one path and a nonempty list of exact old/new text replacements. Every old text must match one unique non-overlapping range in the original snapshot. The complete result and pinned-compatible diff are precomputed before the sole overwrite. Missing, nonunique, overlapping, no-change, or recognized I/O conditions are actionable Outcomes.
- `bash` accepts a command and optional bounded positive timeout. It selects the accepted host shell order, starts from Workspace, and receives an exact fresh copy of the current environment without product injection. It is noninteractive and has no per-call cwd, environment, stdin, login, hook, or operations override.
- Shell stdout and stderr are merged in Runtime chunk-arrival order with per-pipe order only. Display retains the last 2,000 logical lines or 51,200 decoded bytes. Truncation stores complete raw merged bytes in one unpredictable exclusive owner-only spill artifact and exposes its path in the Outcome.
- `bash` emits validated cumulative output updates. Exit zero, nonzero exit, external signal, timeout, invalid command, unavailable Workspace/shell, spawn denial, and recognized output collection/storage failures have truthful closed Outcome classifications. Cancellation kills and drains the owned process tree before settlement.
- Built-in names, schemas, labels, descriptions, parameter descriptions, and system-prompt summaries are fixed product metadata. Extensions cannot shadow, rename, disable, or reclassify a built-in.

### Command Modes

- v0 has exactly two text Command Modes over `AgentSession` and its selected `SessionManager`: interactive `omh` and one-shot `omh --print`. They introduce no second Agent, persistence, cancellation, recovery, or classification policy.
- The closed launcher flags are `--print`, `--cwd`, `--continue`/`-c`, `--session`, `--session-id`, `--no-session`, `--trust-project`, `--help`, and `--version`; `-c` is the sole short alias. Unknown, repeated, conflicting, or missing arguments fail before Session/resource effects.
- Interactive mode requires TTY stdin and stdout. One-shot accepts exactly one normalized source: one positional prompt, or complete strict-UTF-8 non-TTY stdin when no positional prompt is present. It never infers mode from TTY state.
- With no selector, the launcher creates a persistent manager and omission generates UUIDv7. `--session PATH_OR_ID` opens a path-shaped JSONL carrier or searches exact then prefix id; `--continue`/`-c` continues the current project's recent Session or creates when none exists; `--session-id ID` opens an exact current-project match or warns and creates that admitted id when absent; `--no-session` selects `SessionManager.inMemory()` and may combine with `--session-id`. The persistent selectors are mutually exclusive, and `--no-session` conflicts with `--session`/`--continue`.
- The launcher emits `session <ID> new|recovered` immediately after `AgentSession` publication and before any Run. `new` includes fresh persistent and in-memory Sessions; `recovered` includes an existing selected Session.
- Interactive construction without the trust flag asks for a fresh explicit yes/no decision before project-resource discovery. One-shot never prompts; omission is untrusted. Trust-prompt cancellation/EOF performs no Session construction.
- Each nonempty normalized REPL line maps to exactly one `AgentSession.prompt()`. Empty input is a local no-op. Busy nonempty input is discarded with a stable diagnostic and no queue or steering effect.
- The REPL uses one awaited Session listener to produce an append-only strict-UTF-8 transcript. It renders incremental Assistant Text, Tool start, Tool Outcome/Failure, and one confirmed terminal Run classification. It omits Tool updates, Turn events, raw Agent start/end, and private causes.
- Transcript and diagnostic encoding visibly escapes terminal controls and unsafe metadata characters. Structured values use canonical JSON and never Python `repr()`.
- Active Ctrl-C requests and joins Session cancellation; idle Ctrl-C clears the line; empty-line EOF begins orderly disposal. Terminating signals fix conventional statuses and join the one disposal path without a force-exit timeout.
- One-shot subscribes to no renderer. After settlement, stdout contains only terminal Assistant Text; stderr carries identity and stable nonordinary diagnostics. Unconfirmed settlement emits no stdout. Exit zero means Command Mode completion, not verified modification success.
- Every published `AgentSession` is disposed exactly once by launcher ownership. Successful disposal leaves its manager's accepted persistent or in-memory state unchanged. Cleanup failure may upgrade an ordinary status but never replace a primary signal status.
- JSON/JSONL and RPC Command Modes, rich TUI behavior, color/Markdown/widgets, in-REPL launcher commands, shell shorthand, the rich resume picker, CLI fork/session-directory/tree/list controls, recovery hints, and dynamic configuration flags are excluded. JSONL persistence, ephemeral Sessions, and programmatic branch/fork/list/tree operations remain included through `SessionManager`.

### Dependencies, packaging, and release authority

- v0 requires CPython `>=3.12,<3.14`. Direct Runtime dependencies are exactly `httpx==0.28.1`, `google-re2==1.1.20251105`, and `PyYAML==6.0.3`; the Build dependency is exactly `hatchling==1.32.0`. The full transitive resolution is committed and every development, conformance, build, and release command runs locked.
- HTTPX is private raw-byte HTTPS transport for DeepSeek only, with zero transport retries, no environment proxy trust, no redirects, identity encoding, and no timeout. omh owns request encoding, SSE/JSON parsing, Usage validation, redaction, cancellation, and the at-most-one-attempt proof.
- Google RE2 is private Pattern matching only. omh owns schema admission, traversal, conversion, deterministic errors, and redaction.
- PyYAML is private frontmatter syntax parsing only. omh rejects unsupported YAML features and owns field, scalar-style, name, body, UTF-8, and whole-set validation.
- Standard-library/private code owns asyncio lifecycles, the public-`SessionManager`/local-JSONL persistence implementation, canonical JSON, UUIDv7, Extension modules, CLI/REPL behavior, and built-in file/process Tools. No SQLite Session store, SQLAlchemy, `aiosqlite`, ORM/migration framework, Pi-file compatibility library, or exclusive Session lease is added, and no framework may take over public semantics.
- Hatchling builds one universal `omh-<version>-py3-none-any.whl`; v0 publishes no sdist or alternative wheel. A pinned Candidate Build Row builds twice from clean exports with identical complete bytes and SHA-256.
- Exactly two Release Rows are supported: macOS 26 arm64 on CPython 3.12 and 3.13. Ubuntu and every other Linux row are outside v0. Each selected row installs the identical Candidate Wheel offline from a locked prebuilt wheelhouse and runs the complete deterministic suite plus platform-specific evidence.
- The Conformance Obligation Matrix is the sole release coverage index. It fails on a missing/mismatched authority, comparator, observation, evidence class, case, public member, required journey, Ledger record, platform obligation, or known Parity Gap, and on any orphan executable case.
- The committed Reference Observation Corpus records canonical Reference A/L/T/E/C observations, comparator/normalization, fixed revision provenance, and case ids. It is reproducibly recaptured from a clean fixed-revision export and is never regenerated from current omh output.
- The Deterministic Conformance Suite drives only the installed Candidate Wheel through the Public Import Surface and `omh` Command Modes. It uses private internal controls for time, entropy, UUIDs, Model transport, and injected failures; no public test hook is added.
- Deterministic cases use real platform resources for JSONL file creation/append/rewrite/recovery, concurrent managers without a lease, ordinary file behavior, subprocess trees, signals, and terminals. They use explicit barriers rather than wall-clock sleeps and permit no skip, xfail, flaky retry, order dependency, or external network request.
- The human-owned Live Provider Gate uses the exact Candidate Wheel and a newly issued revocable credential to prove real non-thinking DeepSeek streaming, cancellation followed by reuse, and one clean programmatic core journey with a named successful check. Evidence is redacted and binds to the candidate; Faux or earlier evidence cannot substitute.
- One immutable Release Evidence Bundle binds candidate commit/version, Candidate Wheel bytes/hash, lock, Matrix, corpus, reproducible build, every Release Row, Live Provider Gate, zero unresolved Parity Gaps, and named-human approval. An annotated tag grants the Publish Right for exactly that wheel; publication never rebuilds or substitutes it.

## Testing Decisions

- The accepted behavioral test boundary is the installed `omh` Distribution. Tests enter only through the three Public Import Surfaces and the two `omh` Command Modes. They do not import source-checkout implementation functions.
- Good tests assert externally observable Admission, ordered Lifecycle, Terminal classification/value/failure ownership, External effects, and post-settlement Continuity. They do not assert private data structures, helper calls, task layout, private indexes/parsers, or other implementation details.
- Controlled nondeterminism uses private internal seams only. Time, entropy, UUIDs, Model transport, failure injection, and concurrency barriers may be controlled without adding public test APIs.
- The Conformance Obligation Matrix maps every executable case to its local authority, public interface or Command Mode, canonical scenario, A/L/T/E/C observations, comparator/normalization, and evidence class. Coverage percentages and test names alone are not evidence.
- Exact-parity tests compare normalized public observations against the fixed Reference Observation Corpus. PA and ABD tests compare the fixed Reference observation with the separately authoritative omh expectation. Local release policies use an explicit `not_applicable` Reference reason.
- Public-interface tests cover installed metadata, closed import allowlists, exact public members/signatures, excluded aliases/re-exports, immutable values, strict schemas, errors, all four low-level loops, Agent state/events, Product Session behavior, and CLI grammar/projection.
- Journey tests cover the bounded verified code change through both interactive and programmatic product entries; recoverable Tool failure; clean cancellation and reuse; deterministic Provider failure and reuse; missing authentication; busy rejection; JSONL parsed-prefix recovery including an incomplete Run; branch, fork, explicit/automatic compaction, ephemeral mode; one project Skill; and one project Python Extension.
- Stream/lifecycle tests cover lazy activation, one-consumer FIFO delivery, independent result waiters, terminal identity, early close, callback snapshots/order/failure, cancellation cutoffs, Tool scheduling, atomic Session construction, disposal retry, and non-idle state while work is unconfirmed.
- Value tests cover carrier-sensitive equality/hash, absence versus null, numeric distinctions, canonical byte round trips, duplicate/unknown rejection, Message/event grammar, strict Tool Call JSON finalization, Tool Schema/Pattern admission, conversions, and redacted aggregate validation.
- Provider tests use in-process or loopback fixtures to cover exact DeepSeek request bytes, fragmented text/Tool streams, finish/failure mapping, Usage/cost, environment re-read, cancellation, redaction, zero retry, and post-failure reuse. They make no external network request.
- Agent/Tool tests cover Run/Turn traces, admission precedence, state-before-listener order, Context immutability, Tool correlation and preflight, sequential/parallel execution, update ordering, Tool Outcome versus Tool Failure, cancellation synthesis, terminal result identity, and reuse.
- Product Session tests use real temporary JSONL files and in-memory managers to cover path/cwd precedence, generated and caller-chosen identities, lazy first flush, in-memory-first append, raw write failures, limited rewrites/migration, best-effort line parsing, parsed-prefix recovery including incomplete Runs, tree/leaf/labels, branch/fork, compaction, resource rebinding, and cleanup. Concurrent-manager cases prove the absence of an owner-busy lease while making no safe-multi-writer claim.
- Prompt Resource and Extension tests cover zero enumeration when untrusted, deterministic direct discovery, symlink/name/frontmatter/source rejection, immutable snapshots, expansion, fixed system prompt, fresh module generations, registration freeze, handler cutoffs, atomic initialization, and reverse retryable shutdown.
- Built-in Tool tests use real temporary files and subprocess trees to cover literal paths, bounded UTF-8 reads, exact edits/diffs, truthful writes, same-file serialization, shell environment, output merge/truncation/spill, timeout, signals, cancellation, and reserved-name collisions.
- CLI tests use real terminal and signal resources on applicable Release Rows to cover pure help/version, strict source and TTY admission, trust prompt, identity record, REPL transcript/encoding, busy input, one-shot bytes/status, signal settlement, diagnostics, recovery, and exactly-once disposal.
- Concurrency and cancellation tests use pause/release synchronization and explicit ownership barriers. Wall-clock sleeps, flaky retries, order-dependent cases, skips, and xfails are forbidden. A bounded watchdog may only report a hang as failure.
- Every supported Release Row installs the same Candidate Wheel outside the checkout from locked prebuilt artifacts and runs the full Deterministic Conformance Suite. Platform-specific cases use real JSONL filesystem, concurrent-manager, subprocess, signal, and terminal behavior rather than fakes.
- Real DeepSeek behavior is tested only by the separate human-owned Live Provider Gate. The live gate retains redacted candidate-bound evidence and does not expose credentials or raw authenticated traffic.
- The closest prior art is the resolved v0 decision suite itself: each decision's Verification clauses and complete Parity Ledger records define canonical inputs, observation envelopes, comparison rules, and required executable evidence. The implementation test layout must preserve that authority mapping rather than inventing a parallel checklist.

## Out of Scope

- Any behavior outside the bounded v0 Release Surface or any attempt to complete the full Target Compatibility Surface.
- `AgentHarness`, every `harness/**` export, the experimental orchestrator, a standalone reusable TUI package, and Reference product identity in omh public names.
- Rich terminal UI, themes, color, Markdown layout, spinners, folding, widgets, terminal-width adaptation, image display, advanced selectors, and custom renderers.
- JSON/JSONL or RPC Command Modes, an importable launcher, reusable terminal framework, multiple launch messages, `@file`, shell shorthand, in-REPL launcher commands, the rich resume picker, or CLI fork/session-directory/tree/list controls.
- Image input or generation, public thinking/reasoning content or controls, reasoning events, and unsupported content coercion.
- Multiple real Providers or Models, dynamic discovery/catalog refresh, runtime Model switching, custom Provider authoring, custom transport/auth/base URL, OAuth, stored credentials, retry, backoff, or reconnect.
- In-flight steering, follow-up queues, autonomous Runs, hidden Turn/token/cost budgets, or automatic continuation after failure.
- Session import/export/share; arbitrary history edit/delete/merge; safe multi-writer coordination; deterministic cross-process ordering; an arbitrary persistence Adapter; and Pi-file compatibility. Public `SessionManager`, local JSONL/in-memory carriers, discovery/listing, tree/leaf navigation, branch/fork, and compaction are in scope.
- Recovery, replay, or redispatch of incomplete Model, Tool, Extension, or other effects after process failure.
- User-global, ancestor, legacy, or arbitrary Prompt Resource and Extension discovery; TypeScript Extensions; package plugin frameworks; dynamic reload; Extension sandboxes; and rollback of arbitrary Extension-owned side effects.
- Built-in Tool selection, disabling, overriding, approvals, sandboxing, remote execution, extra built-ins, image Tool content, bundled managed shells, filesystem abstraction, or automatic rollback of partial host effects.
- Public canonical-serialization, arbitrary-storage, scheduler, clock, UUID-source, transport, process, terminal, test-hook, or dependency-injection interfaces. The accepted typed `SessionManager` and JSONL carrier are the sole public persistence exception.
- Public schema builders, Pydantic/reflection, full JSON Schema, recursive/reference schemas, Python `re`, generic serialization registries, or permissive Tool Call repair.
- Telemetry, update checks, moving-HEAD compatibility, and mutable Reference evidence.
- An sdist, multiple publication artifacts, unlocked dependency resolution, native source-build fallback on Release Rows, unsupported OS/architecture/interpreter claims, PyPy, free-threaded CPython, WebAssembly, or mobile Python.
- Automatic key-bearing CI or any release claim based only on deterministic Faux/fixture evidence.
- Planning or implementing releases after v0.

## Further Notes

- The fixed Reference Revision is `0e6909f050eeb15e8f6c05185511f3788357ddb3`. It is comparison evidence, never mutable upstream authority.
- The accepted product identity is `omh`; Python public imports are `oh_my_llm`, `oh_my_core`, and `oh_my_coding_agent`; project-owned configuration and resources use `.omh/`.
- A Tool Outcome means every valid returned `AgentToolResult`, including expected negative domain results. A Tool Failure means Runtime rejection, raised execution, invalid result/update, or cancellation. This terminology must remain consistent in code, tests, diagnostics, and documentation.
- Workspace is logical project identity and working context, not a sandbox or authorization grant. Project Resource Trust governs project executable/instructional resources, not built-in Tool authority.
- Exact public names, event fields, stable diagnostics, Tool metadata, error templates, Parity Ledger records, Reference citations, and comparator details remain governed by their resolved decision authorities. Implementation tickets may slice delivery but may not reinterpret those contracts.
- This specification authorizes ticket planning, not implementation. Implementation work should begin only after an approved independently verifiable ticket DAG is published.

# Choose the v0 product journey

Type: grilling
Status: resolved
Blocked by: none

## Question

What exact end-to-end journey makes omh v0 a usable product rather than a collection of Python modules, and which user-visible capabilities must that journey exercise through the simple REPL and programmatic entry points?

## Answer

The **v0 Product Journey** is a bounded, verified code change in an existing local repository, backed by the Reference Revision's public `SessionManager`, local JSONL Session tree, persistence, and recovery behaviour rather than the former private settled-only Session Image contract. The code-change journey must be achievable through both the simple REPL and the `oh_my_coding_agent` programmatic session entry point. They promise the same user outcome, not identical REPL UX. `oh_my_core` and `oh_my_llm` remain independently callable and prove their own layer behaviour; they do not each reproduce the repository journey.

### Core code-change journey

A successful journey:

1. creates or selects a Session for the existing repository;
2. loads one resolved set of applicable repository instructions for both product entry points and treats those instructions as sources of working conventions, not text to execute mechanically;
3. accepts a bounded code-change request;
4. uses built-in repository capabilities to inspect the relevant context, change files, and run verification without depending on a Skill or Extension;
5. streams assistant text, Tool start and end observations, Tool success or failure, and one unambiguous terminal outcome;
6. after the final mutation, runs a specifically named check selected from the user request or an applicable repository convention and observes its Tool Result; and
7. reports what changed, which check ran, and whether that check passed.

A normally ended Run is not by itself a successful modification. A successful first check is sufficient: not every successful journey must fail first or require a later correction. If the named check cannot run or does not pass, omh reports unverified or failed verification and does not claim that the modification succeeded. A deterministic golden path may deliberately exercise red-to-green recovery.

One `AgentSession` admits at most one active Run. Once that Run is idle, the user may submit the next request and the active Session path retains the conversation needed for a correction. A new ordinary message submitted while a Run is busy is observably rejected; v0 has no in-flight steering or follow-up queue. This per-instance Run ownership does not imply exclusive ownership of a persistent Session file.

### Public SessionManager and local JSONL carrier

`oh_my_coding_agent.SessionManager` is the public v0 persistence and recovery authority. Callers may construct one and supply it through `CreateAgentSessionOptions.sessionManager`; omission defaults to `SessionManager.create(cwd)`. The resulting `AgentSession.sessionManager` exposes that same manager.

A persistent Session is a caller-visible local `.jsonl` file, not a private storage envelope. `SessionManager.open(path)` opens a specified path; managers and discovery expose current and candidate file paths. The omh JSONL format is a public carrier, but omh makes no product promise that it and Pi can mutually open each other's Session files and adds no import/export compatibility promise.

Each file contains one Session header followed by immutable typed entries with stable `id`, `parentId`, and `timestamp`. Parent links form an append-only tree; one leaf selects the active root-to-leaf path. `SessionManager` exposes all entries, the tree, leaf, and active path. Ordinary persistence appends entries rather than overwriting or deleting existing entries or abandoned branches.

### Branch, fork, and compaction journeys

v0 proves both forms of divergent continuation:

- **Branch:** move the active leaf to an existing entry in the same JSONL tree; the next append creates a new child while the old path remains intact.
- **Fork:** create a new independently appendable Session file and id from a selected root-to-leaf path, recording the source Session path in the new header.

`SessionManager` exposes branch/reset-leaf, path extraction into a new Session, and source-path-to-target-`cwd` fork operations. These programmatic behaviours do not require a standalone TUI, a rich selector, or a new REPL command in this decision.

Compaction is a complete v0 behaviour, not merely a stored record. `SessionManager` appends a compaction entry containing the summary, retained-tail boundary, and pre-compaction token count while preserving full history in the tree. Model context uses the latest compaction on the active path, its retained tail, and later entries. `AgentSession` exposes explicit compaction and supports automatic threshold or overflow compaction. Successful overflow compaction may retry that same interrupted prompt without introducing a steering or follow-up queue. Evidence covers explicit compaction, automatic threshold/overflow compaction, recovered compacted context, and access to complete pre-compaction history.

### Selection and caller-chosen identity

The simple command surface includes these Reference-style selectors:

- `omh --session PATH_OR_ID` treats path-shaped input as a local JSONL path. Otherwise it searches exact then prefix id in the current project before other projects. A missing id fails rather than creating.
- `omh --continue`/`-c` and programmatic `SessionManager.continueRecent(cwd, sessionDir=None)` continue the current project's most recently modified Session, or create a new Session when none exists.
- `omh --session-id ID` opens an exact current-project match, or warns and creates a new Session with that id when absent.

These selectors are mutually exclusive. v0 does not add the rich `--resume` picker.

`SessionManager.create(...)`, `SessionManager.inMemory(...)`, `SessionManager.forkFrom(...)`, and `newSession(...)` accept `NewSessionOptions(id=...)`. An explicit id is preserved exactly and must be nonempty, start and end with an ASCII alphanumeric, and contain only ASCII alphanumerics plus `.`, `_`, and `-` internally. It is not trimmed, case-folded, or restricted to UUIDs. Omission generates UUIDv7. Syntax rejection precedes persistence effects. Ids are not promised globally unique across projects or custom Session directories.

### Best-effort recovery, incomplete Runs, and concurrency

JSONL recovery is best-effort and entry-based. Reading skips blank and JSON-syntax-malformed lines, continues to later valid lines, accepts a complete final line without a newline, and skips a crash-partial final line. The first successfully parsed entry must still be a Session header with a string id; otherwise a nonempty file fails without rewrite. Exact semantic validation of other syntactically valid entry fields remains downstream.

Recovery rebuilds the tree and active path from successfully parsed entries. It requires no settled marker, complete Session Image, or complete Run. Persisted User, Assistant, and Tool entries remain; an unpersisted tail is lost. omh does not automatically replay an interrupted Model or Tool effect or restore an in-flight steering/follow-up queue. The recovered caller continues with a new prompt.

Opening or recovering a Session file acquires no cross-instance, cross-thread, or cross-process exclusive lease. Multiple `SessionManager` instances may open and append to the same path without an owner-busy rejection. v0 promises Reference-style append behaviour, not safe multi-writer coordination, deterministic cross-process ordering, or repair of stale per-manager leaf/index state. Later recovery reads the parseable file order.

### Ephemeral Session journey

`SessionManager.inMemory(cwd=..., options=...)` creates an ephemeral manager with persistence disabled. `isPersisted()` is false, `getSessionFile()` is `None`, and no JSONL file is created, read, or appended. `omh --no-session` uses this mode and may carry a caller-chosen `--session-id`, whose identity lasts only for that process. Message history, the Session tree, branch, and compaction retain the same in-memory semantics. Process exit discards the state, and it never appears in `list()` or `listAll()` discovery. v0 exposes no arbitrary persistence Adapter: its two modes are local JSONL and in-memory.

### Required non-golden paths

The v0 Release Surface also proves these paths without requiring every golden-path execution to traverse them:

- **Recoverable Tool failure:** a Tool failure, such as a failed check, is observable and returns to the same Run as a Tool Result so the Agent can revise its work and eventually verify successfully. It is not a Provider failure and does not inherently terminate the Run.
- **User cancellation:** both product entry points expose a terminal cancelled outcome distinct from normal completion and Provider failure. Cancellation stops that Run; after it settles, the same `AgentSession` can accept another Run.
- **Provider failure:** a deterministically injected model-request failure during streaming, or the equivalent model-call path, may leave partial output visible but terminates that Run explicitly as Provider failure. Both product entry points distinguish it from normal completion and cancellation, and the `AgentSession` remains usable afterward. Missing credentials are an authentication failure, not this scenario.

Exact event shapes, cancellation mechanics, error categories, retry policy outside accepted compaction retry, partial-message persistence, and exception types remain downstream decisions.

### Model, authentication, and content bounds

At `AgentSession` creation, omh explicitly selects or resolves by a defined default one real model capable of Tool calling. The instance uses that model throughout its lifetime. If neither an explicit selection nor a valid default resolves, creation fails explicitly. v0 requires no model-selector UI, in-session model discovery, switching, cycling, or cross-model hand-off.

At least one real Provider must complete the core journey using a non-interactive environment-variable API key path. Deterministic scripted models provide repeatable evidence but do not replace that real-Provider evidence. Missing credentials fail actionably and never fall back silently to a fake model. Interactive login and OAuth are not required by the core journey; their v0 status remains for the model and authentication decision.

The v0 Product Journey is text-only and exposes no public thinking or reasoning blocks, streaming events, or levels. Unsupported image or reasoning content fails explicitly rather than being silently discarded. Provider-internal, unobservable reasoning is outside omh's public behaviour.

### Companion resource scenarios

These are independently verified against the same usable product but need not replay the complete repository journey:

- **Project Skill:** discover and invoke one project Skill and show that its instructions observably influence Agent behaviour.
- **Python Extension:** load one project-level `.py` Extension and use one observable capability it contributes.

The Skill and Extension scenarios are separate evidence: a Skill is an instructional resource and an Extension is executable code. Neither may stand in for the other. Each must be observable through at least one of the REPL or programmatic session entry points, but neither must cover both entry points or define a complete Skill standard or Extension interface.

### Project-resource trust

v0 project trust initially guards project-resource loading, matching the Reference Revision's seam rather than silently expanding into Tool authorization:

- the REPL obtains an explicit trust decision before loading project-level `.py` Extensions or project-level Skills;
- the programmatic session caller supplies an explicit trust decision, and omission never means that the current directory is trusted;
- the Python Extension companion scenario runs in a trusted project; and
- a core journey that loads no project-level Extension or Skill is not prevented from using built-in inspection, mutation, or verification solely because this resource trust decision is absent.

Whether command execution or file mutation should sit behind the same gate would be an intentional omh tightening and remains for the Tools and Workspace decision. Trust persistence, scope, revocation, untrusted read behaviour, and REPL presentation also remain downstream decisions.

### Explicit later-version surface

The v0 Release Surface excludes:

- image input and generation;
- public thinking or reasoning content and level controls;
- in-flight steering and follow-up queues;
- in-session model discovery, switching, cycling, and cross-model hand-off;
- Session import, export, and sharing;
- mutually opening Pi Session files as an omh product promise; and
- automatic replay or resumption of an in-flight Model request, Tool execution, or other unpersisted effect after process failure.

`AgentHarness`, every `harness/**` export, a standalone reusable TUI package, telemetry, update checks, and multiple real Providers also remain outside the v0 Release Surface under the map's standing scope.

The exact downstream Python value types and remaining names, other run modes, Provider and Model identity, built-in Tools, Workspace rules, resource discovery order, Extension lifecycle, full Session-entry schema and semantic validation, serialization adaptations, conformance rows, packaging, and release authorization remain owned by their downstream decision tickets. This reopened upstream decision supersedes their conflicting settled-only Session Image, linear-only Session, no-compaction, UUIDv7-only, and exclusive-live-owner assumptions; those tickets are intentionally not edited in this session.

## Comments

- 2026-08-22 — Accepted a public `oh_my_coding_agent.SessionManager` as the v0 persistence and recovery authority. Callers may construct it and pass it through `CreateAgentSessionOptions.sessionManager`; omission defaults to `SessionManager.create(cwd)`, and `AgentSession.sessionManager` exposes that same manager. This accepts the public seam and ownership only: JSONL paths, entry trees, branch/fork, compaction, selectors, malformed/incomplete recovery, live-owner concurrency, and ephemeral persistence remain for their separately enumerated bullets.
- 2026-08-22 — Accepted local `.jsonl` paths as public v0 persistence inputs and outputs rather than hiding persistence behind a private Session Image. `SessionManager.open(path)` opens by path, and managers plus session discovery expose current or candidate paths. The omh JSONL carrier is public, while entry/tree structure, append semantics, parser tolerance, default directories, normalization, and errors remain downstream details. omh does not promise that it and Pi can mutually open each other's Session files, nor does this add import/export compatibility.
- 2026-08-22 — Accepted an append-only Session tree. One persistent file has one Session header followed by immutable typed entries with stable `id`, `parentId`, and `timestamp`; parent links form the complete tree, while one leaf selects the active root-to-leaf path. `SessionManager` exposes all entries, the tree, leaf, and active path. Ordinary persistence appends rather than overwriting or deleting existing entries or branches. Branch/fork operations, compaction semantics, old-format migration, and Pi-file compatibility are not implied by this bullet.
- 2026-08-22 — Accepted public branch and fork capabilities. An in-file branch moves the active leaf to an existing entry so the next append creates a new child without deleting the old path. A fork creates a new independently appendable Session file and id from a selected root-to-leaf path, with source-path lineage in its header. `SessionManager` exposes the underlying branch/reset-leaf, path extraction, and source-path-to-target-cwd fork operations; v0 evidence covers both in-file branch and new-file fork. This does not add a standalone TUI or require REPL commands, and leaves high-level selectors, automatic branch summaries, and interaction design downstream.
- 2026-08-22 — Accepted full v0 compaction behavior rather than only a stored entry. `SessionManager` appends compaction summaries with the retained-tail boundary and pre-compaction token count while preserving the complete old tree; active Model context uses the latest compaction summary, retained tail, and later entries. `AgentSession` exposes explicit compaction and supports automatic threshold/overflow compaction, with successful overflow compaction allowed to retry the same interrupted prompt without introducing a steering/follow-up queue. Evidence covers explicit and automatic compaction, recovered compacted context, and continued access to complete pre-compaction history. Exact thresholds, summary prompt, settings, and REPL command shape remain downstream.
- 2026-08-22 — Accepted Reference-style Session selection. `omh --session PATH_OR_ID` treats path-shaped input as a local JSONL path and otherwise searches exact then prefix id in the current project before other projects; a missing id fails rather than creating. `omh --continue`/`-c` and `SessionManager.continueRecent(cwd, sessionDir=None)` continue the current project's most recently modified Session or create a new Session when none exists. `omh --session-id ID` opens an exact current-project match or warns and creates that id when absent. These selectors are mutually exclusive. v0 does not add the rich `--resume` picker; general caller-chosen-id admission remains the next bullet.
- 2026-08-22 — Accepted caller-chosen Session ids across `SessionManager.create(...)`, `SessionManager.inMemory(...)`, `SessionManager.forkFrom(...)`, and `newSession(...)` through `NewSessionOptions(id=...)`. An explicit id is preserved exactly, must be nonempty with ASCII-alphanumeric ends and only ASCII alphanumerics plus `.`, `_`, and `-` internally, and is validated before persistence effects; it is not trimmed, case-folded, or restricted to UUIDs. Omission generates UUIDv7. Ids are not promised globally unique across projects or custom Session directories, and this bullet establishes no live-owner lease.
- 2026-08-22 — Accepted best-effort JSONL recovery from parsed entries, including incomplete Runs. Reading skips blank and JSON-syntax-malformed lines, continues to later valid lines, accepts a complete final line without a newline, and skips a crash-partial final line. The first successfully parsed entry must still be a Session header with a string id; otherwise a nonempty file fails without rewrite. Recovery rebuilds the tree and active path from parsed entries without a settled marker, complete Session Image, or complete Run. Persisted User/Assistant/Tool entries remain, an unpersisted tail is lost, and no interrupted Model/Tool effect or steering/follow-up queue is replayed; a caller continues with a new prompt. Semantic validation of syntactically valid entry fields remains downstream.
- 2026-08-22 — Accepted Reference-style absence of an exclusive live-owner lease. Opening or recovering a Session file acquires no cross-instance, cross-thread, or cross-process lock, and multiple `SessionManager` instances may open and append to the same path without an owner-busy rejection. v0 promises append behavior, not safe multi-writer coordination, deterministic cross-process ordering, or repair of stale per-manager leaf/index state; later recovery reads the parseable file order. Per-`AgentSession` single-active-Run admission, cancellation ownership, and resource cleanup remain unchanged. This reverses the current `ABD:exclusive-product-session-ownership` direction without editing its owning downstream ticket in this session.
- 2026-08-22 — Accepted ephemeral persistence through `SessionManager.inMemory(cwd=..., options=...)` and `omh --no-session`. The manager reports `isPersisted()` false and no Session file, performs no JSONL I/O, may use a process-lifetime caller-chosen id, and retains the same in-memory history/tree/branch/compaction semantics until process exit. Ephemeral Sessions are absent from discovery, and v0 adds no arbitrary persistence Adapter beyond local JSONL and in-memory modes.

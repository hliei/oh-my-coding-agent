# Choose the v0 product journey

Type: grilling
Status: resolved
Blocked by: none

## Question

What exact end-to-end journey makes omh v0 a usable product rather than a collection of Python modules, and which user-visible capabilities must that journey exercise through the simple REPL and programmatic entry points?

## Answer

The **v0 Product Journey** is a bounded, verified code change in an existing local repository. The full journey must be achievable through both the simple REPL and the `oh_my_coding_agent` programmatic session entry point. They promise the same user outcome, not the same REPL UX. `oh_my_agent_core` and `oh_my_ai` remain independently callable and prove their own layer behaviour; they do not each reproduce the repository journey.

### Core journey

A successful journey:

1. creates a product session for the existing repository;
2. loads one resolved set of applicable repository instructions for both product entry points and treats those instructions as sources of working conventions, not text to execute mechanically;
3. accepts a bounded code-change request;
4. uses built-in repository capabilities to inspect the relevant context, change files, and run verification without depending on a skill or extension;
5. streams assistant text, tool start and end observations, tool success or failure, and one unambiguous terminal outcome;
6. after the final mutation, runs a specifically named check selected from the user request or an applicable repository convention and observes its tool result; and
7. reports what changed, which check ran, and whether that check passed.

A normally ended Run is not by itself a successful modification. A successful first check is sufficient: not every successful journey must fail first or require a later correction. If the named check cannot run or does not pass, omh must report unverified or failed verification and must not claim that the modification succeeded. A deterministic golden path may deliberately exercise red-to-green recovery.

The session is linearly multi-turn while the process remains alive. Once a Run is idle, the user may submit the next request and the session retains the conversation needed for a correction. A new ordinary message submitted while a Run is busy is observably rejected; v0 has no in-flight steering or follow-up queue.

### Required non-golden paths

The v0 Release Surface must also prove these paths without requiring every golden-path execution to traverse them:

- **Recoverable Tool failure:** a Tool failure, such as a failed check, is observable and returns to the same Run as a Tool Result so the Agent can revise its work and eventually verify successfully. It is not a Provider failure and does not inherently terminate the Run.
- **User cancellation:** both product entry points expose a terminal cancelled outcome distinct from normal completion and Provider failure. Cancellation stops that Run; after it settles, the same session can accept another Run.
- **Provider failure:** a deterministically injected model-request failure during streaming, or the equivalent model-call path, may leave partial output visible but terminates that Run explicitly as Provider failure. Both product entry points distinguish it from normal completion and cancellation, and the session remains usable afterward. Missing credentials are an authentication failure, not this scenario.

Exact event shapes, cancellation mechanics, error categories, retry policy, partial-message persistence, and exception types remain decisions for later tickets.

### Model, authentication, and content bounds

At product-session creation, omh explicitly selects or resolves by a defined default one real model capable of Tool calling. The session uses that model throughout its lifetime. If neither an explicit selection nor a valid default resolves, creation fails explicitly. v0 requires no model-selector UI, in-session model discovery, switching, cycling, or cross-model hand-off.

At least one real Provider must complete the core journey using a non-interactive environment-variable API key path. Deterministic scripted models provide repeatable evidence but do not replace that real-Provider evidence. Missing credentials fail actionably and never fall back silently to a fake model. Interactive login and OAuth are not required by the core journey; their v0 status remains for the model and authentication decision.

The v0 Product Journey is text-only and exposes no public thinking or reasoning blocks, streaming events, or levels. Unsupported image or reasoning content fails explicitly rather than being silently discarded. Provider-internal, unobservable reasoning is outside omh's public behaviour.

### Companion scenarios

These are independently verified against the same usable product but need not replay the complete repository journey:

- **Project Skill:** discover and invoke one project Skill and show that its instructions observably influence Agent behaviour.
- **Python Extension:** load one project-level `.py` Extension and use one observable capability it contributes.
- **Completed-session recovery:** after a normal exit at a completed boundary, restore the linear history and necessary session context and continue with a later request. This must be observable through at least one product entry point.

The Skill and Extension scenarios are separate evidence: a Skill is an instructional resource and an Extension is executable code. Neither may stand in for the other. Each must be observable through at least one of the REPL or programmatic session entry points, but neither must cover both entry points or define a complete Skill standard or Extension interface.

### Project-resource trust

v0 project trust initially guards project-resource loading, matching the Reference Revision's seam rather than silently expanding into Tool authorization:

- the REPL obtains an explicit trust decision before loading project-level `.py` Extensions or project-level Skills;
- the programmatic session caller supplies an explicit trust decision, and omission never means that the current directory is trusted;
- the Python Extension companion scenario runs in a trusted project; and
- a core journey that loads no project-level Extension or Skill is not prevented from using built-in inspection, mutation, or verification solely because this resource trust decision is absent.

Whether command execution or file mutation should sit behind the same gate would be an intentional omh tightening and remains for the Tools and Workspace decision. Trust persistence, scope, revocation, untrusted read behaviour, and REPL presentation also remain later decisions.

### Explicit later-version surface

The v0 Release Surface excludes:

- image input and generation;
- public thinking or reasoning content and level controls;
- in-flight steering and follow-up queues;
- in-session model discovery, switching, cycling, and cross-model hand-off;
- session trees, branching, cloning, compaction, import, export, and sharing; and
- recovery of an in-flight model request, Tool execution, or other incomplete effect after a process failure.

Because v0 has no compaction, context overflow fails explicitly rather than silently truncating, summarizing, or dropping history. Completed-session recovery is linear and does not imply crash recovery.

The exact public Python names, other run modes, provider and model identity, built-in Tools, workspace rules, resource discovery order, Extension lifecycle, storage format, serialization, parity adaptations, conformance rows, packaging, and release authorization remain owned by their downstream decision tickets.

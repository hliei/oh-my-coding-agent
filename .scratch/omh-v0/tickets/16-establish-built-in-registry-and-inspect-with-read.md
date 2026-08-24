# 16 — Establish the built-in registry and inspect with read

**What to build:** Establish the Product Session's fixed coding Tool registry and make `read` the first fully operational built-in. Through an installed public Session, a Model can inspect literal Workspace-relative or absolute strict-UTF-8 text, continue through bounded pagination, and receive actionable negative Tool Outcomes without conflating Workspace with a sandbox or trust grant.

**Blocked by:** 13 — Prompt one durable no-Tool Product Session.

**Status:** resolved

- [x] Every Product Session registers exact reserved identities `read`, `bash`, `edit`, and `write` in canonical order, with Extension positions reserved after them and no selection/override configuration.
- [x] Workspace is the normalized logical Session cwd for relative Tool paths and initial shell context but imposes no containment, symlink, absolute-path, or host-permission boundary.
- [x] `read` accepts only a nonempty NUL-free literal path plus optional positive safe offset/limit and performs only the selected lexical path normalization.
- [x] Regular-file strict-UTF-8 text is preserved exactly, including BOM and newline form; logical lines and one-based addressing match the accepted definitions.
- [x] Output returns at most 2,000 complete lines or 51,200 UTF-8 bytes, never a partial line or full-output artifact, and provides the exact continuation observation.
- [x] Missing, nonregular, unreadable, nontext, and out-of-range cases return the selected actionable AgentToolResult Outcomes with `isError=False` in Runtime projection.
- [x] Real execution/invariant/cancellation failure remains a Tool Failure and starts no later effect after the cancellation cutoff.
- [x] Built-in Tool name, label, schema, description, parameter descriptions, and prompt summary are exact closed metadata and excluded built-ins remain absent.
- [x] Installed Product Session tests use real temporary filesystem effects and deterministic Tool-calling transport, with Matrix/corpus coverage for literal paths, bounds, Outcomes, and non-sandbox behavior.

## Comments

- Implemented the fixed Product Session built-in registry (`read`, `bash`, `edit`, `write` in canonical order) and a fully operational `read` over real host files: lexical Workspace path resolution without sandboxing, strict UTF-8 regular-file text, one-based pagination, 2000-line/50KiB complete-line truncation, actionable Outcomes, and cancellation/unexpected I/O as Tool Failures. `bash`/`edit`/`write` are registered with closed metadata; their execute paths remain later tickets.
- Review follow-up: close failure is `LifecycleError(code="cleanup")` (loop now propagates that code from execute); `ELOOP` is unexpected I/O / Tool Failure rather than `not_found`; every Outcome return checks AbortSignal first, including after handle close.

# Choose the v0 Python dependencies

Type: research
Status: open
Blocked by: 06, 07, 08, 09, 10, 11

## Question

Which concrete Python libraries, Provider transport or SDK, schema/regex machinery, persistence support, packaging/runtime dependencies, and version constraints satisfy the complete v0 semantics without expanding the Public Import Surface or silently delegating ownership, retries, cancellation, serialization, trust, or error policy to an incompatible dependency?

## Comments

- 2026-08-20 — Upstream [Choose the Python Extension lifecycle](09-choose-extension-lifecycle.md) requires BOM-free strict UTF-8 whole-set snapshots, private no-`pyc`/no-public-cache module execution, ordinary imports of already installed distributions only, and no manifest reading, dependency installation, implicit `.omh/extensions/` package path, sandbox, watcher, or reload runtime. Dependency selection must preserve per-Session independent Extension generations, awaited cancellation/cleanup, exact typed failures, and the closed `ExtensionAPI`/`ExtensionContext` surface rather than delegating them to a plugin framework with incompatible caching or error isolation.

# Choose the v0 Python dependencies

Type: research
Status: resolved
Blocked by: 06, 07, 08, 09, 10, 11

## Question

Which concrete Python libraries, Provider transport or SDK, schema/regex machinery, persistence support, packaging/runtime dependencies, and version constraints satisfy the complete v0 semantics without expanding the Public Import Surface or silently delegating ownership, retries, cancellation, serialization, trust, or error policy to an incompatible dependency?

## Comments

- 2026-08-20 — Upstream [Choose the Python Extension lifecycle](09-choose-extension-lifecycle.md) requires BOM-free strict UTF-8 whole-set snapshots, private no-`pyc`/no-public-cache module execution, ordinary imports of already installed distributions only, and no manifest reading, dependency installation, implicit `.omh/extensions/` package path, sandbox, watcher, or reload runtime. Dependency selection must preserve per-Session independent Extension generations, awaited cancellation/cleanup, exact typed failures, and the closed `ExtensionAPI`/`ExtensionContext` surface rather than delegating them to a plugin framework with incompatible caching or error isolation.
- 2026-08-21 — Accepted the v0 dependency baseline from [the primary-source research](../research/13-python-dependencies.md): one locked Distribution requires CPython `>=3.12,<3.14`; its exact direct Runtime dependencies are `httpx==0.28.1`, `google-re2==1.1.20251105`, and `PyYAML==6.0.3`; its sole Build dependency is `hatchling==1.32.0`; and the complete exact transitive resolution is committed in `uv.lock` and enforced through locked installation and release checks. HTTPX is only a private owned byte transport, RE2 only the private Pattern engine, and PyYAML only a private syntax parser; omh retains Provider protocol/policy, Schema admission/conversion/errors, Prompt Resource admission, cancellation, serialization, persistence, CLI, Extension, and Tool ownership. No Provider SDK, generic JSON Schema/Pydantic layer, ORM, plugin framework, or terminal UI framework enters v0.
- 2026-08-21 — Accepted explicit `timeout=None` for the private HTTPX transport. v0 inherits none of HTTPX's implicit connect/read/write/pool timeouts and invents no Adapter or Run deadline: owner cancellation remains the sole deterministic local stop authority and must drain the request, response, and client through the existing settlement barrier. A timeout actually raised by the network or host still follows the already selected private `ModelsError(code="provider")` path. There is no public timeout option, automatic retry, or total-Run budget; an operation without owner cancellation may therefore wait indefinitely for network progress.
- 2026-08-21 — Accepted a closed Prompt Resource YAML scalar syntax above PyYAML's syntax-only role. String metadata fields admit one-line plain, single-quoted, or double-quoted YAML scalars. A boolean field admits only the unquoted lowercase plain scalars `true` and `false`; quoted spellings, `yes`/`no`, `on`/`off`, case variants, and explicit YAML tags do not satisfy the boolean carrier. The private parser must inspect the scalar node's syntax/style and apply these omh rules itself rather than inherit PyYAML's implicit type resolution. All existing unknown-field, duplicate-key, non-scalar, custom-tag, anchor/alias, multiline, and whole-set rejection rules remain in force.
- 2026-08-21 — Accepted that this dependency decision does not select an OS/architecture support matrix. It requires a prebuilt locked wheel for every native dependency on every release row and permits no release-time native source-build fallback. [Define the v0 conformance and release gate](12-define-v0-conformance-and-release-gate.md) owns the concrete platform/interpreter rows, clean-install evidence, and any platform adaptation needed before making a support claim. Windows cannot be included implicitly because the selected REPL signal contract uses Unix-only facilities; supporting it requires an explicit compatible decision rather than an implementation guess.

## Answer

The source evidence and rejected alternatives are captured in [v0 Python dependency research](../research/13-python-dependencies.md). v0 ships one locked `omh` Distribution on CPython `>=3.12,<3.14` with exactly three direct Runtime dependencies and one Build dependency:

```toml
[project]
requires-python = ">=3.12,<3.14"
dependencies = [
  "httpx==0.28.1",
  "google-re2==1.1.20251105",
  "PyYAML==6.0.3",
]

[build-system]
requires = ["hatchling==1.32.0"]
build-backend = "hatchling.build"
```

The repository commits the complete exact transitive resolution in `uv.lock`. Development, conformance, clean-install, and release commands use locked mode and fail on drift. `uv` is a development/release tool rather than a Runtime dependency; Hatchling is Build-only. A dependency upgrade is a deliberate conformance-reviewed change, never an unconstrained resolver movement.

### Closed dependency roles

`httpx` is only the DeepSeek Adapter's private owned HTTPS byte transport. Each activated Model operation privately owns its client, transport, request, response, and cleanup. It pre-encodes the canonical request body, uses `AsyncHTTPTransport(retries=0)`, `trust_env=False`, no redirect following or optional transport extras, requests identity encoding, and consumes raw bytes. omh—not HTTPX or an SDK—owns authentication resolution, request fields, SSE framing, strict UTF-8/JSON parsing, Tool-delta finalization, Usage validation, redacted failure classification, cancellation, and the proof of at most one network attempt.

The transport explicitly uses `timeout=None`. It inherits no HTTPX connect/read/write/pool default and introduces no Adapter or total-Run deadline. Owner cancellation is the sole deterministic local stop authority and drains all transport resources through the existing settlement barrier. A timeout actually raised by the network or host remains an ordinary private `ModelsError(code="provider")`; v0 exposes no timeout configuration or retry. Without owner cancellation, lack of network progress may therefore wait indefinitely.

`google-re2` is only the private engine for the accepted v0 Pattern Subset. A local iterative schema implementation first rejects syntax outside the exact ECMAScript/RE2 intersection, then compiles with fixed options for the selected ASCII shorthand, dot, anchor, no-flag, and unanchored-search semantics. RE2 does not own Tool Schema admission, conversion, traversal, deterministic aggregate errors, redaction, or public validation carriers. `jsonschema`, Pydantic, Python `re`, the third-party `regex` package, references, formats, and validator registries are absent.

`PyYAML` is only a private syntax scanner/composer for Prompt Resource frontmatter. omh rejects anchors, aliases, explicit tags, duplicate keys, unknown fields, non-scalars, multiple documents, and multiline metadata before admitting the closed field set. String fields accept one-line plain, single-quoted, or double-quoted scalars. Boolean fields accept only unquoted lowercase `true` and `false`; quoted values, `yes`/`no`, `on`/`off`, and case variants are not booleans. omh inspects syntax/style and applies these rules itself rather than inheriting PyYAML implicit typing. Whole-file BOM-free strict UTF-8 decoding, length/name/body validation, whole-set atomicity, and public failure ownership remain local.

`hatchling` builds the one Distribution containing the three explicit `src/` import packages and the `omh` console script. It adds no Runtime import, plugin discovery, configuration, or public seam.

### Standard-library and local ownership

Everything else remains in the standard library or private omh code:

- `asyncio` and `contextlib` implement lazy operations, private tasks, streams, queues, cancellation, subprocesses, and cleanup barriers. Any HTTPX-transitive AnyIO object stays below the transport and never defines public lifecycle semantics.
- `sqlite3` plus explicit private SQL and transactions implement the Session Image store. No SQLAlchemy, `aiosqlite`, migration framework, public storage Adapter, serializer, or service is added. Any blocking work moved off-loop remains owner-held and drained before settlement.
- A private OS lease Adapter holds a real file descriptor for the complete Product Session lifetime and uses nonblocking `fcntl.flock(..., LOCK_EX | LOCK_NB)` where available or `msvcrt.locking(..., LK_NBLCK)` on an admitted Windows row. It has no wait, timeout, retry, heartbeat, takeover, or stale-marker fallback; unsupported or unverified semantics fail closed. A lifetime SQLite write transaction is not used as the lease.
- `json`, `math`, and `struct` support a local iterative canonical codec that owns duplicate-key rejection, the closed scalar domain, exact integer/float and signed-zero spelling, key ordering, omission versus null, and byte-identical re-encoding. No generic serialization library or registry is added.
- `time`, `secrets`, and `uuid.UUID` support the private replaceable RFC 9562 UUIDv7 generator. No UUID Runtime package or public identity-source seam is added.
- `compile`, `types.ModuleType`, `exec`, and ordinary installed-distribution imports implement independent Session-private Extension module generations. There is no plugin framework, entry-point discovery, installer, watcher, sandbox, public module cache, or project package-path injection.
- `argparse`, `asyncio`, `os`, `signal`, `sys`, and `importlib.metadata` implement the exact CLI/REPL grammar and lifecycle. Click, Typer, Rich, prompt-toolkit, Textual, readline policy, and reusable TUI abstractions are absent.
- `pathlib`/`os`, byte I/O, `tempfile`, and asyncio subprocess primitives implement the built-in Tools. No filesystem abstraction, shell wrapper, retry helper, approval layer, or sandbox package may take over literal paths, process-tree cancellation, byte ordering, truncation/spill, or Outcome classification.
- The exact edit display diff and unified patch use a private minimal port of the pinned Reference Revision's `diff@8.0.4` comparator behavior, proven with cross-language golden vectors. Python `difflib` or another visual-diff package cannot substitute for byte-identical evidence.

### Packaging and release boundary

This ticket selects the dependency set and interpreter range, not the supported OS/architecture matrix. [Define the v0 conformance and release gate](12-define-v0-conformance-and-release-gate.md) must select each concrete platform/interpreter row and prove locked clean installation plus its complete relevant behavior. Every selected row must obtain every native dependency—especially `google-re2`—from a prebuilt locked wheel; absence of a wheel blocks that row, with no release-time source-build fallback.

No platform is inferred from a published artifact or an available dependency wheel. In particular, Windows requires an explicit resolution of the selected Unix-shaped REPL signal behavior before it can receive a support claim. PyPy, free-threaded CPython, WebAssembly, mobile Python, and every unselected OS/architecture/interpreter row have no v0 release promise.

These are private implementation ingredients and introduce no new Public Import Surface, PA, ABD, or Parity Ledger record. Their conformance obligation is to preserve every already accepted behavioral envelope and failure owner.

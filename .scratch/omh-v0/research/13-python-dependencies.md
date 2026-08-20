# v0 Python dependency research

Research for [Choose the v0 Python dependencies](../issues/13-choose-v0-python-dependencies.md). This note recommends implementation ingredients; it does not resolve the Wayfinder ticket.

## Recommendation

Use CPython `>=3.12,<3.14`, one locked distribution, and exactly three direct runtime dependencies:

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

Commit the generated `uv.lock` and treat it, including HTTPX's transitive graph, as release evidence. `uv run --locked`/`uv lock --check` must reject drift. uv documents that `uv.lock` captures exact cross-platform resolutions and does not change merely because a newer release exists ([uv locking](https://docs.astral.sh/uv/concepts/projects/sync/), [uv project layout](https://docs.astral.sh/uv/concepts/projects/layout/)). Hatchling can package several explicit `src/` packages into one wheel and is a standards-compatible backend ([Hatch build configuration](https://hatch.pypa.io/latest/config/build/)); packaging metadata, not Hatch, owns the three closed import seams and the `omh` script ([PyPA `pyproject.toml` guide](https://packaging.python.org/en/latest/guides/writing-pyproject-toml/)).

Exact runtime pins are appropriate for v0 because transport, YAML parsing, and regex syntax are inside conformance envelopes. Upgrades should be explicit, fixture-reviewed changes rather than silently accepted resolver movement. Build tooling is not a runtime dependency, and uv is a development/release tool rather than `Requires-Dist`.

## Why these three

### `httpx==0.28.1`: bytes transport only

DeepSeek's official Chat Completions endpoint is JSON over HTTPS; streaming is data-only SSE terminated by `data: [DONE]`, with final usage emitted before it when requested ([DeepSeek Chat Completions](https://api-docs.deepseek.com/api/create-chat-completion)). HTTPX provides an asyncio streaming context and raw-byte iteration, while making response closure the caller's responsibility in manual mode ([HTTPX async streaming](https://www.python-httpx.org/async/)). Its current official stable release is 0.28.1 ([HTTPX releases](https://github.com/encode/httpx/releases/tag/0.28.1)).

Keep HTTPX below the Adapter as an owned byte transport, not a protocol or policy owner:

- Create an operation-private `AsyncClient`/`AsyncHTTPTransport(retries=0)` and close both inside the Model operation's settlement barrier. Do not use the OpenAI SDK: the local Adapter must own request construction, SSE parsing, Tool-delta finalization, Usage validation, error redaction/classification, cancellation, and the one-request/no-retry proof. The pinned Pi revision is useful comparison evidence for its OpenAI-compatible request path and zero-retry setting, not authority for omh ([pinned Pi adapter](https://github.com/earendil-works/pi/blob/0e6909f050eeb15e8f6c05185511f3788357ddb3/packages/ai/src/api/openai-completions.ts#L184-L194)).
- Set `trust_env=False`. HTTPX otherwise consumes `HTTP_PROXY`, `HTTPS_PROXY`, `ALL_PROXY`, `NO_PROXY`, `SSL_CERT_FILE`, and `SSL_CERT_DIR` by default, contradicting the closed runtime environment surface whose sole input is `DEEPSEEK_API_KEY` ([HTTPX environment variables](https://www.python-httpx.org/environment_variables/)).
- Pre-encode the JSON request and pass bytes as `content`; do not let `json=` choose request serialization. Send `Accept-Encoding: identity`, do not follow redirects, and consume `aiter_raw()` so the Adapter owns strict UTF-8, SSE framing, duplicate-key rejection, and malformed-stream classification.
- Do not accept HTTPX's implicit timeout as policy. Its default is five seconds of network inactivity, with separate connect/read/write/pool timeouts ([HTTPX timeouts](https://www.python-httpx.org/advanced/timeouts/)). Ticket acceptance must either choose explicit private values or explicitly choose `timeout=None`; see the open point below.
- HTTPX's runtime graph includes `anyio`, `certifi`, `httpcore`, `idna`, and HTTP/1.1 machinery. Lock it exactly, omit all extras (`cli`, `http2`, `socks`, compression), and fixture-test task cancellation, response/client closure, one network attempt, no redirects/proxies/cookies, and exception-to-private-`ModelsError` mapping. HTTPX documents transport retries as opt-in and limited to connection failures; omh still sets zero explicitly ([HTTPX transports](https://www.python-httpx.org/advanced/transports/)).

### `google-re2==1.1.20251105`: only the pattern engine

The accepted v0 Pattern Subset requires linear-time matching and explicitly forbids unrestricted Python `re`. RE2 guarantees linear-time matching, bounds memory, avoids recursion/stack overflow, and its authors identify `google-re2` as the maintained official Python wrapper ([RE2 project](https://github.com/google/re2)). Release `1.1.20251105` publishes CPython 3.12 and 3.13 wheels across the major OS families; the artifact matrix must still be checked for each release target ([google-re2 on PyPI](https://pypi.org/project/google-re2/)).

RE2 is not the schema validator. A small private iterative validator should first reject every syntax outside the exact ECMAScript/RE2 intersection, then compile with explicit options implementing the accepted ASCII `\d`/`\w`/`\s` and no implicit flags, and use unanchored search. Map compile/resource failure to Tool-construction rejection. A conformance corpus must check ECMAScript dot/anchor/line-terminator details rather than assuming every RE2 default is identical.

### `PyYAML==6.0.3`: syntax parser, never admission authority

Prompt Resources require YAML frontmatter but reject unknown keys, non-scalars, custom tags, anchors, aliases, invalid types, and multiline metadata. PyYAML exposes scanning/composition primitives and distinguishes `SafeLoader` from the unrestricted object-constructing loader; its documentation also shows that anchors/aliases and implicit tag resolution are normal YAML features ([PyYAML documentation](https://pyyaml.org/wiki/PyYAMLDocumentation/)). Version 6.0.3 supports the selected CPython range ([PyYAML on PyPI](https://pypi.org/project/PyYAML/)).

Use a private loader that scans and rejects `AnchorToken`, `AliasToken`, and explicit tags before composition, rejects duplicate mapping keys, admits exactly one mapping document, and constructs only the ticket's exact string/bool fields. Do not call plain `yaml.load`, do not accept PyYAML's broader implicit scalar coercions as the contract, and use the same Python implementation path in fixtures even when the optional LibYAML accelerator is installed. omh retains whole-file BOM-free strict-UTF-8 decoding, single-line/length rules, unknown-field rejection, whole-set atomicity, and stable public failure carriers.

## Keep the rest in the standard library or local code

| Concern | Choice | Ownership reason |
|---|---|---|
| Async lifecycle | `asyncio`, `contextlib`, private tasks/queues/events | Public cancellation is the omh `AbortSignal` and owner barriers. AnyIO/Trio/TaskGroup objects must not leak into public behavior. Python documents `shield()` as only one ingredient; omh must still catch cancellation and drain the owned cleanup task ([asyncio tasks](https://docs.python.org/3.12/library/asyncio-task.html)). |
| Persistence | `sqlite3`, explicit SQL transactions, private schema | SQLite gives an embedded transactional store without a service dependency. Use explicit transaction control, parameter binding, no adapters/converters/extensions, and an owner-drained worker for blocking calls; Python documents transaction and thread-affinity behavior ([Python `sqlite3`](https://docs.python.org/3.12/library/sqlite3.html)). Do not add SQLAlchemy or `aiosqlite`: they would add transaction/thread/cancellation ownership without simplifying the narrow private Image store. |
| Session lease | private OS adapter over `fcntl.flock(..., LOCK_EX | LOCK_NB)` on Unix/macOS and `msvcrt.locking(..., LK_NBLCK)` on Windows | Keep the file descriptor strongly owned for the Session lifetime; process exit closes it, and explicit non-blocking acquisition adds no timeout/retry/takeover policy. Do not hold a lifetime SQLite write transaction, which would serialize unrelated Session writers. Unsupported platforms/filesystems fail closed rather than falling back to a marker/heartbeat lock ([Python `fcntl`](https://docs.python.org/3/library/fcntl.html), [Python `msvcrt`](https://docs.python.org/3/library/msvcrt.html)). |
| Canonical values/JSON | local iterative codec over `json`, `math`, `struct` | `json.dumps()` is not itself the accepted canonical format. The local codec must own duplicate-key rejection, scalar validation, int/float and signed-zero identity, exact float spelling, omitted-vs-null fields, key ordering, and re-encode identity. Do not add Pydantic, `orjson`, Marshmallow, or a codec registry. |
| JSON Schema | local iterative validator plus `google-re2` | Draft 2020-12 is normative ([JSON Schema 2020-12](https://json-schema.org/draft/2020-12)); the omh subset, conversion trace, deterministic aggregate errors, mathematical numeric equality, no depth threshold, and redaction are narrower than a generic validator. `jsonschema` would bring its own recursion, regex, type, reference, and error-order behavior, so do not add it. |
| UUIDv7 | local private generator using `time`, `secrets`, `uuid.UUID` | The accepted source is replaceable for tests and the output is validated as canonical RFC 9562 UUIDv7. A small bit-layout implementation avoids a public or runtime `uuid6` dependency ([RFC 9562](https://www.rfc-editor.org/rfc/rfc9562.html#name-uuid-version-7)). |
| Extensions | `compile`, `types.ModuleType`, `exec`; ordinary `importlib` only for installed dependencies | Compile the already snapshotted strict-UTF-8 source into a fresh private module namespace, never insert the project module into reusable `sys.modules`, and set no bytecode cache. Python documents direct source compilation/execution primitives ([Python `importlib`](https://docs.python.org/3.12/library/importlib.html)). No plugin framework, entry-point discovery, watcher, installer, or sandbox. |
| REPL/CLI | `argparse`, `asyncio`, `os`, `signal`, `sys`, `importlib.metadata` | The transcript and six flags are exact and intentionally exclude TUI behavior. Do not add Click/Typer/Rich/prompt-toolkit/readline abstractions that can add aliases, styling, history, signal, decoding, or terminal-state policy. |
| Built-in Tools | `pathlib`/`os`, byte I/O, `tempfile`, `asyncio.create_subprocess_shell` | `read`/`edit`/`write` retain literal host-I/O semantics; `bash` owns process group termination, merged raw output, truncation/spill, and cleanup. Python's asyncio subprocess API exposes pipes and async wait but leaves timeouts and termination to the caller ([asyncio subprocesses](https://docs.python.org/3.12/library/asyncio-subprocess.html)). No shell wrapper, filesystem abstraction, retry helper, or sandbox package. |
| Exact edit diff | local port of the pinned comparator | Python `difflib` and third-party visual diff packages are not evidence of byte identity. Port the fixed Pi `diff@8.0.4` algorithm/formatting needed by `generateDiffString()` and `generateUnifiedPatch()`, then lock it with cross-language golden vectors ([pinned Pi dependency manifest](https://github.com/earendil-works/pi/blob/0e6909f050eeb15e8f6c05185511f3788357ddb3/packages/coding-agent/package.json), [pinned comparator source](https://github.com/earendil-works/pi/blob/0e6909f050eeb15e8f6c05185511f3788357ddb3/packages/coding-agent/src/core/tools/edit-diff.ts)). Pi is comparison evidence only. |

## Rejected dependency families

- OpenAI/DeepSeek SDKs, SSE clients, retry/backoff libraries, and transport middleware: they duplicate request/SSE/error/retry ownership and usually expand the wire surface.
- `jsonschema`, Pydantic, TypeBox analogues, `regex`, and Python `re`: the accepted subset has custom conversion, deterministic redacted aggregation, mathematical number rules, iterative traversal, and RE2-linear patterns.
- SQLAlchemy, `aiosqlite`, ORM/migration frameworks, serializers, UUID packages, and generic lock/lease libraries: the Image and lifetime lease are private and narrow; omh needs explicit transaction/cancellation/canonical-byte/ownership authority.
- Pluggy/importlib-metadata entry points/plugin managers/watchers: Extension discovery is trusted project-only source snapshots, not installed plugin discovery.
- Click/Typer/Rich/prompt-toolkit/Textual and subprocess/shell wrappers: the exact CLI grammar/transcript, terminal encoding, signal behavior, environment, process-group cancellation, and spill lifecycle are already owned locally.

## Questions surfaced for human decision

All three were subsequently resolved in [Choose the v0 Python dependencies](../issues/13-choose-v0-python-dependencies.md); the notes below preserve the research-stage alternatives and rationale.

1. **Provider timeout policy.** Recommend an explicit private `httpx.Timeout` with separately fixed connect/read/write/pool values and deterministic boundary fixtures. The upstream tickets classify timeout but do not choose durations. `timeout=None` avoids a hidden dependency default but permits indefinite network inactivity until owner cancellation; accepting HTTPX's implicit five seconds would silently add policy.
2. **Release platforms.** Recommend CPython 3.12–3.13 on only the OS/architecture/filesystem rows for which the locked `google-re2` wheel and native `flock`/Windows byte-range lock are installed and exercised. If broad Windows support is required, the exact REPL stdin/signal and process-tree termination behavior needs platform-specific evidence; a terminal framework must not be added merely to hide the difference. CPython 3.14 should be added only after HTTPX plus the complete native-wheel/CLI/subprocess matrix passes, then the upper bound can move to `<3.15`.
3. **YAML scalar spelling.** Recommend explicitly accepting YAML quoted/plain strings and only canonical lowercase `true`/`false` for the boolean flag, rejecting PyYAML 1.1-style synonyms such as `yes`/`on`. The document contract fixes field types but not every YAML lexical spelling; this should be made explicit before fixtures become normative.

None of these open points changes the three-package recommendation. They are semantic choices that must be accepted by the human rather than inherited from dependency defaults.

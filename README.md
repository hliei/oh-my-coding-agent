# omh

omh is a small Python coding-agent product. It provides a terminal coding
agent, a reusable agent runtime, and a unified model interface as one
installable distribution.

## Requirements

- macOS 26 on Apple silicon
- CPython 3.12 or 3.13
- A DeepSeek API key for real model operations

## Install

Download `omh-0.2.1-py3-none-any.whl` from the corresponding GitHub Release,
then install the command into an isolated tool environment:

```bash
uv tool install ./omh-0.2.1-py3-none-any.whl
```

This exposes the `omh` command without installing OMH dependencies into the
system Python environment.

## Quick start

Provide the credential only to the operation that needs it:

```bash
export DEEPSEEK_API_KEY="your-temporary-key"
omh --print --no-session --cwd /path/to/project "Inspect the project and report its test command."
```

Run `omh` without `--print` for the interactive terminal interface:

```bash
omh --cwd /path/to/project
```

Use `omh --help` for session selectors, project trust, and command-mode details.

## Upgrade from 0.1.x

`--trust-project` is removed. Use construction-only `--approve` / `-a` or
`--no-approve` / `-na`; omission resolves saved policy, then
`defaultProjectTrust`. Non-built-in slash input that does not expand may
reach the Model literally. Existing Session files remain recoverable without
a v0.2 Session migration. Trust and settings policy files are created only
when a persistent choice is saved and are not populated by migrating
existing data.

## Updates

`v0.2.1` is the Updater Bootstrap Release. It is the first omh Distribution
that contains Update Check, Update Notice, and Self Update behavior.

Manual bootstrap for `v0.2.0` and earlier: those Distributions cannot
discover or install updater code remotely. Install `v0.2.1` once by hand
from the corresponding GitHub Release using the same command shown under
[Install](#install). After that, ordinary updates below apply.

From `v0.2.1` onward, an installed uv-tool omh does the following:

- Update Check — every ordinary interactive startup begins one anonymous,
  non-blocking, no-retry check against the official GitHub Latest Release.
  `--print`, `--version`, `--help`, and a nonempty `OMH_SKIP_VERSION_CHECK`
  never perform the check; check failures remain invisible.
- Update Notice — when a newer eligible release exists, the terminal shows
  the fixed line `New omh version <version> is available. Run omh update`.
  It appears immediately when the terminal is idle and waits for the active
  operation's settlement when a Product Session is in flight; it enters no
  Session history and installs nothing.
- Self Update — run `omh update` explicitly. It validates the immutable
  Latest Release, proves the running Distribution is uv-tool managed,
  downloads and digest-verifies the exact Candidate Wheel, and delegates
  replacement to ordinary `uv tool install` (no force). It uses no
  credentials, opens no Session, and reads no project state.
- Unsupported-installation guidance — a non-uv or indeterminate installation
  is not modified. `omh update` prints the exact Eligible Update Release
  page and exits without downloading or mutating anything, so you can
  reinstall by the same manual command.

The first public cross-version Self Update proof is deferred to a later
`v0.2.1`-to-successor release. Only that transition demonstrates automatic
public discovery and installation; the manual `v0.2.0` to `v0.2.1` step
above is not automatic-update evidence.

## Python interfaces

The `omh` distribution installs three public import packages:

- `oh_my_llm` — model values, streaming, validation, Faux, and DeepSeek
- `oh_my_core` — agent state, lifecycle, loop, and tool execution
- `oh_my_coding_agent` — product sessions, persistence, extensions, and tools

## Authority and credentials

OMH runs with the filesystem, process, network, and environment authority of
the user who launches it. The built-in shell and file tools are not a sandbox.
Review the working directory and project resources before granting project
trust, and use short-lived provider credentials where practical.

## Development

```bash
uv sync --locked
uv run --locked pytest -q tests/public
uv run --locked mypy src tests/public
```

## License

OMH is licensed under the MIT License. See [LICENSE](LICENSE).

# omh

omh is a small Python coding-agent product. It provides a terminal coding
agent, a reusable agent runtime, and a unified model interface as one
installable distribution.

## Requirements

- macOS 26 on Apple silicon
- CPython 3.12 or 3.13
- A DeepSeek API key for real model operations

## Install

Download `omh-0.1.2-py3-none-any.whl` from the corresponding GitHub Release,
then install the command into an isolated tool environment:

```bash
uv tool install ./omh-0.1.2-py3-none-any.whl
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

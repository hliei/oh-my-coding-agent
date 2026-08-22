# 23 — Complete the one-shot Command Mode

**What to build:** Expose the complete noninteractive `omh --print` Command Mode over a selected persistent or in-memory `SessionManager`. One explicit normalized prompt creates or selects a Session, emits its identity on stderr, buffers the Run to settlement, writes only terminal Assistant Text to stdout, disposes exactly once, and exits with the selected stable status and diagnostic.

**Blocked by:** 22 — Complete the programmatic v0 Product Journey.

**Status:** ready-for-agent

- [ ] The installed Distribution exposes exactly `--print`, `--cwd`, `--continue`/`-c`, `--session`, `--session-id`, `--no-session`, `--trust-project`, `--help`, and `--version`; `-c` is the sole short alias and standard end-of-options handling remains fixed.
- [ ] `--help` and `--version` are mutually exclusive pure terminal actions evaluated before TTY, cwd, input, trust, credentials, storage, or resource effects.
- [ ] One-shot accepts exactly one ECMAScript-trimmed source from a positional prompt or complete strict-UTF-8 non-TTY stdin; conflicting, missing, empty, extra, or invalid input fails before Session construction.
- [ ] After CLI/source admission, one-shot selects exactly one manager: default persistent create; `--session PATH_OR_ID`; recent `--continue`/`-c`; exact-current-project/open-or-create `--session-id ID`; or `--no-session` optionally carrying that id. Selector conflicts and lookup failures follow the accepted pre-Run paths, and one-shot never prompts for trust.
- [ ] Immediately after `AgentSession` publication and before the Run, stderr contains exactly one safe `session <ID> new|recovered` record; in-memory and absent-id creation are `new`, while an existing selected Session is `recovered`.
- [ ] One-shot subscribes to no streaming renderer; after settlement stdout contains only safe-encoded terminal Assistant Text blocks with no Tool or Run records and no inserted separator/newline.
- [ ] Completed, Model-error, confirmed signal cancellation, usage, construction/lifecycle, stdout I/O, and unconfirmed-settlement cases use the selected stdout/stderr/status table.
- [ ] Exit zero means Command Mode completion only and never proves that a modification or named check succeeded.
- [ ] Every published Session enters one launcher-owned disposal path; cleanup failure follows the selected precedence without replacing a primary signal status.
- [ ] Real installed-command tests cover pipe/TTY combinations, exact bytes/statuses, every selector and conflict, JSONL/recent/id/ephemeral selection, trust, failures, and Matrix/corpus Command Mode rows.

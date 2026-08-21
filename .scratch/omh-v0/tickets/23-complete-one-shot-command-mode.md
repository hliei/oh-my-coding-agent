# 23 — Complete the one-shot Command Mode

**What to build:** Expose the complete noninteractive `omh --print` Command Mode over the existing durable Product Session. One explicit normalized prompt creates or recovers a Session, emits its identity on stderr, buffers the Run to settlement, writes only terminal Assistant Text to stdout, disposes exactly once, and exits with the selected stable status and diagnostic.

**Blocked by:** 22 — Complete the programmatic v0 Product Journey.

**Status:** ready-for-agent

- [ ] The installed Distribution exposes the `omh` command with exactly the closed flag set, no short aliases, and standard end-of-options handling.
- [ ] `--help` and `--version` are mutually exclusive pure terminal actions evaluated before TTY, cwd, input, trust, credentials, storage, or resource effects.
- [ ] One-shot accepts exactly one ECMAScript-trimmed source from a positional prompt or complete strict-UTF-8 non-TTY stdin; conflicting, missing, empty, extra, or invalid input fails before Session construction.
- [ ] `--cwd`, optional recovery-only UUIDv7, and `--trust-project` map through shared Product Session preflight; omission creates a new durable Session and one-shot never prompts for trust.
- [ ] Immediately after Session publication and before the Run, stderr contains exactly one safe new/recovered Session identity record.
- [ ] One-shot subscribes to no streaming renderer; after settlement stdout contains only safe-encoded terminal Assistant Text blocks with no Tool or Run records and no inserted separator/newline.
- [ ] Completed, Model-error, confirmed signal cancellation, usage, construction/lifecycle, stdout I/O, and unconfirmed-settlement cases use the selected stdout/stderr/status table.
- [ ] Exit zero means Command Mode completion only and never proves that a modification or named check succeeded.
- [ ] Every published Session enters one launcher-owned disposal path; cleanup failure follows the selected precedence without replacing a primary signal status.
- [ ] Real installed-command tests cover pipe/TTY combinations, exact bytes/statuses, recovery, trust, failures, and Matrix/corpus Command Mode rows.

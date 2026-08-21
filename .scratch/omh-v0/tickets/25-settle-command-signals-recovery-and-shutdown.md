# 25 — Settle Command signals, recovery, and shutdown

**What to build:** Complete launcher control and failure ownership for both Command Modes. Ctrl-C, EOF, SIGINT, SIGHUP, SIGTERM, exact-id recovery, startup/renderer/stdout failure, lifecycle diagnostics, and cleanup all join the Product Session's existing abort/disposal barriers with conventional statuses and no timing window, force path, implicit retry, or secret-bearing output.

**Blocked by:** 24 — Complete the interactive REPL journey.

**Status:** ready-for-agent

- [ ] Active interactive Ctrl-C calls the existing Session abort barrier, joins complete settlement, emits cancellation only when confirmed, and returns the same Session to idle.
- [ ] Repeated Ctrl-C during settlement joins the same attempt; idle Ctrl-C clears/redraws only; empty-line EOF closes admission and begins orderly disposal.
- [ ] One-shot SIGINT and both modes' SIGHUP/SIGTERM fix the selected conventional status, close admission, and join one abort/disposal path without a force timeout.
- [ ] Later terminating signals only join shutdown; SIGKILL/host termination remain outside the contract and no signal creates a second cancellation policy.
- [ ] Exact `--session-id` recovery requires a canonical settled same-cwd Image, emits `recovered`, reobtains trust/auth/resources, and never selects recent/open-or-create/ephemeral behavior.
- [ ] Every published Session is held in one launcher lifetime and receives exactly one automatic disposal call; successful cleanup retains the Image and releases ownership without unsolicited hints.
- [ ] Usage, public Value rejection, lifecycle failure, Model error, cancellation, I/O failure, internal failure, and subordinate cleanup use only the closed safe diagnostic templates and precedence.
- [ ] Diagnostics expose no traceback, exception repr/class, causes, errno, prompt, credential, environment value, Provider text, or secret; stderr failure does not recurse or fall back to stdout.
- [ ] Real PTY/process/signal installed-command tests cover active/idle controls, repeated signals, shutdown races, exact recovery, cleanup failure, statuses, and durable continuity.
- [ ] Matrix/corpus coverage closes every Command Mode ABD and platform-specific terminal/signal obligation.

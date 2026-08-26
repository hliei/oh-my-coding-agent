# 25 — Settle Command signals, recovery, and shutdown

**What to build:** Complete launcher control, Session selection/recovery, and failure ownership for both Command Modes. Ctrl-C, EOF, SIGINT, SIGHUP, SIGTERM, path/id/recent/open-or-create/ephemeral selection, startup/renderer/stdout failure, lifecycle diagnostics, and cleanup all join the Product Session's existing abort/disposal barriers with conventional statuses and no timing window, force path, implicit retry, or secret-bearing output.

**Blocked by:** 24 — Complete the interactive REPL journey.

**Status:** resolved

- [x] Active interactive Ctrl-C calls the existing Session abort barrier, joins complete settlement, emits cancellation only when confirmed, and returns the same Session to idle.
- [x] Repeated Ctrl-C during settlement joins the same attempt; idle Ctrl-C clears/redraws only; empty-line EOF closes admission and begins orderly disposal.
- [x] One-shot SIGINT and both modes' SIGHUP/SIGTERM fix the selected conventional status, close admission, and join one abort/disposal path without a force timeout.
- [x] Later terminating signals only join shutdown; SIGKILL/host termination remain outside the contract and no signal creates a second cancellation policy.
- [x] `--session PATH_OR_ID`, `--continue`/`-c`, `--session-id ID`, and `--no-session` implement their accepted precedence, lookup/create, id grammar, and conflict behavior; existing selection emits `recovered`, fresh persistent/in-memory selection emits `new`, and every construction reobtains trust/auth/resources.
- [x] Every published `AgentSession` is held in one launcher lifetime and receives exactly one automatic disposal call; successful cleanup leaves its manager's JSONL or in-memory state unchanged without a lease release, forced flush, or unsolicited recovery hint.
- [x] Usage, public Value rejection, lifecycle failure, Model error, cancellation, I/O failure, internal failure, and subordinate cleanup use only the closed safe diagnostic templates and precedence.
- [x] Diagnostics expose no traceback, exception repr/class, causes, errno, prompt, credential, environment value, Provider text, or secret; stderr failure does not recurse or fall back to stdout.
- [x] Real PTY/process/signal installed-command tests cover active/idle controls, repeated signals, shutdown races, every selector including incomplete-Run JSONL recovery and ephemeral mode, cleanup failure, statuses, and accepted continuity.
- [x] Matrix/corpus coverage closes every Command Mode ABD and platform-specific terminal/signal obligation.

## Comments

- Split interactive Ctrl-C from terminating shutdown: published SIGINT aborts and rejoins idle, while SIGHUP/SIGTERM close admission, join one abort/disposal path, and keep first-signal 129/143. Trust-prompt Ctrl-C still writes `trust cancelled`; pre-publication HUP/TERM write `cancelled` on stderr with no disposal.
- Installed PTY/process tests cover idle redraw, repeated Ctrl-C join, Escape non-control, one-shot HUP/TERM, later-signal join, incomplete JSONL selector recovery, trust re-obtain, cleanup-subordinate diagnostics, and no recovery hint.
- Recorded `ABD:repl-control-inputs`, `ABD:repl-signal-status`, and local-release `omh-v0.command-posix-terminal-signals` in the Conformance Obligation Matrix and Reference Observation Corpus. Cleanup and one-shot HUP/TERM also attach to the existing diagnostics and one-shot ABD rows.
- Verification: focused Ticket 23/24/25 tests passed, full locked suite passed (517 tests), and locked mypy, compileall, lock validation, JSON validation, and `git diff --check` passed. Standards/Spec review findings (later Ctrl-C joins in-flight shutdown, idle Ctrl-C does not abort, abort tasks cannot leak a traceback) were addressed before commit.

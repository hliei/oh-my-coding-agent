# 19 — Run a shell command in the Workspace

**What to build:** Let the Agent execute one host shell command from the logical Workspace through `bash`, observe cumulative output updates, and receive a truthful bounded final Tool Outcome for success, nonzero exit, signal, timeout, or recognized infrastructure failure. Owned process trees, pipes, spill artifacts, and cancellation must settle before the Run continues.

**Blocked by:** 16 — Establish the built-in registry and inspect with read.

**Status:** resolved

- [x] `bash` accepts exactly command plus optional positive finite bounded timeout, with no per-call cwd, environment, shell, stdin, login, hook, or operations override.
- [x] Shell selection follows the accepted platform order at spawn time, starts in Workspace, and receives a fresh exact copy of the current host environment without product/profile/project injection.
- [x] Stdout/stderr are merged in Runtime chunk-arrival order with per-pipe order only and incrementally decoded under the accepted replacement behavior.
- [x] Cumulative updates include the initial empty value, immediate first dirty snapshot, bounded coalescing, and final dirty snapshot before ordinary terminal construction.
- [x] Final display retains the accepted last-line/byte limits; truncation writes complete merged raw bytes to one unpredictable exclusive owner-only spill artifact and reports its usable path.
- [x] Exit zero, nonzero exit, external signal, timeout, invalid command, unavailable Workspace/shell, denied spawn, and output/spill failure return their selected truthful Outcome codes, texts, and effect details.
- [x] Timeout and cancellation terminate the owned process group/tree, drain pipes/helpers/files, and suppress terminal success until every owned resource confirms settlement.
- [x] Cancellation overrides nonterminal classification and remains a Tool Failure; unconfirmed cleanup never publishes an Outcome or idle state.
- [x] Installed public-seam tests use real subprocesses, signals, filesystem spill effects, and explicit barriers on each applicable platform, with Matrix/corpus coverage for ordering, truncation, failure, and cleanup.

## Comments

- Implemented operational `bash` at the Product Session seam: closed command+timeout admission, POSIX `/bin/bash`-then-PATH-then-`sh` resolution, Workspace cwd, unmodified host environment copy, merged replacement-decoded pipes, empty-then-coalesced cumulative updates, pinned tail truncation with exclusive owner-only `omh-bash-*.log` spill, and closed Outcomes for nonzero/signal/timeout/pre-spawn/output-infrastructure cases. Timeout and cancellation SIGKILL the owned process group, drain pipes/spill, and publish no success; close failure after settlement is `LifecycleError(code="cleanup")`.

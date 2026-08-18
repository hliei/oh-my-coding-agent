# Choose the coding Tools and Workspace contract

Type: grilling
Status: open
Blocked by: 01, 03, 04, 05, 07

## Question

Which built-in coding tools, filesystem and shell operations, workspace rules, project-trust checks, truncation behaviour, mutation ordering, and tool override seams must v0 expose?

## Comments

- 2026-08-17 — Upstream constraint from [Choose the async, stream, and cancellation contract](04-choose-async-stream-cancellation-contract.md): global Tool execution defaults to parallel, while any called Tool declared sequential forces its complete assistant batch to run sequentially. This decision must classify every built-in mutation or command Tool that is not concurrency-safe as sequential; it may not rely on callers to guess.
- 2026-08-18 — Every selected built-in Tool must cooperate with the owner-supplied `AbortSignal`, release owned subprocess/file/stream resources before returning from cancellation, and leave no background work. Runtime cannot report idle until all started Tool work actually settles.
- 2026-08-18 — A cancellation request becomes an `aborted` terminal result only after built-in Tool cleanup confirms success. A cleanup exception produces lifecycle-cleanup failure, while a Tool that never settles keeps the Run non-idle. This ticket must make each built-in cleanup path observable and verifiable rather than relying on synthetic cancellation results.

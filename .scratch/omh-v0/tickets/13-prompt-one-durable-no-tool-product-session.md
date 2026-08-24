# 13 — Prompt one durable no-Tool Product Session

**What to build:** Drive one deterministic no-Tool prompt through a persistent public `SessionManager`. The first Assistant Message performs the lazy JSONL flush, later entries append through the manager's in-memory-first order, and reopening the parseable file rebuilds the active path without a settled-Run gate.

**Blocked by:** 12 — Create and dispose an empty SessionManager-backed Product Session.

**Status:** resolved

- [x] `AgentSession.prompt()` is lazy, accepts text plus the closed PromptOptions carrier, and rejects carrier/busy/closing/disposed misuse before Session-entry append or effect.
- [x] Prompt expansion appends the exact timestamped User Message through the manager before driving the Agent; on an unflushed manager that entry and initial Model/Thinking entries may remain memory-only.
- [x] The first Assistant Message exclusively creates the candidate JSONL file and writes the complete header-plus-entry prefix in physical order; a crash before this flush leaves no file or discovery result.
- [x] After flush, each finalized Session entry first updates manager entries/indexes/leaf and then appends one JSON line; public state/events follow the accepted AgentSession lifecycle rather than a durable-before-visible barrier.
- [x] Session events project the selected Core lifecycle with immutable snapshots and add one `AgentSettled` at the accepted boundary.
- [x] AgentEnd listeners settle while the Session remains streaming/non-idle; `AgentSettled` occurs after the complete Agent/compaction loop with no persisted settled marker.
- [x] The prompt returns `None` after ordinary completion; `messages` exposes the active-path Agent context while AgentEnd carries only the identical Run suffix and `SessionManager.getEntries()` retains the complete file-order tree entries.
- [x] Session subscriptions are independent ordered snapshot barriers with idempotent per-registration removal and state-before-listener observation.
- [x] Reopening the JSONL path reconstructs indexes, tree, leaf, and value-equal active context from its parsed entries, rebinds current resources/authentication, and starts one fresh idle `AgentSession` without restoring busy state.
- [x] A persisted incomplete-Run suffix remains recoverable as entries; reopening never redispatches its interrupted Model/Tool effect and continuation requires a fresh caller prompt.
- [x] Installed tests use deterministic transport plus real JSONL files and add lazy-first-flush, entry-order, active-path, incomplete-Run recovery, and no-lease cases to Matrix/corpus evidence.

## Comments

- Implemented the public `AgentSession.prompt()` and Session-event lifecycle over the caller-visible `SessionManager`, including lazy initial metadata/User state, first-Assistant exclusive prefix flush, in-memory-first later append, active-path/tree reconstruction, and incomplete-prefix recovery without effect redispatch. Implementation and installed evidence commit: `37e17eb`.
- Two-axis review completed with zero hard Standards findings and zero Spec findings. One judgment-only duplicated-test-fixture smell was retained because the installed-wheel scenario must stay self-contained under isolated execution.
- Verification: `uv run --locked pytest -q` (316 passed), `uv run --locked mypy src tests`, `uv run --locked python -m compileall -q src tests`, `uv lock --check`, installed-wheel ticket-13 scenario/corpus comparison, JSON syntax checks, and `git diff --check` all pass.

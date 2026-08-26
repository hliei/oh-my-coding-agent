# 24 — Complete the interactive REPL journey

**What to build:** Expose the interactive `omh` REPL as an append-only plain-text projection of one selected persistent or in-memory Product Session. A human explicitly decides project-resource trust, submits one normalized line per Run, observes safe incremental Assistant and Tool records plus a confirmed terminal classification, and can continue in the same `AgentSession` without a TUI, launcher command namespace, steering, or queue.

**Blocked by:** 23 — Complete the one-shot Command Mode.

**Status:** resolved

- [x] Interactive mode is selected only by absence of `--print` and requires TTY stdin/stdout; failed TTY admission performs no trust or Session/resource effect.
- [x] Without `--trust-project`, the launcher obtains a fresh exact yes/no decision for normalized cwd before construction; EOF/cancellation exits pre-construction under the selected behavior.
- [x] The REPL emits one safe `session <ID> new|recovered` identity record for default, path/id, recent, open-or-create, or in-memory selection, owns one lifetime input reader, and presents exact idle chrome.
- [x] Each nonempty ECMAScript-trimmed UTF-8 line maps to one `AgentSession.prompt()`; empty lines are local no-ops and slash/bang text receives no launcher interception.
- [x] A nonempty line submitted while the current Run/renderer/idle transition is busy is discarded with the exact diagnostic and creates no steering, queue, event, Message, or effect.
- [x] One sequential awaited Session listener renders incremental Assistant Text, Tool starts, Tool Outcomes, Tool Failures, and one confirmed completed/model-error/cancelled Run record.
- [x] Tool updates, Turn events, raw Agent start/end, private causes, and unconfirmed terminal classifications are absent from the transcript.
- [x] Body, field, Tool JSON, and diagnostic encoding visibly escape terminal controls, bidi formats, metadata separators, quotes, and backslashes under the accepted strict-UTF-8 rules.
- [x] Renderer write/flush failure follows listener fail-closed ownership, preserves committed facts, stops later progression, disposes, and never replays or fabricates a terminal record.
- [x] Real PTY installed-command tests cover trust, persistent and `--no-session` startup, transcript bytes/order, negative Tool Outcome labeling, busy input, multiple Runs, I/O failure, and Matrix/corpus evidence.

## Comments

- Implemented the installed `omh` REPL with asynchronous trust admission, one lifetime TTY reader, exact idle/busy behavior, one sequential Session listener, safe Assistant/Tool/Run transcript records, recoverable pre-Run value rejection, and fail-closed renderer ownership.
- Recorded `ABD:plain-repl-transcript` and `ABD:repl-reject-busy-ordinary-message` in the Conformance Obligation Matrix and Reference Observation Corpus. Ticket 25 retains active/idle control signals, complete selector/recovery races, shutdown precedence, and the remaining Command Mode ABD closure.
- Verification after review: Ticket 23/24 focused tests passed (28 tests), full locked suite passed (501 tests), and locked mypy, compileall, lock validation, JSON validation, and `git diff --check` passed. Standards/Spec findings were addressed before the final amend.

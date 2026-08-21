# 24 — Complete the interactive REPL journey

**What to build:** Expose the interactive `omh` REPL as an append-only plain-text projection of one durable Product Session. A human explicitly decides project-resource trust, submits one normalized line per Run, observes safe incremental Assistant and Tool records plus a confirmed terminal classification, and can continue in the same Session without a TUI, launcher command namespace, steering, or queue.

**Blocked by:** 23 — Complete the one-shot Command Mode.

**Status:** ready-for-agent

- [ ] Interactive mode is selected only by absence of `--print` and requires TTY stdin/stdout; failed TTY admission performs no trust or Session/resource effect.
- [ ] Without `--trust-project`, the launcher obtains a fresh exact yes/no decision for normalized cwd before construction; EOF/cancellation exits pre-construction under the selected behavior.
- [ ] The REPL emits one safe new/recovered Session identity record, owns one lifetime input reader, and presents exact idle chrome.
- [ ] Each nonempty ECMAScript-trimmed UTF-8 line maps to one `AgentSession.prompt()`; empty lines are local no-ops and slash/bang text receives no launcher interception.
- [ ] A nonempty line submitted while the current Run/renderer/idle transition is busy is discarded with the exact diagnostic and creates no steering, queue, event, Message, or effect.
- [ ] One sequential awaited Session listener renders incremental Assistant Text, Tool starts, Tool Outcomes, Tool Failures, and one confirmed completed/model-error/cancelled Run record.
- [ ] Tool updates, Turn events, raw Agent start/end, private causes, and unconfirmed terminal classifications are absent from the transcript.
- [ ] Body, field, Tool JSON, and diagnostic encoding visibly escape terminal controls, bidi formats, metadata separators, quotes, and backslashes under the accepted strict-UTF-8 rules.
- [ ] Renderer write/flush failure follows listener fail-closed ownership, preserves committed facts, stops later progression, disposes, and never replays or fabricates a terminal record.
- [ ] Real PTY installed-command tests cover trust, transcript bytes/order, negative Tool Outcome labeling, busy input, multiple Runs, I/O failure, and Matrix/corpus evidence.

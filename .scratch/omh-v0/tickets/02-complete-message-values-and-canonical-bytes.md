# 02 — Complete Message values and canonical bytes

**What to build:** Complete the closed public value language needed to carry text-only Model conversations. Public callers can construct, compare, hash, stream, and receive every selected JSON, Content, Message, Usage, StopReason, and AssistantMessageEvent variant with immutable ownership, carrier-sensitive semantics, stable validation, and type-preserving canonical representation.

**Blocked by:** 01 — Complete the first installed no-Tool Faux Run.

**Status:** ready-for-agent

- [ ] Every selected Public Value Record is frozen, slotted, keyword-only, nominal, constructor-validated, and recursively snapshots admitted containers without retaining mutable aliases.
- [ ] `JSONValue` admits exactly the accepted null, bool, Unicode string, safe integer, finite binary64, tuple, and read-only string-keyed mapping domain and rejects cycles, unsafe carriers, invalid Unicode, and nonfinite values.
- [ ] Equality and hashing preserve concrete record type, tuple order, mapping order-insensitivity, integer-versus-float distinction, and positive-versus-negative float zero.
- [ ] TextContent, ToolCall, UserMessage, AssistantMessage, ToolResultMessage, Usage, UsageCost, Context, and their closed unions expose the exact selected fields, requiredness, absence/null distinctions, and invariants.
- [ ] AssistantMessageEvent exposes only the nine selected nominal variants and enforces cumulative immutable partials, content-index lifecycle, a unique terminal event, and terminal payload identity.
- [ ] Carrier misuse raises `TypeError`; admitted carriers violating a range or cross-field invariant raise `ValueError` at the first deterministic public field/container path.
- [ ] Canonical representation preserves type, omission versus null, Unicode, safe integer spelling, shortest round-trip float spelling, and signed zero while rejecting duplicate, unknown, noncanonical, or invalid input.
- [ ] The installed Faux no-Tool trace continues to pass using only the completed public values; focused public-seam cases cover valid boundaries and every rejected carrier class.
- [ ] Matrix and corpus coverage records every completed public value, event variant, adaptation, and canonical observation without exposing a public serialization API.

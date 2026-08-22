# 02 — Complete Message values and canonical bytes

**What to build:** Complete the closed public value language needed to carry text-only Model conversations. Public callers can construct, compare, hash, stream, and receive every selected JSON, Content, Message, Usage, StopReason, and AssistantMessageEvent variant with immutable ownership, carrier-sensitive semantics, stable validation, and type-preserving canonical representation.

**Blocked by:** 01 — Complete the first installed no-Tool Faux Run.

**Status:** resolved

- [x] Every selected Public Value Record is frozen, slotted, keyword-only, nominal, constructor-validated, and recursively snapshots admitted containers without retaining mutable aliases.
- [x] `JSONValue` admits exactly the accepted null, bool, Unicode string, safe integer, finite binary64, tuple, and read-only string-keyed mapping domain and rejects cycles, unsafe carriers, invalid Unicode, and nonfinite values.
- [x] Equality and hashing preserve concrete record type, tuple order, mapping order-insensitivity, integer-versus-float distinction, and positive-versus-negative float zero.
- [x] TextContent, ToolCall, UserMessage, AssistantMessage, ToolResultMessage, Usage, UsageCost, Context, and their closed unions expose the exact selected fields, requiredness, absence/null distinctions, and invariants.
- [x] AssistantMessageEvent exposes only the nine selected nominal variants and enforces cumulative immutable partials, content-index lifecycle, a unique terminal event, and terminal payload identity.
- [x] Carrier misuse raises `TypeError`; admitted carriers violating a range or cross-field invariant raise `ValueError` at the first deterministic public field/container path.
- [x] Canonical representation preserves type, omission versus null, Unicode, safe integer spelling, shortest round-trip float spelling, and signed zero while rejecting duplicate, unknown, noncanonical, or invalid input.
- [x] The installed Faux no-Tool trace continues to pass using only the completed public values; focused public-seam cases cover valid boundaries and every rejected carrier class.
- [x] Matrix and corpus coverage records every completed public value, event variant, adaptation, and canonical observation without exposing a public serialization API.

## Comments

- TDD: focused cases first failed on missing public variants/codec, then exposed content-index smuggling, stale Tool Call delta partials, and out-of-order mapping diagnostics before the corresponding minimal fixes.
- Review: Standards found no hard violation; two maintainability smells were resolved by naming iterative JSON tasks and centralizing the event registry. Spec review's two P1 and one P2 findings were reproduced and fixed.
- Verification: `uv run --locked pytest -q` reports 33 passed; strict mypy, compileall, lock drift, JSON syntax, and diff checks pass. The installed-wheel no-Tool Faux trace remains green.

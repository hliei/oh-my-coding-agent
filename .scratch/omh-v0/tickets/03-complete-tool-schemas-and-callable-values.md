# 03 — Complete Tool schemas and callable values

**What to build:** Let a public caller declare one strict text Tool, validate a Tool Call into a fresh working argument tree, and execute it through the exact Python callable protocol. The slice closes Tool Schema admission, deterministic validation, executable Tool carriers, update/result values, and redacted error ownership before the Agent loop begins scheduling Tool effects.

**Blocked by:** 02 — Complete Message values and canonical bytes.

**Status:** resolved

- [x] Tool construction accepts only the closed Draft 2020-12 object-root v0 Tool Schema Subset and rejects every unknown, misplaced, recursive, reference, conditional, or unsupported keyword.
- [x] Pattern admission implements the accepted linear-time ECMAScript/RE2 intersection, including ASCII shorthand, dot, anchor, no-flag, and unanchored-search semantics.
- [x] Tool argument validation deep-copies inputs, applies only the selected primitive conversions, preserves bool/numeric distinctions, and returns a fresh mutable JSON working tree.
- [x] `anyOf` selects the first independently converted valid branch, while enum/const, numeric bounds, object, array, string, and additional-property behavior match the accepted contract.
- [x] Validation failure aggregates deterministic RFC-6901 pointer/keyword issues and never exposes argument values, failed values, schema text, arbitrary repr, or private paths.
- [x] Tool lookup distinguishes zero, one, and duplicate name matches; carrier misuse, lookup failure, schema failure, and conversion failure use their selected public classifications.
- [x] `AgentTool` supplies the exact effect-free preparation and four-argument async execution protocol with an active read-only signal and synchronous update callback.
- [x] `AgentToolResult` snapshots Text-only content, required JSON details, and optional termination intent and exposes no `isError` member.
- [x] Public validation functions and Tool constructors are verified through the installed package; Matrix/corpus cases cover all schema keywords, conversions, failures, and Python Adaptations owned by this slice.

## Comments

- TDD: public construction first failed on the missing Tool root, validation next failed on the missing public functions, and Core callable/result cases then failed on their missing roots before each minimal implementation.
- Verification: the installed-wheel Tool scenario, 1200-level object validation, 1100-level `anyOf`, all selected schema keywords/conversions, deterministic redaction, strict mypy, compileall, lock, JSON, and diff checks pass; the full suite reports 108 passed.

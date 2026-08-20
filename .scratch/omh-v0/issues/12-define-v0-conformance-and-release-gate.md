# Define the v0 conformance and release gate

Type: grilling
Status: open
Blocked by: 02, 03, 04, 05, 06, 07, 08, 09, 10, 11, 13

## Question

What deterministic and human-owned evidence proves v0 Behavioral Parity at every selected public interface, validates packaging and a clean installation, distinguishes documented Python adaptations from defects, and authorizes a release?

## Comments

- 2026-08-21 — Upstream [Choose the v0 Python dependencies](13-choose-v0-python-dependencies.md) fixes CPython `>=3.12,<3.14`, exact direct Runtime pins `httpx==0.28.1`, `google-re2==1.1.20251105`, and `PyYAML==6.0.3`, Build-only `hatchling==1.32.0`, and the complete transitive `uv.lock`. This ticket owns the concrete OS/architecture/interpreter release rows. Every row must prove locked clean installation from prebuilt wheels with no native source-build fallback and exercise its platform-specific lease, REPL signal, subprocess-tree, and terminal behavior. A dependency wheel alone never creates a support claim; Windows requires an explicit compatible resolution of the existing Unix-shaped signal contract before inclusion.

- 2026-08-20 — [Choose the Python Extension lifecycle](09-choose-extension-lifecycle.md) adds authoritative `PA:python-extension-entrypoint` plus `ABD:explicit-project-resource-trust`, `ABD:deterministic-extension-discovery`, `ABD:fixed-extension-registration`, `ABD:snapshot-extension-context`, `ABD:fail-closed-extension-handlers`, `ABD:session-scoped-extension-modules`, and `ABD:atomic-extension-initialization`. The gate must cover zero-enumeration untrusted construction, deterministic whole-set loading, the closed API/event/context surface, Tool contribution, handler/effect cutoff, reverse retryable shutdown, independent fresh/recovery module generations, exact failure carriers, no partial Session, and the explicit no-sandbox/arbitrary-side-effect boundary.
- 2026-08-20 — [Choose the Agent loop and Tool lifecycle](07-choose-agent-loop-and-tool-lifecycle.md) adds five authoritative ABD records and exact Run/Turn, event, state, Tool, cancellation, callback, error, and continuity traces. This gate must execute every specified verification row, including absence of leaked secrets/effects, AgentEnd/result identity, post-failure reuse, and non-idle behavior for unconfirmed work.

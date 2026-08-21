# 12 — Create, dispose, and recover an empty Product Session

**What to build:** Publish the first durable `AgentSession` without running a prompt. A caller lazily constructs one fully owned Product Session at a normalized logical Workspace, receives a canonical UUIDv7, observes the fixed Model and empty durable history, disposes it, and recovers the same settled Session by exact id with current authentication rebound.

**Blocked by:** 07 — Make Agent stateful and reusable; 09 — Stream DeepSeek text through deterministic transport.

**Status:** ready-for-agent

- [ ] `createAgentSession()` accepts only the selected frozen options carrier, starts validation/effects on first await, and returns the selected result wrapper with one final factory-produced Session.
- [ ] Omitted/relative cwd captures one process base and performs only lexical absolute normalization; invalid or nonexistent directories reject before identity, auth, store, lease, or resource effects.
- [ ] New construction creates one non-reused canonical lowercase UUIDv7; an explicit id is valid only for recovery and never creates a replacement.
- [ ] The initial Session Image contains only id, logical cwd, fixed Provider/Model identity, contract version, empty history, and settled authority; it excludes credentials and executable/live resources.
- [ ] Construction obtains one crash-released exclusive live-owner lease before reading/committing Image state and holds it through idle until successful disposal.
- [ ] Pre-publication validation, authentication, initialization, cancellation, or commit failure exposes no Session and leaves no usable partial new identity.
- [ ] Successful disposal settles owned resources, retains the settled Image, releases the lease, and leaves stable final read observations.
- [ ] Exact-id recovery validates the complete Image and matching cwd, rechecks current authentication, rebinds current Model/resources, and publishes an idle Session with value-equal empty history.
- [ ] Absent, cross-cwd, invalid, corrupt, incompatible, incomplete, or already-owned identities fail under the selected redacted classification without replacement or repair.
- [ ] Installed public-seam tests use real temporary SQLite and OS leases and record construction/recovery obligations in the Matrix.

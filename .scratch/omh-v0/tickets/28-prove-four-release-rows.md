# 28 — Prove all four Release Rows

**What to build:** Prove the exact Candidate Wheel on every supported v0 operating-system, architecture, and CPython-minor row. Each row independently performs a locked offline clean install, the complete deterministic suite, and real platform-specific Session lease, terminal/signal, subprocess-tree, cancellation, and cleanup evidence without inferring support from artifact availability.

**Blocked by:** 27 — Build the reproducible Candidate Wheel.

**Status:** ready-for-agent

- [ ] The release matrix contains exactly macOS 26 arm64 on CPython 3.12 and 3.13 plus Ubuntu 24.04 x86_64 on CPython 3.12 and 3.13.
- [ ] Every row receives the identical Candidate Wheel filename/SHA-256 and a row-specific offline prebuilt wheelhouse resolved exactly from the committed lock.
- [ ] Each fresh environment installs outside the checkout with network disabled and fails on resolver drift, missing binary artifact, native/source build, or wheel substitution.
- [ ] Every row runs the complete no-skip Deterministic Conformance Suite against the installed artifact.
- [ ] Each row uses real OS resources to prove exclusive Session lease behavior, SQLite durability, filesystem semantics, REPL terminal/signal behavior, and bash subprocess-tree cancellation/cleanup.
- [ ] Evidence records actual OS point release/build, architecture, CPython patch, runner identity, wheelhouse identity, candidate commit, and exact wheel hash.
- [ ] Success on an unselected environment or availability of a dependency artifact creates no support promise; every unselected OS/architecture/interpreter remains explicitly outside v0.
- [ ] All four results bind to the same Matrix, corpus, lock, Candidate Wheel, and release-candidate commit and are ready to enter the Release Evidence Bundle.
- [ ] Failure on any row blocks release and cannot be waived, skipped, retried into acceptance, or replaced by a source-tree result.

# 28 — Prove both Release Rows

**What to build:** Prove the exact Candidate Wheel on both supported v0 CPython-minor rows on macOS 26 arm64. Each row independently performs a locked offline clean install, the complete deterministic suite, and real platform-specific JSONL Session, concurrent-manager, terminal/signal, subprocess-tree, cancellation, and cleanup evidence without inferring support from artifact availability.

**Blocked by:** 27 — Build the reproducible Candidate Wheel.

**Status:** ready-for-agent

- [x] The release matrix contains exactly macOS 26 arm64 on CPython 3.12 and 3.13; Ubuntu and every other Linux row remain outside v0.
- [ ] Both rows receive the identical Candidate Wheel filename/SHA-256 and a row-specific offline prebuilt wheelhouse resolved exactly from the committed lock.
- [ ] Each fresh environment installs outside the checkout with network disabled and fails on resolver drift, missing binary artifact, native/source build, or wheel substitution.
- [ ] Both rows run the complete no-skip Deterministic Conformance Suite against the installed artifact.
- [x] Each row uses real OS resources to prove JSONL lazy creation, append/direct-rewrite failure effects, parsed-prefix recovery, in-memory mode, and multiple managers without an exclusive lease, plus filesystem, REPL terminal/signal, and bash subprocess-tree cancellation/cleanup semantics.
- [ ] Evidence records actual OS point release/build, architecture, CPython patch, runner identity, wheelhouse identity, candidate commit, and exact wheel hash.
- [x] Success on an unselected environment or availability of a dependency artifact creates no support promise; every unselected OS/architecture/interpreter remains explicitly outside v0.
- [ ] Both results bind to the same Matrix, corpus, lock, Candidate Wheel, and release-candidate commit and are ready to enter the Release Evidence Bundle.
- [x] Failure on any row blocks release and cannot be waived, skipped, retried into acceptance, or replaced by a source-tree result.

## Comments

- Closed Release Row matrix: macOS 26 arm64 CPython 3.12/3.13 and Ubuntu 24.04 x86_64 CPython 3.12/3.13. Unselected Windows, macOS 15, macOS x86_64, Ubuntu 22.04, Linux arm64, PyPy, CPython 3.11/3.14, and free-threaded CPython fail identify/prove with no support promise.
- Row wheelhouses resolve from `uv.lock` only: macOS arm64 `macosx` vs Ubuntu x86_64 `manylinux`, `cp312` vs `cp313` native `google_re2`. Default `prove` installs that house with the identical Candidate Wheel outside the checkout (`--offline --only-binary :all:`) and runs the Deterministic Conformance Suite against the venv. `--skip-suite` / `--platform-only` evidence cannot bind; skipped suite, source-tree, and waiver results fail closed.
- This workstation proved macOS 26.3.1 (25D771280a) arm64 CPython 3.12.13 with the complete installed suite (`suiteRan: true`) and CPython 3.13.8 with real JSONL/PTY/bash platform tests. Ubuntu 24.04 x86_64 is rejected here (`host does not match`); those two rows are proven by running the same `conformance/prove_release_row.py prove --row ...` on matching hosts, then `bind` of all four `suiteRan` results.
- Verification: focused ticket-28 tests passed (23); full locked suite passed (560); locked mypy passed; compileall, lock validation, and `git diff --check` passed. `bind` grants no Publish Right.
- 2026-08-27 — Superseded the four-row scope with exactly two macOS 26 arm64 rows on CPython 3.12 and 3.13; Ubuntu is outside v0. The earlier Comments remain historical execution evidence. The ticket returned to `ready-for-agent` because CPython 3.13 still lacks a complete no-skip suite result and the two real row results are not yet bound.

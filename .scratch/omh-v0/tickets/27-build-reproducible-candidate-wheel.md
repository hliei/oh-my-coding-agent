# 27 — Build the reproducible Candidate Wheel

**What to build:** Produce the sole v0 publication artifact reproducibly from one clean release-candidate export. Two isolated builds under the pinned Candidate Build Row yield byte-identical universal wheels, and the exact wheel plus a locked prebuilt wheelhouse installs offline outside the checkout before the complete deterministic suite runs against the installed artifact.

**Blocked by:** 26 — Close the Public Import Surface and deterministic conformance.

**Status:** ready-for-agent

- [ ] One Release Toolchain Manifest pins the actual build environment, exact CPython and uv binaries/hashes, locked Hatchling, and every prefetched build artifact.
- [ ] Two builds start from independent clean directories and the same clean release-candidate export with the accepted deterministic time/locale/environment inputs and no build-time network or user/VCS state.
- [ ] Complete wheel bytes, filename, metadata, internal inventory, RECORD, and SHA-256 match across both builds.
- [ ] The artifact is exactly one universal `py3-none-any` wheel containing the three public packages and `omh` command; no sdist or alternative wheel path exists.
- [ ] A fresh environment outside the checkout installs only the exact Candidate Wheel and row wheelhouse resolved from the drift-free lock with network disabled.
- [ ] Resolver drift, missing prebuilt dependency, native/source fallback, undeclared dependency, checkout import, or differing Candidate Wheel fails the build/install gate.
- [ ] Installed metadata, closed imports, console command, and complete Deterministic Conformance Suite execute against the installed artifact rather than source files.
- [ ] Reproducibility and clean-install evidence records the candidate commit/version, toolchain manifest, wheel filename/hash, lock, wheelhouse, and conformance authority hashes.
- [ ] No platform support claim or Publish Right is created by this build ticket alone.

# 27 — Build the reproducible Candidate Wheel

**What to build:** Produce the sole v0 publication artifact reproducibly from one clean release-candidate export. Two isolated builds under the pinned Candidate Build Row yield byte-identical universal wheels, and the exact wheel plus a locked prebuilt wheelhouse installs offline outside the checkout before the complete deterministic suite runs against the installed artifact.

**Blocked by:** 26 — Close the Public Import Surface and deterministic conformance.

**Status:** resolved

- [x] One Release Toolchain Manifest pins the actual build environment, exact CPython and uv binaries/hashes, locked Hatchling, and every prefetched build artifact.
- [x] Two builds start from independent clean directories and the same clean release-candidate export with the accepted deterministic time/locale/environment inputs and no build-time network or user/VCS state.
- [x] Complete wheel bytes, filename, metadata, internal inventory, RECORD, and SHA-256 match across both builds.
- [x] The artifact is exactly one universal `py3-none-any` wheel containing the three public packages and `omh` command; no sdist or alternative wheel path exists.
- [x] A fresh environment outside the checkout installs only the exact Candidate Wheel and row wheelhouse resolved from the drift-free lock with network disabled.
- [x] Resolver drift, missing prebuilt dependency, native/source fallback, undeclared dependency, checkout import, or differing Candidate Wheel fails the build/install gate.
- [x] Installed metadata, closed imports, console command, and complete Deterministic Conformance Suite execute against the installed artifact rather than source files.
- [x] Reproducibility and clean-install evidence records the candidate commit/version, toolchain manifest, wheel filename/hash, lock, wheelhouse, and conformance authority hashes.
- [x] No platform support claim or Publish Right is created by this build ticket alone.

## Comments

- Pinned Candidate Build Row toolchain: macOS 26.3.1 (25D771280a) arm64, CPython 3.12.13 SHA-256 `f64cf6322e4f20cd0458aab89c0d332895817bb8f243b943109b6a957582fd5d`, uv 0.12.1 SHA-256 `863fe76e73a5d2ddcf192b583df205b996748542023272b1dc2d6f210e430fb3`, `hatchling==1.32.0`, and six prefetched build wheels.
- Two isolated `git archive` builds with `SOURCE_DATE_EPOCH` from the candidate commit, `TZ=UTC`, `LC_ALL=C.UTF-8`, empty HOME, no VCS config, and `sandbox-exec` network denial produced byte-identical `omh-0.1.0-py3-none-any.whl` SHA-256 `5b40b4578c064380e9efc1d665ba93af8b2025b2c28f5f2c928dd633364e3312`.
- Offline install uses a lock-resolved prebuilt wheelhouse, `--only-binary :all:`, and `python -I`; drift, missing native wheel, sdist fallback, undeclared Hatchling, checkout editable import, and a differing rebuild all fail closed. Default `prove` runs the Deterministic Conformance Suite against that venv (ignoring the nested rebuild and this ticket's own recursion).
- Verification: focused ticket-27 tests passed (9); full locked suite passed (537); locked mypy passed; compileall, lock validation, and `git diff --check` passed. This ticket creates no Release Row support claim and no Publish Right.

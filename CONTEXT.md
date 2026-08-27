# omh

omh is a Python coding-agent product whose releases deliberately reproduce selected observable behaviour from one immutable reference revision while retaining an independent product identity.

## Language

**Reference Revision**:
The immutable Pi source revision used to establish behaviour facts and comparison evidence; evidence may name and cite Pi directly. It never follows Pi's moving HEAD and does not transfer Pi's product identity into omh implementation or public naming.
_Avoid_: Latest version, upstream behaviour

**Target Compatibility Surface**:
The long-term set of observable behaviours selected from the Reference Revision for omh to reproduce, after explicit exclusions.
_Avoid_: Complete clone, repository parity

**v0 Release Surface**:
The bounded first usable subset of the Target Compatibility Surface that the omh v0 specification will promise.
_Avoid_: Target Compatibility Surface, full parity

**v0 Product Journey**:
The canonical end-to-end user outcome anchoring the v0 Release Surface: completing a bounded, verified code change in an existing local repository.
_Avoid_: Feature list, generic agent demo

**Product Session**:
One cwd-bound `AgentSession` conversation whose complete history is its caller-visible `SessionManager` tree and whose current Agent context follows the active path. Persistent recovery rebuilds the tree from the parseable local JSONL prefix rather than requiring a completed Run.
_Avoid_: Run, settled-state snapshot, completed-boundary-only history

**SessionManager**:
The caller-visible persistence, tree, and recovery object composed with an `AgentSession`. Its persistent carrier is a local JSONL Session file whose path is part of the API; in-memory mode retains the same tree without a file.
_Avoid_: Session Image, private storage Adapter

**Workspace**:
The logical working directory bound to a Product Session and used as the base for relative built-in Tool paths and the initial shell directory. It is project identity and working context, not a sandbox, containment boundary, or independent grant of authority.
_Avoid_: Project Resource Trust, project root, filesystem sandbox

**Session Image**:
Retired term for the former settled-state recovery envelope; use SessionManager and Session file.
_Avoid_: Current persistence or recovery authority

**Project Resource Trust**:
An explicit per-`AgentSession`-construction grant to discover and load executable or instructional resources owned by the normalized project. It grants no Tool or filesystem-operation authority and is never persisted in a Session file or inherited from `SessionManager` history or a prior Session.
_Avoid_: Workspace permission, Tool authorization, remembered project trust

**Python Extension**:
A project-owned executable `.py` resource admitted under Project Resource Trust and bound to one Product Session instance.
_Avoid_: Prompt Resource, Tool, Python package plugin

**Prompt Resource**:
A non-executable instructional resource consumed by a Product Session, either a Skill or a file Prompt Template.
_Avoid_: Extension, Tool, arbitrary project file

**Behavioral Parity**:
Closed semantic equivalence at a named public interface for the same canonical inputs. Each interface declares an observation envelope covering admission or rejection, causally ordered lifecycle observations, terminal classification and returned values, external effects, and post-terminal continuity where applicable. Observations outside that envelope carry no v0 parity promise. Within it, every difference is a parity gap unless an explicitly named Python adaptation or Accepted Behavioral Deviation permits it.
_Avoid_: Line-by-line translation, structural similarity

**Python Adaptation**:
A named difference in how a Reference Revision behaviour is represented through a Python interface without changing any dimension of its accepted semantic trace or merging distinctions the Reference Revision exposes. Permitted mappings are closed rather than inferred from general Python style. Names remain one-to-one except for independent product identity, Python-illegal identifiers, or a difference individually accepted by the owning public-interface decision.
_Avoid_: Bulk PEP 8 translation, Accepted Behavioral Deviation, implementation difference

**Accepted Behavioral Deviation**:
A deliberately accepted difference from the Reference Revision within a declared observation envelope.
_Avoid_: Python Adaptation, Surface Exclusion

**Surface Exclusion**:
A Reference Revision capability outside the v0 Release Surface, for which v0 makes no Behavioral Parity promise.
_Avoid_: Accepted Behavioral Deviation, Parity Gap

**Parity Gap**:
An observed difference within a declared observation envelope that is neither an applicable Python Adaptation nor an Accepted Behavioral Deviation.
_Avoid_: Accepted difference, out-of-scope capability

**Parity Ledger**:
The closed set of Python Adaptation and Accepted Behavioral Deviation records. Each record is written in full on the decision ticket that accepts it; a later specification only compiles those authoritative records. Surface Exclusions do not enter the ledger, and an unrecorded difference within an observation envelope is a Parity Gap.
_Avoid_: Specification-owned exception list, test-name-only evidence

**Conformance Obligation Matrix**:
The closed, machine-checked index connecting every v0 parity, adaptation, journey, public-surface, and platform obligation to its local authority, canonical observation, comparator, evidence class, and executable cases. Exact parity and accepted differences require fixed Reference evidence; a purely local release policy instead records a closed `not_applicable` reason and never fabricates a comparison. A missing, mismatched, or orphaned obligation fails the release gate.
_Avoid_: Test list, coverage report, implementation checklist

**Reference Observation Corpus**:
The committed, reproducible set of canonical inputs and normalized A/L/T/E/C observations captured from the fixed Reference Revision, with exact provenance and comparators. It is the deterministic parity oracle rather than a recording of current omh output; accepted deviations pair its observations with separately authoritative omh expectations.
_Avoid_: Mutable golden files, snapshots of omh, moving-HEAD output

**Reference Capture Row**:
The single pinned Node/npm and operating-system environment that regenerates the Reference Observation Corpus from the fixed Reference Revision. It is evidence infrastructure only, not an omh Release Row, Runtime dependency, or product support claim.
_Avoid_: Release Row, mutable developer checkout, moving toolchain

**Candidate Build Row**:
The single toolchain-manifest-pinned environment that reproducibly builds the universal Candidate Wheel twice from one clean release-candidate export. It produces an artifact for all Release Rows but creates no product support claim of its own.
_Avoid_: Release Row, developer checkout build, per-platform wheel build

**Deterministic Conformance Suite**:
The offline, no-skip executable evidence that drives the installed candidate Distribution only through public seams while controlling nondeterminism through private internal seams. Platform obligations use real resources on each Release Row, and a watchdog may expose a hang only as failure.
_Avoid_: Unit-test suite, source-tree smoke test, live-provider gate

**omh Distribution**:
The single installable and versioned Python distribution for v0. It installs the `omh` command and all three Public Import Packages as one atomic release unit.
_Avoid_: Python package, separately versioned layer

**Candidate Wheel**:
The one reproducibly built universal wheel whose exact bytes and SHA-256 are installed and tested on every Release Row before becoming the sole v0 publication artifact. A locally built wheel, an sdist, or a rebuild with different bytes is not that candidate.
_Avoid_: Build output, per-platform omh wheel, source checkout

**Live Provider Gate**:
The named-human-triggered and approved real-credential check that proves the Candidate Wheel still speaks the selected external Provider protocol. Its redacted evidence is bound to the exact candidate and is separate from deterministic conformance; Faux or an earlier candidate cannot satisfy it.
_Avoid_: CI secret smoke test, deterministic Provider fixture, automatic release job

**Release Evidence Bundle**:
The immutable manifest and evidence set binding one release-candidate commit, Candidate Wheel, locked inputs, conformance authorities, all Release Row results, and the Live Provider Gate approval. Its successful human review permits one annotated tag to grant the Publish Right for exactly that wheel.
_Avoid_: CI summary, mutable release checklist, rebuilt publication artifact

**Release Row**:
One selected operating-system release family, architecture, and CPython-minor combination on which the omh Distribution independently proves locked clean installation and every platform-relevant release obligation; v0 selects only macOS 26 arm64 on CPython 3.12 and 3.13. Evidence records the actual OS point release/build; an available artifact or an unselected environment that happens to work creates no support promise.
_Avoid_: Dependency wheel, broad platform family, inferred compatibility

**Public Import Package**:
One of `oh_my_llm`, `oh_my_core`, or `oh_my_coding_agent`: a separately importable and callable public namespace with its own public seam inside the omh Distribution. Co-installation neither permits cross-layer public imports nor collapses the three seams.
_Avoid_: Distribution, independently installable package

**Public Import Surface**:
The closed allowlist of import roots, explicit child paths, and names that v0 promises to callers. Installed or technically importable implementation modules are not public unless listed, and one Public Import Package does not re-export another's names.
_Avoid_: All importable modules, filesystem package contents

**Real Provider Adapter**:
The selected integration behind `oh_my_llm.Models` that reaches an external model service using actual credentials, in contrast to the deterministic local Faux Adapter. v0 selects exactly one; the factory-produced public `Provider` value is its handle, not an Adapter-authoring seam.
_Avoid_: Provider handle, Faux Adapter, custom Provider extension

**Public Value Record**:
A passive, closed record value exposed through the Public Import Surface whose object identity and lifecycle are not semantic. In v0 its record shell is immutable and nominal; owned resources, factory-produced handles, exceptions, callable interfaces, and mutable Agent state are not Public Value Records.
_Avoid_: Public object, arbitrary mapping, resource handle

**v0 Tool Schema Subset**:
The closed, Provider-portable subset of JSON Schema Draft 2020-12 accepted by v0 `Tool.parameters`. A valid Draft 2020-12 document outside this subset is not a valid v0 Tool schema.
_Avoid_: Full JSON Schema support, validator-defined schema support

**v0 Pattern Subset**:
The linear-time ECMAScript/RE2-intersection regular-expression dialect accepted by the `pattern` keyword inside the v0 Tool Schema Subset.
_Avoid_: Python `re`, full JavaScript RegExp

**Command Mode**:
A user-visible interaction form of the `omh` command. It is distinct from a Run, which is one admitted Agent-loop invocation within a Product Session.
_Avoid_: Run mode, Agent Run mode

**Run**:
One admitted Agent-loop invocation, beginning with `agent_start` and ending in either its unique ordinary `agent_end` or a settled lifecycle-carrier failure; its result domain contains only Messages newly produced by that invocation.
_Avoid_: Session, Turn, Provider request

**Turn**:
One Assistant response together with the complete Tool batch and source-ordered Tool Results caused by that response; a Run contains one or more Turns.
_Avoid_: Run, prompt, individual Tool Call

**Tool Outcome**:
A valid `AgentToolResult` normally returned by a Tool, including an expected negative domain result; Runtime classifies it with `isError=False`.
_Avoid_: Tool Failure, successful user result

**Tool Failure**:
A Runtime-rejected, raised, invalid, or cancelled Tool attempt represented by a synthesized Tool Result with `isError=True`.
_Avoid_: Negative Tool Outcome, Model failure, lifecycle failure

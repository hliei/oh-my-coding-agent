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
One durably identified, normalized-working-directory-bound linear conversation owned by an `AgentSession`, recoverable after a completed boundary.
_Avoid_: Run, in-memory session, session tree

**Session Image**:
The complete durable semantic state that authorizes recovery of a Product Session at a settled boundary; executable resources and authentication material are rebound rather than stored in it.
_Avoid_: Session file, resource snapshot, serialized runtime

**Project Resource Trust**:
An explicit per-Product-Session-construction grant to discover and load executable or instructional resources owned by the normalized project. It grants no Tool or filesystem-operation authority and is never inherited from a Session Image.
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

**omh Distribution**:
The single installable and versioned Python distribution for v0. It installs the `omh` command and all three Public Import Packages as one atomic release unit.
_Avoid_: Python package, separately versioned layer

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

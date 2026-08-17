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

**Behavioral Parity**:
Equivalent observable results at a named public interface for the same canonical inputs, except where an explicitly accepted Python adaptation says otherwise.
_Avoid_: Line-by-line translation, structural similarity

# 18 — Edit Workspace text exactly

**What to build:** Let the Agent apply one or more precise literal replacements to an existing Workspace text file. All edits match one original snapshot, nonunique or overlapping ranges write nothing, the successful diff/result is fully computed before overwrite, and the same-file mutation queue prevents concurrent edit/write races.

**Blocked by:** 17 — Write exact Workspace bytes.

**Status:** ready-for-agent

- [ ] `edit` accepts only path plus a nonempty closed edits array; each oldText is nonempty, newText may be empty, and no legacy form or repair path exists.
- [ ] The target must be an existing readable/writable strict-UTF-8 regular file under the shared literal Workspace identity and mutation queue.
- [ ] A leading BOM is excluded from matching then restored; every other scalar, byte-preserving untouched substring, whitespace, quote, dash, and newline remains literal.
- [ ] Every oldText is matched against one original snapshot, counting overlapping candidates; each must have one unique start and admitted ranges may touch but not overlap.
- [ ] Missing, nonunique, overlapping, and final-no-change inputs perform no write and return the selected actionable Outcome with exact indexes/counts/effect facts.
- [ ] Successful replacements apply in reverse original-offset order and precompute the complete final text, display diff, unified patch, first changed line, and immutable Result before overwrite.
- [ ] Shared LF fixtures match the fixed Reference diff behavior byte-for-byte, while omh-only newline/Unicode cases use the same comparator without normalization.
- [ ] A started overwrite may remain partial or complete after I/O failure/cancellation; queue ownership lasts through actual phase settlement and no rollback/retry is promised.
- [ ] Installed Product Session tests exercise real files, concurrent edit/write cases, exact public Outcomes, diffs, cancellation, and all owned ABD rows in Matrix/corpus evidence.

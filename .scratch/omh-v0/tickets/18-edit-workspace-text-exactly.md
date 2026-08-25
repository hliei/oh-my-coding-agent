# 18 — Edit Workspace text exactly

**What to build:** Let the Agent apply one or more precise literal replacements to an existing Workspace text file. All edits match one original snapshot, nonunique or overlapping ranges write nothing, the successful diff/result is fully computed before overwrite, and the same-file mutation queue prevents concurrent edit/write races.

**Blocked by:** 17 — Write exact Workspace bytes.

**Status:** resolved

- [x] `edit` accepts only path plus a nonempty closed edits array; each oldText is nonempty, newText may be empty, and no legacy form or repair path exists.
- [x] The target must be an existing readable/writable strict-UTF-8 regular file under the shared literal Workspace identity and mutation queue.
- [x] A leading BOM is excluded from matching then restored; every other scalar, byte-preserving untouched substring, whitespace, quote, dash, and newline remains literal.
- [x] Every oldText is matched against one original snapshot, counting overlapping candidates; each must have one unique start and admitted ranges may touch but not overlap.
- [x] Missing, nonunique, overlapping, and final-no-change inputs perform no write and return the selected actionable Outcome with exact indexes/counts/effect facts.
- [x] Successful replacements apply in reverse original-offset order and precompute the complete final text, display diff, unified patch, first changed line, and immutable Result before overwrite.
- [x] Shared LF fixtures match the fixed Reference diff behavior byte-for-byte, while omh-only newline/Unicode cases use the same comparator without normalization.
- [x] A started overwrite may remain partial or complete after I/O failure/cancellation; queue ownership lasts through actual phase settlement and no rollback/retry is promised.
- [x] Installed Product Session tests exercise real files, concurrent edit/write cases, exact public Outcomes, diffs, cancellation, and all owned ABD rows in Matrix/corpus evidence.

## Comments

- Implemented operational `edit` at the Product Session seam: closed path+edits[] admission, same-file mutation queue shared with `write`, strict-UTF-8 regular-file read with BOM-detached literal matching, overlapping-aware uniqueness, reverse-offset application, pinned `diff@8.0.4` display/unified details precomputed before overwrite, and closed identity/read/match/write Outcomes. Cancellation and injected precompute failure publish no success and do not roll back a started overwrite.

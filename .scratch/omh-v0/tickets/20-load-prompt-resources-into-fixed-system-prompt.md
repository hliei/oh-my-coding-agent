# 20 — Load Prompt Resources into the fixed system prompt

**What to build:** Admit one deterministic project Prompt Resource generation under explicit Project Resource Trust and bind it into one fixed Product Session system prompt. A trusted caller can invoke a snapshotted Skill or Prompt Template; an untrusted caller performs zero project-resource probing; malformed or missing command-shaped resources fail before persistence or effects.

**Blocked by:** 18 — Edit Workspace text exactly; 19 — Run a shell command in the Workspace.

**Status:** resolved

- [x] `projectTrusted` is a construction-local boolean defaulting false; false performs zero project Skill, Prompt Template, or Extension enumeration and does not affect built-in Tool authority.
- [x] Trusted discovery selects only direct nonsymlink project Skill and Prompt Template entries in Unicode-code-point name order and ignores every excluded source.
- [x] The complete selected resource set is BOM-free strict-UTF-8 decoded and schema/name/body validated atomically; one invalid resource rejects the whole construction.
- [x] One immutable per-construction snapshot supplies system-prompt descriptors and every later expansion; mid-Session file mutation cannot change it, while recovery admits a fresh generation.
- [x] Skill and Template command grammar, argument boundary, strict missing/invalid rejection, literal fallback, disabled expansion, wrapper text, and nonrecursive substitution match the accepted contract.
- [x] Rejected command-shaped input creates no durable User Message, Agent event, Model/Tool effect, or state change and leaves the Session reusable.
- [x] The private builder emits exactly the fixed six semantic sections in order using omh identity, canonical Tool summaries, verification guidance, admitted Skills, creation date, and logical cwd.
- [x] `AgentSession.systemPrompt` and every Model request remain identical for one Session instance; recovery rebuilds once from current admitted resources without persisting resource bytes.
- [x] Installed Product Session tests cover trust zero-enumeration, resource validity/symlinks/order/snapshot, invocation/expansion, prompt equality, recovery refresh, and Matrix/corpus authority.

## Comments

- Implemented construction-local `projectTrusted` (default false, non-bool TypeError) so untrusted Sessions admit no Skills/Templates and still register the four built-ins. Trusted discovery reads only direct nonsymlink `.omh/skills/<name>/SKILL.md` and `.omh/prompts/<name>.md` in Unicode name order, validates the whole set atomically, and freezes one immutable snapshot for the six-section system prompt and every later `/skill:` / `/name` expansion. Missing or invalid command-shaped input raises before persistence or Model/Tool effects; `expandPromptTemplates=False` leaves slash text literal. Resource bytes never enter the Session file; recovery rebuilds from then-current admitted resources.

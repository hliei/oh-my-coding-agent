# 09 — Stream DeepSeek text through deterministic transport

**What to build:** Add the sole Real Provider Adapter behind the public Models collection and prove a normal non-thinking text stream through a deterministic local transport fixture. Public callers resolve the static DeepSeek V4 Flash Model, observe environment authentication state, send the exact selected request, and receive one terminal Assistant value without external network access.

**Blocked by:** 04 — Own streams and all four low-level loops.

**Status:** ready-for-agent

- [ ] The only real Provider child seam is the zero-argument DeepSeek factory with the accepted id, name, API dialect, fixed base URL, and static one-Model catalog.
- [ ] The final Model exposes only the selected identity fields; no dynamic discovery, alias, capability metadata, string selector, or switching seam exists.
- [ ] `createModels()` is zero-argument and returns an empty mutable collection; registration and lookup occur only through the selected collection methods.
- [ ] Authentication observation rereads `DEEPSEEK_API_KEY` on each activated call, returns only configuration source, and never exposes or persists secret bytes.
- [ ] Missing authentication and foreign/Faux/fabricated Model selection fail before network effect and cannot publish a Product Session Model fallback.
- [ ] A deterministic local fixture observes the exact non-thinking streaming request identity, headers, body, environment isolation, raw-byte transport, and at-most-one request attempt.
- [ ] Fragmented normal text events accumulate into the selected public Assistant stream grammar and one complete terminal Message with valid Usage.
- [ ] No Provider SDK, proxy environment inheritance, redirect, retry, timeout, reasoning, image, or custom transport surface is introduced.
- [ ] Installed public-seam tests make no external request and Matrix/corpus rows cover factory/import allowlists, auth observation, request bytes, text events, and terminal identity.

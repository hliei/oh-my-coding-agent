# Issue tracker: Local Markdown

Wayfinder decision issues, specs, and implementation tickets for this repo live as markdown files in `.scratch/`.

## Conventions

- One feature per directory: `.scratch/<feature-slug>/`
- Wayfinder decision issues are one file per decision at `.scratch/<feature-slug>/issues/<NN>-<slug>.md`, numbered from `01`
- The spec is `.scratch/<feature-slug>/spec.md`
- Implementation tickets are one file per tracer-bullet slice at `.scratch/<feature-slug>/tickets/<NN>-<slug>.md`, numbered from `01` — never a single combined tickets file
- Status is recorded as a `Status:` line near the top of each decision issue or implementation ticket (see `triage-labels.md` for implementation-ticket role strings)
- An implementation ticket uses a triage role while open and moves to terminal status `resolved` only after every acceptance criterion is implemented and verified; `resolved` is not a triage role
- Comments and conversation history append to the bottom of the file under a `## Comments` heading

## When a skill says "publish to the issue tracker"

Create the artifact in its role-specific location under `.scratch/<feature-slug>/` (creating the directory if needed): Wayfinder decisions in `issues/`, the spec at `spec.md`, and implementation tickets in `tickets/`.

## When a skill says "fetch the relevant ticket"

Read the file at the referenced path. The user will normally pass the path or the issue number directly.

## Implementation ticket operations

Used by `/to-tickets` and `/implement`.

- **Ticket**: `.scratch/<feature>/tickets/NN-<slug>.md`, numbered from `01`, with one complete tracer-bullet slice and its acceptance criteria.
- **Blocking**: a `Blocked by:` line names only implementation tickets whose delivered behavior gates this ticket.
- **Frontier**: an implementation ticket whose blockers are complete. For a local tracker, work the lowest-numbered frontier ticket in a fresh `/implement` session.
- Wayfinder decision issues are specification authority, not implementation blockers, and remain unchanged when implementation tickets are published or completed.

## Wayfinding operations

Used by `/wayfinder`. The **map** is a file with one child decision issue per decision.

- **Map**: `.scratch/<effort>/map.md` — the Notes / Decisions-so-far / Fog body.
- **Decision issue**: `.scratch/<effort>/issues/NN-<slug>.md`, numbered from `01`, with the question in the body. A `Type:` line records the issue type (`research`/`prototype`/`grilling`/`task`); a `Status:` line records `claimed`/`resolved`.
- **Blocking**: a `Blocked by: NN, NN` line near the top. A decision issue is unblocked when every file it lists is `resolved`.
- **Frontier**: scan `.scratch/<effort>/issues/` for files that are open, unblocked, and unclaimed; first by number wins.
- **Claim**: set `Status: claimed` and save before any work.
- **Resolve**: append the answer under an `## Answer` heading, set `Status: resolved`, then append a context pointer (gist + link) to the map's Decisions-so-far in `map.md`.

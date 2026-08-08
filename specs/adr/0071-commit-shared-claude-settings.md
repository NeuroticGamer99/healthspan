# ADR-0071: Commit the Shared Claude Code Settings File

## Status
Proposed

## Context and Problem Statement
Claude Code reads project-scoped configuration from two files: `.claude/settings.json` (designed to be shared — checked-in permissions, hooks) and `.claude/settings.local.json` (designed to be personal and gitignored). This repo gitignored **both**, so the only place a project hook or a shared permission policy could live was unversioned, unreviewable, and machine-local — which is this repo's whole objection to unreviewable harness (every other gate and agent definition is tracked and reviewed).

The exclusion was deliberate but unrecorded: commit `2547542` (2026-07-07, "Update gitignore to exclude .claude settings.json") carries no body and no ADR, so its rationale is lost. Measured while confirming this in 2026-08: the owner's recollection was that the exclusion had already been *removed* — the polarity had inverted in memory, which is what an unrecorded decision costs. The likely original motive was keeping machine-specific absolute paths out of the repo; that concern is real but belongs to the *local* file. Both entries were also misfiled in `.gitignore` — `settings.local.json` under the "Recovery Kit renders" section and `settings.json` under "Python" — which made a section-by-section scan miss them.

A harness audit (2026-08-06) surfaced the practical blocker: planned work items (project-scoped hooks, a shared permissions allowlist) have no home until the shared file exists and is tracked. This ADR records the owner's decision (2026-08-06, implemented in the BRIEF-1 work item) so the rationale cannot invert again.

## Decision Drivers
- A project hook or shared permission policy must be versioned and reviewed like any other gate; an unversioned harness file is the exact shape `scripts/check_reviewer_agents.py`'s docstring criticises — behavior enforced by nothing visible.
- The upstream tool already provides the shared/personal split; recreating it by ignoring both files discards the mechanism while keeping its costs.
- The original exclusion's rationale was never recorded and was later mis-remembered with inverted polarity — the cheapest proof this decision needs a durable record.

## Considered Options
1. **Commit `.claude/settings.json`; keep `settings.local.json` ignored** (chosen) — the upstream-intended split.
2. **Keep both ignored** — any hook would have to ship via user-level settings: unversioned, unreviewable, machine-local. For a solo repo this is survivable, but it forecloses the planned hook work items and leaves shared policy invisible to review.
3. **Commit both** — `settings.local.json` exists precisely to hold machine-specific absolute paths and personal grants; committing it would leak machine detail into a public repo and break on every other checkout.

## Decision Outcome
`.claude/settings.json` is **tracked and committed**. The discriminator, recorded here and as a comment in `.gitignore`:

- **`settings.json` (committed)** — anything shared: permission policy meant to apply to every session of this repo, hooks, harness configuration that review should see.
- **`settings.local.json` (gitignored)** — anything machine-specific: absolute paths, personal permission grants, local experiment flags.

The file lands as a minimal valid stub (`permissions.allow`/`deny`, both empty) so the tracking decision is severed from any content decision — hooks and shared grants arrive in their own reviewed changes (the harness-audit work items own them). Before any content lands in `settings.json`, it must be checked for machine-specific paths or personal data; anything local-only moves to `settings.local.json`. The two `.gitignore` entries are re-filed under a dedicated, commented section in the same change.

## Consequences

### Positive
- Project hooks and shared permission policy gain a versioned, reviewable home; the planned hook work items are unblocked.
- The shared file's diffs go through the same review pipeline as every other harness file (and its prose is inside the docs gates' widened scope where applicable).
- The 2026-07-07 exclusion's missing rationale is reconstructed and recorded; the polarity cannot silently invert again.

### Negative / Tradeoffs
- A contributor (or a future session) can now commit machine-specific values into the shared file by mistake; the guard is the discriminator above plus review, not a gate — content judgement is not mechanizable here, the same conclusion ADR-0070 reached for personal-data content.
- The stub carries no behavior; until the hook work items land, the file is empty scaffolding that must not be read as "no shared policy was ever intended."

## Links
- Related: [ADR-0070](0070-personal-data-containment-gate.md) — the content-half-is-not-gateable reasoning this ADR's tradeoff section leans on
- Related: [ADR-0068](0068-reviewer-isolation-worktrees.md) — precedent that this repo versions and reviews its own agent/harness machinery
- Related: [CLAUDE.md](../../CLAUDE.md) — decision-capture routing (rule 1: an unrecorded harness decision already cost a polarity inversion; this is the record)

# ADR-0060: CLI Catalog-Add Commands (`biomarkers add`, `labs add`, `categories list`)

## Status
Proposed

## Context and Problem Statement
The first real manual-entry session (2026-07-18, the quickstart walk-through) hit the
catalog boundary immediately: the seed is 64 biomarkers, a real panel exceeds it, and
`healthspan enter` deliberately cannot create biomarkers inline — the [ADR-0057](0057-reference-data-and-catalog-import-implementation-decisions.md)
§9 same-batch constraint requires a biomarker to be *stored* before results reference it,
and the entry flow's unresolved-name pick searches only existing rows. The only escape was
the raw `POST /v1/import`, which is not a workflow (the lab-name failure hint literally
pointed users at the endpoint). [ADR-0059](0059-cli-manual-entry-implementation-decisions.md)
scoped WI-4's surface to *entry + readback*, leaving catalog curation open; ADR-0059 is
now Accepted, so extending that surface takes this extension ADR (CLAUDE.md governance
rule 4).

## Decision Drivers
- The blocked flow is mid-panel: the owner is holding a lab report; adding the missing
  biomarker must be one command, not a JSON payload
- Reuse over invention: the catalog-import path (ADR-0057) already validates, audits, and
  reconciles catalog rows; the CLI session/credential model (ADR-0059 `_Api`) already
  exists — the commands must be thin clients over both
- Never silently wrong: an "add" must not overwrite, and must not claim an insert that
  did not happen

## Considered Options
1. Interactive create-inside-`enter` (offer to create when the pick finds nothing)
2. **Standalone add-only commands, thin clients over the catalog import** (chosen)
3. A generic `healthspan import <file.json>` command

## Decision Outcome
Chosen: **option 2**. Option 1 tempts mid-draw catalog writes that the §9 same-batch
constraint exists to prevent (and a mistyped name becoming a catalog row is the failure
ADR-0054 fail-loud resolution guards against); option 3 serves adapters, not a person
holding a lab report, and remains open for Phase 7.

### 1. Command surface
- **`healthspan biomarkers add NAME`** — options `--unit` (the `canonical_unit` that
  `enter` offers as the default unit), `--category` (name; default `not_assigned`),
  `--loinc`, `--description`, `--token-name`.
- **`healthspan labs add NAME`** — options `--description`, `--token-name`.
- **`healthspan categories list`** — a new `categories` read group completing the
  catalog read surface ([ADR-0053](0053-read-endpoint-surface-and-pagination.md) pattern;
  `GET /v1/categories` previously had no CLI client), so `--category` values are
  discoverable.

All three run on the ADR-0059 per-command `_Api` session (config + keyring + one client);
the adds `POST /v1/import` with the single table's rows; nothing touches the database
directly. The `enter` lab-not-found hint now names `labs add` instead of the raw endpoint.

### 2. Add-only: `conflict_policy` fixed at `reject`, no policy flag
The import engine reconciles the **full column shape** — a column omitted from an upsert
row is filled from table defaults ([ADR-0057](0057-reference-data-and-catalog-import-implementation-decisions.md) §4),
so a partial "edit" would silently overwrite unspecified columns (e.g. reset an existing
biomarker's category to `not_assigned`). An add command must never carry that footgun, so
no `--on-conflict` is offered. **Catalog editing from the CLI is deferred** — it needs a
merge-aware read-modify-write, which is a different design; tracked in
[open-questions.md](../open-questions.md) ("CLI catalog editing"). Until then, editing
stays on `POST /v1/import` with an explicitly full row.

### 3. Honest outcome reporting from the import summary
`reject` does not reject an *identical* row — the catalog reconcile reports it
`rows_unchanged` in a no-op `200` (ADR-0057). The command reads the summary and says
"already in the catalog (id N); nothing changed" — exit 0, never a false "Added". The
three outcomes:
- inserted → `Added biomarker '…' (id N, category[, unit]).` / `Added lab '…' (id N).`
- identical existing row → the honest no-op line, exit 0 (idempotent re-run is safe)
- same name, different values → the `reject` conflict error, exit 1, with a hint naming
  the endpoint-with-full-row as the current edit path

A name-variant that *normalizes* to an existing canonical name is rejected by the
server's ambiguity validation ([ADR-0054](0054-biomarker-name-alias-fallback.md) §4) with
its own message — the commands add no client-side duplicate of that rule.

**Labs get a client-side case-variant guard.** `labs.name` is unique under *binary*
collation and the import natural-key match is exact — but `enter` resolves lab names
case-insensitively, so `labs add "quest"` beside a stored `"Quest"` would insert a
second row that poisons every later lab prompt ("matches more than one lab by case").
Biomarkers are immune via the server-side normalization guard; labs have no server
equivalent, so `labs add` rejects a case-insensitive clash with a different exact
spelling before submitting. The guard is a **best-effort preflight, not a transactional
invariant**: it covers only this command (the raw endpoint can still create
case-variants), and two racing adds could slip a variant past it — accepted at
single-user, human-paced CLI scale, where the residue's damage is an ambiguous lab
prompt (recoverable via `--lab-id`), not corruption. The identity-layer fix
(case-insensitive lab-name uniqueness enforced for *all* import callers, atomically) is
deferred with its own [open-questions.md](../open-questions.md) entry and closes both
gaps when its trigger fires.

### 4. Category by name, resolved case-insensitively, default `not_assigned`
`--category` takes the category *name* (matching the `?category=` read-filter ergonomics,
[ADR-0057](0057-reference-data-and-catalog-import-implementation-decisions.md) §10) and
resolves it case-insensitively (casefold — the same client-side rule ADR-0059's lab-name
match uses) against `GET /v1/categories`; unknown names fail listing the known set.
Omitting it lands the reserved default (id 0, [ADR-0055](0055-biomarker-category-taxonomy.md)) —
"needs categorizing" is a first-class state, and recategorization is a later edit, not a
blocked add.

### 5. Post-add readback echoes the id
After a successful add the command reads the row back and echoes its id — `labs add`
because `enter --lab-id` consumes it; `biomarkers add` for symmetry and confirmation the
resolver will now find it (the same normalized-name match the `enter` resolver uses).

### 6. The `enter` resolution snapshot refreshes on a miss
`enter` loads the catalog + alias namespace once per session (ADR-0059). With adds now
one command away, the natural real-world flow is a *second terminal* running
`biomarkers add` beside a live `enter` — which a session-start snapshot would silently
defeat (the new biomarker would stay invisible until `enter` exits, with nothing
explaining why). So an exact-match **miss** now re-fetches the catalog + alias namespace
once and re-checks before the interactive pick, re-applying this session's
queued-but-unwritten aliases on top (they exist nowhere server-side until the draw
commits, so a plain rebuild would lose them). The refresh runs only on the miss path —
never per-result — and costs one catalog fetch against a ~70-row table. Adding while
sitting *inside* the pick prompt still requires leaving it (blank skip) and re-typing
the name, which triggers a fresh miss.

### Positive Consequences
- A missing biomarker or lab is a one-command fix mid-session; the interim helper script
  is retired
- The single validated write path keeps its monopoly — the commands add no second write
  surface, no new scopes, no new endpoints
- Idempotent re-runs are safe and honestly reported

### Negative Consequences / Tradeoffs
- Catalog *editing* is still endpoint-only — accepted, deferred with its own entry
- `biomarkers add` fetches the catalog twice (category resolution + readback) — trivial
  at catalog scale, on one keep-alive session

## Consequences for Other Documents
- **[open-questions.md](../open-questions.md)**: new "CLI catalog editing" deferral (this PR)
- **[manual-entry-quickstart.md](../manual-entry-quickstart.md)**: the missing-biomarker
  path documented (this PR)
- **[ADR-0059](0059-cli-manual-entry-implementation-decisions.md)** (Accepted): navigation
  link — `Extended by: ADR-0060` (permitted Links-only addition)
- **[api-reference.md](../api-reference.md)**: no change — no REST surface is added or
  altered; these are clients of existing endpoints

## Links
- Extends: [ADR-0059](0059-cli-manual-entry-implementation-decisions.md) — grows the CLI
  surface it scoped to entry + readback; reuses its `_Api` session and credential model
- Builds on: [ADR-0057](0057-reference-data-and-catalog-import-implementation-decisions.md) —
  the catalog-import reconcile these commands submit through (and whose full-row shape
  motivates add-only)
- Related: [ADR-0054](0054-biomarker-name-alias-fallback.md) — the server-side ambiguity
  validation that guards normalized-name collisions
- Related: [ADR-0055](0055-biomarker-category-taxonomy.md) — the reserved `not_assigned`
  default the category option lands on
- Related: [ADR-0052](0052-bulk-import-identity-and-conflict-resolution.md) — the
  conflict-policy semantics (`reject`, identical-row reconcile) the outcome reporting
  reads back

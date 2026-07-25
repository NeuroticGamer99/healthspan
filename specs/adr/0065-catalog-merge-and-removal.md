# ADR-0065: Catalog Merge and Removal Semantics

## Status
Proposed

## Context and Problem Statement
Phase 3 delivered catalog **growth** (`biomarkers add`, `labs add`, alias capture in `enter`)
but no catalog **correction**. The first real entry session produced the predicted failure
([open-questions.md](../open-questions.md), "CLI catalog editing"): a same-concept duplicate
biomarker — added under a lab's name beside the seeded canonical whose name normalizes
differently, so [ADR-0054](0054-biomarker-name-alias-fallback.md)'s uniqueness validation
cannot catch it — with results split across the two ids and framework ranges resolving on
only one. Phase 3.5 ([development-plan.md](../development-plan.md)) owns the fix: remove,
merge, and first-class alias management.

The 2026-07-19 architecture review ([3.A](../reviews/architecture-review-2026-07-19.md),
its most important finding) requires this ADR **before** the merge work item exists in code,
because merge collides with two Accepted decisions:

1. **Correction routing.** [ADR-0027](0027-audit-trail-and-corrections.md) gives every
   content-table change one of two routes — value corrections supersede; designated metadata
   columns update in place — and re-pointing a result's `biomarker_id` is deliberately
   neither: it is an identity fix, a category the model does not have.
2. **Natural-key collision.** `ux_lab_results_natural_key` (migration 0003) permits one
   current result per `(lab_draw_id, biomarker_id)`. When one draw holds current results for
   both duplicate rows — precisely the scenario motivating merge — the re-point violates the
   index, and without a decision here the database decides: a runtime `IntegrityError`.

Scope (owner decision, 2026-07-24): merge and removal cover **biomarkers and labs
symmetrically** — a lab case-variant pair (`"quest"` beside `"Quest"`,
[ADR-0060](0060-cli-catalog-add-commands.md) §3) is the same defect, and a lab with draws
attached cannot be fixed by unreferenced-only removal. Referencing tables, from the schema:
`biomarkers` ← `lab_results`, `biomarker_aliases`, `framework_ranges`; `labs` ← `lab_draws`.

Terminology: the **orphan** is the duplicate row being merged away; the **survivor** is the
row that keeps the identity. The owner names both explicitly — merge direction is never
inferred.

## Decision Drivers
- The merge must **complete**: the orphan row must end deleted, or the catalog stays
  polluted and every reader filters tombstones forever — the soft-delete trap ADR-0027
  already rejected
- **Never silently wrong**: merge must never choose between conflicting clinical values, and
  a collision may mean the merge premise itself is wrong (two method-variant measurements
  conflated — the [ADR-0032](0032-biomarker-loinc-cardinality.md) cardinality case)
- **INV-7** ([security.md](../security.md)): merge and removal are exactly the "later
  features that merge or delete data" the invariant warns about — their cleanup must only
  ever *append* audit records, never touch one
- History must survive: after the orphan is gone, "these results were recorded against a
  different catalog name until the merge on date X" must remain answerable
- A later mention of the merged-away biomarker name must not silently resurrect the
  duplicate (labs, having no alias table, get an honestly weaker guarantee — §4)
- No schema change: catalog tables carry no `superseded_by` (supersession is a content-table
  mechanism), the `audit_log` `operation` CHECK is closed, and this ADR should not force a
  migration — Phase 3.5's only planned migration rides other work

## Considered Options
1. Re-point by **supersession**: each current referencing row is superseded by a copy
   pointing at the survivor
2. Re-point **in place** across all referencing rows, sanctioned through ADR-0027's own
   per-table designated-column declaration (chosen)
3. **Tombstone** the orphan (`merged_into` column) instead of deleting it

For the natural-key collision: **refuse and report** (chosen) vs. auto-supersede the
orphan's result as a duplicate vs. merging values.

## Decision Outcome

### 1. Merge re-points in place — an identity repair under ADR-0027's designated-column rule
Merge executes `UPDATE … SET biomarker_id = survivor` (labs: `lab_id`) across **all** rows
referencing the orphan — current and superseded chain members alike — then hard-deletes the
orphan catalog row, and only then inserts the merged-away name as a survivor alias (§4: the
order is forced by [ADR-0054](0054-biomarker-name-alias-fallback.md) §3, which rejects any
alias normalizing to a live biomarker's canonical name). One transaction; the supersession
machinery is not involved.

Option 1 fails structurally, not merely aesthetically. Supersession creates new *current*
rows pointing at the survivor, but every already-superseded chain member still holds the
orphan's id — and with `foreign_keys=ON` ([ADR-0035](0035-migration-execution-semantics.md))
those references make the orphan row undeletable. The merge cannot complete without option
3's tombstone column, which is soft-delete by another name: ADR-0027 rejected `deleted_at`
because every query in the system would filter around it forever, and a `merged_into`
catalog tombstone has exactly that shape. Supersession also distorts what chains *mean*:
they answer "what did we believe about this value", and a merge would insert hops where the
value is bit-identical and only the catalog pointer moved.

The in-place route is sanctioned through ADR-0027's own extension point: *"which columns
qualify as designated metadata is declared per table in the schema documentation — the
default for any column is supersession."* This ADR declares `lab_results.biomarker_id` and
`lab_draws.lab_id` **merge-repairable reference columns** — repairable *only within the
merge operation*, never by general edit. The fit is genuine, not a loophole: as with the
timezone carve-out, the clinical observation is untouched; what is repaired is recorded
context — here, which catalog row names the concept. The owner's merge command asserts the
two rows were always one concept; given that assertion, the rows never changed meaning — the
catalog had two ids for one meaning. Chains move whole, so chain integrity and the
`*_current` views are unaffected; [ADR-0052](0052-bulk-import-identity-and-conflict-resolution.md)
§3 established the same shape when it declared `lab_draws`' non-key columns designated
metadata precisely to keep draw ids stable.

**Merge never edits the survivor's own row.** The survivor's catalog columns
(`canonical_unit`, `loinc_code`, `category_id`, description) stay exactly as they are;
anything worth porting from the orphan's *columns* is a separate catalog edit through the
existing full-row path (ADR-0060 §2), and the orphan's column values survive in its delete
audit image. The orphan's *reference rows* do move — its results, its aliases, and its
framework ranges all become the survivor's. That is not an edit of the survivor but the
owner's same-concept assertion working as intended: a range or alias recorded for the
concept is a fact about the concept under either id. The visible consequence is deliberate
and stated (Consequences): a survivor lacking a range for some framework may resolve the
orphan's after the merge, turning `no_range` results into flagged ones; when both rows carry
a range for the same framework, §3 refuses rather than choosing. Re-pointed results and
transferred ranges keep their stored units — range comparison already normalizes per
[ADR-0031](0031-units-and-ucum.md)/[ADR-0058](0058-range-comparison-implementation-decisions.md),
and a non-convertible unit surfaces through that existing fail-loud path; merge rewrites no
values and no units.

### 2. Audit shape: per-row `update` images, delete image as the merge record
The [ADR-0027 granularity rule](0027-audit-trail-and-corrections.md) (2026-07-06 review,
T1.1) keys audit shape on write path: batch-level audit exists *only* for bulk-import
inserts; every mutation of existing data is per-row with images. Merge mutates existing
rows, so:

- one `update` audit row per re-pointed row, full old/new images, `reason` auto-populated
  (`catalog merge: biomarker 'Carbon Dioxide' (id 71) → 'CO2 (Bicarbonate)' (id 12)`)
- one `insert` audit row for the auto-alias when it is written (§4's no-op branch writes
  none)
- one `delete` audit row for the orphan, full row image — **this row is the merge's own
  durable record**: what was merged away, into what, when, by whom, in the shared `reason`

Scale supports the precedent: catalog references are lab-result-sized (tens to low hundreds
of rows), not CGM-sized, and `audit_log` grows with mutations by design. No new `operation`
value is minted — the existing CHECK constraint stands, so **merge requires no migration at
all**. INV-7 holds by construction: every step appends audit rows; none reads, updates, or
deletes one.

One accepted residue: SQLite may later reuse a deleted orphan's rowid, so a bare id in an
old audit row is not globally unique across time. Audit images are self-describing (the
JSON carries names, not just ids), so the record stays interpretable; an `AUTOINCREMENT`
rebuild is not worth a migration.

### 3. Collisions refuse and report — merge is a pure re-point that never chooses values
Three uniqueness surfaces can collide, and all take the same rule:

| Surface | Collision | Meaning |
|---|---|---|
| `ux_lab_results_natural_key` | one draw holds current results for both orphan and survivor | double entry — or two genuinely distinct measurements conflated |
| `framework_ranges` `UNIQUE(framework_id, biomarker_id, effective_date)` (and the dateless-default partial index) | both rows carry a range for the same framework and date | two curated range values, possibly different |
| `ux_lab_draws_natural_key` (lab merge) | both lab spellings hold a **current** draw at the same `draw_utc` | the same real draw entered twice |

The preflight carries one check beyond this table: §4's defensive alias-namespace
condition — an equal-normalized alias pointing at a *third* biomarker — which takes the
same refuse-and-report rule (biomarker merges only; the schema cannot collide there, so it
is an ambiguity check, not a uniqueness surface).

**Any collision aborts the entire merge; nothing is written.** The refusal is the report: it
lists every colliding pair with both rows' values, discovered by preflight before the first
write (mirroring [ADR-0004](0004-data-ingestion-strategy.md)'s collect-all-errors
validation). Two constraints keep the report the only failure mode: the executing merge's
preflight runs under its own `BEGIN IMMEDIATE` — [ADR-0057](0057-reference-data-and-catalog-import-implementation-decisions.md)'s
established rule that a read-then-write check is race-free only while the write lock is
held, already implemented in the import path — and an `IntegrityError` that the merge's
writes raise despite the preflight surfaces as this same refusal report, never as a raw
database error (the "database decides" outcome this ADR exists to eliminate). A dry-run's read may sit outside
the lock; its report is advisory, and only the executing merge's own preflight is binding. The owner resolves each pair under ADR-0027's correction and delete
*semantics* — the colliding rows are ordinary data errors — then re-runs the merge. The
*surfaces* for those fixes do not yet exist: there is no delete or correction endpoint
anywhere in the implemented API (merge and removal are themselves the first write endpoints
beyond import and entry, §6). The implementing work item must therefore ship the resolution
path its own refusal report points at — at minimum result deletion, audited
framework-range deletion (a range collision survives any value edit, since the colliding
key remains; only removing one row clears it), and duplicate-draw cleanup for lab
merges — or a dirty merge stays loudly blocked until such a surface lands.

One boundary is accepted rather than solved: ADR-0027 forbids deleting supersession-chain
rows, and correction cannot move identity (that categorization is this ADR's own premise) —
so a colliding pair whose rows are **both** chain members has no resolution path at all, and
that catalog pair simply stays unmerged, named in every refusal. This is recorded as a
negative consequence, not papered over: a corrected result on each side of a same-concept
duplicate is strong evidence the pair deserves owner scrutiny rather than automation, and if
the state ever arises in practice the escape is a deliberate extension of ADR-0027's delete
rule — not an improvisation here.

Why not auto-resolve: a result collision has two sub-cases, and only the owner can tell them
apart. A true double entry is safely collapsible — but two *distinct measurements* wrongly
conflated (LDL direct beside LDL calculated, ADR-0032) mean the merge premise itself is
wrong, and the collision is the one signal saying so. Auto-superseding the orphan's result
would fabricate a correction relationship between possibly-different measurements and let
the tool decide which clinical value stays current; merging values is the same error with
more steps. The invariant this buys is crisp and testable: **merge never mutates, creates,
or supersedes a value row — it only re-points, aliases, and deletes the orphan catalog row.**

Considered and rejected for now: auto-resolving collisions whose two results are
bit-identical on every value column (the plain enter-it-twice case). It is defensible — no
value choice is made — but it breaks the invariant's crispness for a case whose manual fix
is one delete. Revisit trigger: real merges hitting identical-value collisions at a
friction scale the refusal report makes painful.

### 4. The merged-away name becomes an alias of the survivor — after the orphan is deleted
As its final step, merge inserts one `biomarker_aliases` row: the orphan's `canonical_name`,
normalized per ADR-0054, pointing at the survivor (audited `insert`). The ordering is
forced: [ADR-0054](0054-biomarker-name-alias-fallback.md) §3 rejects writing an alias whose
normalized form equals any live biomarker's canonical name, so the insert is only legal
*after* the orphan row is deleted — re-point, delete, then alias, still one transaction,
and the namespace invariant holds at rest, not merely at commit. This is what makes the
merge durable against the future: the next `enter` session or result import that speaks the
orphan's name resolves to the survivor instead of recreating the duplicate.

Collision handling is already settled by the schema and the ordering: the orphan's own alias
rows were re-pointed to the survivor earlier in the transaction (`alias_normalized` is
globally unique, so re-pointing `biomarker_id` alone can never collide), and ADR-0054 §3
never permitted storing an alias equal to the orphan's own name while it lived — so the
final insert should find the name free. Defensively: if an equal normalized alias
nonetheless exists (data predating the guard), a survivor-pointing one makes the insert a
no-op; one pointing at a *third* biomarker is a namespace ambiguity ADR-0054 exists to
prevent — refuse and report, same rule as §3, and checked in §3's preflight (the ambiguous
state is a pre-existing fact readable before any write; nothing the merge itself does can
create it).

Labs have no alias table: a merged-away lab name survives only in the delete audit image,
and nothing durably prevents a later write from re-creating it — ADR-0060 §3's case-variant
preflight is by its own declaration best-effort and CLI-only, and the identity-layer fix
(case-insensitive lab-name uniqueness for **all** import callers) is separately deferred
with its own [open-questions.md](../open-questions.md) entry. Accepted asymmetry, honestly
weaker than the biomarker guarantee (see Negative Consequences): a resurrected lab duplicate
costs an ambiguous lab prompt, never a mis-resolved value — labs are containers, not
measurands — and is recoverable by re-running the merge.

### 5. Removal: observations block, own attributes cascade
`remove` deletes a catalog row that no observation references. The rule distinguishes what
the row *has* from what was *measured against* it:

- **Observations block.** Any `lab_results` row referencing the biomarker (any `lab_draws`
  row referencing the lab), current **or superseded**, refuses the removal with a count and
  a pointer to merge. The FK constraints under `foreign_keys=ON` are the schema backstop;
  the command adds the preflight and the honest message.
- **Attributes cascade.** The row's own dependent rows — its aliases, its framework ranges —
  delete with it in the same transaction, each with a per-row `delete` audit image. An alias
  or curated range *of* a catalog entry has no meaning once the entry is gone, and refusing
  on them would strand removal behind surfaces that do not exist (there is no
  range-delete command). Removal deletes exactly what merge transfers — the difference is
  the assertion: merge asserts the concept continues under the survivor, so its facts move;
  removal asserts nothing continues, so they go with it.

The catalog-row delete itself follows ADR-0027 delete semantics unchanged: full image in
`old_values`, and — chains being impossible on catalog tables — no chained-row
complications.

### 6. Surface and scope
Merge and removal are the platform's first write endpoints beyond import and manual entry.
They are **new authenticated REST endpoints on the Core Service** — they cannot ride
`POST /v1/import` (the reconcile engine has no delete or re-point vocabulary, ADR-0057), and
nothing touches the database except the Core repository layer, preserving the single
validated write path ([ADR-0004](0004-data-ingestion-strategy.md)). They require **both
`write` and `import`** (owner decision, 2026-07-25): `write` because this is data-plane
curation, not control-plane administration — `admin` stays the token/backup/process tier
([ADR-0026](0026-named-scoped-tokens.md)) — and `import` because ADR-0026 already treats it
as the mass-mutation marker, and an N-row identity re-point plus a catalog delete is a mass
mutation. The pairing is load-bearing for exactly one credential: `automation-host` carries
a documented `write` opt-in (automations that flag results) while ADR-0026 deliberately
withholds `import` from it, because that process credential is what directory-loaded
automation plugins are handed (INV-3). Under bare `write`, the opt-in would also have
handed those plugins catalog rewrites; requiring both scopes keeps that door shut. No
credential gains a capability class it lacked — every default token that can merge
(`cli-admin`, `cli-plugins`, `gui`) can already mass-mutate through `/v1/import` — and the
MCP default token (read-only) remains nowhere near either operation. Every merge/removal
step is per-row audited with images — reconstructible, attributable, loud.

Exact endpoint paths, request shapes, and CLI command forms are the implementing work item's
to record — [api-reference.md](../api-reference.md) per decision-capture rule 2, and the CLI
surface as an edit to ADR-0060, whose worklist-T3.5 flip hold this ADR extends through that
work item (recorded in the worklist itself, same PR) so the edit stays cheap. This ADR fixes only the semantics those surfaces must implement, plus one
ergonomic requirement: the preflight collision report must be reachable without executing
(a dry-run form), since §3's refuse-and-report is the owner's working tool for resolving a
dirty merge, not just an error — and removal's cascade manifest (the aliases and ranges §5
deletes with the entry) must likewise be readable without executing, since ADR-0027's
delete confirmations identify exactly what will be deleted.

### 7. Events
`data.corrected` / `data.deleted` emission is deferred to Phase 4 with the event bus — the
same posture as ADR-0052 §4: the audit rows are the durable record until then. When the bus
lands, merge and removal emit per their constituent operations, invalidating ADR-0021
aggregates.

### Positive Consequences
- The real duplicate (and every future one) is fixable in-app; the merge completes — no
  tombstones, no catalog residue
- The audit story is single-homed: the entire history of a merge lives in `audit_log`,
  which INV-7 guarantees no later feature can rewrite
- A later mention of the merged-away biomarker name cannot silently recreate the duplicate,
  on either path: observation paths (`enter`, result imports via `biomarker_name`) resolve
  through the alias to the survivor, while catalog writes of the name (`biomarkers add`, a
  catalog import row) are loudly rejected by ADR-0054 §3's namespace validation — resolution
  or refusal, never a silent second concept
- The survivor inherits the orphan's non-conflicting framework ranges — results that
  reported `no_range` may begin flagging after the merge; intended (the same-concept
  assertion at work), per-row audited, and refused outright when the two rows' ranges
  conflict (§3)
- The pure-re-point invariant makes the operation's safety argument one sentence long, and
  its tests mechanical

### Negative Consequences / Tradeoffs
- A dirty merge (collisions present) is a multi-step workflow: resolve each reported pair,
  re-run — accepted; the collision may be the only warning that the merge is conceptually
  wrong. Until the implementing work item ships the resolution surfaces §3 requires, a
  dirty merge is blocked outright — loudly, with the report naming what needs fixing
- A colliding pair whose rows are both supersession-chain members has **no** resolution
  path — chain rows are not deletable (ADR-0027) and correction cannot move identity — so
  that pair stays permanently unmerged unless a future ADR extends the delete rule (§3)
- The lab-side guarantee is weaker than the biomarker-side one: with no alias table, a
  re-imported merged-away lab spelling can recreate the duplicate until the deferred
  identity-layer lab-name uniqueness lands ([open-questions.md](../open-questions.md)) —
  recoverable by re-running the merge, never a mis-resolved value (§4)
- "What did we believe" queries that walk audit history must interpret re-pointed rows
  through their `update` audit rows rather than supersession chains — accepted; identity
  repair is context, not value history
- Attribute cascade on removal deletes curated ranges with the entry — recoverable from
  audit images, and blocked anyway whenever results exist (the only case with real stakes)

## Consequences for Other Documents
- **[data-model.md](../data-model.md)**: cross-cutting concerns gain the catalog-correction
  entry declaring the two merge-repairable reference columns (this PR)
- **[testing-strategy.md](../testing-strategy.md)**: integration-test targets for merge
  atomicity, collision refusal, audit shape, and removal guarantees (this PR)
- **[open-questions.md](../open-questions.md)**: the "CLI catalog editing" entry's Phase 3.5
  capture now points here for the decided semantics; the entry stays open until the work
  item lands the commands (this PR)
- **[development-plan.md](../development-plan.md)**: the Phase 3.5 catalog-correction bullet
  restated to match — observation-blocked removal with attribute cascade, labs included in
  merge, the §3 resolution surfaces in the work item's scope (this PR)
- **[ADR-0027](0027-audit-trail-and-corrections.md)** (Accepted): navigation link —
  `Extended by: ADR-0065` (permitted Links-only addition)
- **[ADR-0052](0052-bulk-import-identity-and-conflict-resolution.md)** (Accepted):
  navigation link — `Extended by: ADR-0065` (permitted Links-only addition; this ADR grows
  the `lab_draws` column-class declaration its §3 recorded)
- **[ADR-0060](0060-cli-catalog-add-commands.md)** (Proposed): no change now; the
  implementing work item adds the command surface there
- **[api-reference.md](../api-reference.md)**: no change now; endpoints land with the work
  item per decision-capture rule 2

## Links
- Extends: [ADR-0027](0027-audit-trail-and-corrections.md) — declares the merge-repairable
  reference columns through its per-table designated-column rule; adds no third correction
  category
- Cites: [security.md](../security.md) INV-7 — every merge/removal step appends audit
  records; none touches an existing audit row
- Resolves: [architecture review 2026-07-19](../reviews/architecture-review-2026-07-19.md),
  item 3.A — both collisions decided before the merge work item
- Extends: [ADR-0052](0052-bulk-import-identity-and-conflict-resolution.md) — grows the
  `lab_draws` column-class declaration its §3 recorded (the key column `lab_id` becomes
  merge-repairable, merge-only) and builds §3's collision rules on the natural keys it
  established
- Related: [ADR-0054](0054-biomarker-name-alias-fallback.md) — the namespace validation
  that forces §4's delete-before-alias ordering and loudly rejects catalog re-adds of the
  merged-away name
- Related: [ADR-0057](0057-reference-data-and-catalog-import-implementation-decisions.md) —
  the catalog-import reconcile that merge deliberately does not ride
- Related: [ADR-0060](0060-cli-catalog-add-commands.md) — the add-only surface this
  correction surface completes; held Proposed for the command-surface edit
- Related: [ADR-0032](0032-biomarker-loinc-cardinality.md) — the method-variant cardinality
  case behind §3's refusal argument

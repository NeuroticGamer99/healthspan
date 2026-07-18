# ADR-0055: Biomarker Category Taxonomy — First-Class Categories with a Reserved Default (Phase 3 D1)

## Status
Proposed

## Context and Problem Statement

`biomarkers.category` shipped in migration 0001 as a plain `TEXT` column ([ADR-0030](0030-biomarker-identity.md) sketch, DDL in [`0001_initial_schema.sql`](../src/healthspan/migrations/0001_initial_schema.sql)) — free text, no constraint, no controlled vocabulary. [open-questions.md](../open-questions.md) flagged the taxonomy as "must resolve before bulk data entry begins" so categories are consistent across sources. Phase 3 is where bulk entry begins, so the gate (development-plan.md D1) is due.

Two real-world category lists framed the decision (2026-07-14): the database owner's Function Health categories and their prior hand-maintained spreadsheet categories. Both mixed two organizing axes — **physiological system** ("what does this measure": Heart, Kidney, Liver, Thyroid) and **clinical theme** ("why do I care": Biological Age, Stress & Aging, Male Health, Inflammation). A single `category` column asked to carry both axes is the source of the inconsistency the owner observed in their own data (ApoB is `Heart` by theme but `Lipoproteins` by biology; hs-CRP is `Inflammation` by theme but has no clean system home). This ADR fixes the axis, the storage shape, the seed vocabulary, and the future path to cross-cutting tags.

## Decision Drivers

- Free-text categories drift silently (`lipid` vs `lipids`) — the same confidently-wrong-data family the alias resolver ([ADR-0054](0054-biomarker-name-alias-fallback.md)) and the unit-normalization safety work ([ADR-0031](0031-units-and-ucum.md)) close elsewhere
- A single category column is a partition (one home per biomarker); the owner explicitly wants cross-cutting classification too — those are two different structures and must not be conflated
- The taxonomy is the owner's to curate and will change; it should be editable data, not schema
- "Not categorized" must be a defined, first-class concept, not a NULL whose meaning lives only in client code
- The MCP surface (Phase 4) makes cross-cut queries valuable — the tag path should be reachable additively

## Considered Options

- **Free-text `category` (status quo)** — rejected: drifts, no controlled vocabulary
- **`category` as a closed enum in code / CHECK constraint** — rejected: the vocabulary is owner-editable data, not a code constant; a CHECK would need a migration per edit
- **First-class `categories` catalog table + `category_id` FK, with a reserved default row** — chosen
- **Many-to-many tags from day one** — rejected *for now*: premature; deferred with a trigger (see §4)

## Decision Outcome

### 1. `categories` catalog table + `category_id` FK, single primary category

Categories become a first-class catalog table; each biomarker points at exactly one. Realized in **migration 0004** (with Phase 3 WI-2; concrete DDL lands in [data-model.md](../data-model.md) that PR):

```sql
CREATE TABLE categories (
    id          INTEGER PRIMARY KEY,
    name        TEXT NOT NULL UNIQUE,        -- 'lipoproteins', 'thyroid', 'screening', ...
    description TEXT
) STRICT;

-- Reserved default, seeded by the migration itself (not owner-editable catalog data):
INSERT INTO categories (id, name, description)
VALUES (0, 'not_assigned', 'Reserved: biomarker has not been assigned a category.');

CREATE TABLE biomarkers (
    id             INTEGER PRIMARY KEY,
    canonical_name TEXT NOT NULL UNIQUE,
    loinc_code     TEXT UNIQUE,
    canonical_unit TEXT,
    category_id    INTEGER NOT NULL DEFAULT 0 REFERENCES categories(id),
    description    TEXT
) STRICT;

CREATE INDEX ix_biomarkers_category ON biomarkers (category_id);
```

The FK makes drift structurally impossible — a biomarker can only name a category that exists. Like the other catalog tables, `categories` does not supersede (no `*_current` view; deletes are hard deletes with the mandatory [ADR-0027](0027-audit-trail-and-corrections.md) audit row). The existing `GET /v1/biomarkers?category=` filter ([ADR-0053](0053-read-endpoint-surface-and-pagination.md)) keeps accepting the category *name* (resolved to `category_id`), so clients never handle raw ids.

**Lookup key semantics.** The `?category=` resolution is **case-insensitive** — names are stored lowercase and the lookup is normalized before matching, so `?category=Thyroid` and `?category=thyroid` resolve identically. Without this, a case-mismatched filter returns a silently empty page (SQLite's default `UNIQUE`/`=` on `TEXT` is case-sensitive) — the wrong-answer-not-an-error family this ADR closes elsewhere (§Decision Drivers, and the naive-join argument in §2). Because the name doubles as the lookup value while remaining an owner-editable display label (§2), **renaming a category is a breaking change to any saved `?category=` filter** that names the old value. Accepted under the single-owner model: the owner curates both the taxonomy and the queries, renames are rare and deliberate, and the empty result is self-evident to the one client. A separate immutable `slug` column (human-readable, rename-stable) is the additive escape hatch if a future multi-client or MCP surface needs filters that survive renames.

**Migration 0004 rebuilds `biomarkers`** (SQLite cannot alter a column type). The table is empty until WI-2 seeds it and has no `*_current` view, so the rebuild moves zero rows in practice and sidesteps the additive-`ALTER TABLE`/view-recreation convention ([open-questions.md](../open-questions.md), Schema); it is still written correctly for the general (non-empty) case so a re-run from an older snapshot cannot lose rows.

### 2. Reserved default over nullable FK

`category_id` is `NOT NULL DEFAULT 0`, pointing at a reserved `not_assigned` row, rather than a nullable FK meaning "uncategorized." The reserved row is **identified by its id (0), not its name** — the display text may be renamed freely; only *deleting* id 0 is forbidden at the write path (the reserved-row rule, mirroring the reserved token-name precedent in [ADR-0051](0051-auth-lifecycle-and-rate-limiting-implementation-decisions.md)).

Rationale (owner decision, over the orthodox NULL):
- "Not assigned" is a defined concept living in the data, not a NULL whose meaning every UI/GUI/MCP client must independently know to render — the per-client-convention drift this project routinely closes.
- A naive `JOIN categories` (rather than `LEFT JOIN`) silently drops uncategorized biomarkers under a nullable FK — a wrong-answer-not-an-error bug. `NOT NULL` + sentinel makes the naive join correct and "count by category" aggregations complete.
- An import row omitting the category falls to the default explicitly; the server never guesses.

Consequence accepted knowingly: "needs categorizing" is `WHERE category_id = 0`, not `IS NULL` — id 0's meaning is part of the spec, which is exactly where the owner wanted it.

**Reserved-row presence is an application invariant, not a schema one.** `PRAGMA foreign_key_check` — the integrity gate the migration runner, backup, and restore all enforce ([db.py](../src/healthspan/db.py), [migrate.py](../src/healthspan/migrate.py)) — proves every `category_id` points at an existing row, but it does *not* prove id 0 itself is present. The check stays green after the reserved row is deleted while empty, until the next import defaults a biomarker to `category_id = 0` (the column default) and hits a confusing far-from-cause FK failure. WI-2 therefore owns two obligations: (a) a delete-guard forbidding removal of id 0 — the same reserved-name principle as [ADR-0051](0051-auth-lifecycle-and-rate-limiting-implementation-decisions.md)'s guard, with the enforcement mechanism left to the implementing WI (the implementing [ADR-0057](0057-reference-data-and-catalog-import-implementation-decisions.md) §1 chose a SQL `BEFORE DELETE` trigger as the single enforcement point, not an app-level check) — and (b) a startup/verify assertion that the reserved row exists, alongside the existing `schema_version` verification — so a missing reserved row fails loudly at open, not mid-import.

### 3. Axis: physiological system, with an orphan-rescue rule

The primary category axis is **physiological system**. A *theme* (an audience or clinical-concern cut) earns a primary category **only when its members have no system home**; otherwise a theme is a tag (§4). This rule keeps the partition coherent:

- `environmental_toxins` (PFAS, lead) and `screening` (PSA, cancer screens) are themes with no organ-system home → **categories** (orphan rescue).
- `male_health` and `stress_and_aging` scatter entirely onto `hormones` (testosterone/SHBG/estradiol; cortisol/DHEA-S/IGF-1) → **tags**, not categories. A theme that projects cleanly onto a system axis is the signature of a tag.
- `inflammation` is the borderline case kept as a category: its marquee markers double-book (ferritin → `nutrients`; hs-CRP → `heart`), but a residue (hs-CRP, ESR, fibrinogen) has no clean system home, so it earns a slot under orphan rescue — with the understanding that a future `inflammation` *tag* will overlap it.

**Containment ⇒ tag, not sibling category.** The owner's set-theoretic observation — all oncology is screening, not all screening is oncology — is precisely why `oncology` is a tag, not a second category beside `screening`: two categories where one *contains* the other give every oncology marker two valid homes, defeating the partition. `screening` is the category; `oncology` marks the cancer subset as a tag.

### 4. Cross-cutting tags: deferred, additive, trigger = Phase 4 MCP

A single primary category ships now. Cross-cutting classification (the owner's stated intent, valuable once the AI/MCP surface exists) is deferred as a **purely additive** future change: a `biomarker_categories(biomarker_id, category_id)` — or a dedicated `tags` — many-to-many table *alongside* the untouched `category_id`. The primary category remains "which section does this render under"; tags answer "what else is it about." The concrete first tag vocabulary the two source lists kept reaching for: `oncology`, `male_health`, `stress_and_aging`, `supplementing`, `cardiac_risk`, `inflammation`. Recorded as an [open-questions.md](../open-questions.md) deferral; trigger = Phase 4 MCP (or the first real cross-cut need).

### 5. Computed scores are not categorized biomarkers

Externally-supplied *calculated* values (Function Health's Biological Age; the IGF-1 **Z-Score**, published range −2..+2) are **derived data ([ADR-0044](0044-derived-data-points.md)), not `biomarkers` rows**, so they take no category. They are out of scope for this taxonomy. The owner's need to import them for analysis is real and is captured against the ADR-0044 deferral as externally-computed-subtype evidence ([open-questions.md](../open-questions.md), Schema — Derived data points); their storage is ADR-0044-gated (Phase 5 trigger), not a Phase 3 decision. If a future ADR-0044 resolution routes externally-computed snapshots as source-class values, they would gain rows and categories then.

### 6. Seed vocabulary (WI-2 catalog data — generic reference data, safely committable)

Nineteen system-axis categories plus the reserved default:

`not_assigned` (id 0, reserved) · `autoimmunity` · `allergy` · `body_composition` · `electrolytes` · `environmental_toxins` · `heart` · `hematology` · `hormones` · `immune` · `inflammation` · `kidney` · `liver` · `lipoproteins` · `metabolic` · `nutrients` · `pancreas` · `screening` · `thyroid` · `urine`

The seed is a curated starting point, not an authoritative standard (there is no external browsable clinical taxonomy; LOINC classes are lab-organizational, not clinically browsable). Because categories are owner-editable data forever after, the stakes of the initial list are low — the load-bearing decisions are §1–§4, not the exact names. Note two derivations that inform WI-2 seeding intent: `homocysteine → inflammation` (its clinical use is vascular/inflammatory risk, not only metabolic), and `PSA → screening` (with future tags `oncology`, `male_health`).

Categories deliberately excluded and why: Function's `Daily Metrics` (Fitbit — lands in `wearable_daily`, not `biomarker_id`, so the category would be empty, same reasoning that drops a `cgm` category); `Biological Age`/`Z Score` (derived data, §5); the two allergy buckets merged to `allergy`; `Weight`/`InBody`/`Glucose and Insulin`/`Supplement Levels`/`Uric Acid` folded into their system category or dropped as sources/single-marker categories.

### Positive Consequences

- Category drift is structurally impossible (FK), not policed by convention
- "Not assigned" is a first-class, spec-defined concept; naive joins and aggregations stay correct
- The taxonomy is owner-editable data — add/rename/remove without a migration
- The tag path is reachable additively, on the axis the owner actually wants, when MCP makes it valuable
- The system-vs-theme rule gives a repeatable test for future category proposals

### Negative Consequences / Tradeoffs

- Migration 0004 rebuilds `biomarkers` (cheap: empty table, no view), and the reserved-row delete-guard is one more write-path rule to test
- A single primary category forces a "best home" choice for double-booking markers (ferritin, hs-CRP) until tags land — accepted; tags are the designed resolution
- The reserved sentinel means "uncategorized" queries are `= 0`, a project convention downstream code must know (documented here and in data-model.md)
- The category name is both the mutable display label and the `?category=` lookup key; renaming a category breaks any saved filter that names the old value (accepted — single-owner; an immutable `slug` is the additive fix if a multi-client surface ever needs rename-stable filters)

## Consequences for Other Documents

- **[open-questions.md](../open-questions.md)**: "Biomarker category taxonomy" → Resolved (this PR); new "Biomarker cross-cutting tags" deferral with the Phase-4 trigger (this PR); the "Derived data points" deferral gains the Z-Score/Biological Age externally-computed evidence (this PR)
- **[development-plan.md](../development-plan.md)**: D1 gate and decision-gates table → decided (this PR)
- **[data-model.md](../data-model.md)**: `categories` table, `biomarkers.category_id`, the reserved-row and `= 0` conventions — done (this PR)
- **[api-reference.md](../api-reference.md)**: case-insensitive `?category=` name resolution (rename = breaking filter change, §1); `categories` as reference-data — done (this PR)
- **[ADR-0030](0030-biomarker-identity.md)** (Accepted): its `biomarkers` DDL was an illustrative sketch with `...`-elided columns and `category TEXT` as an example; this ADR concretizes `category` into `category_id` without reversing any ADR-0030 decision (identity, LOINC, value model unchanged). Navigation link added to ADR-0030's Links — no content edit to the accepted ADR.
- **[ADR-0057](0057-reference-data-and-catalog-import-implementation-decisions.md)** (Proposed): the implementing WI-2 ADR that resolves the migration DDL mechanics, the startup reserved-row assertion, and the read-side wiring this ADR left open

## Links

- Resolves: [open-questions.md](../open-questions.md), Schema — "Biomarker category taxonomy"
- Concretizes: [ADR-0030](0030-biomarker-identity.md) — the `category` column it sketched
- Related: [ADR-0054](0054-biomarker-name-alias-fallback.md) — the sibling Phase 3 catalog decision (D2); both land in migration 0004 / WI-2
- Related: [ADR-0044](0044-derived-data-points.md) — computed scores (Biological Age, Z-Score) are derived data, not categorized biomarkers
- Extends: [ADR-0053](0053-read-endpoint-surface-and-pagination.md) — the `GET /v1/biomarkers?category=` filter becomes a case-insensitive catalog-name lookup (a breaking change from its free-text filter)
- Related: [ADR-0051](0051-auth-lifecycle-and-rate-limiting-implementation-decisions.md) — the reserved-name write-path precedent the reserved category row mirrors
- Related: [ADR-0027](0027-audit-trail-and-corrections.md) — catalog edits are audited insert/update/delete, not supersession
- Implemented by: [ADR-0057](0057-reference-data-and-catalog-import-implementation-decisions.md) — migration 0004 DDL, the startup reserved-row assertion, and the import-engine generalization

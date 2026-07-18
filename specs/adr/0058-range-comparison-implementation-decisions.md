# ADR-0058: Range Comparison, Flag Vocabulary, and Molar-Mass Persistence (Phase 3 WI-3)

## Status
Accepted

## Context and Problem Statement

Phase 3 WI-3 implements reference-range **comparison**: given a stored lab result and a range framework, decide whether the result is inside that framework's target — with both endpoints normalized to the biomarker's `canonical_unit` before any numeric comparison ([ADR-0005](0005-reference-range-frameworks.md), [ADR-0031](0031-units-and-ucum.md)).

The specs fix the hard constraints and deliberately leave the surface open:

- [ADR-0005](0005-reference-range-frameworks.md) is binding: every range row carries a mandatory UCUM `unit`; comparison **must** normalize result and range to `canonical_unit` before flagging; units that cannot be reconciled **fail loudly, never silently flag**; point-in-time resolution takes the greatest `effective_date <= D`, falling back to the dateless (`NULL`) default, and the schema's `UNIQUE` + partial index guarantee at most one row resolves.
- [ADR-0030](0030-biomarker-identity.md) is binding: a result is not a bare number. A non-`NULL` `comparator` is a censored bound; a `NULL` `value_num` is qualitative. Comparison must respect both rather than coercing to a float.
- [ADR-0056](0056-units-module-api-and-molar-context.md) is binding: molar conversions (mg/dL ↔ mmol/L) require an explicit `molar_mass` argument and fail loud without one — and **nothing yet stores molar mass** (the open question this WI's first molar comparison triggers).

What is open: where comparison lives in the API surface, what a comparison *answers* (the flag vocabulary, especially for censored and qualitative values), where molar mass is persisted, and which frameworks and ranges are seeded (WI-2 shipped the framework read endpoints empty, deferring their seed here).

Owner decisions (2026-07-16, at WI-3 kickoff) fix these.

## Decision Drivers

- ADR-0005's safety correction is the reason this ADR exists: a silently wrong flag is worse than no flag. Every ambiguous path must be *named* in the output, not collapsed into a boolean.
- A censored result is genuinely three-valued against a range: `<0.1` vs. a `>= 0.5` target is decidably below; `<5` vs. a `0–10` target is undecidable. Both must be expressible.
- Comparison is a read-path concern over data the client already fetches; it must not fork the value-fidelity contract ADR-0053 established.
- Molar mass is per-biomarker reference data; the catalog is its natural home ([ADR-0030](0030-biomarker-identity.md) `canonical_unit` sits beside it).
- Framework range data is curated content with clinical consequence; the seed's provenance must be recorded, and it must contain no personal health data (CLAUDE.md containment).

## Decision Outcome

### 1. Comparison API surface: opt-in enrichment on the existing read path

Comparison is **not** a new resource. `GET /v1/lab-results?framework=<name>` (and the same parameter on the get-by-id route) adds a **`range_comparison`** object to each serialized result row. Absent the parameter, rows serialize exactly as [ADR-0053](0053-read-endpoint-surface-and-pagination.md) defined them — the Phase 2 contract does not shift.

A dedicated `/v1/lab-results/{id}/comparisons` route was considered and rejected for now: the dominant read is a biomarker history page (`?biomarker_id=N`), and a per-result route turns a flagged history into N+1 fetches — the exact cost ADR-0053 §5 embedded draw context to avoid. The per-result all-frameworks view remains addable later, additively, when a client needs it.

`framework` is a **projection, not a filter**: it changes what each row carries, not which rows are returned or their order. It is therefore deliberately **not** part of the pagination cursor, and a cursor stays valid whether or not the parameter is present.

**An unknown framework name is a `422`** — deliberately unlike `?category=`'s unknown-name-answers-an-empty-page rule ([ADR-0055](0055-biomarker-category-taxonomy.md) §1). The asymmetry is the point: a typo'd category yields an obviously empty page, whereas a typo'd framework would yield a *full page of plausible-looking rows* all flagged `no_range`. A silently wrong answer that looks right is precisely the failure mode this ADR exists to prevent, so the name resolves once, before the page query, and fails loud. Matching is case-insensitive, consistent with `?category=`.

### 2. Flag vocabulary: a closed seven-value set

`in_range` | `below` | `above` | `indeterminate` | `not_comparable` | `no_range` | `error`

A boolean "in range?" cannot express the states real lab data actually produces, and every one of them collapses to a *wrong* answer under a boolean. The set is closed and asserted as such by a test — a new flag must break a test rather than leak into clients silently.

| flag | meaning |
|---|---|
| `in_range` | the result is provably within the target |
| `below` / `above` | the result is provably outside the target, on that side |
| `indeterminate` | a censored result whose interval straddles a target boundary — undecidable, and said so |
| `not_comparable` | the target is `range_text`-only, or the result is qualitative (`value_num IS NULL`) |
| `no_range` | the framework has no range row for this biomarker at this date |
| `error` | the units could not be reconciled; carries a reason string |

### 3. Comparison model: intervals, and no assumption about sign

Both the result and the target are modeled as **intervals on the real line**, and the verdict is a subset/disjointness question. This framing is what makes censored values correct rather than special-cased.

The result interval follows the [ADR-0030](0030-biomarker-identity.md) triple: `comparator IS NULL` → the point `{v}`; `<`/`<=` → `(-inf, v)` / `(-inf, v]`; `>`/`>=` → `(v, +inf)` / `[v, +inf)`. The target is the **inclusive** interval `[range_low, range_high]`, with a `NULL` bound meaning infinity. Then: `R ⊆ T` → `in_range`; `R ∩ T = ∅` and R below/above T → `below`/`above`; otherwise → `indeterminate`. The four are mutually exclusive and exhaustive.

Open-versus-closed is load-bearing at the boundary, which is why this is implemented as interval arithmetic and not a chain of comparator cases: `<0.5` against `[0.5, +inf)` is **`below`** (0.5 is never attained), while `<=0.5` against the same target is **`indeterminate`** (the value could be exactly 0.5, or lower).

**The engine assumes nothing about the sign of a quantity.** `<0.1` is `(-inf, 0.1)`, not `[0, 0.1)`. Non-negativity is domain knowledge the comparison engine does not have and must not silently invent — inventing it is the same class of unstated assumption as the missing unit ADR-0005 corrected. The visible consequence: a censored `<0.1` against an explicit `[0, 10]` target is `indeterminate`, not `in_range`.

That consequence is nearly always avoided by encoding rather than by assumption: a "lower is better" target has **no clinically meaningful floor**, so it is seeded as `range_low = NULL` (unbounded below), never `range_low = 0`. Under that encoding `<0.1` against "optimal is under 1.0" is `(-inf, 0.1) ⊆ (-inf, 1.0]` → `in_range`, with no assumption required. The seed follows this rule (§5); it is the honest encoding independently of this ADR.

**Bounds coincide within a relative 1e-9, not exactly.** Normalization is float arithmetic, so a result that is *exactly* a bound in one unit need not land exactly on it in another. Concretely: 5.171967933798811 mmol/L is exactly 200 mg/dL for Total Cholesterol, but bridging it through the molar mass yields 200.00000000000003 — and an exact `==` flags that `above` while the identical physical quantity expressed in mg/dL or g/L flags `in_range`. One value, three unit representations, two verdicts: the silently-wrong-flag class this ADR exists to close, reintroduced by the very arithmetic that closes it.

Two magnitudes within a relative 1e-9 are therefore treated as the same point. The tolerance is ~7 orders of magnitude above the ~1e-16 relative error a conversion actually introduces, and far below any clinically meaningful difference (2e-7 mg/dL at a cholesterol bound of 200); it matches the tolerance WI-1's property suite already uses for conversion round-trips ([ADR-0056](0056-units-module-api-and-molar-context.md)). It decides only **whether two bounds coincide** — never the open/closed question — so §3's load-bearing `<0.5`-vs-`<=0.5` distinction survives it untouched. No absolute tolerance is set and none is needed: every conversion is a multiplication, so an exact zero converts to an exact zero and never has to be recognized across error.

**Evaluation order** short-circuits, so unit work never runs where it cannot matter: resolve the range row (none → `no_range`) → target has no numeric bounds (→ `not_comparable`) → result is qualitative (→ `not_comparable`) → normalize (failure → `error`) → compare.

**Point-in-time resolution** is ADR-0005's rule unchanged: the greatest `effective_date <= D` where *D* is the date portion of the result's `draw_utc`, else the dateless (`NULL`) default, else `no_range`. The schema's `UNIQUE` + `ux_framework_ranges_default` partial index guarantee at most one row resolves; the implementation relies on that guarantee rather than masking an ambiguity with `LIMIT 1`.

### 4. Molar-mass persistence: a nullable, CHECK-guarded `biomarkers.molar_mass`

Resolves the open question [ADR-0056](0056-units-module-api-and-molar-context.md) §3 deferred, in the work item named as its trigger. Migration 0005:

```sql
ALTER TABLE biomarkers
    ADD COLUMN molar_mass REAL CHECK (molar_mass IS NULL OR molar_mass > 0);
```

The catalog is the natural home — molar mass is per-biomarker reference data, and it sits beside `canonical_unit` ([ADR-0030](0030-biomarker-identity.md)), the other column the comparison path reads for exactly the same purpose. ADR-0056's decision that `units.convert` takes molar mass as an **explicit argument** stands unchanged: the units module remains a pure function of its arguments with no catalog dependency. This ADR decides only where the *caller* reads it from.

`NULL` is the honest "not applicable, or not curated", and it stays safe: a molar conversion against a NULL molar mass raises `MissingMolarContextError` (ADR-0056 §3) and surfaces as an `error` flag naming the biomarker — never a scalar fallback. The `CHECK` is the database-level analog of `units.convert`'s own positivity guard, following the ADR-0030 enforcement pattern (a malformed value cannot exist even if written through a future path that skips validation).

It is a plain `ADD COLUMN`, **not** a table rebuild. The `legacy_alter_table` hazard migration 0004 documents — where `ALTER TABLE ... RENAME` silently repoints other tables' `REFERENCES` clauses — applies to `RENAME`, not `ADD COLUMN`, so `biomarkers` is left in place and the `lab_results` / `framework_ranges` foreign keys are untouched. SQLite accepts and enforces a `CHECK` on `ADD COLUMN` (verified on SQLite 3.50.4); existing rows take `NULL`, which the constraint permits.

### 5. Framework and range seeding

Migration 0005 seeds three frameworks, each carrying a `description` and a `source_url`, with every range row as the dateless default (`effective_date IS NULL`):

| framework | rows | source |
|---|---|---|
| `nih_medlineplus_lipid_targets` | Total Cholesterol, LDL, Triglycerides | NIH MedlinePlus adult lipid targets |
| `ada_standards_of_care` | Glucose, Hemoglobin A1c | ADA *Standards of Care in Diabetes* |
| `aha_cdc_hscrp_risk_strata` | hs-CRP (low-risk band only) | AHA/CDC hs-CRP risk strata |

Nineteen molar masses are seeded alongside, sourced from PubChem and each cross-checked against an independently published clinical conversion factor. Three carry a subtlety recorded in the migration's own comments: **BUN = 28.014** is the *urea-nitrogen equivalent* (2 × 14.007), not urea's molecular weight (60.06) — BUN measures nitrogen mass and each urea molecule carries two nitrogen atoms, so urea's own mass cancels out of the conventional 0.357 factor entirely; **Triglycerides = 885.4** (triolein) and **Folate = 441.4** (folic acid) are assay-calibration proxies, not the physiologically dominant species. Albumin deliberately gets none: it is a ~66 kDa protein with no clinically used molar conversion.

**Only defensibly-sourced ranges are seeded.** Coverage is partial by construction; an uncovered biomarker flags `no_range` rather than carrying a guessed target. Four things were deliberately *not* seeded, each for a reason worth preserving:

- **No `Lab Standard` framework**, despite ADR-0005 listing the name as an example. That same ADR is explicit that the lab's own range "remains — it is a historical fact about what the lab reported, not a framework comparison", and it already sits on every `lab_results` row as `reference_low`/`reference_high`/`reference_text`. Seeding it as a framework would duplicate per-row data under a second, divergent source of truth.
- **No Attia framework**, despite ADR-0005 also naming it. The only numeric ApoB/Lp(a) targets locatable were podcast show notes, which is not a citable source. The gap is deliberate, not an oversight.
- **No HDL range at all**, though the source states two numbers for it. The low cutoff is sex-specific (40 mg/dL men / 50 women) and `framework_ranges` has no sex dimension; silently picking one sex's number is not acceptable. The sex-neutral optimal target (≥60) *does* fit the schema, but encoding it alone makes HDL's only guidance a goal roughly half of healthy adults miss, reported as `below` — a flag that reads as "abnormally low" beside a genuinely elevated LDL, when the source calls 45 mg/dL neither low nor optimal. One biomarker wanting three distinct ranges (male-normal, female-normal, optimal, where *optimal* is a different axis from *sex* rather than a third sibling) is a schema gap, not an HDL quirk — hormone panels will force it. Deferred to [open-questions.md](../open-questions.md) ("Reference ranges that depend on more than the biomarker"); until then HDL flags `no_range`, which says nothing rather than something misleading, and the target is one catalog import away (§6).
- **The age dimension is silently assumed.** The same source states different lipid targets for age ≤19 (TC <170, LDL <110) than for 20+ (TC <200, LDL <100); the adult values are seeded. Correct for this platform's single adult owner, but an assumption the schema cannot express — recorded in the same open question rather than left implicit.
- **The glucose thresholds are fasting-specific and applied unconditionally.** `FPG` is what ADA states; the catalog has one `Glucose` biomarker and the comparison never reads `lab_draws.fasting`, so a normal post-meal 130 mg/dL flags `above`. It errs conservative (a false out-of-range, not a false normal), and is deferred to [open-questions.md](../open-questions.md) with the owner's leaning recorded: a distinct `Glucose (fasting)` biomarker.
- **No hs-CRP intermediate/high bands.** One row per `(framework, biomarker)` holds one target zone; the low-risk band is the optimal target, which is what a comparison flag is asking about.

Two seeding rules fall out of §3 and are load-bearing enough to state as decisions, because both were caught as live bugs during this WI:

**Inclusive bounds mean the ceiling is the largest value still normal, not the next band's floor.** The ADA source states prediabetes as `A1C 5.7–6.4%` and `FPG 100–125 mg/dL`. Encoding those floors as an inclusive `range_high` would flag an exactly-prediabetic result `in_range` — a clinical falsehood. The seeded ceilings are therefore **5.6%** and **99 mg/dL**, which is also exactly how labs print these ranges.

**`range_low = NULL` means "no clinically meaningful floor" — it is not a default.** It is correct for genuinely one-sided markers (LDL, ApoB, hs-CRP, triglycerides). It is *wrong* for a two-sided marker: a floorless glucose target flags a fasting glucose of 40 mg/dL — severe hypoglycemia, a medical emergency — as `in_range`. Glucose is therefore seeded `[70, 99]`, with the floor sourced separately from the ceiling (ADA's Level 1 hypoglycemia alert value of 70 mg/dL, from the *Glycemic Goals and Hypoglycemia* chapter, distinct from the diagnostic-criteria chapter the ceiling comes from). The general rule for future seeding: **a floorless target is a claim that arbitrarily low values are safe, and must be justified per biomarker, never assumed.**

Framework names carry **no edition year** (`ada_standards_of_care`, not `ada_standards_of_care_2026`). ADR-0005 versions a framework's targets via `effective_date`; baking the year into the name would make a future revision spawn a *second* framework rather than a dated row on the existing one, silently breaking longitudinal comparison across the boundary. The edition lives in `description`/`source_url`, and a future revision adds dated rows to the same framework — which is exactly the point-in-time model §3 implements.

Where a source could only be reached at one remove, the migration says so: the hs-CRP band is quoted from a peer-reviewed review restating the AHA/CDC primary statement, because the primary statement (Pearson et al., *Circulation* 2003) returned HTTP 403 on every fetch. Provenance honesty is part of the seed, not a footnote to it.

### 6. `range_frameworks` and `framework_ranges` are importable

Both tables join the [ADR-0052](0052-bulk-import-identity-and-conflict-resolution.md)/[ADR-0057](0057-reference-data-and-catalog-import-implementation-decisions.md) catalog-import registry, as identity-classified catalog tables (no supersession, no provenance — a genuine difference reconciles in place, like every other catalog table).

Without this they were unreachable by any supported path. The seed is deliberately partial (§5), so `no_range` is a *normal* answer that the owner is expected to resolve by curating a target — and curation had no route: not the import endpoint, not the CLI, only a migration or direct database access. That also left [ADR-0005](0005-reference-range-frameworks.md)'s stated advantage of the option it chose — "new practitioners/guidelines are data additions, not schema changes" — undeliverable, and left the whole `effective_date` point-in-time mechanism §3 implements unreachable in practice, since no dated row could be created.

Three decisions this forced:

**`effective_date` is a nullable natural-key column** — the first the registry has. The natural key is the ADR-0005 `UNIQUE(framework_id, biomarker_id, effective_date)`, which is also what makes point-in-time resolution provably single-valued, so it must be the match key. But `NULL` there means "always current", which is the *common* row, not an edge case. The registry previously required every key column and matched with `=`; SQL's `x = NULL` is NULL, so a dateless row would never match its stored self and would be re-INSERTed into its own partial unique index on every import. A new `ImportableTable.nullable_key` declares such columns; they are exempt from the required-column check and matched with `IS` (NULL-safe equality, identical to `=` for non-NULL operands and likewise indexable). An omitted `effective_date` and an explicit `null` are therefore the same key — both mean "always current" — which is the correct reading. Generalizing the registry was preferred over special-casing one table, per the same reasoning that made §3's interval model general rather than a chain of comparator cases.

**`effective_date` must be a date-only `YYYY-MM-DD`**, validated at the import boundary. §3's point-in-time rule compares it lexically against the date portion of `draw_utc` (the [ADR-0053](0053-read-endpoint-surface-and-pagination.md) prefix convention). That is sound only for a date-only value: `'2024-06-01T00:00:00Z'` sorts *after* `'2024-06-01'`, so a timestamped row would silently lose its own effective day and resolve to the previous row or to none — a wrong range, chosen quietly. The column's `TEXT` type and its "ISO-8601 date" comment never enforced this; nothing did, because nothing could write the table. Making it writable makes the validation load-bearing, so it lands here.

**Both FKs resolve against already-stored rows, never same-batch**, inheriting ADR-0057 §9's constraint unchanged: a framework must land in a prior import before its ranges can reference it.

`framework_ranges.unit` is required explicitly rather than by the generic pass (it is `NOT NULL` but not part of the natural key), and the two ADR-0005 integrity CHECKs are mirrored as validation so a bad row is one named error among the batch's rather than an opaque `IntegrityError` aborting the apply — the same reasoning that already validates the ADR-0030 value model at this boundary despite its CHECKs.

**Not** validated: whether `unit` is well-formed UCUM. No import path validates a unit today (`biomarkers.canonical_unit` is equally unchecked), and singling out this one column would be inconsistent; an unparseable unit is also fail-safe rather than silently wrong — it surfaces as a named `error` flag (§2) on every comparison, never a bad number. Deferred to [open-questions.md](../open-questions.md) rather than fixed asymmetrically here.

### Positive Consequences

- The mg/dL-vs-g/L silent mis-flagging class ADR-0005 exists to close is now actually closed, with a property test (unit-normalization invariance) as its regression net rather than an assertion in prose.
- Censored results — the assay-limit values that matter most clinically — are flagged when decidable and honestly marked `indeterminate` when not, instead of being conflated with measured values or dropped.
- A biomarker history page comes back already flagged in one request, which is exactly the shape WI-4's CLI needs.
- The molar-mass question is closed in its trigger WI, so no comparison path silently lacks a molar source.
- `error` is loud but not fatal: one uncurated unit names itself in its own row instead of blanking a page.

### Negative Consequences / Tradeoffs

- Framework range data must be curated with a correct UCUM unit and (for molar pairs) a molar mass. Coverage starts partial; uncovered biomarkers flag `no_range`.
- `indeterminate` and `not_comparable` are real answers clients must handle — a boolean would have been simpler and wrong.
- The `?framework=` 422 diverges from `?category=`'s empty-page convention. The inconsistency is deliberate (§1) but is a thing to know.
- A censored result against an explicit `range_low = 0` target reads `indeterminate`, which will look like a bug to anyone who has not read §3. The seed's NULL-not-zero encoding keeps it rare.

## Consequences for Other Documents

- **[api-reference.md](../api-reference.md)** — the `?framework=` parameter on `GET /v1/lab-results` and `GET /v1/lab-results/{id}`, the `range_comparison` response object, the flag vocabulary, and the 422-on-unknown-framework rule.
- **[data-model.md](../data-model.md)** — the `biomarkers.molar_mass` column.
- **[open-questions.md](../open-questions.md)** — the biomarker molar-mass persistence entry is **resolved** by §4 and removed.
- **[ADR-0056](0056-units-module-api-and-molar-context.md)** — gains an `Extended by ADR-0058` navigation link (its Consequences noted molar-mass persistence as deferred to a later WI; §4 resolves it). Status stays Proposed; no decision of its is reversed.

## Links
- Implements: [ADR-0005](0005-reference-range-frameworks.md) — framework ranges, mandatory unit, unit-normalized comparison, point-in-time lookup rule
- Depends on: [ADR-0030](0030-biomarker-identity.md) — the result value model (comparator/qualitative) comparison must respect
- Depends on: [ADR-0031](0031-units-and-ucum.md) — UCUM strings and the normalization mechanism
- Extends: [ADR-0056](0056-units-module-api-and-molar-context.md) — resolves the deferred molar-mass persistence question
- Extends: [ADR-0052](0052-bulk-import-identity-and-conflict-resolution.md) — §6 adds `range_frameworks`/`framework_ranges` to its importable registry and generalizes the natural-key model with `nullable_key`
- Extends: [ADR-0053](0053-read-endpoint-surface-and-pagination.md) — adds the opt-in `?framework=` projection to its lab-results routes (shape unchanged when absent)
- Extends: [ADR-0057](0057-reference-data-and-catalog-import-implementation-decisions.md) — adds the two framework tables to its catalog-import registry, with the `nullable_key` generalization
- Related: [open-questions.md](../open-questions.md) — biomarker molar-mass persistence (this WI is its named trigger)

# Lab Data Dimensions — Discovery Survey

A generic domain model of the shapes real laboratory and health data takes, surveyed from real reports to scope the data-lifecycle work ([development-plan.md](development-plan.md), Phase 3.5) and to surface deferred-decision triggers *before* they fire reactively — designing the model once against the full dimension set instead of migrating the schema one report at a time.

**This is a design input, not a spec of record.** It enumerates *what real data looks like*; the *decisions* about how the platform handles each dimension live in their owning ADR, [data-model.md](data-model.md), or [open-questions.md](open-questions.md) per the [CLAUDE.md](../CLAUDE.md) decision-capture rules. Where an item here drives a decision, that decision is recorded there and the entry links to it. This document never becomes a second home for a decision.

**Containment.** Everything here is generic and publishable — value shapes, dimensions, biomarker names and units, source-format quirks. No personal values, result patterns, or identifiers appear: those stay in `specs/personal/` (raw corpus under `specs/personal/ingestion/`, working inventory in `specs/personal/`). The survey is a domain model — "what lab data in general looks like" — never "what any individual's reports contain."

**Status key** on each item:

- **[validated]** — a real report exercised this shape; high confidence in the model.
- **[known-needed]** — enumerated from domain knowledge, but the owner's single corpus *cannot* exercise it (female / pediatric / pregnancy ranges, SI-default units, specialty result types). Design the slot; the fill is a separate, unblocked question. This is how one person's corpus drives a model that generalizes.
- **[open]** — cardinality or handling still being surveyed.

**Survey progress.** One source family reviewed so far — a comprehensive US blood-chemistry + urinalysis + CBC panel (metabolic, lipid, CBC-with-differential, urinalysis, A1c, plus preliminary fatty-acid/inflammation/insulin panels). Unsurveyed: continuous glucose (Levels, Dexcom), body composition (InBody), and wearable exports (Apple / Fitbit / Samsung). This document fills in as those are surveyed; see the gitignored `specs/personal/` source inventory for the raw list.

---

## 1. Value shapes

How a single result's value is encoded. The schema model is `value_num` + `comparator` + `value_text` ([ADR-0030](adr/0030-biomarker-identity.md)).

| Shape | Example form | Schema fit | Status |
|---|---|---|---|
| Exact numeric | `5.2` | `value_num` | validated |
| Censored (below/above detection) | `<0.1`, `>150` | `value_num` + `comparator` | validated |
| Qualitative text | NEGATIVE, YELLOW, CLEAR | `value_text` | validated |
| Ordinal grade (semi-quantitative) | trace / 1+ / 2+ / 3+ | `value_text` (stored); **no ordinal comparison** | validated — comparison gap |
| Count-with-threshold (microscopy) | NONE SEEN vs `< OR = 5 /HPF` | `value_text` + `reference_text` | validated — comparison gap |
| Calculated / derived | rows the lab marks `(calc)` | **not a lab result** ([ADR-0044](adr/0044-derived-data-points.md)) | validated |
| Pending / preliminary | resulted later, no value yet | not entered until resulted | validated |
| Titer / dilution | `1:160` | `value_text`, or a numeric with a unit convention | known-needed |
| Result-with-organism (culture) | "many *E. coli*" | `value_text` (name is clinical, not a value) | known-needed |
| Genotype / allele | `*1/*2`, homozygous | `value_text` | known-needed |

Storage is solved for every shape seen. **Comparison is not:** [ADR-0058](adr/0058-range-comparison-implementation-decisions.md) §2 short-circuits to `not_comparable` the moment `value_num IS NULL`, so qualitative, ordinal, and count results store faithfully but never flag — see [open-questions.md](open-questions.md) "Non-numeric reference comparison". Derived/`(calc)` values are not entered at all (§8 below, [ADR-0044](adr/0044-derived-data-points.md)).

---

## 2. Units

| Family | Examples | Handling | Status |
|---|---|---|---|
| Standard UCUM | `mg/dL`, `mmol/L`, `U/L`, `g/dL` | UCUM string, unit-normalized comparison ([ADR-0031](adr/0031-units-and-ucum.md)) | validated |
| Percentage | `%` (differential, A1c) | UCUM `%` | validated |
| Dimensionless | pH, specific gravity, ratios | UCUM `[pH]`, `{SG}`, unitless | open — UCUM validity of each |
| Microscopy per-field | `/HPF`, `/LPF` | UCUM annotation form | open — validity + comparison |
| Composite/rate | `mL/min/{1.73_m2}` (eGFR) | UCUM (already seeded) | validated |
| Molar (SI-default labs) | `mmol/L` cholesterol/glucose | molar-mass conversion ([ADR-0058](adr/0058-range-comparison-implementation-decisions.md) §4) | known-needed |

No import path validates UCUM well-formedness today ([open-questions.md](open-questions.md) "UCUM validation at the import boundary") — an unparseable unit is fail-safe (a named `error` flag), not silently wrong. The dimensionless and microscopy units above are the first that need their UCUM spelling confirmed against `units.is_valid_unit`. Molar units are **known-needed**: a US-default corpus rarely exercises them, but they are the *first* thing a non-US lab reports, and they are what makes the tier-B molar masses go live ([open-questions.md](open-questions.md) "Independent verification of the tier-B molar masses").

---

## 3. Range dimensions

What a reference range depends on *besides* the biomarker. `framework_ranges` is keyed `(framework_id, biomarker_id, effective_date)` — one target per biomarker per framework per date. Real ranges vary along more axes ([open-questions.md](open-questions.md), "Reference ranges that depend on more than the biomarker").

| Dimension | Example | Status |
|---|---|---|
| Sex | HDL cutoff differs male vs female; hormone ranges strongly sex-specific | validated (male only) / known-needed (female) |
| Fasting state | ADA glucose thresholds are *fasting* plasma glucose | validated |
| Method | LDL-C by Friedewald vs Martin-Hopkins gives different values | validated |
| Age band | pediatric lipid targets differ from adult | known-needed |
| Life-stage | pregnancy-specific intervals | known-needed |
| Specimen type | a urine analyte is not its serum namesake | validated |
| Bound closure | "< 200" (exclusive) vs `[L, H]` (inclusive) | validated — see exclusive-bounds entry |

The owner's corpus validates the **male / adult / fasting** cells with real data and confirms the *method* and *bound-closure* axes; **female / pediatric / pregnancy** are known-needed — enumerable from domain knowledge, filled from published reference data, never from this corpus. The schema should admit the full cohort key even though only some cells are seeded. All of this is the same **range-model-expressiveness** theme as the cohort-dimension, exclusive-bounds, and `physical_min` entries in [open-questions.md](open-questions.md), and should land in one range-model PR.

---

## 4. Biomarker identity

When are two result lines the same biomarker, and when are they different biomarkers that merely share a word?

| Situation | Example | Rule | Status |
|---|---|---|---|
| Same concept, different name (labs) | "Carbon Dioxide" vs "CO2 (Bicarbonate)" | one biomarker + an **alias** | validated — mis-modeled as a duplicate |
| Same name, different specimen | urine Glucose vs serum Glucose | **different** biomarkers | validated |
| Same analyte, absolute vs percentage | Absolute Neutrophils (cells/uL) vs Neutrophils (%) | **different** biomarkers; naming convention needed | validated |
| Same measurement, different context | fasting vs non-fasting/CGM Glucose | leaning: split the entity ([open-questions.md](open-questions.md) "Fasting-state glucose") | validated |
| One concept, many LOINC | LDL-C reported under several codes by method | canonical LOINC + reported code ([ADR-0032](adr/0032-biomarker-loinc-cardinality.md)) | known-needed |

The alias-vs-duplicate case is the concrete driver for Phase 3.5's catalog correction: adding a same-concept row under a different name succeeds (the names normalize differently, so [ADR-0054](adr/0054-biomarker-name-alias-fallback.md)'s uniqueness check does not catch it) and there is no in-app remove/merge/alias-add to fix it. The absolute-vs-% duality is a catalog-seed *naming* decision (settle "Absolute Neutrophils" vs "Neutrophils" before seeding, so it is consistent), not a schema gap.

---

## 5. Draw / panel structure

The container around the result rows.

| Aspect | Reality | Model today | Status |
|---|---|---|---|
| Performing lab | one accession split across multiple performing labs | `lab_draws.lab_id` is single | validated — gap ([open-questions.md](open-questions.md) "One draw, multiple performing labs") |
| Collection vs report time | draw time ≠ result time; each panel has its own | `draw_utc` on the draw | validated |
| Preliminary → final | whole report preliminary; some panels unresulted | re-entry supersedes ([ADR-0027](adr/0027-audit-trail-and-corrections.md)) | validated |
| Panel grouping | CMP / CBC / lipid / UA groupings on one sheet | not modeled (biomarker category is the proxy) | open — is panel identity worth storing? |
| Ordering physician / accession | present on the report | **not stored** (and identifiers are personal) | validated — intentionally dropped |

The preliminary→final workflow is already handled by supersession — enter the resulted values, re-enter the same draw when finals land. The multi-performing-lab case is a genuine model limitation deferred to its own open-questions entry.

---

## 6. Source formats

Each family has its own structure; each adapter is gated on inspecting a real export ([open-questions.md](open-questions.md), Data Ingestion). Manual entry (Phase 3) and its Phase-3.5 hardening are the ingress for anything without an adapter yet.

| Family | Format | Notable quirks | Status |
|---|---|---|---|
| US blood chemistry (Quest / LabCorp, via aggregators) | PDF | multi-performing-lab, preliminary status, `(calc)` rows, mixed value shapes | validated |
| Function Health | (aggregated panels) | externally-computed scores (Biological Age, Z-scores) — [ADR-0044](adr/0044-derived-data-points.md) data | partially validated |
| Continuous glucose (Levels, Dexcom) | CSV / JSON / API | high-volume time series; molar units possible | known-needed (Phase 7) |
| Body composition (InBody) | CSV / PDF | device-fixed-unit metrics, not UCUM biomarkers ([data-model.md](data-model.md)) | known-needed (Phase 7) |
| Wearables (Apple / Fitbit / Samsung) | XML / JSON | broad aggregation; overlaps other sources | known-needed (Phase 7) |

---

## 7. Catalog lifecycle

The operations a real catalog needs over time — not just at first load.

| Operation | Available today | Status |
|---|---|---|
| Add biomarker / lab | `biomarkers add` / `labs add` ([ADR-0060](adr/0060-cli-catalog-add-commands.md)) | validated |
| Add alias | only via `enter` miss-path or import — **no `add` command** | validated — gap |
| Remove (unreferenced) | **none** | validated — gap (Phase 3.5) |
| Merge (dedupe onto a survivor) | **none** | validated — gap (Phase 3.5) |
| Edit (recategorize, fix unit) | full-row `POST /v1/import` only ([open-questions.md](open-questions.md) "CLI catalog editing") | deferred |

Add-only was a deliberate ADR-0060 choice, but real use showed it is one-way: a mistake (a same-concept duplicate) has no in-app remedy short of a database purge. Phase 3.5 owns remove / merge / alias-add; merge-aware edit stays deferred.

---

## 8. Derived data

Values the lab computes and prints, but that are not measurements. [ADR-0044](adr/0044-derived-data-points.md) classes these as a distinct content type that must **not** enter source-data tables; the schema is deferred to Phase 5. Two subtypes, both seen:

| Subtype | Examples | Reproducible? | Handling |
|---|---|---|---|
| Internally computed | Non-HDL (`Total − HDL`), Globulin (`TP − Albumin`), A/G ratio, Chol/HDL ratio | yes — formula + inputs stored | recompute later; do not store |
| Externally opaque | LDL-C (Martin-Hopkins), Biological Age, IGF-1 Z-Score | no — proprietary/adjustable method | store as derived (Phase 5); reconstruct from inputs if method implemented |

Interim rule, now in the manual-entry policy: **derived values are not entered.** Deferring costs no information — the reproducible ones recompute exactly from components that *are* stored, and the opaque ones from their stored inputs when the method lands. The discriminator is not "how hard is the arithmetic" but "can we reproduce it": Globulin (`TP − Albumin`) is as reproducible as Non-HDL, so both defer; LDL-C by Martin-Hopkins cannot be reproduced without the published divisor table, so it is the genuine opaque snapshot. See [open-questions.md](open-questions.md) "Derived data points" for the accumulation evidence.

---

## How this feeds the plan

- **Phase 3.5 scope** is the `[validated]` gaps that block a clean dataset: catalog correction (§7), entry fidelity for lab ranges (§5), and a fuller starter catalog (§1, §4).
- **Range-model PR** absorbs the numeric *and* non-numeric expressiveness gaps (§3, §1) — one PR, one trigger cluster.
- **`[known-needed]` items** are designed-for (the schema admits the dimension) but seeded only where the corpus validates them — the discipline that lets one comprehensive corpus produce a model that generalizes without over-building.
- Every gap that becomes a decision is recorded in its owning doc; this survey only maps the territory.

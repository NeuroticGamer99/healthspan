# Biomarker molar-mass provenance

Where every value in `biomarkers.molar_mass` came from, why three of them are not the number you
would guess, and — explicitly — **which ones have been verified and by whom**.

Seeded by migration 0005 (Phase 3 WI-3); the column and its rationale are decided in
[ADR-0058](adr/0058-range-comparison-implementation-decisions.md) §4, which resolves the persistence
question [ADR-0056](adr/0056-units-module-api-and-molar-context.md) §3 deferred.

## Why this exists

Mass-concentration ↔ substance-concentration conversions (mg/dL ↔ mmol/L) are not scalar factors:
they need the biomarker's molar mass as context, which a UCUM string cannot supply
([ADR-0056](adr/0056-units-module-api-and-molar-context.md) §3). A **wrong** molar mass does not
fail — it silently produces a wrong number, and therefore a wrong range flag. That is the failure
class [ADR-0005](adr/0005-reference-range-frameworks.md) exists to close, so these values carry the
same evidentiary burden as the reference ranges themselves.

`NULL` is always safe: a molar conversion against it raises `MissingMolarContextError` and surfaces
as a named `error` flag, never a guessed factor. **When in doubt, prefer NULL to a plausible
number.**

## Verification status — read this before trusting the table

| Tier | What it means | Which rows |
|---|---|---|
| **A — verified live** | Molar mass fetched from PubChem's REST API in-session and confirmed against the table below | Total Cholesterol / HDL / LDL (386.7), Glucose (180.16), Creatinine (113.12), Uric Acid (168.11), Testosterone Total (288.4) |
| **B — agent-fetched, arithmetically consistent** | Fetched from PubChem by a subagent, quoted with a CID, and reconciles with an independently quoted clinical conversion factor | the remaining 12 |
| **— not proposed** | no clinically used molar conversion exists | Albumin |

**The tier-B check is weaker than it looks, and the weakness is worth naming.** The published factor
is essentially `10/MW`, so verifying "the factor matches `10/MW`" is close to tautological *if the
factor was back-computed from the MW rather than quoted*. The evidence that it was genuinely quoted
is the subagent's citation of a specific source table (`PMC11132569`), which **has not been opened
by a human or by the reviewing session**. So tier B rests on: PubChem being right (independently
plausible), and the citation being real (not independently confirmed).

Nothing here is *suspected* wrong. This section exists so the distinction between "checked" and
"reported as checked" is on the record rather than in someone's memory. See the open question in
[open-questions.md](open-questions.md).

## The values

Molar mass in grams per mole. "Factor" is the conventional clinical conversion the value must
reproduce; it is the independent cross-check, not a derived quantity.

| Biomarker | canonical_unit | molar_mass | PubChem CID | Conventional factor | Tier |
|---|---|---|---|---|---|
| Total Cholesterol | mg/dL | 386.7 | [5997](https://pubchem.ncbi.nlm.nih.gov/compound/5997) | ×0.0259 → mmol/L | **A** |
| HDL Cholesterol | mg/dL | 386.7 | 5997 | ×0.0259 → mmol/L | **A** |
| LDL Cholesterol | mg/dL | 386.7 | 5997 | ×0.0259 → mmol/L | **A** |
| Glucose | mg/dL | 180.16 | [5793](https://pubchem.ncbi.nlm.nih.gov/compound/5793) | ×0.0555 → mmol/L | **A** |
| Creatinine | mg/dL | 113.12 | [588](https://pubchem.ncbi.nlm.nih.gov/compound/588) | ×88.4 → µmol/L | **A** |
| Uric Acid | mg/dL | 168.11 | [1175](https://pubchem.ncbi.nlm.nih.gov/compound/1175) | ×59.48 → µmol/L | **A** |
| Testosterone Total | ng/dL | 288.4 | [6013](https://pubchem.ncbi.nlm.nih.gov/compound/6013) | ×0.0347 → nmol/L | **A** |
| Triglycerides | mg/dL | 885.4 | [5497163](https://pubchem.ncbi.nlm.nih.gov/compound/5497163) | ×0.0113 → mmol/L | B ⚠️ |
| Total Bilirubin | mg/dL | 584.7 | [5280352](https://pubchem.ncbi.nlm.nih.gov/compound/5280352) | ×17.1 → µmol/L | B |
| BUN | mg/dL | 28.014 | [947](https://pubchem.ncbi.nlm.nih.gov/compound/947) | ×0.357 → mmol/L urea | B ⚠️ |
| Calcium | mg/dL | 40.08 | [271](https://pubchem.ncbi.nlm.nih.gov/compound/271) | ×0.25 → mmol/L | B |
| Magnesium | mg/dL | 24.305 | [5462224](https://pubchem.ncbi.nlm.nih.gov/compound/5462224) | ×0.4114 → mmol/L | B |
| Homocysteine | µmol/L | 135.19 | [91552](https://pubchem.ncbi.nlm.nih.gov/compound/91552) | n/a — already SI | B |
| Iron | µg/dL | 55.84 | [23925](https://pubchem.ncbi.nlm.nih.gov/compound/23925) | ×0.179 → µmol/L | B |
| Cortisol | µg/dL | 362.5 | [5754](https://pubchem.ncbi.nlm.nih.gov/compound/5754) | ×27.6 → nmol/L | B ⚠️ |
| Estradiol | pg/mL | 272.4 | [5757](https://pubchem.ncbi.nlm.nih.gov/compound/5757) | ×3.67 → pmol/L | B |
| Vitamin D 25-OH | ng/mL | 400.6 | [5283731](https://pubchem.ncbi.nlm.nih.gov/compound/5283731) | ×2.5 → nmol/L | B |
| Vitamin B12 | pg/mL | 1355.4 | [166596686](https://pubchem.ncbi.nlm.nih.gov/compound/166596686) | ×0.738 → pmol/L | B |
| Folate | ng/mL | 441.4 | [135398658](https://pubchem.ncbi.nlm.nih.gov/compound/135398658) | ×2.266 → nmol/L | B ⚠️ |
| **Albumin** | g/dL | **none** | — | g/dL↔g/L is ×10, not molar | — |

⚠️ = carries a subtlety below. Do not "correct" these to the obvious value.

## The four that are not what you would guess

### BUN = 28.014, not urea's 60.06

**This is the one most likely to be "fixed" into a bug.** Blood *urea nitrogen* measures the mass of
**nitrogen**, not of the urea molecule. Each urea molecule (CH₄N₂O) carries exactly two nitrogen
atoms, so the relevant mass is 2 × 14.007 = **28.014 g/mol** — numerically identical to N₂'s molar
mass, which is why PubChem CID 947 (dinitrogen) is cited as a verified stand-in for the arithmetic.
The concept is urea-nitrogen, **not** dissolved nitrogen gas.

Urea's own molar mass cancels out of the conversion entirely:

```
mmol/L urea = (mg N/dL × 10 dL/L) ÷ (28.014 mg N per mmol urea)
            = mg N/dL × 0.357          ← the conventional BUN factor
```

Using 60.06 would give ×0.1665 — a different number, wrong by ~2.14×. That factor **is** correct
for a lab reporting *urea* directly (common outside the US) rather than *urea nitrogen*; the
platform does not currently distinguish the two, which is a real gap should such a feed ever arrive.

### Triglycerides = 885.4 (triolein)

Circulating triglyceride is a heterogeneous mixture of triacylglycerols — there is no
"triglycerides molecule". The conventional clinical factor assumes a single average proxy,
**triolein** (glyceryl trioleate, C₅₇H₁₀₄O₆). 885.4 is triolein's mass, and it reproduces the
published ×0.0113. This is a convention, not a claim about what is in the blood.

### Folate = 441.4 (folic acid), not 5-MTHF's 459.5

Serum folate is predominantly **5-methyltetrahydrofolate** (5-MTHF, MW 459.5), *not* folic acid.
The conventional factor (×2.266) is nonetheless calibrated to **folic acid** (MW 441.4) — an
assay-calibration convention, the same shape as the triolein proxy. Same reasoning: match the
convention the published factor uses, not the physiologically dominant species.

### Cortisol = 362.5, with a unit-basis adjustment

The cross-check source states cortisol's factor on a **ng/mL** basis (×2.76 → nmol/L), while this
catalog's `canonical_unit` is **µg/dL**. 1 µg/dL = 10 ng/mL, so the µg/dL-basis factor is ×27.6,
which matches `10000/362.5 = 27.59`. This is the one row where a source number was *adapted* rather
than used directly — worth a second pair of eyes on the dimensional analysis.

### Albumin — deliberately none

A ~66 kDa protein. Routine practice converts g/dL ↔ g/L by a flat ×10, never through a molar
concentration; no clinically used mass↔molar conversion exists for a heterogeneous glycoprotein.
Inventing one would fail the "clinically meaningful" bar. Omitted by design, not by search failure.

## Sources

- **Molar masses** — PubChem (NIH/NLM), via
  `https://pubchem.ncbi.nlm.nih.gov/rest/pug/compound/<name-or-cid>/property/MolecularWeight/TXT`.
  The REST endpoint rather than the compound page: the latter renders via JavaScript, so quoting it
  would mean quoting unrendered HTML.
- **Conversion factors** — mostly a peer-reviewed SI-unit conversion table hosted on PMC
  ([PMC11132569](https://pmc.ncbi.nlm.nih.gov/articles/PMC11132569/)); testosterone and estradiol
  from [Endotext / NCBI Bookshelf NBK279145](https://www.ncbi.nlm.nih.gov/books/NBK279145/);
  vitamin D from the [NIH Office of Dietary Supplements fact sheet](https://ods.od.nih.gov/factsheets/VitaminD-HealthProfessional/);
  uric acid from [PMC6224962](https://pmc.ncbi.nlm.nih.gov/articles/PMC6224962/); magnesium from
  [PMC9186275](https://pmc.ncbi.nlm.nih.gov/articles/PMC9186275/).

## Changing a value

These are ordinary catalog data — `biomarkers` is importable, so a correction is a
`POST /v1/import`, not a migration. But: re-source it first, record the source here, and prefer
`NULL` over a value you cannot cite. A wrong molar mass is silent.

`tests/test_migration_0005.py` pins every seeded value, with named regression tests for BUN,
Triglycerides, and Folate specifically — so "correcting" one of the three counter-intuitive values
fails the suite rather than quietly skewing conversions.

## Links

- [ADR-0058](adr/0058-range-comparison-implementation-decisions.md) §4 — the persistence decision
- [ADR-0056](adr/0056-units-module-api-and-molar-context.md) §3 — the explicit-argument API, unchanged
- [ADR-0031](adr/0031-units-and-ucum.md) — UCUM and the conversion engine
- [data-model.md](data-model.md) — the `biomarkers.molar_mass` column
- [open-questions.md](open-questions.md) — the outstanding tier-B verification

# ADR-0056: Units-Module API and Molar Context (Phase 3 WI-1)

## Status
Proposed

## Context and Problem Statement
[ADR-0031](0031-units-and-ucum.md) decided the units *representation* (UCUM strings, a canonical unit per biomarker, normalize-at-comparison) and resolved the conversion *engine* to `ucumvert` (+ `pint`) behind "a small internal units module." It deliberately did not fix that module's API surface: the function names, the error taxonomy, and — the load-bearing one — *how a molar conversion receives its mandatory biomarker context*, given that the biomarker catalog carries no molar-mass column ([ADR-0030](0030-biomarker-identity.md)'s `biomarkers` table).

Implementing Phase 3 WI-1 (the units module and its property-based acceptance suite, [testing-strategy.md](../testing-strategy.md)) forces those decisions. `ucumvert`/`pint` also land here as the project's first runtime dependencies used by production code paths, which needs a spec record ([CLAUDE.md](../../CLAUDE.md) rule 1). WI-1 is intentionally independent of WI-2's reference-data/schema work, so nothing about the catalog shape can be assumed.

Following the [ADR-0047](0047-crypto-surface-implementation-decisions.md)/[ADR-0049](0049-core-service-skeleton-implementation-decisions.md) precedent, the accumulated WI-1 decisions land as one batched Proposed ADR in the same PR as the implementing change.

## Decision Drivers
- The units module is the *only* code allowed to import `ucumvert`/`pint`; downstream code depends on canonical-unit normalization, not on the engine ([ADR-0031](0031-units-and-ucum.md)) — the API must not leak `pint` types as its contract
- A units mismatch must fail loudly, never silently produce a number — this is the exact safety bug ([ADR-0005](0005-reference-range-frameworks.md) ApoB mg/dL-vs-g/L) that motivated ADR-0031
- Molar conversions (mass concentration ↔ substance concentration) are not scalar factors; they need molar mass, and a molar conversion attempted without it must fail loudly rather than fall back to a scalar ([ADR-0031](0031-units-and-ucum.md), [testing-strategy.md](../testing-strategy.md))
- WI-1 must not couple to WI-2's schema; where molar mass is *persisted* is a separable question
- The property suite ([testing-strategy.md](../testing-strategy.md)) is the acceptance harness and must be written against this API, engine-agnostically

## Considered Options
1. Record each decision (API shape, molar-context mechanism, dependencies) separately
2. **One batched Proposed ADR for the WI-1 units module** (chosen) — matching the ADR-0047/0049 batched-WI pattern
3. For molar context specifically: (a) explicit caller-supplied `molar_mass` parameter; (b) add a `molar_mass` column to `biomarkers` now and look it up inside the module

## Decision Outcome
Chosen: **option 2**, with **molar context via option 3(a)** — an explicit caller-supplied parameter, persistence deferred to WI-2.

### 1. `ucumvert` + `pint` as runtime dependencies
The units module is built on **`ucumvert`** (UCUM 2.2 grammar → `pint`) with **`pint`** as its quantity/registry layer, exactly as [ADR-0031](0031-units-and-ucum.md) resolved. They are the first runtime dependencies exercised by a domain code path (prior deps were infrastructure). They are subject to the pip-audit gate and lockfile hash-pinning ([ADR-0045](0045-repository-workflow-and-ci-enforcement.md)); the pre-1.0 maturity of `ucumvert` is contained by (a) this module being the sole importer and (b) the local verification layer described in decision 4. `ucumvert` ships no type information, so a minimal local stub (`typings/ucumvert/`) declares the used surface under the project's strict pyright, following the `sqlcipher3` stub precedent.

### 2. The units-module API surface
The module ([`src/healthspan/units.py`](../../src/healthspan/units.py)) exposes a small, `pint`-free contract:

| Function | Contract |
|---|---|
| `parse_unit(unit) -> Any` | Parse/validate a UCUM string; raise `UnknownUnitError` if it is not valid UCUM. Returns the engine's own quantity object, deliberately typed `Any` (opaque) so no `pint`/`ucumvert` type enters this module's contract; not the primary downstream surface. |
| `is_valid_unit(unit) -> bool` | Whether a string parses as UCUM. |
| `convert(value, from_unit, to_unit, *, molar_mass=None) -> float` | Convert between two UCUM units, returning a plain `float`. |

`convert` is the load-bearing surface, and its contract is:

- **Same unit string → the value is returned unchanged**, exactly (no float round-trip is introduced) — but the unit is still validated first, so an identical *invalid*-UCUM pair fails loud rather than becoming a silent validation bypass. This is what makes the property suite's *identity* hold exactly rather than within tolerance.
- **Same dimensionality → scalar conversion** through the engine.
- **Mass concentration ↔ substance concentration → bridged by `molar_mass`** (decision 3).
- **Anything else → `IncommensurableUnitsError`.**

The error taxonomy is a single base, `UnitError`, with three subclasses — `UnknownUnitError`, `IncommensurableUnitsError`, `MissingMolarContextError` — so a caller normalizing before a comparison can catch the base and treat any unreconcilable-units condition as the one loud failure the normalization requirement demands. The engine's own `pint`/`ucumvert` exceptions never escape the module.

### 3. Molar context is an explicit caller parameter; persistence deferred to WI-2
A molar conversion receives molar mass as an **explicit `molar_mass` keyword argument** (grams per mole), not a lookup inside the module. The module stays a pure function of its arguments, with no catalog dependency — which is why WI-1 can land ahead of WI-2.

- The module classifies a dimensional mismatch as *molar* only when the two units differ by exactly a `[mass]/[substance]` factor (the dimensionality of `g/mol`); it bridges by dividing or multiplying the value by `molar_mass · g/mol` accordingly.
- If the mismatch is molar but `molar_mass` is `None` → **`MissingMolarContextError`** (fail loud; never a scalar fallback).
- If `molar_mass` is supplied for a conversion that does not need it → it is **ignored**, not an error.
- A non-positive `molar_mass` is a `ValueError`.

**Where molar mass is *stored* is explicitly out of WI-1 scope.** The `biomarkers` catalog has no molar-mass column today; adding one (so WI-3's comparison path and WI-4's CLI can supply the argument from stored reference data) is **deferred to WI-2's reference-data/schema work** and recorded there. WI-1 delivers the mechanism; WI-2 gives it a persistent source. This is logged as an [open-questions.md](../open-questions.md) item so the dangling parameter is not forgotten.

### 4. Local verification layer = generic, committable known-answer conversions
[ADR-0031](0031-units-and-ucum.md) calls for "our own verification of the specific biomarkers/units in real use" over blind trust in the pre-1.0 engine. That verification is implemented as an **example-based known-answer suite** ([`tests/test_units.py`](../../tests/test_units.py)) asserting conversions for common biomarkers (glucose, cholesterol, creatinine, uric acid, calcium) against independently published clinical values, alongside the fail-loud error paths.

Per [CLAUDE.md](../../CLAUDE.md) personal-data containment, the verification set is **generic public reference data** — textbook molar masses and textbook conversion figures, no individual's results — not the database owner's actual biomarker panel. The [testing-strategy.md](../testing-strategy.md) property suite ([`tests/test_units_properties.py`](../../tests/test_units_properties.py)) is the standing regression net over the wider input space, written against this API so any future engine must pass it unchanged.

### 5. Hypothesis `dev`/`ci` profiles are registered
[testing-strategy.md](../testing-strategy.md) specifies two Hypothesis profiles; WI-1 is the first property suite that needs the CI-scale run, so the profiles are registered now (in [`tests/conftest.py`](../../tests/conftest.py)): `dev` (few examples, fast inner loop) and `ci` (more examples, `derandomize=True` for deterministic failure reproduction). CI selects `ci` automatically via the `CI` environment variable GitHub Actions always sets; `HYPOTHESIS_PROFILE` overrides either way. The per-example deadline is disabled suite-wide — these property targets do real work (KDF hashing, UCUM parsing) whose per-example timing is noise, and a one-time lazy engine build must not fail an example.

### Positive Consequences
- The conversion contract (identity, fail-loud, molar-context requirement) is recoverable from the specs and pinned by an engine-agnostic acceptance suite
- WI-1 lands independently of WI-2: no schema coupling, molar mass supplied by argument
- The pre-1.0 engine is contained (sole importer + local verification + property net), exactly as ADR-0031 intended
- The `pint`/`ucumvert` types never become part of the downstream contract, keeping the engine swappable behind the module

### Negative Consequences / Tradeoffs
- Two new runtime dependencies, one pre-1.0 — accepted with eyes open, gated by pip-audit and the verification layer
- Until WI-2 adds a persisted molar-mass source, callers needing a molar conversion must supply `molar_mass` themselves; there is no catalog lookup yet (tracked in open-questions.md)
- Disabling the Hypothesis deadline suite-wide removes one slow-strategy signal — acceptable, since these suites' timings are dominated by legitimate work

## Consequences for Other Documents
- **[open-questions.md](../open-questions.md)**: new entry — persisting biomarker molar mass (a `biomarkers.molar_mass` column or equivalent) so the comparison path (WI-3) and CLI (WI-4) can supply `convert`'s `molar_mass` from stored reference data; trigger = WI-2 reference-data schema
- **[testing-strategy.md](../testing-strategy.md)**: the Hypothesis `dev`/`ci` profiles it describes are now registered (decision 5); the unit-conversion property suite it specifies is implemented as the ADR-0031 acceptance harness
- **[ADR-0031](0031-units-and-ucum.md)**: navigation link — this ADR concretizes its "small internal units module" into a named API and molar-context mechanism (nav-link only; ADR-0031 is Accepted and unchanged)

## Links
- Extends: [ADR-0031](0031-units-and-ucum.md) — concretizes the internal units module, its API, and the molar-context mechanism it left open
- Related: [ADR-0030](0030-biomarker-identity.md) — the `canonical_unit` column conversions normalize toward; molar-mass persistence (deferred) would live alongside it
- Related: [ADR-0005](0005-reference-range-frameworks.md) — the unit-normalized comparison (WI-3) that consumes this module; the mg/dL-vs-g/L bug this closes
- Related: [testing-strategy.md](../testing-strategy.md) — the property-based acceptance suite and the Hypothesis profiles
- Related: [ADR-0045](0045-repository-workflow-and-ci-enforcement.md) — the pip-audit gate covering the two new dependencies

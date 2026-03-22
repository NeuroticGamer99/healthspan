# Data Model — Health Data Types

A living document cataloging the types of data this platform needs to represent, their sources, and schema considerations. This is a design surface, not a decision record — it evolves as new sources are onboarded and analytical needs become clearer.

Discrete architectural decisions that emerge from this document should be captured as ADRs.

---

## Data Type Inventory

### Lab Results
- **Sources:** Primary care (Corewell/Beaumont → Quest), Function Health (Quest)
- **Cadence:** Periodic (annual physical, targeted panels)
- **Volume:** Low-to-medium (tens of biomarkers per draw, multiple draws per year)
- **Schema considerations:** Lab source as first-class attribute; canonical biomarker names; reference ranges stored per result row; draw context (what ordered this panel)
- **Status:** Partially designed — see [design-rationale.md](design-rationale.md)

### Body Composition
- **Sources:** Any body composition scanner; currently InBody 120 and InBody 580 (via Enara Health)
- **Cadence:** Periodic (monthly or less)
- **Volume:** Low
- **Schema considerations:** Single table with `source` column; device-specific metrics (e.g. phase angle, ECW/TBW, intracellular/extracellular water) are NULL for devices that don't produce them
- **Status:** Partially designed — see [design-rationale.md](design-rationale.md)

### Continuous Glucose (CGM)
- **Sources:** Levels (primary); Dexcom API (potential direct hardware source — more resilient if Levels subscription lapses)
- **Cadence:** Continuous (reading every 5 minutes)
- **Volume:** High (potentially millions of rows over years)
- **Schema considerations:** Separate table from periodic labs; timestamp-based range indexing; most analysis joins to lab data at daily/weekly aggregate level
- **Timezone note:** Consumer CGM apps commonly export timestamps in local time without explicit timezone metadata. The first CGM import will likely result in all records having `tz_inferred = 1`. This is expected and handled — a single timezone correction pass recomputes UTC across the entire batch. Plan for this review step after the first import.
- **Export:** Levels exports glucose data as CSV. **Delivery is async — Levels emails a download link when the export is ready.** This makes fully automated export difficult; watch folder import (user saves file, importer picks it up) is the preferred approach for this data type. CSV column schema unknown until a real export is inspected.
- **Import adapter:** `levels.glucose`
- **Status:** Partially designed — see [design-rationale.md](design-rationale.md)

### Metabolic Context (Levels — Zone Data)
- **Sources:** Levels
- **Cadence:** Per-session / per-day computed scores
- **Volume:** Low
- **Description:** Levels' proprietary computed layer: Zone scores and Glucose Response analysis. Represents Levels' interpretation of CGM + context data. Stored as read-only imported values — cannot be independently recomputed. Long-term, analysis plugins can compute similar metrics from raw data.
- **Export:** JSON export available manually via Levels export page.
- **Import adapter:** `levels.zones`
- **Status:** Not yet designed — new data type

### Activity Logs (Levels)
- **Sources:** Levels
- **Cadence:** Event-based (per food entry, per exercise session, per note)
- **Volume:** Low-to-medium
- **Description:** Food events, exercise sessions, and free-form notes. Provides the contextual layer that makes CGM data clinically interpretable. The "how I'm feeling" subjective tracker may appear here as a note type — confirm when inspecting a real export.
- **Export:** CSV export available manually via Levels export page.
- **Import adapter:** `levels.activity`
- **Status:** Not yet designed — new data type; overlaps with subjective health log and activity event types

### Nutrition Logs (Levels)
- **Sources:** Levels
- **Cadence:** Per meal / per food item
- **Volume:** Low-to-medium
- **Description:** Food log entries with nutritional metadata (macros, micros). Distinct from activity logs — activity logs capture food timing and context; nutrition logs carry the detailed nutritional content. Both are needed for full metabolic analysis.
- **Export:** CSV export available manually via Levels export page.
- **Import adapter:** `levels.nutrition`
- **Status:** Not yet designed — new data type

### Wearable / Activity
- **Sources:** Fitbit
- **Cadence:** Daily aggregates (not raw intraday)
- **Volume:** Medium (one row per day per metric)
- **Metrics:** Steps, active minutes, resting HR, sleep duration, sleep score, HR zone minutes
- **Schema considerations:** Daily aggregate table; intraday tables deferred unless HRV or sleep stage analysis becomes a priority
- **Status:** Partially designed — see [design-rationale.md](design-rationale.md); export not yet attempted

### Clinical Events
- **Sources:** Manual entry
- **Cadence:** Point-in-time, infrequent
- **Examples:** Arterial stent placement, hospitalizations, diagnoses, significant lifestyle changes
- **Schema considerations:** Date, type, description, free-text notes; used as annotation points on all trend analysis
- **Status:** Table defined; no data entered yet

### Interventions
- **Sources:** Manual entry
- **Cadence:** Duration-based (start date, end date or ongoing)
- **Examples:** TRT, medications, supplements, therapies
- **Schema considerations:** Dose, route, frequency; dose history child table TBD (see [open-questions.md](open-questions.md))
- **Status:** Table defined; no data entered yet

---

## CGM and Mobile Health Platform APIs

### Direct CGM APIs
- **Dexcom Developer API** — official OAuth-based REST API, web-accessible without a mobile app. Most authoritative source for Dexcom CGM data. Should be the primary CGM API target. Provides real-time and historical readings.
- **Abbott LibreView** — Abbott's cloud platform for Libre sensors has limited API access; less open than Dexcom. Research needed.
- **Nightscout** — open source CGM data bridge with a REST API. If the user already runs Nightscout, it aggregates CGM data from multiple sensor types and is cleanly accessible from a desktop. An import adapter would be straightforward.

### Mobile Health Platform APIs
**Apple HealthKit** (iOS) and **Google Health Connect** (Android) are valuable because they aggregate data from many health apps in one place — Levels, Dexcom, Fitbit, wearables, and more — providing a single integration point per platform. Both are **sandboxed to native mobile apps** and cannot be queried directly from a Python desktop application.

**Apple HealthKit / Apple Health**
- **XML bulk export** — the iOS Health app can export all HealthKit data as a bulk XML file. Desktop-accessible without a native app. Comprehensive single-source import for iOS users; covers data from any HealthKit-connected app. Import adapter is viable near-term.
- **Live HealthKit API** — sandboxed; requires a native iOS app. Future direction if a mobile companion app is built.

**Google Health Connect** (Android)
- **Live API** — sandboxed; requires a native Android app. No bulk desktop export mechanism equivalent to Apple Health XML.
- **Samsung Health** — on Samsung Galaxy devices, Samsung Health acts as an aggregator alongside Health Connect. Has a manual CSV export and a developer API worth researching. Relevant for testing and development since the project developer uses a Samsung S23 Ultra.
- **Third-party bridge** — services like Terra API expose Health Connect data via REST without a native app. Tradeoff: health data transits a third-party service, which conflicts with the local-first privacy model.

**Third-party aggregators (Terra API, etc.)** — cover both platforms via a single REST API. Convenient but introduce an external dependency and a privacy concern. Worth noting as a path for users who cannot use platform-native options.

### Recommended approach
- **Near term:** Dexcom API for authoritative CGM data; Apple Health XML export adapter for iOS users; Samsung Health manual export for Android/Galaxy users during development
- **Medium term:** Investigate Samsung Health and Google Health Connect web APIs; Nightscout adapter
- **Future:** Native iOS and Android companion apps unlock live HealthKit and Health Connect APIs without third-party dependency

---

## Data Types Not Yet Evaluated

The following are candidate data types that may be worth modeling. Each needs research before schema design begins.

- **Genomic / genetic data** (e.g. 23andMe raw data, MTHFR and other SNPs)
- **Blood pressure / home vitals** (manual or connected device)
- **Medication adherence** (distinct from the intervention record itself)
- **Imaging and clinical reports** (documents rather than structured data — may be reference-only)
- **Additional wearable sources** (Apple Watch, Oura, Whoop)
- **Subjective health logs** (energy, symptoms, mood — may be covered by Levels activity logs export; confirm before designing a separate table)
- **Dietary logs / nutrition** (partially covered by Levels nutrition logs export; general nutrition tracking beyond Levels is a separate concern)

---

## Cross-Cutting Schema Concerns

Topics that affect multiple data types and need consistent treatment:

- **Units and unit normalization** — how are units stored, validated, and converted at query time?
- **Biomarker alias resolution** — where does lab-name → canonical-name mapping live? (see [open-questions.md](open-questions.md))
- **Timezone handling** — resolved: UTC ground truth + `local_recorded` + `local_tz` (IANA) + `tz_inferred` flag on every timestamp. See [design-rationale.md](design-rationale.md) for the full convention.
- **Source provenance** — every row should carry an import batch reference; the `audit_log` table (see below) provides the full trail
- **Longitudinal data correction** — the `superseded_by` pattern or a `corrections` table; see open-questions.md for decision; affects all data tables
- **Audit trail** — a platform-wide `audit_log` table recording all data mutations; must be in the schema from migration 0001
- **Multiple reference range frameworks** — results need to be comparable against more than one set of ranges: the lab's own range (already stored per result row), longevity-optimized ranges (e.g. Function Health), and practitioner-specific optimal ranges (e.g. Attia, Brewer, Hyman). Requires a named framework table separate from per-result lab ranges. See [ADR-0005](adr/0005-reference-range-frameworks.md).

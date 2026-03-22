# ADR-0004: Data Ingestion Strategy

## Status
Accepted (partially — see open decisions below)

## Context and Problem Statement
Health data arrives from multiple sources in multiple formats: patient portal exports, lab PDFs, wearable data exports (Google Takeout, Fitbit API), CGM exports, InBody printouts, and manual data entry. What is the architectural approach for getting data into the database? Should ingestion be manual-only, source-specific automated importers, or a hybrid?

## Decision Drivers
- Data sources are heterogeneous — no single format or protocol covers all of them
- Some sources have stable export formats suitable for automation; others do not
- Manual entry is always possible but does not scale to hundreds of biomarkers across years of history
- Automated importers add maintenance burden when source formats change
- The project has open source intent — importers for common sources (Quest, Function Health, Fitbit, Levels) have value to others
- Data integrity: automated import must handle duplicates, schema mismatches, and unit normalization

## Considered Options
- Manual entry only
- Hybrid: manual entry + source-specific automated importers
- Structured import pipeline with a defined importer interface

## Decision Outcome
Chosen option: **Structured import pipeline with a defined importer interface**

All data enters the system through the Core REST API bulk import endpoint. No importer process has direct database access — this is not a supported path. Direct database writes bypass validation, bypass the abstraction layer, and are not maintainable as a project pattern. Power users who want to write directly to the database can do so on their own responsibility but the project does not document or support it.

### Positive Consequences
- Consistent validation, duplicate detection, and error handling across all sources
- Security model is uniform — all writes go through the authenticated Core REST API
- The import pipeline is just another REST API client; it can be replaced, extended, or supplemented by plugins (ADR-0010)
- Open source contributors can add importer modules without understanding the database layer

### Negative Consequences / Tradeoffs
- Interface design adds upfront work; risk of over-engineering before enough source formats are understood — mitigated by starting with one or two sources before finalizing the interface
- All bulk imports go over HTTP — adds latency vs. direct database writes; acceptable at personal-data scale

## Bulk Import Endpoint Behavior

The Core REST API exposes a `/v1/import` endpoint with the following guarantees:

**Validation before writes.** The entire batch is parsed and validated before any database transaction begins. All validation errors are collected and returned together — callers receive the full error list, not just the first failure.

**Dry-run mode.** A `?dry_run=true` parameter runs full validation and returns what would be imported without writing anything. Essential for debugging import files before committing data.

**Atomic transaction.** If validation passes, the entire batch is written in a single database transaction. If any write fails, the transaction rolls back. The database is never left in a partial state.

**Explicit conflict policy.** Every import request must specify a conflict policy:
- `reject` (default) — error if any row conflicts with existing data
- `skip` — silently ignore conflicting rows, import the rest
- `upsert` — overwrite existing rows with incoming data

There is no implicit default that silently mutates data. Callers must be explicit.

**Structured response.** The response includes: rows imported, rows skipped, rows rejected, and per-row error details where applicable.

## Importer Interface (to be finalized)

Each source importer is a module that conforms to a defined interface:

```
parse(raw_input) → List[ImportRecord]
validate(records) → ValidationResult
normalize(records) → List[NormalizedRecord]
```

The import pipeline calls these in sequence, then submits the normalized records to the Core REST API bulk import endpoint. The interface version is declared per-importer (see design-rationale.md versioning surfaces).

The specific interface contract is not finalized — it will be refined as the first two or three source importers are built. The pattern above is the intended shape.

## Pros and Cons of the Options

### Manual entry only
- Pro: Simplest — no import code to write or maintain
- Pro: Full human review of every record before it enters the database
- Con: Does not scale to Function Health's ~200 biomarker panels or years of CGM data
- Con: High friction discourages keeping data current

### Hybrid: manual entry + source-specific automated importers
- Pro: Manual entry covers one-off or unsupported sources; importers handle high-volume sources
- Pro: Importers can be added incrementally as sources are prioritized
- Con: No consistent interface — each importer is bespoke
- Con: Importers break when source export formats change (patient portals are especially unstable)

### Structured import pipeline with a defined importer interface
- Pro: Each source gets a discrete importer module conforming to a shared interface (parse → validate → normalize → upsert)
- Pro: Consistent duplicate detection, unit normalization, and error handling across all sources
- Pro: Easier for open source contributors to add new source importers
- Con: Interface design adds upfront work before any data is imported
- Con: Risk of over-engineering the interface before enough sources are understood

## Known Sources Requiring Importer Design
- Quest / Function Health lab panels (CSV or JSON export — format TBD)
- Beaumont / Corewell patient portal (format TBD)
- Levels CGM (export format unknown — feasibility TBD)
- Fitbit (Google Takeout JSON or Fitbit API)
- InBody 120 (format TBD)
- InBody 580 via Enara Health (format TBD)

## Open Decisions
- Importer interface contract — to be finalized after the first two or three source importers are built
- Per-source export format research (see open-questions.md)

## Links
- Related: [ADR-0003](0003-database-backend.md) — database backend
- Related: [ADR-0006](0006-application-architecture.md) — import pipeline as a first-class process
- Related: [ADR-0010](0010-cli-plugin-model.md) — plugins can add new import sources
- Related: [specs/security.md](../security.md) — bulk import validation requirements
- Related: [open-questions.md](../open-questions.md) — per-source export format research

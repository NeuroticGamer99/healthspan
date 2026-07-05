# ADR-0021: Time-Series Data Aggregation Strategy

## Status
Proposed — stub

## Context and Problem Statement
Some biomarker data sources produce high-frequency time-series data. Continuous glucose monitors (CGM) record readings every 5 minutes — roughly 288 readings per day or 105,000 per year. Other wearable sources (heart rate, activity, sleep stages) can be similarly dense. Raw data must be preserved for clinical accuracy, but dashboards and trend analyses need pre-computed aggregates to render efficiently without scanning millions of rows on every query.

Home Assistant solves a similar problem with a two-stage statistics pipeline (raw → 5-minute → hourly), purging raw data after 10 days. Healthspan cannot purge raw health data — it has permanent clinical value — but it faces the same query performance challenge.

## Decision Drivers
- Raw readings must never be discarded — personal health data has permanent value for longitudinal analysis
- Dashboard rendering and trend queries must not degrade as the dataset grows over years
- Different biomarker types have different natural aggregation windows (CGM is minute-scale; labs are quarterly)
- Aggregation strategy must work with SQLite (the default backend) and not require a dedicated time-series database
- Materialized aggregates must stay consistent with source data — stale summaries erode trust
- The aggregation system should be implementable behind the plugin interface (micro-kernel principle) — noting that if it runs inside the Core Service, it must be a first-party internal component, not a loadable plugin (ADR-0025)

## Decision Outcome
TBD — design after the database schema (ADR-0003, ADR-0009) and at least one high-frequency data importer (CGM) are implemented.

## Open Questions
- What aggregation windows are needed? (hourly, daily, weekly, monthly?)
- Should aggregates be materialized views, summary tables, or computed on demand with caching?
- ~~How are aggregates invalidated when source data is corrected?~~ — Answered by [ADR-0027](0027-audit-trail-and-corrections.md): aggregates are rebuildable caches, never authoritative; they derive from current-state data (`*_current` views) and are invalidated/recomputed in response to the Core-emitted `data.imported`, `data.corrected`, and `data.deleted` events
- Should aggregation run as a background job (ADR-0012) triggered by import events (ADR-0011)?
- What CGM-specific derived metrics are needed? (time-in-range, GMI, coefficient of variation, daily overlay)

## Comparable Prior Art
- Home Assistant Recorder statistics pipeline (5-min → hourly, with purge)
- TimescaleDB continuous aggregates (automatic materialized views over hypertables)
- InfluxDB downsampling tasks (continuous queries that write to lower-resolution retention policies)

## Links
- Related: [ADR-0003](0003-database-backend.md) — database backend choice affects aggregation strategy
- Related: [ADR-0009](0009-database-migration.md) — aggregate tables require migrations
- Related: [ADR-0011](0011-event-bus.md) — import events could trigger aggregation jobs
- Related: [ADR-0012](0012-job-abstraction.md) — aggregation as a background job
- Related: [ADR-0027](0027-audit-trail-and-corrections.md) — aggregates are rebuildable read models; correction/deletion invalidation contract
- Related: [open-questions.md](../open-questions.md) — CGM indexing strategy

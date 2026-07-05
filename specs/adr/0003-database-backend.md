# ADR-0003: Database Backend

## Status
Accepted

## Context and Problem Statement
The platform stores longitudinal health data — lab results, body composition, clinical events, interventions, CGM readings, and wearable aggregates. The initial target is a single-user local deployment. Should the database layer be SQLite-only, or should the architecture support pluggable backends to accommodate users who want a server-based database or a hosted option?

## Decision Drivers
- Local-first is a strong default for personal health data privacy
- SQLite is already specified in the design rationale for good reasons (portable, no server, trivial backup)
- Some users may want PostgreSQL for multi-device sync, multi-user households, or larger data volumes
- A database abstraction layer adds complexity and constrains query patterns to a common subset
- The MCP server queries the database directly — the abstraction boundary matters for query design

## Considered Options
- SQLite-only (no abstraction)
- Pluggable backend (SQLite default, PostgreSQL and others via adapter)
- SQLite with optional PostgreSQL sync (local primary, remote replica)

## Decision Outcome
Chosen option: **SQLite-only for v1 (no abstraction)**

This decision was made in practice before it was recorded here: [ADR-0013](0013-encryption-at-rest.md) (Accepted) is SQLCipher-specific, and [ADR-0009](0009-database-migration.md)'s custom migration runner was justified on a single known SQL dialect. This ADR makes the record match reality.

The platform targets SQLite exclusively. Queries, migrations, and schema design may use SQLite-specific features freely — there is no common-subset constraint and no abstraction layer to maintain before the core feature set exists.

PostgreSQL (or any other backend) is not a configuration option to be added later; it is an architectural change requiring a new ADR. That ADR must also revisit at minimum:
- [ADR-0009](0009-database-migration.md) — the custom migration runner assumes a single dialect; multi-backend migration management was the explicit trigger for reconsidering Alembic
- [ADR-0013](0013-encryption-at-rest.md) / [ADR-0028](0028-key-derivation-and-rotation.md) — encryption at rest is SQLCipher-specific and does not transfer to PostgreSQL, which has an entirely different model (TDE, pgcrypto, or disk-level encryption)

### Positive Consequences
- Single portable encrypted file, trivial backup (`healthspan db backup`), no server dependency — the local-first privacy default costs nothing to operate
- SQLite-specific features (JSON functions, partial indexes, generated columns) are freely available to the schema and query layer
- Sufficient for personal-scale data indefinitely — SQLite handles millions of rows well beyond any single person's health record volume

### Negative Consequences / Tradeoffs
- No path to multi-device write access or multi-user households without a future ADR that reworks migrations and encryption alongside the backend
- Users who already run PostgreSQL cannot point the platform at it

## Pros and Cons of the Options

### SQLite-only (no abstraction)
- Pro: Simplest implementation — queries can use SQLite-specific features freely
- Pro: Single portable file, trivial backup, no server dependency
- Pro: Sufficient for personal-scale data indefinitely (SQLite handles millions of rows well)
- Con: No path to multi-device or multi-user scenarios without replacing the stack
- Con: Limits the project's value to users who want a server-based setup

### Pluggable backend (SQLite default, PostgreSQL and others via adapter)
- Pro: Opens the project to server-based deployments and multi-user households
- Pro: PostgreSQL is a natural fit for users who already run it
- Con: Must restrict queries to a common SQL subset — some SQLite conveniences (e.g. flexible typing, JSON functions) may not be portable
- Con: Schema migrations must be tested against each supported backend
- Con: Increases implementation complexity significantly before the core feature set exists

### SQLite with optional PostgreSQL sync
- Pro: Keeps local-first as the primary model while enabling remote backup/access
- Con: Sync logic is non-trivial — conflict resolution, schema version parity, replication lag
- Con: Two databases in play increases operational complexity for users

## Links
- Related: [ADR-0009](0009-database-migration.md) — migration runner justified on a single dialect
- Related: [ADR-0013](0013-encryption-at-rest.md) — SQLCipher-specific encryption at rest
- Related: [ADR-0001](0001-mcp-server-language.md)
- Related: [design-rationale.md](../design-rationale.md)

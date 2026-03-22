# ADR-0009: Database Migration Approach

## Status
Accepted

## Context and Problem Statement
The database schema will evolve over time as new data types are supported, existing tables are extended, and bugs are corrected. Users may have an existing database from an older version of the platform. How are schema changes applied safely to existing databases, and how is the current schema version tracked?

## Decision Drivers
- Users must be able to upgrade an existing database without losing data
- Migrations must be atomic — a failed migration must not leave the database in a partial state
- The migration history must be auditable — it must be possible to know exactly which migrations have been applied and when
- The approach must work with the pluggable database layer (ADR-0003); SQLite is the initial target
- Dependencies should be kept minimal for a component this fundamental
- The approach must be accessible to contributors without deep ORM knowledge

## Considered Options
- Alembic — full-featured Python migration framework, SQLAlchemy-based
- yoyo-migrations — lightweight Python migration library, SQL-file-based
- Custom migration runner — purpose-built, ~50 lines of Python, no additional dependencies

## Decision Outcome
Chosen option: **Custom migration runner as a CLI subcommand (`biocontext db migrate`)**

The logic required is straightforward and well-understood. A custom runner is fully transparent, introduces no additional dependencies, and is directly aligned with the `sql/migrations/` convention already specified in design-rationale.md. Alembic can be reconsidered if the database layer grows to require multi-backend migration management.

### Positive Consequences
- No additional runtime dependency
- Fully transparent — the runner is readable Python that any contributor can understand
- Migration files are plain SQL — readable, diffable, portable
- Atomic per-migration: each migration runs in its own transaction; failure stops the runner without corrupting the database
- Alembic remains available as a future option if requirements grow

### Negative Consequences / Tradeoffs
- No auto-generation of migration files (Alembic can generate diffs from ORM models) — migrations must be written manually
- No built-in rollback support — rollbacks require a separate down-migration file by convention

## Migration File Convention

Files live in `sql/migrations/` and are named with a zero-padded sequence number and a descriptive slug:

```
sql/migrations/
  0001_initial_schema.sql
  0002_add_biomarker_aliases.sql
  0003_add_intervention_dose_history.sql
```

Each file contains one or more SQL statements. The runner wraps each file in a transaction. A file must be idempotent where possible (use `CREATE TABLE IF NOT EXISTS`, `ALTER TABLE ... ADD COLUMN IF NOT EXISTS`).

## schema_version Table

```sql
CREATE TABLE IF NOT EXISTS schema_version (
    version     INTEGER PRIMARY KEY,
    filename    TEXT NOT NULL,
    applied_at  TEXT NOT NULL  -- ISO 8601 UTC timestamp
);
```

## Runner Behavior (`biocontext db migrate`)

1. Read all `.sql` files in `sql/migrations/` in numeric order
2. Query `schema_version` for already-applied migration numbers
3. For each unapplied migration, in order:
   a. Begin a transaction
   b. Execute the SQL
   c. Insert a row into `schema_version`
   d. Commit
   e. If any step fails: rollback, report the error with filename and line number, stop
4. Report the number of migrations applied and the final schema version

The runner is invoked automatically by the process launcher on startup (ADR-0008) and is also available as a standalone command.

## Rollback Convention

Rollback is not automated. If a migration must be reversed:
1. Write a new migration file that undoes the change (a "down migration" by another name)
2. Apply it with `biocontext db migrate`
3. Never modify or delete an already-applied migration file

## Future Consideration
If the database layer is extended to support PostgreSQL (ADR-0003), evaluate Alembic at that point. Alembic's multi-backend support and auto-diff generation become more valuable when the SQL dialect is no longer a single known target.

## Links
- Related: [ADR-0003](0003-database-backend.md) — database pluggability
- Related: [ADR-0006](0006-application-architecture.md) — CLI as the entry point for migrations
- Related: [ADR-0008](0008-process-lifecycle.md) — migration runner invoked on startup
- Related: [design-rationale.md](../design-rationale.md) — original schema versioning specification

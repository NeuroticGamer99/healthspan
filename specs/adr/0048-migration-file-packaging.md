# ADR-0048: Migration File Packaging and Discovery

## Status
Accepted

## Context and Problem Statement
[ADR-0009](0009-database-migration.md) (Accepted) chose a custom migration runner over numbered plain-SQL files and, in its "Migration File Convention" section, located those files under a repository-root `sql/migrations/` directory — echoed in [design-rationale.md](../design-rationale.md) ("Schema Versioning") and the [glossary](../glossary.md) "Migration 0001" entry. That location predates any code.

Implementing the runner (Phase 1 WI-3) surfaced a conflict with [ADR-0023](0023-distribution-mechanism.md) (Accepted): Healthspan is distributed as an installed Python package (`uv tool install healthspan`, a built wheel). A repository-root `sql/` directory is **not part of the installed distribution** — it exists only in a source checkout — so a runner that reads `sql/migrations/` at runtime works from a dev checkout and silently finds nothing once the tool is installed the way ADR-0023 says users install it. The runner must locate its migrations from *inside* the installed distribution.

ADR-0009 is immutable under ADR governance; this ADR extends it. The runner choice, the numbered-plain-SQL file convention, the `schema_version` table, the rollback convention, and [ADR-0035](0035-migration-execution-semantics.md)'s execution semantics all stand unchanged. This ADR decides only **where the files physically live and how the runner discovers them** so the convention survives installation.

## Decision Drivers
- The installed CLI (`healthspan db migrate`) must find its migration files at runtime, under ADR-0023's wheel/`uv tool install` distribution — the dev-checkout-only `sql/migrations/` path does not satisfy this
- ADR-0009's load-bearing properties must be preserved: plain SQL, readable and diffable, numbered, applied in order, tracked by `schema_version`
- No new runtime dependency for a mechanism this fundamental (ADR-0009's own driver)
- One source of truth for the schema — the files, not a copy embedded elsewhere
- The mechanism should be the standard library's, not a bespoke path search

## Considered Options
1. **Keep `sql/migrations/` at the repository root, add build-backend configuration to include it as package data, and resolve it at runtime** from wherever the build backend installs that data directory
2. **Ship the files as package data inside the package, at `src/healthspan/migrations/NNNN_*.sql`, discovered via `importlib.resources`** (chosen)
3. **Embed the DDL in Python** (versioned modules or SQL string constants), so there is nothing separate to package

## Decision Outcome
Chosen: **option 2.** Migration files live at `src/healthspan/migrations/NNNN_<slug>.sql` and are discovered with `importlib.resources.files("healthspan.migrations")`.

Because the files sit inside the package tree under the `src/` layout, the `uv_build` backend includes them in the wheel with no extra configuration (verified: the built wheel contains `healthspan/migrations/0001_initial_schema.sql`), and `importlib.resources` — the standard-library API for exactly this — reads them identically from a source checkout, an installed wheel, or a zipimport. ADR-0009's convention is preserved in full; only the *directory* moves, from a repo-root sibling of the package to a subpackage of it. The runner still reads every `.sql` file in numeric order, still tracks applications in `schema_version`, and the files remain plain, diffable SQL.

Option 3 was rejected for the same reason ADR-0009 rejected ORM-managed migrations: it sacrifices the plain-SQL, independently-diffable file that is the convention's whole point. Option 1 keeps the ADR-0009 path literally but pays for it with build-backend data-inclusion configuration and a more roundabout runtime resource lookup, to preserve a directory location that carries no decision content — the value ADR-0009 assigned was to *plain numbered SQL files under version tracking*, not to the specific parent directory.

### Positive Consequences
- The installed CLI finds its migrations under the real distribution mechanism (ADR-0023); the dev-only failure mode is closed
- ADR-0009's plain-SQL, numbered, diffable, single-source convention is fully preserved
- No new dependency and no build configuration — `importlib.resources` is stdlib and the `src/` layout ships package data by default
- Identical discovery from a checkout, a wheel, or zipimport — no code path that only works in development

### Negative Consequences / Tradeoffs
- The physical location differs from ADR-0009's stated `sql/migrations/`; contributors look inside the package (`src/healthspan/migrations/`) rather than at the repository root. Mitigated by this ADR, the `Extended by` link on ADR-0009, and the corrected references in design-rationale.md and the glossary
- Migration `.sql` files are shipped as installed package data — appropriate, since they *are* the schema the installed tool must apply

## Pros and Cons of the Options

### Option 1 — repo-root `sql/migrations/` included as package data
- Pro: honors ADR-0009's path literally
- Con: needs build-backend data-inclusion config and a less direct runtime resource lookup, all to keep a directory that carries no decision content

### Option 2 — package data under `src/healthspan/migrations/` via `importlib.resources` (chosen)
- Pro: works under ADR-0023 distribution with zero build config; stdlib discovery; convention preserved
- Con: diverges from ADR-0009's stated directory (resolved by this extension ADR)

### Option 3 — DDL embedded in Python
- Pro: nothing separate to package
- Con: loses the plain-SQL, independently-diffable file ADR-0009 deliberately chose

## Links
- Extends: [ADR-0009](0009-database-migration.md) — preserves the runner, numbering, plain-SQL, and `schema_version` convention; relocates the files so an installed distribution can discover them
- Related: [ADR-0023](0023-distribution-mechanism.md) — the wheel / `uv tool install` distribution under which a repo-root `sql/` is unreachable at runtime
- Related: [ADR-0035](0035-migration-execution-semantics.md) — execution semantics, unaffected by this relocation
- Related: [design-rationale.md](../design-rationale.md) — schema-versioning reference (location corrected to match)
- Related: [glossary.md](../glossary.md) — "Migration 0001" entry (location corrected to match)

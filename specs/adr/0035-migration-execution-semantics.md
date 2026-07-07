# ADR-0035: Migration Execution Semantics and Connection Pragmas

## Status
Proposed

## Context and Problem Statement
ADR-0009 (Accepted) chose a custom migration runner and promised "atomic per migration." The [architecture review](../architecture-review-2026-06-10.md) found the promise does not survive contact with the driver, and two adjacent defects in the same ADR (items 3.F and 1.G — the latter deferred here because fixing it changes decision content in an Accepted ADR):

1. **The driver silently breaks migration atomicity** (3.F). Python's DB-API sqlite driver — and `sqlcipher3`, a pysqlite fork — has legacy implicit transaction handling: in its default mode it auto-begins transactions around DML and issues implicit commits around DDL statements. A migration file containing several `CREATE TABLE`/`ALTER TABLE` statements gets committed piecemeal; a mid-file failure leaves the schema half-migrated — exactly the partial state ADR-0009 says cannot happen. ADR-0009's runner sketch ("begin a transaction, execute the SQL") assumed transaction control it never actually had.
2. **Two load-bearing pragmas were never recorded as decisions** (3.F). `PRAGMA foreign_keys` is **off by default** in SQLite and per-connection — every FK constraint in the schema is decorative on any connection that forgets it. `PRAGMA journal_mode=WAL` is what makes concurrent reads under FastAPI work; [ADR-0028](0028-key-derivation-and-rotation.md)'s connection-pool decision leans on it and explicitly deferred the pragma discipline to this review item.
3. **ADR-0009 cites SQLite syntax that does not exist, in service of a requirement that should not exist** (1.G). Its file convention requires migrations to be idempotent "where possible," citing `ALTER TABLE ... ADD COLUMN IF NOT EXISTS` — SQLite has no such syntax. And the requirement itself is wrong: the runner's `schema_version` tracking already guarantees a file never runs twice, and `IF NOT EXISTS`-style SQL actively harms safety by silently papering over schema drift instead of failing loudly.

ADR-0009 is immutable under ADR governance; this ADR extends it. The runner choice, file convention (numbering, plain SQL), `schema_version` table, and rollback convention all stand — this ADR replaces the *execution semantics* and corrects the file-authoring rules.

## Decision Drivers
- "Atomic per migration" must be true mechanically, not aspirationally — the guarantee is the whole reason a failed migration is recoverable
- Transaction control must not depend on driver default behavior that varies across Python versions and forks
- Foreign-key enforcement must be impossible to forget: a per-connection pragma left to call-site discipline will eventually be missed
- Schema drift must fail loudly at migration time, not be masked by defensive SQL
- Table-rebuild migrations (SQLite's documented 12-step `ALTER TABLE` procedure) must be possible — they require foreign keys off during the rebuild
- Durability/performance settings are real decisions and belong on the record, per this project's convention

## Considered Options
1. **Status quo** — driver-default transaction handling, pragmas left to implementation, idempotent-where-possible files
2. **Explicit runner-controlled transactions (`BEGIN IMMEDIATE`), recorded pragma set, exact-predecessor-state migration files** (chosen)

## Decision Outcome
Chosen: **option 2.**

### Transaction discipline (replaces ADR-0009's runner steps 3a–3e)
The migration connection is opened with **driver-level transaction management disabled** — `isolation_level=None`, or the PEP 249 `autocommit` connection attribute on Python 3.12+ if `sqlcipher3` exposes it (implementation verifies; the required *semantics* are that the driver never issues an implicit `BEGIN` or `COMMIT`). The runner then executes, per unapplied migration file, in order:

1. `PRAGMA foreign_keys = OFF` — set outside the transaction; the pragma is a silent no-op inside one, which is precisely why the runner manages it explicitly
2. `BEGIN IMMEDIATE` — takes the write lock up front, so a busy database fails fast at the start rather than deadlocking mid-migration on a lock upgrade
3. Execute the file's SQL statements
4. Insert the file's row into `schema_version`
5. `PRAGMA foreign_key_check` — any reported violation aborts
6. `COMMIT`; on any failure at any step: `ROLLBACK`, report filename and failing statement, stop the run

Steps 3–5 succeed or disappear together. The `schema_version` row is inside the same transaction as the DDL it records — the ledger can never disagree with the schema.

**Foreign keys during migration** follow SQLite's documented table-rebuild procedure: off for the migration connection, with the mandatory `foreign_key_check` before commit as the honesty mechanism. Enforcement is not weakened — a migration that introduces an FK violation cannot commit — while legitimate rebuilds (copy table, drop, rename) remain possible.

**Migration files must contain only transactional SQL.** `VACUUM` and `PRAGMA journal_mode` changes cannot run inside a transaction and are prohibited in migration files; they are runner- or maintenance-level operations. (In SQLite, essentially all DDL and DML is transactional — this restriction bites rarely.)

### Connection pragmas (recorded decisions)
| Pragma | Value | Where set | Why |
|---|---|---|---|
| `foreign_keys` | `ON` | Every runtime connection, in the **single shared connection factory** used by the Core Service pool and CLI direct-DB commands | Off by default and per-connection; call-site discipline will eventually miss one, so there is exactly one place that opens connections. The migration runner is the sole documented exception (off + check, above). |
| `journal_mode` | `WAL` | Once, at `healthspan init` (persistent in the database file) | Concurrent readers with a single writer — how SQLite serves FastAPI reads well; assumed by ADR-0028's connection pool. The `-wal`/`-shm` sidecars are why the live database must never be cloud-synced (ADR-0019). |
| `synchronous` | `NORMAL` | Connection factory | The standard WAL pairing. Full corruption safety; on power loss the last transaction(s) may roll back rather than being durably committed. `FULL` buys per-commit durability at a per-write fsync cost — the wrong trade for a single-user platform whose ingest is re-runnable imports. Recorded so the tradeoff is a decision, not a driver default. |

### File-authoring rules (corrects ADR-0009's convention)
- **The per-file idempotency requirement is dropped.** Runner-level idempotency — applied files are skipped via `schema_version` — is the real mechanism and the only one needed.
- Migration files **assume the exact schema state their predecessors produced** and fail loudly if reality differs. Defensive `IF NOT EXISTS` guards are not permitted in migrations (the `schema_version` bootstrap DDL, which must run on both fresh and existing databases before the ledger exists, is the sole exception): a guard that "helps" a migration succeed against a drifted schema converts a detectable defect into silent divergence.
- The cited `ALTER TABLE ... ADD COLUMN IF NOT EXISTS` does not exist in SQLite; column additions are plain `ALTER TABLE ... ADD COLUMN`, correct precisely because the file runs exactly once against a known predecessor state.

### Positive Consequences
- "Atomic per migration" is mechanically true — enforced by explicit transaction control, not driver defaults that vary by Python version
- The schema ledger cannot drift from the schema (same transaction), and schema drift cannot hide behind defensive SQL (loud failure is the design)
- FK enforcement is on everywhere by construction (one connection factory), yet table rebuilds remain possible with integrity verified before commit
- The pragma set is on the record; ADR-0028's deferred parenthetical is resolved

### Negative Consequences / Tradeoffs
- Migration authors must know the predecessor state (no defensive guards) — mitigated by the testing-strategy requirement that every migration applies against a database at exactly the previous version
- `synchronous=NORMAL` accepts loss of the most recent transaction(s) on power failure — an explicit trade of per-commit durability for write performance, appropriate here
- `BEGIN IMMEDIATE` means the runner refuses to proceed against a busy database — correct behavior (the launcher runs migrations before the Core Service starts, per ADR-0008), but stricter than a deferred lock

## Pros and Cons of the Options

### Status quo
- Pro: nothing to write
- Con: atomicity silently false under the real driver; FK enforcement decorative on any forgotten connection; a nonexistent syntax cited as the convention's cornerstone

### Explicit transactions + recorded pragmas + exact-state files (chosen)
- Pro: every guarantee ADR-0009 states becomes mechanically enforced; drift fails loudly; decisions on the record
- Con: more specified runner behavior to implement and test; migration authoring is stricter

## Links
- Extends: [ADR-0009](0009-database-migration.md) — replaces the runner's execution semantics (steps 3a–3e) and the file convention's idempotency rule; runner choice, numbering, `schema_version`, and rollback convention unchanged
- Related: [ADR-0028](0028-key-derivation-and-rotation.md) — connection pool assumes WAL; pragma discipline deferred to this ADR from there
- Related: [ADR-0008](0008-process-lifecycle.md) — launcher runs migrations before the Core Service starts (why exclusive access holds)
- Related: [ADR-0019](0019-multi-device-sync.md) — WAL sidecar files and live-file sync unsafety
- Related: [specs/testing-strategy.md](../testing-strategy.md) — migration test targets updated to match (mid-file atomicity, foreign_key_check, pragma verification)
- Resolves: [architecture review 2026-06-10](../architecture-review-2026-06-10.md), items 3.F (transaction discipline, pragmas) and 1.G (both corrections)

# ADR-0050: Token-Store and Auth-Enforcement Implementation Decisions (Phase 2 WI-2)

## Status
Accepted

## Context and Problem Statement

Phase 2 WI-2 implements [ADR-0026](0026-named-scoped-tokens.md)'s named scoped bearer tokens: migration 0002 (`tokens`, `auth_audit`), the verification/scope-enforcement dependency, and (WI-2b) the failure rate limiter, lifecycle commands, and default-set bootstrap. Implementation surfaced decisions the ADR left open — most prominently a genuine sequencing contradiction: ADR-0026 says *"`healthspan init` creates the database, then mints the default token set"*, but Phase 1 deliberately made `init` produce an **empty** database (no schema) with migration a separate explicit step ([ADR-0009](0009-database-migration.md)/[ADR-0035](0035-migration-execution-semantics.md); Core never migrates, [ADR-0039](0039-startup-sequence-and-passphrase-handoff.md)) — so at `init` time the `tokens` table does not exist.

Per the CLAUDE.md decision-capture convention these land as one Proposed ADR: several touch the Accepted ADR-0026, and batching a WI's accumulated implementation decisions into one extension ADR is the sanctioned pattern ([ADR-0049](0049-core-service-skeleton-implementation-decisions.md) precedent).

## Decision Drivers

- ADR-0026's security posture must survive implementation untouched: hashes only at rest, uniform denials, audit without values
- Phase 1's init/migrate separation is Accepted and correct — the bootstrap contradiction must be resolved without reopening it
- An existing already-initialized database (any Phase-1 database) must reach the tokened state the same way a fresh install does
- The ADR-0037 concurrency rules (driver never on the event loop; thread-affine connections) bind every request-path database touch

## Decision Outcome

### 1. Default-set bootstrap happens at first `service start`, not at `init`

When the Core Service starts and finds the `tokens` table **empty**, it mints the ADR-0026 default token set (hashes into `tokens`, plaintexts into the holders' keyring entries) and the MCP client-facing secret (hash into the MCP keyring entry, plaintext printed once). A non-empty table is never re-minted, so the check is idempotent and runs on every start at the cost of one `SELECT count(*)`.

This **extends ADR-0026's bootstrap clause**: `init` keeps its Phase-1 meaning (provision an empty encrypted file), `db migrate` stays the only schema writer, and minting happens at the first moment both the schema exists and the key-holding process is running. It also covers databases created before migration 0002 existed with no separate backfill path — their first post-upgrade `service start` mints identically. Direct-start is foreground (Phase 2 ships only ADR-0039 direct-start, [ADR-0049](0049-core-service-skeleton-implementation-decisions.md) §5), so the one-time plaintext printout lands on the operator's console. (Minting itself ships in WI-2b; the timing decision is recorded now because migration 0002 and the verify path are built against it.)

### 2. Token names exclude underscore, making the token format parseable

ADR-0026 fixed the format `hsp_<name>_<secret>` with a base64url secret — whose alphabet includes both `-` and `_`, so nothing in the ADR's grammar made the name/secret boundary unambiguous. Decision: **token names match `[a-z0-9][a-z0-9:-]*`** (lowercase alphanumerics, `-`, and `:` for the `job:<uuid>` convention; never `_`), and the first `_` after the `hsp_` prefix terminates the name. Every default token name already conforms. Enforced at mint time; the advisory-name parse (`healthspan.tokens.parse_name`) is what the rate limiter's bucket key and nothing else consumes.

### 3. Uniform 401 concretized; `WWW-Authenticate: Bearer` on denial

ADR-0026 requires that no unknown-vs-revoked distinction leak. Concretely: missing header, malformed value, unknown token, and revoked token all answer `401` with body `{"detail": "authentication failed"}` and header `WWW-Authenticate: Bearer` — the bare HTTP-required challenge, no realm, no OAuth metadata (consistent with the MCP-side posture in testing-strategy.md). The `auth_audit` outcome (`denied:invalid` vs `denied:revoked`) records the distinction server-side. Every credential that resolves to no token row — missing, unparseable, or well-formed `hsp_<name>_<secret>` matching nothing — audits as `denied:invalid` with `token_name = 'invalid'`: the embedded name is attacker-supplied text and never populates the audit column of an unrecognized credential (it feeds only the rate limiter's bucket key, ADR-0026 rule 2). A recognized-but-revoked token audits under its real (server-side) name.

### 4. Scope-vocabulary enforcement lives in the application layer, and route declarations validate at assembly

The `tokens.scopes` column is a space-separated `TEXT` list with **no CHECK constraint on scope names**: the vocabulary grows by ADR ([ADR-0040](0040-health-endpoint-authentication.md)/[ADR-0043](0043-ai-authored-analyses-and-annotate-scope.md) precedents) and must not cost a schema migration per scope. The canonical set lives in code (`healthspan.tokens.SCOPES`), enforced at mint time and at route-declaration time: `require(<scope>)` with an unknown scope raises at app assembly — a typo'd declaration would otherwise deny every caller forever, and ADR-0026's declare-every-route rule already established assembly time as where declaration errors surface. Column encodings (space-separated `scopes` and `publish_namespaces`, `job_id` FK binding, `revoked`/`revoked_utc`) are recorded in [data-model.md](../data-model.md), Migration 0002.

### 5. Request-path database access: the ADR-0037 pool, and auth as a synchronous dependency

The verify dependency is the Core Service's first request-path database consumer, so WI-2 realizes [ADR-0037](0037-core-service-concurrency-and-driver.md)'s connection shape: a pool of thread-local connections created lazily per worker thread from the single `healthspan.db` factory, keyed once from the retained key, `check_same_thread=True` as the affinity enforcement. The dependency is a synchronous callable — FastAPI runs it on the AnyIO threadpool, keeping the driver off the event loop; the `public` liveness marker stays an async no-op (no thread hop, no database, per [ADR-0040](0040-health-endpoint-authentication.md)). Every authentication outcome, including `ok`, writes its `auth_audit` row per ADR-0026's outcome list; the `ok` row and the `last_used_utc` touch share one `BEGIN IMMEDIATE` transaction. Unauthenticated failures write their audit row per attempt **even when rate-limited**: the WI-2b limiter ([ADR-0051](0051-auth-lifecycle-and-rate-limiting-implementation-decisions.md)) delays responses and bounds its own in-memory state, but deliberately still records every throttled attempt — the evidence trail is the point, and [api-reference.md](../api-reference.md) makes the throttled-attempt audit row contractual. A local flooder can therefore grow `auth_audit` without bound. Accepted with eyes open: the listener binds loopback-only, so the only possible flooder is a local process already running as the owner — an actor who could exhaust the disk directly — and `auth_audit` is prunable per ADR-0026.

### 6. `auth_audit` is a separate table from `audit_log`

ADR-0026 describes `auth_audit` and [ADR-0027](0027-audit-trail-and-corrections.md) owns `audit_log`; implementation keeps them distinct rather than folding auth outcomes into the mutation log: different columns (no row images, no batch/job provenance), different write rate (every request vs. every mutation), different retention pressure (prunable per ADR-0026 vs. permanent history per ADR-0027). Same append-only trigger pattern.

### Positive Consequences

- The init/mint contradiction is resolved without touching Phase 1's accepted init semantics, and upgrade and fresh-install paths converge on one mechanism
- The token format is now mechanically parseable, which the rate limiter's bucket key (ADR-0026 rule 2) requires
- Scope growth stays a code-plus-ADR change, never a migration
- The first request-path database consumer lands already inside the ADR-0037 discipline instead of retrofitting it in WI-3

### Negative Consequences / Tradeoffs

- Tokens do not exist until the first `service start` — a freshly-migrated database has an API surface nothing can call yet; acceptable because nothing serves until `service start` either
- One `auth_audit` row per request (including successes) is deliberate write volume — single-user scale makes it negligible; pruning follows the jobs-table pattern ([ADR-0012](0012-job-abstraction.md)) when jobs land
- Unauthenticated failures write one `auth_audit` row per attempt even when rate-limited — unbounded append under a local flood, accepted knowingly (loopback-only listener; a local flooder already has direct disk access; the table is prunable per ADR-0026)

## Consequences for Other Documents

- **[ADR-0026](0026-named-scoped-tokens.md)** (Accepted): navigation link — bootstrap timing and name-charset extension recorded here
- **[data-model.md](../data-model.md)**: Migration 0002 section (tokens/auth_audit shapes) — same PR
- **[api-reference.md](../api-reference.md)**: authentication error formats; `/v1/health/detail` and `/v1/metrics` implemented shapes — same PR

## Links

- Extends: [ADR-0026](0026-named-scoped-tokens.md) — bootstrap timing (first `service start` mint), token-name charset, uniform-401 concretization
- Implements: [ADR-0037](0037-core-service-concurrency-and-driver.md) — the thread-affine pool and sync-dependency bridging
- Related: [ADR-0040](0040-health-endpoint-authentication.md) — `monitor`-scoped detail/metrics endpoints landed with this WI
- Related: [ADR-0043](0043-ai-authored-analyses-and-annotate-scope.md) — the `authorship` token attribute realized in migration 0002
- Related: [ADR-0049](0049-core-service-skeleton-implementation-decisions.md) — the WI-decisions-bundle pattern; direct-start context for the bootstrap decision
- Related: [ADR-0009](0009-database-migration.md), [ADR-0035](0035-migration-execution-semantics.md) — the init/migrate separation the bootstrap decision preserves
- Extended by: [ADR-0051](0051-auth-lifecycle-and-rate-limiting-implementation-decisions.md) — WI-2b realizes §1's bootstrap with keyring-first ordering, plus the limiter and lifecycle decisions

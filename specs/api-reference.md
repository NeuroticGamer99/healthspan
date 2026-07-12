# API Reference

Design-time specification of the Core REST API surface. This document consolidates the API endpoints defined across ADRs and design documents into a single reference. It does not replace the auto-generated OpenAPI documentation that FastAPI will produce at runtime — it captures the intended design before implementation begins.

This document is also the **ledger for API-surface decisions made during implementation** (see [CLAUDE.md](../CLAUDE.md), Implementation decision capture): endpoint paths, request/response shapes, error formats, status codes, and per-route scopes decided while coding are recorded here in the same PR that implements them, replacing the "*Endpoints TBD during implementation*" markers below as the work happens.

---

## Conventions

**Base URL:** All endpoints are prefixed with `/v1/`. The version prefix is applied from the first endpoint (see [design-rationale.md](design-rationale.md), versioning surfaces).

**Authentication:** Every endpoint requires a valid `Authorization: Bearer <token>` header and declares a required scope ([ADR-0026](adr/0026-named-scoped-tokens.md)), with one exemption: the liveness endpoint `GET /v1/health` is unauthenticated and returns only a status word, so the launcher and container healthchecks can poll readiness without a credential ([ADR-0040](adr/0040-health-endpoint-authentication.md)). Detailed health and metrics are authenticated (`monitor` scope). See [security.md](security.md).

**Content type:** `application/json` for request and response bodies unless otherwise noted.

**Error responses:** Errors return a JSON body with a structured error object. Error responses must not echo back sensitive input values. See [security.md](security.md).

**Authentication errors (implemented, Phase 2 WI-2):**

- **`401`** — every authentication failure (missing `Authorization` header, malformed value, unknown token, revoked token) answers identically: body `{"detail": "authentication failed"}` and header `WWW-Authenticate: Bearer` (the bare scheme, no realm, no OAuth metadata). Which failure it was is recorded in the append-only `auth_audit` table ([data-model.md](data-model.md), migration 0002), never disclosed to the caller ([ADR-0026](adr/0026-named-scoped-tokens.md) uniform-denial rule).
- **`403`** — authenticated but lacking the route's required scope: `{"detail": "token '<name>' lacks the required scope '<scope>'"}` — names the token and the missing scope, echoes nothing else ([ADR-0026](adr/0026-named-scoped-tokens.md)).
- **`429`** — a throttled authentication *failure* ([ADR-0026](adr/0026-named-scoped-tokens.md) rules 1–4, defaults [ADR-0051](adr/0051-auth-lifecycle-and-rate-limiting-implementation-decisions.md)): body `{"detail": "too many failed authentication attempts"}` plus a `Retry-After` header (integer seconds, rounded up, accurate — a throttled attempt is not a new failure, never extends the window, and a client that waits the advertised time escapes). Only failures are ever throttled — a request presenting a valid credential is never delayed or rejected by the limiter, and a `403` (authenticated, missing scope) never counts toward it. Buckets key on (source address, advisory token-name prefix) with a per-address aggregate cap; backoff starts after `auth.failure_threshold` free failures (default 5) at 1 s, doubles per failure, and caps at `auth.max_backoff_seconds` (default 60); buckets idle past the prune window are forgotten. Limiter state is in-memory only — a restart clears it, and `POST /v1/auth/reset-limits` clears it on demand. A rate-limited request writes an `auth_audit` row with the `rate-limited` outcome.

Every authentication outcome — including success — writes one `auth_audit` row, and a successful authentication updates the token's `last_used_utc`, in the same transaction. Scope enforcement is a per-route FastAPI dependency created by the route's `require(<scope>)` declaration; an unknown scope name in a declaration fails at app assembly, not at request time.

---

## Endpoint Groups

### Data import
Defined in [ADR-0004](adr/0004-data-ingestion-strategy.md). Bulk import with full-batch validation, atomic transactions, dry-run mode, and explicit conflict policies.

*Endpoints TBD during implementation.*

### Data query and retrieval
The primary read path for the GUI, MCP server, and CLI. Endpoint design will follow from the schema and the MCP tool definitions in [design-rationale.md](design-rationale.md).

*Endpoints TBD during implementation.*

### Data export
Defined in [ADR-0015](adr/0015-data-export.md). Platform-native JSON and CSV export with date range, data type, and biomarker filtering.

*Endpoints TBD during implementation.*

### Jobs
Defined in [ADR-0012](adr/0012-job-abstraction.md). Async job submission, status tracking, and cancellation for long-running operations.

*Endpoints TBD during implementation.*

### Events
Defined in [ADR-0011](adr/0011-event-bus.md). SSE event stream for server-push notifications; inbound webhook for external event sources.

*Endpoints TBD during implementation.*

### Health and metrics
Defined in [observability.md](observability.md) and [ADR-0040](adr/0040-health-endpoint-authentication.md).

| Endpoint | Auth |
|---|---|
| `GET /v1/health` | none (`public`) — liveness only: `200`/`503` and a status word |
| `GET /v1/health/detail` | `monitor` — version, `schema_version`, `db_connected`, uptime |
| `GET /v1/metrics` | `monitor` — request counts, status histogram, job counts |
| `POST /v1/system/process-reports` | `supervise` — launcher supervision reports, from which the Core Service emits the reserved `system.process.*`/`system.core.restarted` events ([ADR-0042](adr/0042-process-supervision-and-single-instance-locking.md)) |

**Implemented:** `GET /v1/health` landed in Phase 2 WI-1 ([ADR-0049](adr/0049-core-service-skeleton-implementation-decisions.md)) exactly as specified — a `{"status": …}` body and nothing else, answered from a cached readiness flag (no database query, [ADR-0037](adr/0037-core-service-concurrency-and-driver.md)), and carrying the per-source-address liveness rate cap (30 req/s, `429` beyond) the exemption requires ([ADR-0040](adr/0040-health-endpoint-authentication.md)). It is the platform's single `public` route, enforced at app assembly.

`GET /v1/health/detail` and `GET /v1/metrics` (both `monitor`) landed in Phase 2 WI-2 with the [observability.md](observability.md) response shapes:

- `/v1/health/detail` → `{"status", "version", "schema_version", "db_connected", "uptime_seconds"}`. `status` is `"healthy"` when the service is ready and the database answers `SELECT 1`, else `"unhealthy"`; `db_connected` reflects a real query through the connection pool (this endpoint is authenticated, so the O(1)-no-database rule applies only to liveness). `uptime_seconds` counts from readiness, as an integer.
- `/v1/metrics` → `{"requests_total", "requests_by_status", "active_jobs", "db_query_count", "uptime_seconds"}`. Request counts come from ASGI middleware and include every HTTP response (liveness and denials included); `requests_by_status` keys are status-code strings. **`active_jobs` is a constant `0` until the job system lands ([ADR-0012](adr/0012-job-abstraction.md), Phase 4)** — the field ships now so the response shape is stable for monitoring clients. `db_query_count` counts SQL statements executed through the Core Service connection pool since startup.

`POST /v1/system/process-reports` (`supervise`, Phase 6 supervision) is not yet implemented.

### Auth administration
Defined in [ADR-0026](adr/0026-named-scoped-tokens.md) (lifecycle commands, rate limiting) and concretized in [ADR-0051](adr/0051-auth-lifecycle-and-rate-limiting-implementation-decisions.md). Implemented in Phase 2 WI-2b. Every route requires `admin`; the `healthspan token`/`auth`/`mcp` CLI groups are thin REST clients over these endpoints, authenticating with the `cli-admin` token from its keyring entry (the Core Service must be running — there is no direct-database fallback).

| Endpoint | Auth | Behavior |
|---|---|---|
| `GET /v1/tokens` | `admin` | `{"tokens": [{name, scopes, authorship, publish_namespaces, created_utc, last_used_utc, revoked}]}` — metadata only, never values or hashes |
| `POST /v1/tokens` | `admin` | Mint. Body `{name, scopes, publish_namespaces?}`; `201` `{name, scopes, token}` — the plaintext appears in this response only. `409` if the name already exists (a revoked name stays reserved as its revocation record; rotate to reissue), `400` on an invalid name, a reserved name (`invalid`, `mcpclient`), an unknown scope, or a namespace/scope mismatch — validation errors are `400` even when the name also happens to exist |
| `POST /v1/tokens/{name}/revoke` | `admin` | Immediate revocation, no grace overlap; idempotent (`200` on an already-revoked name); `404` unknown name; `409` if `{name}` authenticates the current request, or if it is the last live token carrying `admin` — both lockout guards ([ADR-0051](adr/0051-auth-lifecycle-and-rate-limiting-implementation-decisions.md) §4); rotate instead |
| `POST /v1/tokens/{name}/rotate` | `admin` | Reissue under the same name/scopes as an atomic in-place hash swap; `200` `{name, token, keyring_updated}` — plaintext shown once; updates the `token:<name>` keyring entry when one exists; a revoked name rotates back to live; `404` unknown name |
| `POST /v1/auth/reset-limits` | `admin` | Clear all auth-failure limiter state; `200` `{"reset": true}`. Always reachable — valid admin credentials are never throttled |
| `POST /v1/mcp/rotate-client-secret` | `admin` | Regenerate the MCP client-facing secret: replace the SHA-256 hash in the MCP keyring entry, answer `200` `{secret, restart_required: true}` — plaintext shown once; the MCP Server must restart to pick it up |

### Reference data
Lab sources, biomarker catalog, reference range frameworks. Read-mostly endpoints used by the GUI and MCP tools for lookups and validation.

*Endpoints TBD during implementation.*

---

## MCP Tool Surface

The MCP server translates AI client tool calls into Core REST API requests ([ADR-0006](adr/0006-application-architecture.md), [ADR-0007](adr/0007-mcp-transport.md)). The intended tool set is sketched in [design-rationale.md](design-rationale.md). The exact tool definitions will be finalized during implementation and are extensible via plugins ([ADR-0010](adr/0010-cli-plugin-model.md)).

*Tool definitions TBD during implementation. Every definition, first-party or plugin-provided, must satisfy the output contract below.*

### Tool output contract

The contract is the AI-facing half of the [ADR-0030](adr/0030-biomarker-identity.md) value model: the schema represents censored and qualitative results faithfully, and the tool surface must not lose that fidelity in serialization — otherwise the AI client re-introduces the bugs the schema fixed (architecture review 2026-07-06, item 3.G). Written against the current MCP spec (2025-11-25 revision at time of writing); the tool-annotation and structured-output surfaces it relies on are unchanged in the 2026-07-28 release candidate.

1. **Value fidelity ([ADR-0030](adr/0030-biomarker-identity.md)).** Every lab value renders as a display string that preserves the comparator (`"<0.1"`, never the bare numeric `0.1`); qualitative results render their `value_text`. The UCUM unit ([ADR-0031](adr/0031-units-and-ucum.md)) and the applicable reference range with its framework ([ADR-0005](adr/0005-reference-range-frameworks.md)) accompany every value — the AI client never guesses units or ranges. Tools declare an `outputSchema` and return `structuredContent` carrying the explicit triple (`value_num`, `comparator`, `value_text`) alongside the display string, so a client consuming structured output cannot lose the censoring either. The failure this prevents: a below-detection ApoB read as a measured `0.1` is exactly the wrong number ADR-0030 exists to make unrepresentable.

2. **Tool annotations.** Every tool in the default set declares `readOnlyHint: true`. Any future write-capable tool declares `readOnlyHint: false` plus honest `destructiveHint` and `idempotentHint` values. Annotations are display and gating hints for the client, never a security boundary — the MCP spec itself says clients must not trust them — so a tool's actual capability is bounded by the MCP server's token scopes ([ADR-0026](adr/0026-named-scoped-tokens.md)) regardless of what the annotation claims.

3. **Pagination and row caps on every tool.** Every tool that returns rows takes a cursor and is subject to a server-enforced page cap (default 100 rows, configurable), enforced at the Core REST API — the single enforcement point, so the GUI and CLI inherit the same bound — with the MCP server passing it through. The rationale is dual: context-window hygiene, and exfiltration friction — a prompt-injected client needs many visible, auditable tool calls to dump years of data, never one.

4. **Untrusted free text is shielded.** User-authored or imported free text (clinical-note `body`, intervention notes, event descriptions) is returned inside a clearly delimited data block with an instruction-shielding preamble stating that the block is data from the user's records, not instructions. The delimiter is a per-response random boundary token: a note whose text contains the closing delimiter cannot break out of the block, which a fixed delimiter cannot guarantee against exactly the adversary this rule exists for. This makes [security.md](security.md)'s "must not amplify injected instructions" requirement concrete and testable.

5. **Document originals stay unexposed.** Restating [ADR-0034](adr/0034-clinical-document-storage.md): binary originals are not exposed through MCP tools by default; the queryable surface is the extracted `body` text, which is subject to rules 3 and 4 like any other tool output.

---

## Links
- Related: [ADR-0004](adr/0004-data-ingestion-strategy.md) — import endpoint behavior
- Related: [ADR-0006](adr/0006-application-architecture.md) — Core Service as the API owner
- Related: [ADR-0007](adr/0007-mcp-transport.md) — MCP server as an API client
- Related: [ADR-0011](adr/0011-event-bus.md) — event stream endpoints
- Related: [ADR-0012](adr/0012-job-abstraction.md) — job management endpoints
- Related: [ADR-0015](adr/0015-data-export.md) — export endpoints
- Related: [observability.md](observability.md) — health and metrics endpoints
- Related: [ADR-0030](adr/0030-biomarker-identity.md) — the value model the tool output contract preserves
- Related: [ADR-0034](adr/0034-clinical-document-storage.md) — document originals unexposed by default
- Related: [ADR-0040](adr/0040-health-endpoint-authentication.md) — liveness exemption and the `monitor` scope
- Related: [security.md](security.md) — authentication and input validation requirements
- Related: [design-rationale.md](design-rationale.md) — MCP tool definitions and versioning surfaces

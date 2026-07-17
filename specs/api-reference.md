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
Defined in [ADR-0004](adr/0004-data-ingestion-strategy.md) and concretized in [ADR-0052](adr/0052-bulk-import-identity-and-conflict-resolution.md). Implemented in Phase 2 WI-3; extended with catalog tables and the biomarker-name resolver in Phase 3 WI-2 ([ADR-0054](adr/0054-biomarker-name-alias-fallback.md), [ADR-0055](adr/0055-biomarker-category-taxonomy.md)).

| Endpoint | Auth | Behavior |
|---|---|---|
| `POST /v1/import` | `import` | Validate a per-table batch (all errors collected first), then apply it in one atomic transaction under an explicit conflict policy; `?dry_run=true` validates and reports counts without writing |

**Request body** (`application/json`):

```json
{
  "source": "manual",
  "adapter_id": null,
  "adapter_version": null,
  "note": null,
  "conflict_policy": "reject | skip | upsert",
  "categories":         [ { /* row */ } ],
  "labs":               [ { /* row */ } ],
  "biomarkers":         [ { /* row */ } ],
  "biomarker_aliases":  [ { /* row */ } ],
  "range_frameworks":   [ { /* row */ } ],
  "framework_ranges":   [ { /* row */ } ],
  "lab_draws":          [ { /* row */ } ],
  "lab_results":        [ { /* row */ } ]
}
```

- `conflict_policy` is **required** — there is no implicit default that mutates data ([ADR-0004](adr/0004-data-ingestion-strategy.md)). Unknown top-level keys (a mistyped field or an unregistered table name) are rejected `422`.
- Phase 2 registered exactly two importable tables — `lab_draws` and `lab_results` ([development-plan.md](development-plan.md)). Phase 3 WI-2 adds four reference-data catalog tables — `categories`, `labs`, `biomarkers`, `biomarker_aliases` ([ADR-0054](adr/0054-biomarker-name-alias-fallback.md)/[ADR-0055](adr/0055-biomarker-category-taxonomy.md)) — and WI-3 adds two more, `range_frameworks` and `framework_ranges` ([ADR-0058](adr/0058-range-comparison-implementation-decisions.md) §6), so a reference range is a data addition rather than a migration ([ADR-0005](adr/0005-reference-range-frameworks.md)); the remaining content tables land with their Phase-7 adapters. Each is a list of row objects whose columns are the [data-model.md](data-model.md) columns of that table.
- **Row `id` is a batch-local handle** ([ADR-0052](adr/0052-bulk-import-identity-and-conflict-resolution.md)): it wires intra-batch foreign keys (a `lab_results` row's `lab_draw_id` names a `lab_draws` row's payload `id`) and is never persistent identity — the server assigns real primary keys and rewrites the child FK. A `lab_results` row must reference a `lab_draws` row present in the same batch. `import_batch_id`/`superseded_by` in a payload row are ignored (server-owned); the six catalog tables carry neither column (they are not content rows — no batch provenance, no supersession chain).
- **Conflict identity is a natural key per table**, enforced as a partial-unique index over current rows for the content tables (migration 0003) and a plain unique index for the catalog tables (migrations 0004/0001): `categories` = `(name,)`, `labs` = `(name,)`, `biomarkers` = `(canonical_name,)`, `biomarker_aliases` = `(alias_normalized,)`, `range_frameworks` = `(name,)`, `framework_ranges` = `(framework_id, biomarker_id, effective_date)`, `lab_draws` = `(lab_id, draw_utc)`, `lab_results` = `(lab_draw_id, biomarker_id)`. `framework_ranges.effective_date` is the one **nullable** key column: `null` (or omitted) is the ADR-0005 dateless "always current" default and matches an existing dateless row rather than duplicating it — see [Reference data](#reference-data). External foreign keys (`category_id`, `biomarker_id`, `lab_id`, `framework_id`) must already exist in the catalog — a catalog row created earlier **in the same batch** is not visible to a later table in that batch (every catalog FK is a plain already-stored foreign key, never a batch-local handle; import them in a prior call first).
- **`lab_results` accepts exactly one of `biomarker_id` / `biomarker_name`** (both present or both absent is a collected `422` naming the row). A `biomarker_name` is resolved server-side to a `biomarker_id` **before** the natural-key/conflict handling below runs, so the rest of the pipeline — and the stored row — only ever sees an id ([ADR-0054](adr/0054-biomarker-name-alias-fallback.md) §4). Resolution is exact-match only, over the union of normalized `biomarkers.canonical_name` and stored `biomarker_aliases.alias_normalized` (normalization: NFKC → casefold → trim → collapse internal whitespace); zero or more than one match is a collected validation error naming the unresolved string — no fuzzy matching. Only already-stored catalog rows are searched, per the same-batch visibility rule above. The [ADR-0030](adr/0030-biomarker-identity.md) value model is enforced structurally: a result needs `value_num` or `value_text`, and a `comparator` (`<`, `<=`, `>=`, `>`) requires `value_num`.
- **`biomarker_aliases.alias_normalized` and `created_utc` are server-derived**, never client-supplied ([ADR-0054](adr/0054-biomarker-name-alias-fallback.md) §3): the client sends `biomarker_id`, `alias` (display text), and an optional `source`; the server normalizes `alias` (the same NFKC → casefold → trim → collapse rule) into `alias_normalized` and stamps `created_utc`, silently overwriting any client-supplied value for either. A display name may not appear as both a canonical biomarker name and an alias — an alias whose normalized form equals any biomarker's normalized `canonical_name` (or vice versa), stored or in the same batch, is a collected `422`; this also rejects an alias equal to its own biomarker's exact canonical spelling as redundant.

**Conflict resolution** ([ADR-0052](adr/0052-bulk-import-identity-and-conflict-resolution.md), [ADR-0027](adr/0027-audit-trail-and-corrections.md)) — a *conflict* is an incoming row whose natural key exists and whose compared columns differ:

| Policy | New key | Identical | Differs — `lab_results` (value) | Differs — `lab_draws` / catalog tables (metadata) |
|---|---|---|---|---|
| `reject` | insert | no-op | batch fails `422`, all conflicts listed | batch fails `422` |
| `skip` | insert | `rows_unchanged` | keep existing, `rows_skipped` | keep existing, `rows_skipped` |
| `upsert` | insert | `rows_unchanged` | supersede (insert + chain, per-row `correct` audit), `rows_corrected` | in-place `update` (per-row image audit), `rows_corrected` |

A `lab_draws` match is *reused* so its id stays stable and its results stay attached; only its designated-metadata columns are repaired in place. A `lab_results` value difference supersedes, so no clinical value is lost. The six catalog tables (`categories`, `labs`, `biomarkers`, `biomarker_aliases`, `range_frameworks`, `framework_ranges`) carry no `superseded_by` column at all — they are reference data, not clinical values — so a genuine difference is always an in-place `update`, the same repair path as `lab_draws` metadata, never a supersession. Inserts are audited at batch level (one `import` row per (batch, table), zero per-row insert rows — [ADR-0027](adr/0027-audit-trail-and-corrections.md)); the audit `actor` is the token name.

**Success response** (`200`):

```json
{
  "batch_id": 12,
  "dry_run": false,
  "conflict_policy": "upsert",
  "summary": {
    "lab_draws":   {"rows_inserted": 1, "rows_corrected": 0, "rows_skipped": 0, "rows_unchanged": 0},
    "lab_results": {"rows_inserted": 3, "rows_corrected": 1, "rows_skipped": 0, "rows_unchanged": 2}
  }
}
```

`batch_id` is the `import_batches` provenance id, or `null` on a dry-run. The four per-table counts partition every input row for that table.

**Validation error** (`422`) — the whole batch is rejected before any write; `detail` is the full collected error list, one entry per offending row:

```json
{"detail": [{"table": "lab_results", "row_index": 2, "message": "biomarker_id=99 does not exist in biomarkers"}]}
```

`data.imported`/`data.corrected` event emission is deferred to Phase 4 with the event bus; the `audit_log` rows are the durable record until then.

### Data query and retrieval
The primary read path for the GUI, MCP server, and CLI. Landed in Phase 2 WI-4 ([ADR-0053](adr/0053-read-endpoint-surface-and-pagination.md)): generic list/get over the current-state views ([ADR-0027](adr/0027-audit-trail-and-corrections.md)) for the import-populated tables, plus the catalog tables that make their foreign keys interpretable. All routes require scope `read`. Semantic query endpoints (biomarker history, panel by date — the MCP tool shapes in [design-rationale.md](design-rationale.md)) are deferred to Phase 4; the filters below already express them.

| Endpoint | List filters |
|---|---|
| `GET /v1/lab-draws`, `GET /v1/lab-draws/{id}` | `lab_id`, `draw_from`, `draw_to` |
| `GET /v1/lab-results`, `GET /v1/lab-results/{id}` | `biomarker_id`, `lab_draw_id`, `lab_id`, `draw_from`, `draw_to`, `framework` — see [Range comparison](#range-comparison) below (Phase 3 WI-3, [ADR-0058](adr/0058-range-comparison-implementation-decisions.md)); a **projection, not a filter** |
| `GET /v1/labs`, `GET /v1/labs/{id}` | — |
| `GET /v1/biomarkers`, `GET /v1/biomarkers/{id}` | `category` — a **case-insensitive category-NAME lookup** (Phase 3 WI-2, [ADR-0055](adr/0055-biomarker-category-taxonomy.md) §1; breaking change from the Phase 2 free-text `category` filter). An unknown name answers an empty page, never an error. Biomarker rows carry both the resolved category **name** in `category` (back-compat with the Phase-2 field) and the raw FK in `category_id`. |

See [Reference data](#reference-data) below for `categories`, `range-frameworks`, and `framework-ranges` — the read counterparts to the six catalog import tables (`labs` and `biomarkers` are documented in this section; `biomarker_aliases` has no read endpoint yet).

**Pagination** — every list route takes `limit`, `cursor`, and `order`, and answers `{"items": [...], "next_cursor": <token|null>}`. The cursor is an opaque token; pass it back verbatim (with filters and `order` unchanged) to fetch the next page; `null` means exhaustion. Ordering is fixed per resource: `lab-draws`/`lab-results` by draw time, **newest-first** by default (`order=asc` for chronological walks); `labs`/`biomarkers` by name ascending. Page sizes are bounded by the server-enforced cap (`service.page_cap`, default 100 — the single enforcement point, MCP tool-convention rule 3 below): an omitted `limit` means a full capped page, an oversized `limit` **clamps** to the cap, `limit < 1`, a malformed cursor, or a cursor replayed under the other `order` is a `422`.

`draw_from`/`draw_to` compare lexically against the stored ISO-8601 UTC `draw_utc`, so date-only prefixes (`draw_from=2024-06-01`) work naturally.

**Response rows** mirror the table columns (minus `superseded_by`, `NULL` on every current row by construction). `lab_results` rows additionally carry:

- the explicit value triple `value_num` / `comparator` / `value_text` ([ADR-0030](adr/0030-biomarker-identity.md)), the UCUM `unit` as stored ([ADR-0031](adr/0031-units-and-ucum.md)), and the lab's own `reference_low`/`reference_high`/`reference_text` (framework ranges are Phase 3 reference data)
- `display` — a derived presentation string that preserves the comparator (`"<0.1"`, never a bare `0.1`); clients doing arithmetic use the triple
- `draw_utc` and `lab_id` — read-only draw context embedded from the joined draw, so a biomarker-history page is plottable without per-row draw fetches
- `range_comparison` — **only** when `?framework=` is supplied; see below

#### Range comparison

`GET /v1/lab-results?framework=<name>` (and the same parameter on the get-by-id route) adds a `range_comparison` object to each result row, comparing that result against the named framework's point-in-time target ([ADR-0005](adr/0005-reference-range-frameworks.md), [ADR-0058](adr/0058-range-comparison-implementation-decisions.md)). Landed in Phase 3 WI-3. **Absent the parameter, rows serialize exactly as above** — the enrichment is opt-in and the Phase 2 contract is unchanged.

```json
"range_comparison": {
  "framework": "nih_medlineplus_lipid_targets",
  "flag": "above",
  "range_low": null,
  "range_high": 100.0,
  "unit": "mg/dL",
  "effective_date": null,
  "range_text": null,
  "reason": null
}
```

`range_low`/`range_high`/`unit` are the **normalized** values the comparison actually used — converted to the biomarker's `canonical_unit`, not the raw stored row — so a client sees what was really compared (the same honesty principle as `display`). They are `null` whenever no normalized comparison happened. `framework` echoes the **stored** name, not the caller's casing. `reason` is set only for `error` and `not_comparable`.

`flag` is a closed set:

| flag | meaning |
|---|---|
| `in_range` | the result is provably within the target |
| `below` / `above` | provably outside the target, on that side |
| `indeterminate` | a censored result (`<0.1`) whose interval straddles a target boundary — undecidable, and said so rather than guessed |
| `not_comparable` | the target is `range_text`-only, or the result is qualitative (`value_num` is `null`) |
| `no_range` | the framework has no range row for this biomarker at this result's draw date |
| `error` | the units could not be reconciled; `reason` says why |

Notes clients must know:

- **`framework` is a projection, not a filter.** It changes what each row carries, never which rows are returned or their order, so it plays no part in the cursor: a cursor stays valid whether or not the parameter is present, and may be added or dropped mid-walk.
- **An unknown framework name is a `422`** — deliberately unlike `?category=`'s unknown-name-answers-an-empty-page rule ([ADR-0055](adr/0055-biomarker-category-taxonomy.md) §1). A typo'd category yields an obviously empty page; a typo'd framework would yield a *full page of plausible-looking rows* all flagged `no_range` — a wrong answer that looks right. Matching is case-insensitive. On the get-by-id route the framework resolves first, so a bad name is a `422` even when `{id}` does not exist (never masked into a `404`).
- **`error` is loud, not fatal.** An unreconcilable unit names itself in its own row's `reason`; it never fails the page. It can never be mistaken for a flag, which is what [ADR-0005](adr/0005-reference-range-frameworks.md)'s "fail loudly, never silently flag" requires.
- **Censored and qualitative results are respected, not coerced** ([ADR-0030](adr/0030-biomarker-identity.md)). A `<0.1` is compared as an interval: decidable against some targets (`below` a `≥0.5` target), undecidable against others (`indeterminate`). It is never treated as the number `0.1`.
- **Bounds are inclusive, and coincide within a relative 1e-9.** A result sitting exactly on a bound is `in_range`, and stays `in_range` whichever unit it is reported in — normalization is float arithmetic, so exact equality would make the verdict depend on the reporting unit ([ADR-0058](adr/0058-range-comparison-implementation-decisions.md) §3). The tolerance is far below clinical significance and never affects the censored open/closed distinction above.

**Get-by-id** answers `200` with the row, or `404` for an id that is absent *or superseded* — the current view has no such row. Reads are not audited (`audit_log` records mutations, `auth_audit` records authentication outcomes).

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
Lab sources, biomarker catalog, reference range frameworks. Read-mostly endpoints used by the GUI and MCP tools for lookups and validation. `labs` and `biomarkers` are documented under [Data query and retrieval](#data-query-and-retrieval) above (they existed since Phase 2 WI-4); `categories`, `range-frameworks`, and `framework-ranges` land here in Phase 3 WI-2 ([ADR-0055](adr/0055-biomarker-category-taxonomy.md)) as the read counterparts to the catalog import tables. All routes require scope `read` and share the same keyset pagination, page-cap clamp, and cursor `422` handling as every other list route.

| Endpoint | List filters | Sort |
|---|---|---|
| `GET /v1/categories`, `GET /v1/categories/{id}` | — | `name` asc |
| `GET /v1/range-frameworks`, `GET /v1/range-frameworks/{id}` | — | `name` asc |
| `GET /v1/framework-ranges`, `GET /v1/framework-ranges/{id}` | `framework_id`, `biomarker_id` | `id` asc (no name column) |

`categories` rows mirror the migration 0004 `categories` table columns (`id`, `name`, `description`) — the reserved `not_assigned` row (`id` 0) is included like any other row. `biomarker_aliases` has no read endpoint yet (deferred to WI-4); aliases are import-only for now.

`range_frameworks`/`framework_ranges` **are seeded as of Phase 3 WI-3** (migration 0005, [ADR-0058](adr/0058-range-comparison-implementation-decisions.md) §5) — they answered empty pages under WI-2. Three frameworks ship, each carrying a `description` and a `source_url`: `nih_medlineplus_lipid_targets`, `ada_standards_of_care`, and `aha_cdc_hscrp_risk_strata`. Every seeded range is the dateless default (`effective_date` `null` = "always current", [ADR-0005](adr/0005-reference-range-frameworks.md)). Coverage is **deliberately partial** — only defensibly-sourced ranges are seeded, so a biomarker with no row flags `no_range` rather than carrying a guessed target.

Both tables are ordinary owner-editable catalog data: `range_frameworks` and `framework_ranges` are importable through `POST /v1/import` ([ADR-0058](adr/0058-range-comparison-implementation-decisions.md) §6), so filling a `no_range` gap — or adding a whole framework — is a data addition, never a migration. Rules specific to `framework_ranges`:

- Its natural key is `(framework_id, biomarker_id, effective_date)`. **Omitting `effective_date` (or sending `null`) is the dateless "always current" default** and is the same key either way — re-importing it reconciles the existing row rather than creating a second one. A row *with* a date is a distinct key, so dated and dateless rows coexist: that is [ADR-0005](adr/0005-reference-range-frameworks.md)'s versioning model.
- `effective_date` must be a **date-only `YYYY-MM-DD`**. A timestamp is a `422`: point-in-time resolution compares it lexically against a result's draw date, so `2024-06-01T00:00:00Z` would silently lose its own effective day.
- `unit` is **required** (UCUM string). A numeric range with no unit is the safety bug ADR-0005 exists to close. Its UCUM validity is not currently checked at import — an unparseable unit surfaces later as a per-row `error` flag rather than a wrong number.
- At least one of `range_low` / `range_high` / `range_text` must be present, and `range_low <= range_high`.
- `framework_id` and `biomarker_id` name **already-stored** rows, never same-batch handles ([ADR-0057](adr/0057-reference-data-and-catalog-import-implementation-decisions.md) §9): import a framework, then its ranges.

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
- Related: [ADR-0052](adr/0052-bulk-import-identity-and-conflict-resolution.md) — import identity, natural keys, conflict resolution
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

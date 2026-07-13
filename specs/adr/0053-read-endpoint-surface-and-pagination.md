# ADR-0053: Read-Endpoint Surface, Keyset Pagination, and Value-Fidelity Serialization (Phase 2 WI-4)

## Status
Proposed

## Context and Problem Statement

Phase 2 WI-4 implements the read/query endpoints — the Phase 2 milestone's other half: an authenticated client that wrote data through `POST /v1/import` reads it back through the REST API. The specs fix the constraints — reads consume the `*_current` views by name ([ADR-0027](0027-audit-trail-and-corrections.md)), every row-returning surface takes a cursor and is subject to a server-enforced page cap enforced once at the Core REST API ([api-reference.md](../api-reference.md), MCP tool-convention rule 3), the value model's comparator and qualitative text must survive serialization ([ADR-0030](0030-biomarker-identity.md)), units are the stored UCUM strings ([ADR-0031](0031-units-and-ucum.md)) — but leave the concrete surface open: which resources, what endpoint shapes, how the cursor works, and what a serialized result row carries.

Four owner decisions (2026-07-12, at WI-4 kickoff) fix these.

## Decision Drivers

- ADR-0027 is binding: readers see current state; superseded rows are reachable only through history surfaces (later phases)
- api-reference.md's MCP tool-convention rule 3 is binding: a single page-cap enforcement point at Core REST, inherited by GUI/CLI/MCP alike — context-window hygiene and exfiltration friction
- A result row's meaning must not degrade: a censored `<0.1` serialized as a bare `0.1` is a clinical falsehood (ADR-0030)
- A client holding only `lab_results` rows cannot interpret them: `biomarker_id`/`lab_id` are integers into catalog tables
- The future MCP tools (`get_biomarker_history`, `get_panel_by_date`, `get_labs` — design-rationale.md) must be expressible over this surface without schema changes

## Decision Outcome

### 1. Surface: generic list/get over four resources, catalog included

Eight `read`-scoped GET routes — a list and a get-by-id for each of:

| Resource | Backing relation | List filters |
|---|---|---|
| `/v1/lab-draws` | `lab_draws_current` | `lab_id`, `draw_from`, `draw_to` |
| `/v1/lab-results` | `lab_results_current` ⋈ `lab_draws` | `biomarker_id`, `lab_draw_id`, `lab_id`, `draw_from`, `draw_to` |
| `/v1/labs` | `labs` | — |
| `/v1/biomarkers` | `biomarkers` | `category` |

The two catalog tables ship now because the data rows are uninterpretable without them (`get_labs` is also an already-sketched MCP tool). **No semantic endpoints this phase** — biomarker history is `GET /v1/lab-results?biomarker_id=N`, a panel by date is `?lab_draw_id=N` (or a draw-date window); the MCP server defines its real tool contracts against these in Phase 4. `draw_from`/`draw_to` compare lexically against the stored ISO-8601 UTC `draw_utc`, so date-only prefixes (`2024-06-01`) work naturally.

Get-by-id answers `404` for an id that is absent *or superseded* — the current view simply has no such row; distinguishing the two (or following a supersession chain forward) is a history surface for a later phase.

### 2. Pagination: opaque keyset cursor, fixed per-resource order

List responses are `{"items": [...], "next_cursor": <token|null>}`. The cursor is an opaque base64url token encoding a schema version, the direction it was minted under, and the last row's `(sort key, id)` keyset — resumable and stable under concurrent inserts, with none of OFFSET's drift or rescan cost. Ordering is **fixed per resource**: clinical rows by `draw_utc` with `id` as tiebreak, **newest-first by default** (`?order=asc` flips to chronological, for export-style walks); catalog rows by name, ascending. No arbitrary sort parameters this phase — a deterministic total order is what makes keyset pagination correct, and every added sort key is a new index commitment.

A cursor replayed under the other `order`, or malformed in any way, is a `422`. Filters are not embedded in the cursor: the client keeps its filters constant while paginating (documented in [api-reference.md](../api-reference.md)); changing them mid-walk yields well-formed but unhelpful pages, not an error.

### 3. Page cap: `service.page_cap`, clamp not error

The server-enforced page bound is **`service.page_cap`, default 100 rows** (`[service]` config, [ADR-0049](0049-core-service-skeleton-implementation-decisions.md) pattern; `>= 1` enforced at parse). A request without `limit` gets a full capped page; a `limit` above the cap **clamps** to it rather than erroring — the cap is a bound, not a contract negotiation, and clamping keeps clients working when an operator lowers it (the response's item count and `next_cursor` keep pagination honest). `limit < 1` is a `422`. This is the single enforcement point of api-reference.md's MCP tool-convention rule 3: GUI, CLI, and the Phase 4 MCP passthrough all inherit it.

### 4. Value-fidelity serialization

A serialized `lab_results` row carries the explicit ADR-0030 triple — `value_num`, `comparator`, `value_text` — plus the UCUM `unit` as stored (ADR-0031), the lab's own `reference_low`/`reference_high`/`reference_text` (framework ranges are Phase 3 reference data), and a derived **`display`** string that preserves the comparator: `<0.1` renders as `"<0.1"`, never a bare `0.1`. `display` is presentation convenience only — clients doing arithmetic use the triple. Numeric wins when both numeric and text forms are present (both still travel); integral magnitudes render without a trailing `.0`.

### 5. Embedded draw context on result rows

Each `lab_results` row embeds read-only `draw_utc` and `lab_id` from its draw (joined on the base table — a result's FK names a real draw row regardless of currency; draws never supersede, [ADR-0052](0052-bulk-import-identity-and-conflict-resolution.md) §3). A biomarker-history page is directly plottable without N+1 draw fetches. These are derived-from-draw fields, not `lab_results` columns; the draw remains the authority.

`superseded_by` is omitted from serialized rows — it is `NULL` by construction on every current-view row.

### 6. Reads are not audited

`audit_log` records data mutations (ADR-0027); `auth_audit` records authentication outcomes ([ADR-0050](0050-token-store-and-auth-implementation-decisions.md)). A read is neither. Per-request observability comes from the structured request log and `/v1/metrics`.

### Positive Consequences

- The Phase 2 milestone closes: write via `/v1/import`, read back via four coherent resources, all under the token scopes
- Keyset pagination is stable under writes and O(page) per request; the partial indexes over current rows (0001/0003) serve the order and the common filters
- The comparator can never silently vanish from a reading a client displays
- The MCP tools of Phase 4 map 1:1 onto filters that already exist

### Negative Consequences / Tradeoffs

- Fixed ordering is a commitment: a client wanting results ordered by value or biomarker name sorts client-side within the cap, until a future ADR adds sort keys
- Clamping (rather than rejecting) an oversized `limit` means a client cannot detect the cap from a `4xx`; it must observe page sizes — accepted, the cap is operator policy, not client contract
- Embedded `draw_utc`/`lab_id` denormalize the wire format; if a draw's metadata is repaired in place, previously fetched result pages are stale — inherent to any snapshot read

## Consequences for Other Documents

- **[api-reference.md](../api-reference.md)**: Data query and retrieval — endpoints, filters, pagination contract, response shapes, status codes, scope — same PR
- **[ADR-0049](0049-core-service-skeleton-implementation-decisions.md)** (Proposed): navigation link — `[service]` gains `page_cap`
- **[data-model.md](../data-model.md)**: no schema change; no update
- **[open-questions.md](../open-questions.md)**: no new deferral

## Links

- Implements: [ADR-0027](0027-audit-trail-and-corrections.md) — readers consume `*_current` views
- Implements: [ADR-0030](0030-biomarker-identity.md) / [ADR-0031](0031-units-and-ucum.md) — value-model and unit fidelity on the wire
- Related: [ADR-0026](0026-named-scoped-tokens.md) — `read` scope
- Related: [ADR-0037](0037-core-service-concurrency-and-driver.md) — sync handlers on the thread-affine pool
- Related: [ADR-0052](0052-bulk-import-identity-and-conflict-resolution.md) — draws as stable identity rows (the join target)
- Related: [ADR-0049](0049-core-service-skeleton-implementation-decisions.md) — the `[service]` config section this extends; the WI-decisions-bundle pattern
- Related: [api-reference.md](../api-reference.md) — the MCP tool-convention page-cap rule (rule 3) this ADR realizes
- Related: [design-rationale.md](../design-rationale.md) — the MCP tool sketch this surface must support

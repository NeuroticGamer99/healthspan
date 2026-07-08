# API Reference

Design-time specification of the Core REST API surface. This document consolidates the API endpoints defined across ADRs and design documents into a single reference. It does not replace the auto-generated OpenAPI documentation that FastAPI will produce at runtime — it captures the intended design before implementation begins.

---

## Conventions

**Base URL:** All endpoints are prefixed with `/v1/`. The version prefix is applied from the first endpoint (see [design-rationale.md](design-rationale.md), versioning surfaces).

**Authentication:** Every endpoint requires a valid `Authorization: Bearer <token>` header and declares a required scope ([ADR-0026](adr/0026-named-scoped-tokens.md)), with one exemption: the liveness endpoint `GET /v1/health` is unauthenticated and returns only a status word, so the launcher and container healthchecks can poll readiness without a credential ([ADR-0040](adr/0040-health-endpoint-authentication.md)). Detailed health and metrics are authenticated (`monitor` scope). See [security.md](security.md).

**Content type:** `application/json` for request and response bodies unless otherwise noted.

**Error responses:** Errors return a JSON body with a structured error object. Error responses must not echo back sensitive input values. See [security.md](security.md).

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

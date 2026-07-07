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

### Reference data
Lab sources, biomarker catalog, reference range frameworks. Read-mostly endpoints used by the GUI and MCP tools for lookups and validation.

*Endpoints TBD during implementation.*

---

## MCP Tool Surface

The MCP server translates AI client tool calls into Core REST API requests ([ADR-0006](adr/0006-application-architecture.md), [ADR-0007](adr/0007-mcp-transport.md)). The intended tool set is sketched in [design-rationale.md](design-rationale.md). The exact tool definitions will be finalized during implementation and are extensible via plugins ([ADR-0010](adr/0010-cli-plugin-model.md)).

*Tool definitions TBD during implementation.*

---

## Links
- Related: [ADR-0004](adr/0004-data-ingestion-strategy.md) — import endpoint behavior
- Related: [ADR-0006](adr/0006-application-architecture.md) — Core Service as the API owner
- Related: [ADR-0007](adr/0007-mcp-transport.md) — MCP server as an API client
- Related: [ADR-0011](adr/0011-event-bus.md) — event stream endpoints
- Related: [ADR-0012](adr/0012-job-abstraction.md) — job management endpoints
- Related: [ADR-0015](adr/0015-data-export.md) — export endpoints
- Related: [observability.md](observability.md) — health and metrics endpoints
- Related: [ADR-0040](adr/0040-health-endpoint-authentication.md) — liveness exemption and the `monitor` scope
- Related: [security.md](security.md) — authentication and input validation requirements
- Related: [design-rationale.md](design-rationale.md) — MCP tool definitions and versioning surfaces

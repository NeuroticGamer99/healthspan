# Observability

Standards for health endpoints, structured logging, and metrics across all platform processes. Observability is built in from the start — it costs almost nothing to add early and saves significant debugging time.

---

## Health Endpoints

Every HTTP process exposes a health endpoint. The process launcher uses these to verify that a process is ready before starting dependent processes.

### Core Service
```
GET /v1/health
```
Response:
```json
{
  "status": "healthy",
  "version": "1.0.0",
  "schema_version": 5,
  "db_connected": true,
  "uptime_seconds": 3600
}
```
Returns `200` when healthy, `503` when not ready (e.g. database unreachable). There is no "migration pending" state: migrations run in the launcher before the Core Service starts, and a Core Service that finds an unexpected `schema_version` at startup logs `CRITICAL` and exits rather than serving ([ADR-0039](adr/0039-startup-sequence-and-passphrase-handoff.md)).

### MCP Server
```
GET /health
```
Returns `200` when ready to accept AI client connections.

### Automation Host, other processes
Same pattern: `GET /health` returning status and process-specific readiness indicators.

---

## Structured Logging

All processes emit JSON-structured logs. Plain-text logs are not acceptable — structured logs are parseable by log aggregators and grep-able by field.

### Required fields on every log entry

```json
{
  "timestamp": "2026-03-21T14:30:00.000Z",
  "level": "INFO",
  "process": "core_service",
  "message": "Request completed",
  "request_id": "uuid"
}
```

### Log levels

| Level | Use |
|---|---|
| `DEBUG` | Detailed trace information; request/response metadata; disabled in production by default |
| `INFO` | Normal operation: requests, job completions, plugin loads, startup/shutdown |
| `WARNING` | Recoverable issues: deprecated plugin API version, slow query, config fallback |
| `ERROR` | Operation failed but process continues: import validation failure, plugin load error |
| `CRITICAL` | Process cannot continue: database unreachable, config missing required key |

### Health data in logs

Log entries must never contain health data values (biomarker results, clinical notes, medication details). Permitted in logs: timestamps, endpoint names, HTTP status codes, biomarker names (not values), record counts, error types, plugin names, job IDs.

This is a hard requirement, not a best-effort guideline. See [security.md](security.md).

### Log output

Default: structured JSON to stdout. Each process handles its own logging. The launcher prefixes each line with the process name when displaying combined output. Log rotation and aggregation are deployment concerns handled outside the application (OS log facilities, Docker logging drivers).

---

## Metrics

Basic request metrics are exposed by the Core Service for debugging and monitoring. Detailed metrics infrastructure is a future concern; the following are available from day one at negligible cost via FastAPI middleware.

```
GET /v1/metrics
```

Returns:
```json
{
  "requests_total": 1204,
  "requests_by_status": { "200": 1180, "400": 18, "500": 6 },
  "active_jobs": 2,
  "db_query_count": 4820,
  "uptime_seconds": 3600
}
```

No external metrics infrastructure (Prometheus, Grafana) is required or expected for personal use. The endpoint exists for ad hoc inspection and for future integration.

---

## Request Tracing

Every HTTP request to the Core Service is assigned a `request_id` (UUID) on receipt. This ID is:
- Included in the response header (`X-Request-ID`)
- Included in all log entries associated with the request
- Passed as a header to any downstream calls the Core Service makes (e.g. to event bus, to plugin services)

Enables correlating log entries across a request/response lifecycle without a full distributed tracing system.

---

## Process Startup Sequence and Readiness

The launcher polls each process's health endpoint after starting it, with a configurable timeout and retry count. A process that does not become healthy within the timeout is considered failed; the launcher logs the error and stops.

Startup order (enforced by launcher):
0. Launcher pre-step ([ADR-0039](adr/0039-startup-sequence-and-passphrase-handoff.md)): collect passphrase, derive key, run any pending migrations with exclusive database access, close the connection and discard the key
1. Core Service (receives the passphrase over stdin, derives its own key, verifies `schema_version`, refuses to start on mismatch)
2. MCP Server (depends on Core Service being healthy)
3. GUI (depends on Core Service being healthy)
4. Automation Host (depends on Core Service being healthy)

This order is determined by health endpoint readiness, not fixed sleep intervals.

---

## Links
- Related: [ADR-0008](adr/0008-process-lifecycle.md) — process lifecycle and launcher behavior
- Related: [ADR-0039](adr/0039-startup-sequence-and-passphrase-handoff.md) — startup sequence: launcher-owned migrations, passphrase handoff, Core Service schema check
- Related: [ADR-0025](adr/0025-plugin-host-process-matrix.md) — the Automation Host, the fourth supervised process; there is no Import Pipeline daemon (imports run as jobs, [ADR-0012](adr/0012-job-abstraction.md))
- Related: [security.md](security.md) — health data logging prohibition

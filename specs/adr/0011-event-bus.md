# ADR-0011: Event Bus and Transport Adapters

## Status
Proposed

## Context and Problem Statement
The REST API handles request-response well but has no mechanism for the Core Service to push notifications to connected clients, for background plugins to signal completion, or for external systems to push events into the platform. A background sync that finishes, an out-of-range alert that fires, or an external device publishing a reading — none of these fit the request-response model. What is the event communication architecture?

## Decision Drivers
- The GUI must update when background operations complete without polling
- Background plugins (scheduled sync, analysis) need to signal state changes to other components
- External event sources (MQTT devices, webhooks) should be able to push events into the platform
- The event system should not require a separate broker process by default (no Redis, no RabbitMQ dependency)
- Transport should be swappable — SSE for HTTP clients today, ZeroMQ or WebSocket later if needed
- Consistent with the micro-kernel principle: transport adapters implement the plugin interface — as internal components compiled into the platform, not loadable plugins (see ADR-0025)

## Considered Options
- Polling — clients poll REST API endpoints for changes
- SSE event stream — Core Service exposes a persistent `GET /v1/events` HTTP stream
- ZeroMQ — socket-level pub/sub, no broker required
- Dedicated broker (Redis Pub/Sub, RabbitMQ) — separate broker process

## Decision Outcome
Chosen option: **Internal event bus with pluggable transport adapters; SSE as the default adapter**

The Core Service hosts an internal asyncio-based event bus. Transport adapters bridge the internal bus to external protocols. SSE ships as the default adapter. ZeroMQ and MQTT are available as optional adapters, enabled in config. Adapters are **internal components** (ADR-0025): they implement the plugin interface but are never loaded from the plugins directory, because they execute inside the Core Service process. The internal event API is uniform regardless of which adapters are active.

### Positive Consequences
- No additional infrastructure dependency by default — the event bus runs inside the Core Service process
- The Automation Host and GUI hold persistent SSE subscriptions — a standard HTTP connection, no special client library required; the MCP server and CLI consume the REST and event API without maintaining a standing subscription (see Event API)
- Transport adapters sit behind a uniform internal interface — MQTT and ZeroMQ are opt-in, not forced
- Qt integration is clean: one background thread subscribes to the SSE stream and emits Qt signals on the GUI main thread, with no Qt dependency in the Core Service
- External event sources (MQTT devices, webhooks) feed through inbound adapters into the same bus

### Negative Consequences / Tradeoffs
- SSE is server-push only (one direction) — bidirectional use cases require WebSocket (see ADR-0014)
- Event bus is in-process — if the Core Service crashes, buffered events are lost (acceptable for personal-use scale; persistent event log is a future option)

## Architecture

```
External MQTT device  → MQTT inbound adapter  ─┐
Webhook / HTTP POST   → HTTP inbound adapter   ─┤
                                                 ├→ Internal Event Bus
Internal component    → bus publish            ─┘      │
(scheduler, jobs)                                        ├→ SSE outbound adapter  → Automation Host / GUI (persistent subscribers)
                                                         ├→ ZeroMQ outbound adapter → external processes
                                                         └→ MQTT outbound adapter → external subscribers
```

## Transport Adapters

**Inbound adapters** translate external events into internal bus events:
- `http_webhook` — `POST /v1/events/inbound` accepts JSON event payloads from external sources; callers authenticate with the `events`-scoped webhook token, which grants nothing else and may publish only `external.*` events (ADR-0026)
- `mqtt_inbound` — subscribes to configured MQTT topics; each message becomes an internal event
- More can be added at build time — adapters are internal components, not loadable plugins (ADR-0025)

**Outbound adapters** broadcast internal events to external subscribers:
- `sse` (default) — `GET /v1/events` — persistent HTTP stream; clients subscribe and receive a continuous text/event-stream
- `zeromq_pub` (optional) — ZeroMQ PUB socket; external processes subscribe via ZeroMQ SUB
- `mqtt_outbound` (optional) — publishes internal events to configured MQTT topics

Adapters are configured in TOML. Inactive adapters consume no resources.

**Inbound publication caps** (review item 2.5): `POST /v1/events/inbound` enforces a per-event payload size cap (default 64 KiB → `413`) and a per-token sustained rate cap (default 60 events/min, burst 120 → `429`), both configurable. Rejections are audited (ADR-0026's `auth_audit` outcomes). The caps apply to every inbound publisher, including the `automation-host` token — first-party residency does not exempt a plugin-driven publish path from flood limits.

## Delivery Guarantees and Event Replay

In-process subscribers cannot miss events; SSE subscribers can — a dropped connection or a restart creates a gap. For the GUI this is cosmetic. For the Automation Host it is a correctness problem: an automation must not silently miss its trigger (ADR-0025). The SSE adapter therefore provides bounded replay:

- Every event carries a monotonically increasing sequence ID (per Core Service run), sent as the SSE `id:` field
- The Core Service retains a bounded replay window of recent events (configurable; default 10,000 events or 24 hours, whichever is smaller). The window is held in memory — events are not written to the database
- The window is **partitioned by origin** (review item 2.5): reserved-namespace events emitted by the Core Service (`data.*`, `job.*`, `schema.*`, `plugin.*`, `system.*`, `schedule.*`) and inbound-published events (`alert.*`, `sync.*`, `external.*`) are retained in separate partitions — default split 7,500 / 2,500 of the window, each also bounded by the age limit, both configurable. Sequence IDs remain globally monotonic; replay merges the partitions in ID order. The consequence is the point: an `events`-scoped caller flooding `external.*` can evict only other inbound events, never `data.*`/`job.*` — a reconnecting Automation Host always receives its retained platform triggers, which is what keeps ADR-0025's "brief outages do not lose triggers" claim true under flood
- On reconnect, a client sends the standard SSE `Last-Event-ID` header; the adapter replays retained events after that ID, then resumes live streaming
- If the requested ID has already aged out of a partition, the stream begins with an explicit `gap` marker event naming which partition(s) were lossy, so the subscriber knows delivery was lossy and can reconcile
- **Gap reconciliation is a requirement, not an aside:** on receiving a `gap` marker, the Automation Host must reconcile before resuming normal trigger processing — re-query recent imports and job states via REST — a requirement levied on ADR-0025's subscriber contract
- The Automation Host persists its last-processed event ID across restarts (ADR-0025)

Events are best-effort beyond the replay window. A persistent event log remains a future option if real usage shows the window is insufficient.

## Event API (via PluginContext)

No plugin code runs inside the Core Service (ADR-0025), so no plugin touches the in-process bus directly. `context.events` presents the same API in every host process; the implementation behind it differs by host:

- **Core Service internal components** (scheduler, job orchestrator, adapters): direct in-process bus access
- **Automation Host plugins**: `publish` POSTs to `/v1/events/inbound`, bounded by the host token's publish-namespace allowlist (`alert.*`, `sync.*`, `external.*` — ADR-0026); `subscribe` is backed by the host's SSE connection with replay
- **CLI and MCP Server plugins**: `publish` requires the plugin-tier credential to carry `events` scope — under the default token set it does not (ADR-0026), so publication from directory-loaded plugins fails closed. The CLI *process* credential `cli-admin` does carry `events` (ADR-0026/ADR-0040), so first-party CLI commands can publish. Either way `subscribe` is unavailable to both hosts — persistent event subscription belongs to the Automation Host (and the GUI, cosmetically)

```python
# Publishing (uniform in every host)
context.events.publish("sync.complete", {"lab": "quest", "count": 42})
context.events.publish("alert.triggered", {"biomarker": "insulin", "value": 18.4})

# Subscribing (Automation Host plugins and Core Service internal components)
context.events.subscribe("data.imported", my_handler)
context.events.subscribe("alert.*", my_wildcard_handler)  # namespace wildcards

# Unsubscribing
context.events.unsubscribe("data.imported", my_handler)
```

The GUI subscribes to the SSE stream directly (see Qt Integration Pattern) — it is not a plugin host.

## Event Schema

Every event has a consistent envelope:

```json
{
  "id": "uuid",
  "type": "sync.complete",
  "timestamp": "2026-03-21T14:30:00Z",
  "source": "automation-host",
  "payload": { ... }
}
```

Event types use dot-notation namespaces (consistent with service names in ADR-0010).

**Provenance is stamped, not claimed.** The `source` field is set by the platform, never by the publisher: the Core Service stamps inbound events with the authenticated token's name and rejects payloads that attempt to supply `source`; internal components' events are stamped by the bus itself. Subscribers may treat `source` as trustworthy data (ADR-0026).

## Initial Event Type Catalog

| Namespace | Events | Emitted by |
|---|---|---|
| `data.*` | `data.imported`, `data.corrected`, `data.deleted` | Core Service, on validated mutations — **reserved** |
| `job.*` | `job.queued`, `job.started`, `job.progress`, `job.complete`, `job.failed` | Core Service job orchestrator; children report via `POST /v1/jobs/{id}/progress` — **reserved** |
| `alert.*` | `alert.triggered`, `alert.resolved` | Core internals and the Automation Host |
| `sync.*` | `sync.started`, `sync.complete`, `sync.failed` | Automation Host (sync/poller plugins) |
| `schema.*` | `schema.migrated` | Core Service migration path — **reserved** |
| `plugin.*` | `plugin.loaded`, `plugin.failed` | Core Service, on host loader status reports — **reserved** |
| `system.*` | `system.started`, `system.stopping`, `system.process.restarted`, `system.process.failed`, `system.process.recovered`, `system.core.restarted` | Core Service — **reserved**; the supervision members on launcher reports via `POST /v1/system/process-reports` (ADR-0042) |
| `schedule.*` | `schedule.interval`, `schedule.cron` | Core Service scheduler — **reserved** |
| `external.*` | externally sourced events | Inbound adapters (webhook, MQTT); source-stamped per token |

**Reserved** namespaces are statements of platform fact and are never accepted through the generic `/v1/events/inbound` endpoint, for any token — the facts they represent enter only through purpose-built, validated REST endpoints, from which the Core Service emits the event itself (ADR-0026). Non-reserved publication is bounded by each token's publish-namespace allowlist.

## Scheduled and Cron Triggers

The event bus is reactive — events fire when something changes. But health data workflows also need time-based triggers: "every Monday, generate a weekly CGM summary"; "every 6 hours, poll Dexcom API for new readings"; "on the 1st of each month, check for overdue lab orders."

A **scheduler component** inside the Core Service emits time-based events onto the bus, making scheduled triggers look like any other event to automation plugins (ADR-0016) and subscribers.

### Event types

**Interval events** fire at a fixed period:

```json
{
  "type": "schedule.interval",
  "payload": { "name": "dexcom_poll", "interval_seconds": 21600 }
}
```

**Cron events** fire on a cron-style schedule:

```json
{
  "type": "schedule.cron",
  "payload": { "name": "weekly_cgm_summary", "cron": "0 8 * * MON" }
}
```

### Configuration

Schedules are declared in the shared TOML config:

```toml
[[schedule]]
name = "dexcom_poll"
type = "interval"
interval = "6h"

[[schedule]]
name = "weekly_cgm_summary"
type = "cron"
cron = "0 8 * * MON"
```

Automation plugins can also register schedules programmatically via the Core REST API (they run in the Automation Host — ADR-0025).

### Design constraints

- The scheduler runs inside the Core Service process — no external cron daemon required
- Missed triggers (Core Service was stopped) are not retroactively fired; the next scheduled time applies
- Schedule names are unique; duplicate names in config are rejected at startup
- The scheduler is an internal component: it implements the plugin interface (micro-kernel principle) but is not loadable from the plugins directory, because it runs inside the Core Service process (ADR-0025)

## Qt Integration Pattern

The GUI process subscribes to the SSE stream in a background thread. Incoming events are converted to Qt signals on the main thread:

```python
class EventStreamWorker(QThread):
    event_received = Signal(dict)

    def run(self):
        for event in stream_sse(f"{base_url}/v1/events"):
            self.event_received.emit(event)
```

The Core Service has no knowledge of Qt. The conversion is entirely inside the GUI process.

## Pros and Cons of the Options

### Polling
- Pro: No event infrastructure required
- Con: GUI responsiveness is latency-bound; wasteful for a desktop application

### SSE event stream (chosen as default transport)
- Pro: Standard HTTP, works with any HTTP client, no special library
- Pro: FastAPI supports SSE natively
- Con: Server-push only — no client-to-server messages over the event channel

### ZeroMQ
- Pro: Bidirectional, high-performance, no broker required
- Pro: Supports complex routing patterns (pub/sub, push/pull, dealer/router)
- Con: Non-HTTP — requires ZeroMQ client library in every consumer
- Con: Not suitable for browser-based clients

### Dedicated broker (Redis, RabbitMQ)
- Pro: Persistence, replay, fan-out at scale
- Con: Additional process dependency — contradicts local-first, zero-config goal

## Links
- Constrained by: [ADR-0025](0025-plugin-host-process-matrix.md) — no plugin code in the Core Service; adapters and scheduler are internal components; replay requirements come from the Automation Host
- Related: [ADR-0026](0026-named-scoped-tokens.md) — webhook and subscriber token scopes
- Related: [ADR-0006](0006-application-architecture.md) — process architecture
- Related: [ADR-0010](0010-cli-plugin-model.md) — plugin type system; PluginContext events API
- Related: [ADR-0012](0012-job-abstraction.md) — job events flow through this bus
- Related: [ADR-0014](0014-websocket.md) — bidirectional extension to this architecture
- Related: [ADR-0027](0027-audit-trail-and-corrections.md) — `data.imported`/`data.corrected`/`data.deleted` are emitted by Core on validated mutations and drive aggregate invalidation
- Related: [ADR-0042](0042-process-supervision-and-single-instance-locking.md) — the `system.process.*`/`system.core.restarted` catalog members are Core-emitted from the launcher's supervision reports (review 2026-07-07 item 1.A)

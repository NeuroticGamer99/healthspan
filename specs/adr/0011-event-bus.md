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
- The Automation Host, GUI, MCP server, and CLI subscribe via the SSE stream — a standard HTTP connection, no special client library required
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
(scheduler, jobs)                                        ├→ SSE outbound adapter  → Automation Host / GUI / MCP server / CLI
                                                         ├→ ZeroMQ outbound adapter → external processes
                                                         └→ MQTT outbound adapter → external subscribers
```

## Transport Adapters

**Inbound adapters** translate external events into internal bus events:
- `http_webhook` — `POST /v1/events/inbound` accepts JSON event payloads from external sources
- `mqtt_inbound` — subscribes to configured MQTT topics; each message becomes an internal event
- More can be added at build time — adapters are internal components, not loadable plugins (ADR-0025)

**Outbound adapters** broadcast internal events to external subscribers:
- `sse` (default) — `GET /v1/events` — persistent HTTP stream; clients subscribe and receive a continuous text/event-stream
- `zeromq_pub` (optional) — ZeroMQ PUB socket; external processes subscribe via ZeroMQ SUB
- `mqtt_outbound` (optional) — publishes internal events to configured MQTT topics

Adapters are configured in TOML. Inactive adapters consume no resources.

## Delivery Guarantees and Event Replay

In-process subscribers cannot miss events; SSE subscribers can — a dropped connection or a restart creates a gap. For the GUI this is cosmetic. For the Automation Host it is a correctness problem: an automation must not silently miss its trigger (ADR-0025). The SSE adapter therefore provides bounded replay:

- Every event carries a monotonically increasing sequence ID (per Core Service run), sent as the SSE `id:` field
- The Core Service retains a bounded replay window of recent events (configurable; default 10,000 events or 24 hours, whichever is smaller). The window is held in memory — events are not written to the database
- On reconnect, a client sends the standard SSE `Last-Event-ID` header; the adapter replays retained events after that ID, then resumes live streaming
- If the requested ID has already aged out of the window, the stream begins with an explicit `gap` marker event, so the subscriber knows delivery was lossy and can run reconciliation (e.g. re-query recent data via REST)
- The Automation Host persists its last-processed event ID across restarts (ADR-0025)

Events are best-effort beyond the replay window. A persistent event log remains a future option if real usage shows the window is insufficient.

## Event API (via PluginContext)

No plugin code runs inside the Core Service (ADR-0025), so no plugin touches the in-process bus directly. `context.events` presents the same API in every host process; the implementation behind it differs by host:

- **Core Service internal components** (scheduler, job orchestrator, adapters): direct in-process bus access
- **Automation Host plugins**: `publish` POSTs to `/v1/events/inbound`; `subscribe` is backed by the host's SSE connection with replay
- **CLI and MCP Server plugins**: `publish` POSTs to `/v1/events/inbound`; `subscribe` is unavailable — persistent event subscription belongs to the Automation Host

```python
# Publishing (uniform in every host)
context.events.publish("data.imported", {"source": "quest", "count": 42})
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
  "type": "data.imported",
  "timestamp": "2026-03-21T14:30:00Z",
  "source": "plugin:quest_importer",
  "payload": { ... }
}
```

Event types use dot-notation namespaces (consistent with service names in ADR-0010).

## Initial Event Type Catalog

| Namespace | Events |
|---|---|
| `data.*` | `data.imported`, `data.corrected`, `data.deleted` |
| `job.*` | `job.queued`, `job.started`, `job.progress`, `job.complete`, `job.failed` |
| `alert.*` | `alert.triggered`, `alert.resolved` |
| `sync.*` | `sync.started`, `sync.complete`, `sync.failed` |
| `schema.*` | `schema.migrated` |
| `plugin.*` | `plugin.loaded`, `plugin.failed` |
| `system.*` | `system.started`, `system.stopping` |
| `schedule.*` | `schedule.interval`, `schedule.cron` |

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
- Related: [ADR-0006](0006-application-architecture.md) — process architecture
- Related: [ADR-0010](0010-cli-plugin-model.md) — plugin type system; PluginContext events API
- Related: [ADR-0012](0012-job-abstraction.md) — job events flow through this bus
- Related: [ADR-0014](0014-websocket.md) — bidirectional extension to this architecture

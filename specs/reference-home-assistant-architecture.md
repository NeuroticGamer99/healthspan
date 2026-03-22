# Home Assistant Architecture Reference

> Research compiled 2026-03-22. Sources linked at bottom.

---

## 1. Core Architecture

Home Assistant (HA) is a **single-process Python monolith** built on `asyncio`. The entire core runs in one Python process with a single-threaded event loop. There is no process isolation between integrations -- they all share the same address space and event loop.

The core consists of four foundational components:

| Component | Role |
|-----------|------|
| **Event Bus** | The central nervous system. Facilitates firing and listening for events. |
| **State Machine** | Tracks entity states. Fires `state_changed` events when state mutates. |
| **Service Registry** | Listens for `call_service` events. Allows integrations to register callable service actions. |
| **Timer** | Fires `time_changed` events every 1 second on the event bus. |

Everything else -- integrations, automations, the frontend, the recorder -- is built on top of these four primitives.

### Layered System Architecture

The full HA stack has distinct layers:

- **Home Assistant OS** -- Minimal Linux distribution (buildroot-based) that provides the bare metal environment
- **Supervisor** -- Manages the OS layer, orchestrates Docker containers for add-ons, handles updates/backups
- **Core** -- The Python monolith described above; interacts with users, the Supervisor, and IoT devices/services
- **Frontend** -- A Polymer/Lit web application served by Core over HTTP/WebSocket

The Supervisor and Core communicate over a local REST API. Add-ons (Docker containers managed by Supervisor) communicate with Core through their associated integration, typically over localhost network connections (e.g., MQTT, HTTP).

---

## 2. Event Bus

### Transport Mechanism

The event bus is **entirely in-process**. It is a Python object (`EventBus`) that maintains a dictionary mapping event types to lists of listener callbacks. There is no external message broker, no IPC, no serialization -- events are Python objects passed by reference within the asyncio event loop.

### Dispatch Model

- **Pattern**: Pure **publish/subscribe**. No request/reply, no routing keys, no topics hierarchy.
- **Delivery**: When an event fires, the bus iterates over all registered listeners for that event type and schedules asyncio tasks to deliver the event to each subscriber.
- **Ordering**: Event delivery tasks are added to the asyncio task queue. They execute after the current task yields control back to the event loop. This means event handlers run asynchronously relative to the code that fired the event, but on the same thread.
- **Backpressure**: None. If listeners are slow, tasks accumulate in the event loop queue.

### Key Built-in Events

| Event | Fired When |
|-------|-----------|
| `state_changed` | Any entity state update (old_state, new_state) |
| `call_service` | A service action is invoked |
| `service_registered` | A new service is registered |
| `homeassistant_start` | HA is starting up |
| `homeassistant_started` | HA startup complete |
| `homeassistant_stop` | HA is shutting down |
| `automation_triggered` | An automation fires |
| `component_loaded` | An integration finishes loading |
| `time_changed` | Every 1 second from the Timer |

### Sync/Async Compatibility

HA provides both sync (`hass.bus.fire()`) and async (`hass.bus.async_fire()`) interfaces. Synchronous calls from legacy integrations are dispatched to a worker thread pool and then marshalled back to the event loop. Modern integrations should use async methods exclusively.

---

## 3. Plugin/Integration System

### Structure

Each integration lives in `homeassistant/components/{domain}/` and contains:

```
manifest.json        # Metadata, dependencies, requirements
__init__.py          # Setup functions (async_setup, async_setup_entry)
config_flow.py       # UI configuration wizard (optional)
sensor.py            # Platform implementations (sensor, light, switch, etc.)
services.yaml        # Service action descriptions
strings.json         # Translations
```

### Manifest Fields

| Field | Purpose |
|-------|---------|
| `domain` | Unique identifier (matches directory name) |
| `name` | Human-readable name |
| `integration_type` | `hub`, `device`, `service`, `entity`, `helper`, `hardware`, `system`, `virtual` |
| `dependencies` | Integrations that must load before this one |
| `after_dependencies` | Soft dependencies (load first if present, but not required) |
| `requirements` | Python pip packages to install |
| `config_flow` | `true` if UI configuration is supported |
| `iot_class` | Connection type: `local_polling`, `local_push`, `cloud_polling`, `cloud_push`, `assumed_state`, `calculated` |
| `codeowners` | GitHub maintainer usernames |
| `quality_scale` | Maturity level: `bronze`, `silver`, `gold`, `platinum` |

### Discovery Mechanisms

Integrations declare discovery matchers in their manifest for automatic device detection via:
- **Zeroconf/mDNS** -- service type matching
- **SSDP** -- UPnP device matching
- **DHCP** -- MAC address prefix matching
- **Bluetooth** -- local name and service UUID matching
- **MQTT** -- topic pattern matching
- **USB** -- vendor/product ID matching
- **HomeKit** -- accessory category matching

These matchers are compiled into generated Python files at build time for fast lookup.

### Lifecycle Hooks

| Hook | When Called | Purpose |
|------|-----------|---------|
| `async_setup(hass, config)` | Once at integration load | Global setup: register services, set up shared resources |
| `async_setup_entry(hass, entry)` | Per config entry | Initialize API clients, store runtime data in `hass.data[DOMAIN][entry.entry_id]`, forward platform setup |
| `async_unload_entry(hass, entry)` | Config entry removed/reloaded | Clean up resources, unsubscribe listeners |
| `async_remove_entry(hass, entry)` | Config entry permanently deleted | Final cleanup (e.g., delete cloud accounts) |
| `async_setup_platform(...)` | Per platform (legacy YAML) | Set up entities for a platform |

Config entries progress through states: `NOT_LOADED` -> `SETUP_IN_PROGRESS` -> `LOADED` (or `SETUP_RETRY` with exponential backoff on failure).

### What Integrations Can Register

- **Entities** (sensors, switches, lights, etc.) via platform files
- **Services** (callable actions) via `hass.services.async_register()`
- **Event listeners** via `hass.bus.async_listen()`
- **Panels** (custom UI tabs) via `hass.components.frontend.async_register_built_in_panel()`
- **Webhooks** via `hass.components.webhook.async_register()`
- **API views** (custom REST endpoints) via `hass.http.register_view()`

### DataUpdateCoordinator Pattern

The standard pattern for integrations that poll external APIs. A `DataUpdateCoordinator` fetches data at a configured interval, and multiple entities register as listeners. When data refreshes, the coordinator calls `async_write_ha_state()` on all attached entities. This prevents redundant API calls when one device exposes many entities.

---

## 4. Automation Engine

### Architecture

The automation system is implemented as a **core integration** (`homeassistant/components/automation/`), not a special subsystem. It uses the same integration lifecycle as any other component. Internally, it builds on the **Script execution engine** (`homeassistant/helpers/script.py`).

### Structure: Trigger -> Condition -> Action

```
Trigger (OR logic)  -->  Condition (AND logic)  -->  Action (sequence)
     "when"                  "only if"                 "then do"
```

- **Triggers** are OR'd: any single trigger firing starts evaluation
- **Conditions** are AND'd by default: all must be true (supports explicit `or`/`not` grouping)
- **Actions** execute sequentially as a script

### Trigger Types

State change, numeric state threshold, time, time pattern, sun (sunrise/sunset), zone (enter/leave), device trigger, webhook, MQTT message, template (Jinja2 expression becomes truthy), event, geo-location, calendar, tag scan, persistent notification, sentence (voice), and more.

### Condition Types

State, numeric state, time, time range, zone, template, sun, trigger (which trigger fired), and, or, not.

### Script Execution Engine

The `Script` class manages action sequences with four concurrency modes:

| Mode | Behavior |
|------|----------|
| `single` | New trigger ignored if already running (default) |
| `restart` | Running instance cancelled, new one starts |
| `queued` | New run waits for current to finish |
| `parallel` | Runs concurrently |

Action types supported by the script engine:
- **Service calls** -- call any registered service
- **Conditions** -- inline condition checks (stops execution if false)
- **Delays** -- pause for a duration
- **Wait for trigger** -- pause until a trigger fires
- **Wait for template** -- pause until a template becomes truthy
- **Repeat** -- loops with `count`, `while`, `until`, `for_each`
- **Choose** -- multi-way branching (if/elif/else)
- **If/Then/Else** -- simple conditional
- **Variables** -- set variables mid-sequence
- **Fire event** -- emit a custom event
- **Stop** -- halt execution

### Storage

- **UI-created automations** are stored in `automations.yaml` (YAML list format, auto-generated IDs)
- **YAML-defined automations** are in `configuration.yaml` or included files
- Config entry data for integrations is stored in `.storage/core.config_entries` (JSON)

### Debugging

The **Trace system** records execution paths through automations. Each step generates a `TraceElement` storing variables, outcomes, and branch decisions. Traces use `ContextVar` to maintain execution context across nested steps. Visible in the UI.

### Blueprints

Reusable automation templates. A blueprint defines a parameterized automation; users supply inputs that are substituted into the template. Both automations and scripts support blueprints.

---

## 5. State Management

### Central State Machine

`hass.states` is the central `StateMachine` object. All entity state lives here as an in-memory dictionary keyed by `entity_id`.

### State Objects

Each state is a `State` object containing:

| Field | Description |
|-------|-------------|
| `entity_id` | `{domain}.{object_id}` (e.g., `sensor.temperature`) |
| `state` | String value (max 255 chars) |
| `attributes` | Dict of JSON-serializable metadata |
| `last_changed` | Timestamp when `state` value last changed |
| `last_updated` | Timestamp when state object was last written (even if value unchanged) |
| `last_reported` | Timestamp of most recent report from the device |
| `context` | Tracks causation chain (who triggered this change) |

### Immutability

**State objects are immutable (frozen)**. When you read `hass.states.get(entity_id)`, you get a snapshot. The state machine creates a new `State` object on each update rather than mutating existing ones. This is safe for concurrent readers on the asyncio loop.

### Propagation

1. Integration calls `self.async_write_ha_state()` (for entities) or `hass.states.async_set()` (directly)
2. State machine creates new `State` object, stores it in the dict
3. State machine fires `state_changed` event on the event bus with `old_state` and `new_state`
4. All subscribers (automations, recorder, logbook, frontend WebSocket subscriptions, template sensors, etc.) receive the event asynchronously via the event loop task queue

### Entity Registries

HA maintains several persistent registries:
- **Entity Registry** (`entity_registry.py`) -- persistent metadata about entities (disabled, hidden, custom names, icons)
- **Device Registry** -- groups entities by physical device
- **Area Registry** -- organizes devices by physical location

These persist to `.storage/` as JSON and survive restarts even when the associated integration is not loaded.

---

## 6. Service Registry

### How It Works

The `ServiceRegistry` (`hass.services`) maintains a dictionary of registered services keyed by `(domain, service_name)`. When a `call_service` event fires on the event bus, the registry dispatches to the registered handler.

### Registration

```python
# In async_setup() or setup():
hass.services.async_register(
    domain=DOMAIN,
    service="my_action",
    service_func=handle_my_action,  # async callback
    schema=vol.Schema({...}),       # voluptuous validation
)
```

Services MUST be registered in `async_setup()` (not `async_setup_entry()`) so they exist even when no config entries are loaded. This allows the UI automation editor to validate service references.

### Service Descriptors

Each integration provides a `services.yaml` that describes:
- Service name and description
- Target (entity, device, area selectors)
- Fields (parameters with types, defaults, examples)
- Supported features filtering

### Calling Services

- **Python**: `await hass.services.async_call(domain, service, data, blocking=True)`
- **REST API**: `POST /api/services/{domain}/{service}`
- **WebSocket**: `{"type": "call_service", "domain": "...", "service": "...", "service_data": {...}}`
- **Automations/Scripts**: `service: domain.service_name` in YAML

### Response Data

Services can return data (introduced more recently). Declared via `supports_response: SupportsResponse.OPTIONAL` or `ONLY`. Response must be a JSON-serializable dict.

---

## 7. Configuration

### Dual Configuration Model

HA has been migrating from YAML-first to UI-first configuration:

| Mechanism | Used For | Storage |
|-----------|----------|---------|
| **Config Flows (UI)** | Modern integrations, device setup | `.storage/core.config_entries` (JSON) |
| **Options Flows (UI)** | Runtime settings for config entries | `.storage/core.config_entries` (JSON) |
| **YAML** | Automations, scripts, some legacy integrations | `configuration.yaml`, `automations.yaml`, etc. |
| **Helpers (UI)** | Input helpers (input_boolean, timer, counter, etc.) | `.storage/` |

### Config Flows

Integrations with `"config_flow": true` in their manifest provide a `config_flow.py` with step methods (`async_step_user`, `async_step_zeroconf`, `async_step_reauth`, etc.). Each step returns either:
- A form (data schema + UI selectors) for user input
- An entry creation call (`self.async_create_entry(...)`)
- An abort

### Schema Validation

- **YAML validation**: Uses the **Voluptuous** library. Integrations define `CONFIG_SCHEMA` (whole-config) or `PLATFORM_SCHEMA` (per-platform) as voluptuous schemas.
- **Config flow validation**: Basic validation from the data schema, plus custom validation in step methods.
- **Custom validators**: `homeassistant/helpers/config_validation.py` extends Voluptuous with HA-specific types: `entity_id`, `template`, `time_period`, `boolean`, `positive_int`, `slug`, etc.
- **Thread safety**: Validators marked `@not_async_friendly` raise `MustValidateInExecutor` if called within the event loop, forcing validation onto a worker thread.

### Selectors

Selectors provide metadata that the frontend uses to render appropriate UI controls (entity picker, area picker, color picker, device selector, etc.). They bridge the gap between schema validation and UI generation.

---

## 8. Messaging/Transport Options

HA does not use any external message broker for internal communication. All internal messaging is via the in-process event bus. However, HA supports multiple external messaging protocols through integrations:

### MQTT

- **Native integration** (`homeassistant/components/mqtt/`)
- Supports MQTT brokers via TCP or **WebSocket transport** (configurable in the integration)
- MQTT discovery: devices can self-announce by publishing to `homeassistant/{domain}/{unique_id}/config`
- Supports QoS 0, 1, 2
- Commonly paired with the **Mosquitto** add-on (Docker container managed by Supervisor)

### WebSocket

- Core provides a **WebSocket server** at `/api/websocket`
- Used by the frontend for real-time state updates
- Used by the companion mobile apps for local push notifications
- Bidirectional: clients can subscribe to events, call services, and receive state changes in real time

### Webhooks

- Integrations can register webhook endpoints via `hass.components.webhook.async_register()`
- External services POST to `https://{ha_url}/api/webhook/{webhook_id}`
- Webhooks can also be delivered via WebSocket API (`webhook/handle` command)
- Used as automation triggers and for receiving data from external services

### REST

- Core provides a full **REST API** (see section 12)
- The `rest` integration allows HA to consume external REST APIs as sensors/switches
- The `rest_command` integration allows HA to call arbitrary REST endpoints as service actions

---

## 9. Data Storage

### Database Layer

The **Recorder** integration (`homeassistant/components/recorder/`) handles all database persistence. It runs as a **dedicated background thread** consuming events from an in-memory queue, writing to the database via **SQLAlchemy ORM**.

### Supported Backends

| Backend | Notes |
|---------|-------|
| **SQLite** (default) | WAL mode, `synchronous=NORMAL`. No external server needed. |
| **MySQL/MariaDB** | Supported via SQLAlchemy dialect. Specific charset/collation requirements. |
| **PostgreSQL** | Supported via SQLAlchemy dialect. |

### Schema and Data Tiers

| Table | Content | Retention |
|-------|---------|-----------|
| `states` + `states_meta` | Every entity state change (entity_id, state value, timestamp) | Purged after `purge_keep_days` (default 10) |
| `state_attributes` | JSON attribute blobs (deduplicated via content hash) | Purged with states |
| `events` + `event_types` | Every event fired on the bus (deduplicated type strings) | Purged after `purge_keep_days` |
| `event_data` | JSON event data blobs (deduplicated) | Purged with events |
| `statistics_short_term` | 5-minute aggregates (mean, min, max, sum) | Purged after ~10 days |
| `statistics` + `statistics_meta` | Hourly aggregates | **Never purged** (~2MB/entity/year) |
| `statistics_runs` | Tracks compilation progress | Permanent |

### Statistics Compilation

Two-stage pipeline:
1. Raw state changes -> 5-minute short-term statistics (mean, min, max for `MEASUREMENT` state class; sum for `TOTAL`/`TOTAL_INCREASING`)
2. 5-minute statistics -> hourly long-term statistics

The system **normalizes units** to ensure consistency even if a sensor's display unit changes over time. Handles meter resets for `TOTAL_INCREASING` class.

### Performance Optimizations

- Dedicated thread isolates all DB I/O from the event loop
- Batch commits at configurable `commit_interval`
- Content-addressed deduplication for attributes and event data
- Composite indexes on `(metadata_id, timestamp)` for efficient range queries
- `StatementLambdaElement` for execution plan caching
- SQLite `VACUUM` for disk reclamation (configurable via `auto_repack`)
- Orphan cleanup during purge cycles

### Configuration Storage

Separate from the Recorder database:
- `.storage/` directory contains JSON files for config entries, entity registry, device registry, area registry, auth tokens, etc.
- `configuration.yaml` and included files for YAML-based config
- `automations.yaml`, `scripts.yaml`, `scenes.yaml` for UI-created items

---

## 10. Scheduler / Job System

HA does not have a standalone job scheduler like Celery or a persistent task queue. Instead, it provides **asyncio-based scheduling helpers** in `homeassistant/helpers/event.py`:

### Scheduling Primitives

| Helper | Purpose |
|--------|---------|
| `async_call_later(hass, delay, callback)` | One-shot timer: call callback after N seconds |
| `async_track_time_interval(hass, callback, interval)` | Recurring timer: call callback every N seconds |
| `async_track_point_in_time(hass, callback, point_in_time)` | Call callback at a specific datetime |
| `async_track_utc_time_change(hass, callback, hour, minute, second)` | Call callback at specific clock times |
| `async_track_state_change_event(hass, entity_ids, callback)` | Call callback when entity state changes |
| `async_track_template_result(hass, template, callback)` | Call callback when template result changes |

### Task Creation

| Method | Purpose |
|--------|---------|
| `hass.async_create_task(coro)` | Schedule a coroutine on the event loop |
| `hass.async_create_background_task(coro, name)` | Background task that survives without blocking shutdown |
| `entry.async_create_background_task(coro, name)` | Background task tied to a config entry lifecycle |
| `hass.async_add_executor_job(func, *args)` | Run blocking I/O on the thread pool |

### Long-Running Operations

- Blocking I/O (network calls, file reads) must use `hass.async_add_executor_job()` to avoid blocking the event loop
- The `DataUpdateCoordinator` handles periodic polling in a structured way
- Config entries in `SETUP_RETRY` state use **exponential backoff** for retries
- The `Script` engine handles long-running automation sequences with wait/delay steps

### Timer Integration

The core `Timer` fires `time_changed` every second. The scheduling helpers hook into this (and asyncio's `call_later`) to provide time-based scheduling without requiring an external scheduler.

---

## 11. Notification System

### Architecture

Notifications are implemented as **integrations**, not a core subsystem. The `notify` domain defines a standard interface that notification integrations implement.

### Built-in Notification Types

| Type | Mechanism |
|------|-----------|
| **Persistent Notifications** | Sidebar notifications in the web UI. `persistent_notification.create` service. |
| **Mobile Push (Companion App)** | Via Firebase Cloud Messaging (Android) or Apple Push Notification Service (iOS) |
| **Local Push** | Via WebSocket API directly to companion app (no cloud, no rate limits) |
| **Notify Groups** | Fan-out to multiple notification targets with a single call |

### External Notification Integrations

The integration ecosystem provides hundreds of notification channels: Telegram, Slack, Discord, email (SMTP), SMS (Twilio), Signal, Pushover, Pushbullet, NTFY, and many more. Each is a separate integration implementing the `notify` platform.

### Advanced Features

- **Actionable notifications** -- buttons on mobile notifications that trigger automations
- **Critical notifications** -- bypass Do Not Disturb on mobile
- **Notification channels** (Android 8.0+) -- categorize notifications with custom sounds/vibration
- **TTS (Text-to-Speech)** -- separate `tts` domain for audio announcements via media players
- **Media attachments** -- images, video, audio in mobile notifications

### Calling Notifications

```yaml
# Service call:
service: notify.mobile_app_my_phone
data:
  message: "Front door opened"
  title: "Security Alert"
  data:
    actions:
      - action: "LOCK_DOOR"
        title: "Lock Door"
```

---

## 12. REST API / WebSocket API

### REST API

Served by the built-in `aiohttp` web server. All endpoints require `Authorization: Bearer {access_token}` header.

**GET endpoints:**

| Endpoint | Returns |
|----------|---------|
| `/api/` | API status check |
| `/api/config` | Current HA configuration |
| `/api/components` | Loaded components list |
| `/api/events` | Active event types and listener counts |
| `/api/services` | Available services by domain |
| `/api/states` | All entity states |
| `/api/states/{entity_id}` | Single entity state |
| `/api/error_log` | Current session error log |
| `/api/history/period/{timestamp}` | Historical state changes |
| `/api/logbook/{timestamp}` | Logbook entries |
| `/api/camera_proxy/{entity_id}` | Camera image |
| `/api/calendars` | Calendar entities |
| `/api/calendars/{entity_id}` | Calendar events |

**POST endpoints:**

| Endpoint | Action |
|----------|--------|
| `/api/states/{entity_id}` | Create/update entity state |
| `/api/events/{event_type}` | Fire custom event |
| `/api/services/{domain}/{service}` | Call service (`?return_response` for data) |
| `/api/template` | Render Jinja2 template |
| `/api/config/core/check_config` | Validate configuration |
| `/api/intent/handle` | Process intent (voice/NLP) |

**DELETE endpoints:**
| Endpoint | Action |
|----------|--------|
| `/api/states/{entity_id}` | Remove entity |

### WebSocket API

Located at `/api/websocket`. JSON messages with `type` field and `id` for correlation.

**Authentication flow:**
1. Server sends `auth_required` with HA version
2. Client sends `auth` with `access_token`
3. Server responds `auth_ok` or `auth_invalid`

**Core commands:**

| Command | Purpose |
|---------|---------|
| `get_states` | All entity states |
| `get_config` | HA configuration |
| `get_services` | Available services |
| `get_panels` | UI panels |
| `subscribe_events` | Subscribe to event bus (optional type filter) |
| `unsubscribe_events` | Unsubscribe |
| `fire_event` | Fire event |
| `call_service` | Execute service action |
| `subscribe_trigger` | Monitor trigger conditions |
| `validate_config` | Validate trigger/condition/action |
| `ping` / `pong` | Heartbeat |

### Authentication

| Token Type | Lifespan | Use Case |
|------------|----------|----------|
| **OAuth 2.0 refresh tokens** | Until revoked | Third-party applications |
| **Long-lived access tokens** | 10 years | Simple integrations, scripts, API testing |
| **Short-lived access tokens** | 30 minutes | Obtained via OAuth refresh flow |

Both REST and WebSocket APIs use the same token system. Tokens are scoped to a user account.

---

## 13. Add-on System

### Add-ons vs Integrations

| Aspect | Integration | Add-on |
|--------|-------------|--------|
| **Runs in** | HA Core Python process (same address space) | Separate Docker container |
| **Isolation** | None -- shares memory, event loop, thread pool | Full process and filesystem isolation |
| **Managed by** | Core's integration loader | Supervisor |
| **Creates entities directly** | Yes | No -- needs a corresponding integration |
| **Language** | Python only | Any (Go, Rust, Node.js, C, shell scripts, etc.) |
| **Requires** | Any HA installation | HA OS or Supervised installation only |
| **Examples** | Philips Hue, Z-Wave JS, Google Cast | Mosquitto MQTT broker, Zigbee2MQTT, Node-RED, Frigate NVR, VS Code Server |

### How Add-ons Work

1. **Supervisor** pulls a Docker image (from a repository or local build)
2. Container runs with configured networking, storage mounts, and environment variables
3. Add-on communicates with HA Core over **localhost network** (HTTP, MQTT, or other protocols)
4. A corresponding **integration** inside Core translates the add-on's data into entities, services, and events
5. Supervisor handles lifecycle: start, stop, update, backup, log collection

### Add-on Capabilities

- Can request hardware access (USB, GPIO, audio)
- Can request host network mode or specific port mappings
- Can declare dependencies on other add-ons
- Configuration is defined via a `config.yaml` in the add-on repo, rendered as a UI form by Supervisor
- Ingress support allows add-on UIs to be proxied through the HA frontend

### HACS (Home Assistant Community Store)

Not an official feature but widely used. HACS is itself a custom integration that provides a UI for discovering and installing:
- Custom integrations (loaded into Core, same as official integrations)
- Custom frontend cards/themes
- Custom Python scripts

HACS content does NOT run in Docker containers -- it runs inside the Core process, with no isolation.

---

## 14. Known Architectural Pain Points and Criticisms

### Single-Threaded Event Loop Bottleneck

The entire Core runs on one Python thread (asyncio). A single misbehaving integration that blocks the event loop can make the entire system unresponsive. Users have reported CPU spikes where the python3 process pegs a single core at 100%, causing UI lag and delayed automations. There is no preemptive scheduling -- cooperative multitasking means one bad actor affects everything.

### No Integration Isolation

All integrations share the same process. A buggy custom integration can crash all of Home Assistant. There is no sandboxing, no memory limits per integration, no fault isolation. This is particularly problematic with HACS custom integrations that have varying quality.

### YAML Configuration Pain

The community is deeply divided on YAML:
- YAML syntax sensitivity (indentation, colons, dashes) causes frequent configuration errors
- Error messages from Voluptuous validation are often cryptic ("extra keys not allowed")
- The ongoing migration from YAML to UI-only configuration frustrates power users who prefer text-based, version-controllable configuration
- Some features are YAML-only, some are UI-only, and some support both, creating confusion

### State Representation Limitations

- Entity state is a **single string** (max 255 characters). Complex state must be encoded into attributes, which are a flat JSON dict. This is limiting for entities that have rich, structured state.
- No built-in concept of **previous state history** at the entity level. Automations that need "was in state X for N minutes" require template workarounds.
- `last_changed` vs `last_updated` vs `last_reported` semantics confuse users. Attributes changes update `last_updated` but not `last_changed`, leading to subtle bugs.

### Entity/Device Model Gaps

- HA struggles to model **composite devices** (e.g., a fridge with temperature sensor, door sensor, and light as a unified concept)
- "Zombie entities" (unavailable entities from removed/offline devices) accumulate and cause system instability
- Entity categorization and room assignment become unwieldy at scale

### Database/Recorder Limitations

- Default SQLite works for typical homes but struggles with very large installations (1000+ entities with high-frequency updates)
- The 10-day default purge window means detailed historical data is ephemeral
- Long-term statistics lose granularity (hourly only) -- no way to keep 5-minute resolution long-term without external tools
- No native time-series database. The recorder bolts time-series semantics onto a relational schema, which is inherently less efficient than purpose-built TSDBs (InfluxDB, TimescaleDB)

### Automation Engine Limitations

- No native **state machine** construct -- users must simulate state machines with input_select helpers and complex automations
- Automation debugging improved with traces but remains difficult for complex multi-step sequences
- No dependency graph or visualization of automation interactions
- Race conditions are possible when multiple automations react to the same state change

### Python Performance Ceiling

- Python's GIL and asyncio's cooperative model limit throughput
- Large installations report slow startup times (minutes)
- Memory usage grows with entity count; no ability to page out inactive entities
- CPU-intensive operations (template rendering, voice processing) compete with event dispatch on the same thread

### Upgrade Fragility

- Monthly release cycle with frequent breaking changes to integration APIs
- Custom integrations (HACS) frequently break on upgrades
- No formal API stability guarantees for integration developers

---

## Sources

- [Core Architecture - HA Developer Docs](https://developers.home-assistant.io/docs/architecture/core/)
- [Architecture Overview - HA Developer Docs](https://developers.home-assistant.io/docs/architecture_index/)
- [Home Assistant Concurrency Model - The Candid Startup](https://www.thecandidstartup.org/2025/10/20/home-assistant-concurrency-model.html)
- [Integration Manifest - HA Developer Docs](https://developers.home-assistant.io/docs/creating_integration_manifest/)
- [Integration Service Actions - HA Developer Docs](https://developers.home-assistant.io/docs/dev_101_services/)
- [Recorder and Statistics - DeepWiki](https://deepwiki.com/home-assistant/core/3.1-recorder-and-statistics)
- [Entity Platforms and Domain Components - DeepWiki](https://deepwiki.com/home-assistant/core/5.3-entity-platforms-and-domain-components)
- [Discovery and Network Protocols - DeepWiki](https://deepwiki.com/home-assistant/core/5.1-discovery-and-network-protocols)
- [Configuration Validation and Automation - DeepWiki](https://deepwiki.com/home-assistant/core/2.4-configuration-validation-and-automation)
- [Understanding HA Database Model - SmartHomeScene](https://smarthomescene.com/blog/understanding-home-assistants-database-and-statistics-model/)
- [Long and Short Term Statistics - HA Data Science](https://data.home-assistant.io/docs/statistics/)
- [REST API - HA Developer Docs](https://developers.home-assistant.io/docs/api/rest/)
- [WebSocket API - HA Developer Docs](https://developers.home-assistant.io/docs/api/websocket/)
- [Authentication API - HA Developer Docs](https://developers.home-assistant.io/docs/auth_api/)
- [Automation Basics - HA Docs](https://www.home-assistant.io/docs/automation/basics/)
- [Automation Triggers - HA Docs](https://www.home-assistant.io/docs/automation/trigger/)
- [Recorder Integration - HA Docs](https://www.home-assistant.io/integrations/recorder/)
- [MQTT Integration - HA Docs](https://www.home-assistant.io/integrations/mqtt/)
- [Notifications - HA Docs](https://www.home-assistant.io/integrations/notify/)
- [Persistent Notification - HA Docs](https://www.home-assistant.io/integrations/persistent_notification/)
- [Companion App Notifications - HA Companion Docs](https://companion.home-assistant.io/docs/notifications/notifications-basic/)
- [Integrations vs Add-ons - nachbelichtet](https://nachbelichtet.com/en/home-assistant-what-is-the-difference-between-integrations-add-ons-hacs-ha-core-and-haos)
- [Deprecating async_run_job and async_add_job - HA Developer Blog](https://developers.home-assistant.io/blog/2024/03/13/deprecate_add_run_job/)
- [Events - HA Docs](https://www.home-assistant.io/docs/configuration/events/)
- [States - HA Developer Docs](https://developers.home-assistant.io/docs/dev_101_states/)
- [State Object - HA Docs](https://www.home-assistant.io/docs/configuration/state_object/)
- [Local-First Rebellion - GitHub Blog](https://github.blog/open-source/maintainers/the-local-first-rebellion-how-home-assistant-became-the-most-important-project-in-your-house/)
- [Roadmap 2025 - HA Blog](https://www.home-assistant.io/blog/2025/05/09/roadmap-2025h1/)

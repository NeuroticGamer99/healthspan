# Glossary

Project-specific terminology used across the healthspan documentation. Terms are grouped by domain. General software terms (REST, JSON, SQLite, etc.) are not included — only terms that have specific meanings in this project.

---

## Architecture

**Core Service**
The central process that owns the database connection. Exposes a versioned REST API (`/v1/`). All other processes — GUI, MCP server, import pipeline, CLI — are clients of the Core Service. The only process that performs database writes during normal operation. See [ADR-0006](adr/0006-application-architecture.md).

**Micro-kernel architecture**
The design principle where the Core Service is a thin host that enforces contracts (auth, validation, transactions), and business logic is delivered as plugins against the same interfaces available to third-party contributors. There is no privileged distinction between first-party and user-contributed plugins. See [ADR-0006](adr/0006-application-architecture.md).

**Process isolation**
Each platform component (Core Service, MCP server, GUI, import pipeline, CLI) runs as an independent process communicating over HTTP. No process has privileged access; the Core Service enforces auth and validation uniformly for all clients. See [ADR-0006](adr/0006-application-architecture.md).

**AI client**
Any MCP-compatible application that connects to the MCP server to query health data. This is the correct term — the platform does not reference any specific AI product by name in interfaces, configuration, or documentation. See [ADR-0007](adr/0007-mcp-transport.md).

**Shared TOML config**
A single versioned TOML configuration file read by all processes. Contains: service ports, bearer token, database path, binding address, log level, plugin directory path, and schedule definitions. Processes do not hardcode any of these values. See [design-rationale.md](design-rationale.md).

---

## Data Model

**Canonical biomarker name**
The normalized, human-readable name assigned to each biomarker in the `biomarkers` table. Different labs name the same biomarker differently (e.g. "Glucose, Serum" and "Fasting Glucose"); the canonical name resolves these to a single identifier used in all queries and analysis. See [design-rationale.md](design-rationale.md).

**Draw context**
The ordering context that explains why a particular set of biomarkers were tested together (e.g. "annual physical", "comprehensive metabolic panel", "Function Health quarterly"). Captured as a field on lab result records. See [design-rationale.md](design-rationale.md).

**Lab source**
The laboratory that processed a blood draw. A first-class attribute on every lab result row — not optional. Required because different labs use different assay platforms that produce systematically different values for the same biomarker (especially immunoassay-based markers like insulin). See [design-rationale.md](design-rationale.md).

**Reference range framework**
A named set of optimal ranges for biomarkers, separate from the lab-reported reference range stored per result row. Examples: "Lab Standard" (what the lab flags), "Function Health" (longevity-optimized), "Attia" (practitioner-specific). Frameworks are queryable and extensible via plugins. See [ADR-0005](adr/0005-reference-range-frameworks.md).

**Timestamp quadruple**
The four-column convention used on every timestamped record: `*_utc` (UTC ground truth), `*_local_recorded` (original value from source, immutable), `*_local_tz` (IANA timezone name), `*_tz_inferred` (boolean flag — 1 if timezone was assumed, not known from source). See [design-rationale.md](design-rationale.md).

**Metabolic context**
Levels' proprietary computed layer: zone scores and glucose response analysis. Stored as read-only imported values that cannot be independently recomputed. See [data-model.md](data-model.md).

---

## Interventions

**Authority type**
An enum on the `intervention_dose_history` table recording who directed a dose change: `prescribing_physician`, `supervising_clinician`, `self`, or `protocol`. Orthogonal to `reason` — the same reason can occur under different authorities. See [data-model.md](data-model.md).

**Change type**
An enum on the `intervention_dose_history` table recording the nature of a dose change: `initiation`, `increase`, `decrease`, `hold`, `resumption`, or `discontinuation`. See [data-model.md](data-model.md).

---

## Import Pipeline

**Bulk import endpoint**
`POST /v1/import` on the Core REST API. The single entry point for all data entering the system. Provides full-batch validation before any write, atomic transactions, dry-run mode, and explicit conflict policies. See [ADR-0004](adr/0004-data-ingestion-strategy.md).

**Conflict policy**
A required parameter on every import request specifying how to handle rows that conflict with existing data: `reject` (error on any conflict — the default), `skip` (silently ignore conflicting rows), or `upsert` (overwrite existing rows). There is no implicit default that silently mutates data. See [ADR-0004](adr/0004-data-ingestion-strategy.md).

**Dry-run mode**
A `?dry_run=true` parameter on the bulk import endpoint that runs full validation and returns what would be imported without writing anything. See [ADR-0004](adr/0004-data-ingestion-strategy.md).

**Import adapter**
A plugin type (`import_adapter`) that implements the parse → validate → normalize pipeline for a specific data source. Registered as a named service (e.g. `import.quest_labs`). Submits normalized records to the Core REST API bulk import endpoint. See [ADR-0010](adr/0010-cli-plugin-model.md).

---

## Plugin System

**Plugin type**
A declared capability that a plugin implements. The initial set: `cli`, `mcp_tool`, `import_adapter`, `analysis`, `query`, `reference_ranges`, `automation`, `notification_channel`, `provider`. A single plugin can implement multiple types. See [ADR-0010](adr/0010-cli-plugin-model.md).

**Provider plugin**
A plugin whose sole purpose is to register services for other plugins via the service registry. Carries no CLI commands or MCP tools of its own. Useful for shared parsers, API clients, and utility libraries. See [ADR-0010](adr/0010-cli-plugin-model.md).

**PluginContext**
The single parameter passed to every plugin's `register()` function. Carries platform infrastructure (CLI app, MCP server, config, logger, API client) and the service registry for inter-plugin communication. See [ADR-0010](adr/0010-cli-plugin-model.md).

**Service registry**
The mechanism in `PluginContext` for inter-plugin communication. Plugins register named, versioned services; other plugins consume them by name. Service names use dot-notation namespaces (e.g. `quest.parser.labs`). Direct Python imports between plugins are not a supported pattern. See [ADR-0010](adr/0010-cli-plugin-model.md).

**Plugin API version**
A single integer maintained by the platform, incremented on breaking changes to the plugin-to-platform interface. Plugins declare `PLUGIN_API_MIN_VERSION` (required) and optionally `PLUGIN_API_MAX_VERSION`. Distinct from service version. See [ADR-0010](adr/0010-cli-plugin-model.md).

**Service version**
The version of a specific service registered in the service registry. Independent of the plugin API version — a plugin at API version 3 may provide service version 1. Both must be declared and checked independently. See [ADR-0010](adr/0010-cli-plugin-model.md).

**PLUGIN_PACKAGES**
A module-level declaration in a plugin listing pip packages it requires (e.g. `["pandas", "numpy"]`). Packages are resolved from a curated, version-locked catalog maintained by the platform. Explicit version pins or unknown packages are treated as off-catalog and require user confirmation. See [ADR-0024](adr/0024-plugin-extensions.md).

**Catalog-governed package**
A pip package declared in `PLUGIN_PACKAGES` by name only (no version pin), resolved from the platform's curated catalog at a locked version. All catalog-governed packages resolve to the same version across all plugins. See [ADR-0024](adr/0024-plugin-extensions.md).

---

## Event Bus

**Event bus**
An internal asyncio-based event bus hosted by the Core Service. Transport adapters bridge the internal bus to external protocols. Events use dot-notation namespaces (e.g. `data.imported`, `job.progress`, `alert.triggered`). See [ADR-0011](adr/0011-event-bus.md).

**Transport adapter**
A plugin that bridges the internal event bus to an external protocol. Inbound adapters (HTTP webhook, MQTT) translate external events into internal bus events. Outbound adapters (SSE, ZeroMQ, MQTT) broadcast internal events to external subscribers. See [ADR-0011](adr/0011-event-bus.md).

---

## Jobs

**Job (lightweight)**
An async operation that runs as an asyncio task within the Core Service process. Suitable for small imports and quick analysis. See [ADR-0012](adr/0012-job-abstraction.md).

**Job (heavyweight)**
An async operation that runs as a separate process spawned by the Core Service. Suitable for large backfills and intensive computation. The child process has no direct database access — all writes go through the Core REST API. See [ADR-0012](adr/0012-job-abstraction.md).

---

## Security and Encryption

**Bearer token**
A cryptographically random token (minimum 32 bytes, base64url-encoded) required in the `Authorization` header of every HTTP request. Generated automatically on first run. Stored in the shared TOML config file. Never appears in URLs, logs, or source code. See [security.md](security.md).

**Secret key**
A randomly generated 32-byte value stored in the OS keychain via `keyring`. One of the two components of the two-factor hybrid key model. Never typed by the user under normal operation. See [ADR-0013](adr/0013-encryption-at-rest.md).

**Master passphrase**
A user-chosen passphrase that is the second component of the two-factor hybrid key model. Known only to the user; never stored by default (except in full auto-unlock mode). See [ADR-0013](adr/0013-encryption-at-rest.md).

**Two-factor hybrid key model**
The key management approach where the database encryption key is derived via Argon2id from two independent components: the secret key (stored in the OS keychain) and the master passphrase (known only to the user). Neither component alone is sufficient to decrypt the database. See [ADR-0013](adr/0013-encryption-at-rest.md).

**Recovery Kit**
A printable document generated at `healthspan init` containing the secret key as a QR code and Base32 string, with a blank line to handwrite the master passphrase. Enables cross-device database recovery. Useless without the passphrase. See [ADR-0013](adr/0013-encryption-at-rest.md).

**Passphrase-only mode**
An alternative key management mode where the encryption key is derived solely from the passphrase via Argon2id. No secret key, no OS keychain dependency. Single-factor protection. Fully portable without a Recovery Kit. See [ADR-0013](adr/0013-encryption-at-rest.md).

**Trust layer**
The security model defines three trust layers: **storage** (the encrypted file — zero-knowledge for the provider), **processing** (the machine running Core Service — must be trusted), **transport** (communication between processes — requires HTTPS if crossing a machine boundary). See [security.md](security.md).

---

## Observability

**Health endpoint**
An HTTP endpoint (`GET /v1/health` on Core Service, `GET /health` on other processes) that returns process readiness status. Used by the launcher to gate startup ordering. See [observability.md](observability.md).

**Request ID**
A UUID assigned to every HTTP request on receipt, included in the response header (`X-Request-ID`) and all associated log entries. Enables correlating log entries across a request lifecycle. See [observability.md](observability.md).

# Glossary

Project-specific terminology used across the Healthspan documentation. Terms are grouped by domain. General software terms (REST, JSON, SQLite, etc.) are not included — only terms that have specific meanings in this project.

---

## Architecture

**Core Service**
The central process that owns the database connection. Exposes a versioned REST API (`/v1/`). All other processes — GUI, MCP server, Automation Host, CLI — are clients of the Core Service. The only process that performs database writes during normal operation. See [ADR-0006](adr/0006-application-architecture.md).

**Micro-kernel architecture**
The design principle where the Core Service is a thin host that enforces contracts (auth, validation, transactions), and business logic is delivered as plugins against the same interfaces available to third-party contributors. There is no privileged distinction between first-party and user-contributed plugins at the *interface* level; which process hosts each plugin type — and why the Core Service loads none — is defined in [ADR-0025](adr/0025-plugin-host-process-matrix.md). See [ADR-0006](adr/0006-application-architecture.md).

**Process isolation**
Each platform component (Core Service, MCP server, GUI, Automation Host, CLI) runs as an independent process communicating over HTTP. No process has privileged access; the Core Service enforces auth and validation uniformly for all clients. See [ADR-0006](adr/0006-application-architecture.md).

**Automation Host**
The fourth launcher-supervised process (after Core Service, MCP server, GUI) and the single execution locus for event-driven plugin code. Hosts `automation` and `notification_channel` plugins, the first-party declarative rule engine, and the watch-folder importer. Subscribes to events via SSE with `Last-Event-ID` replay; acts exclusively through the Core REST API with its own scoped token. See [ADR-0025](adr/0025-plugin-host-process-matrix.md).

**AI client**
Any MCP-compatible application that connects to the MCP server to query health data. This is the correct term — the platform does not reference any specific AI product by name in interfaces, configuration, or documentation. See [ADR-0007](adr/0007-mcp-transport.md).

**Shared TOML config**
A single versioned TOML configuration file read by all processes. Contains: service ports, database path, binding address, log level, plugin directory path, and schedule definitions. It contains no credentials — each client stores its own token separately (see [ADR-0026](adr/0026-named-scoped-tokens.md)). Processes do not hardcode any of these values. See [design-rationale.md](design-rationale.md).

---

## Data Model

**Canonical biomarker name**
The normalized, human-readable *display* label assigned to each biomarker in the `biomarkers` table. Different labs name the same biomarker differently (e.g. "Glucose, Serum" and "Fasting Glucose"); the canonical name gives humans one consistent handle. The machine identifier is the internal `biomarker_id` surrogate key, with `loinc_code` as the standard interoperability identifier — the canonical name is not itself the key ([ADR-0030](adr/0030-biomarker-identity.md)). See [design-rationale.md](design-rationale.md).

**LOINC code**
Logical Observation Identifiers Names and Codes — the healthcare-standard identifier for a laboratory observation. Stored as a nullable `loinc_code` attribute on each biomarker (NULL for biomarkers with no LOINC, e.g. body-composition device metrics and proprietary scores), not as the primary key. Anchors interoperability (FHIR `Observation.code`) and reduces the name-based alias problem. One biomarker concept can map to several LOINC codes (method variants); that cardinality is handled by [ADR-0032](adr/0032-biomarker-loinc-cardinality.md). See [ADR-0030](adr/0030-biomarker-identity.md).

**UCUM**
Unified Code for Units of Measure — the healthcare-standard, machine-parseable encoding for units (e.g. `mg/dL`, `nmol/L`). All units in the platform are stored as UCUM strings; every biomarker has a canonical UCUM unit, and comparisons unit-normalize to it. It is also FHIR's `Quantity.code` system. See [ADR-0031](adr/0031-units-and-ucum.md).

**Value comparator**
The column on a result row (following FHIR's `valueQuantity.comparator`) that marks a censored value: `<`, `<=`, `>=`, or `>`, with NULL meaning an exact value. A below-detection `<0.1` is stored as `value_num = 0.1, comparator = '<'` so it is never conflated with a measured `0.1`; qualitative results use `value_text` instead. See [ADR-0030](adr/0030-biomarker-identity.md).

**Draw context**
The ordering context that explains why a particular set of biomarkers were tested together (e.g. "annual physical", "comprehensive metabolic panel", "Function Health quarterly"). Captured as a field on lab result records. See [design-rationale.md](design-rationale.md).

**Lab source**
The laboratory that processed a blood draw. A first-class attribute on every lab result row — not optional. Required because different labs use different assay platforms that produce systematically different values for the same biomarker (especially immunoassay-based markers like insulin). See [design-rationale.md](design-rationale.md).

**Migration 0001**
The first schema migration file, `sql/migrations/0001_initial_schema.sql` (naming convention per [ADR-0009](adr/0009-database-migration.md)). It creates the complete initial schema, including the `audit_log` table and its immutability triggers ([ADR-0027](adr/0027-audit-trail-and-corrections.md)). The file does not exist yet — "before migration 0001" is used throughout the specs as the milestone by which all schema-shaping decisions must be resolved, since retrofitting them after data exists is far more invasive.

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

## Data Import

**Import pipeline**
The structured path all data takes into the system: adapter parse/validate/normalize, then submission to the bulk import endpoint. It is a *pattern*, not a process — there is no resident import daemon. Imports run as jobs through the Core Service ([ADR-0012](adr/0012-job-abstraction.md)); the one import concern that needs residency, watch-folder importing, runs in the Automation Host ([ADR-0025](adr/0025-plugin-host-process-matrix.md)).

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
An internal component (not a loadable plugin — see [ADR-0025](adr/0025-plugin-host-process-matrix.md)) that bridges the internal event bus to an external protocol. Inbound adapters (HTTP webhook, MQTT) translate external events into internal bus events. Outbound adapters (SSE, ZeroMQ, MQTT) broadcast internal events to external subscribers. See [ADR-0011](adr/0011-event-bus.md).

---

## Jobs

**Job (lightweight)**
An async operation that runs as an asyncio task within the Core Service process. Suitable for small imports and quick analysis. See [ADR-0012](adr/0012-job-abstraction.md).

**Job (heavyweight)**
An async operation that runs as a separate process spawned by the Core Service. Suitable for large backfills and intensive computation. The child process has no direct database access — all writes go through the Core REST API. See [ADR-0012](adr/0012-job-abstraction.md).

---

## Security and Encryption

**Bearer token**
A named, scoped, revocable credential (cryptographically random, minimum 32 bytes, base64url-encoded, `hsp_`-prefixed) required in the `Authorization` header of every HTTP request. The default token set is issued on first run; each client stores only its own token via the OS keychain — never in the shared TOML config. The Core Service stores only hashes. Never appears in URLs, logs, or source code. See [ADR-0026](adr/0026-named-scoped-tokens.md) and [security.md](security.md).

**Secret key**
A randomly generated 32-byte value stored in the OS keychain via `keyring`. One of the two components of the two-factor hybrid key model. Never typed by the user under normal operation. See [ADR-0013](adr/0013-encryption-at-rest.md).

**Master passphrase**
A user-chosen passphrase that is the second component of the two-factor hybrid key model. Known only to the user; never stored by default (except in full auto-unlock mode). See [ADR-0013](adr/0013-encryption-at-rest.md).

**Two-factor hybrid key model**
The key management approach where the database encryption key is derived via Argon2id from two independent components: the secret key (stored in the OS keychain, serving as the Argon2id salt) and the master passphrase (known only to the user). Neither component alone is sufficient to decrypt the database. See [ADR-0013](adr/0013-encryption-at-rest.md); precise construction and rotation in [ADR-0028](adr/0028-key-derivation-and-rotation.md).

**Recovery Kit**
A printable document generated at `healthspan init` containing the secret key as a QR code and Base32 string, with a blank line to handwrite the master passphrase. Enables cross-device database recovery. Useless without the passphrase. See [ADR-0013](adr/0013-encryption-at-rest.md).

**Passphrase-only mode**
An alternative key management mode where the encryption key is derived solely from the passphrase via Argon2id, with a random non-secret salt stored in the `.keyparams` sidecar. No secret key, no OS keychain dependency. Single-factor protection. Fully portable without a Recovery Kit. See [ADR-0013](adr/0013-encryption-at-rest.md) and [ADR-0028](adr/0028-key-derivation-and-rotation.md).

**`.keyparams` sidecar**
A small non-secret plaintext file stored next to the database recording what key re-derivation needs but must not guess: KDF name and version, Argon2id parameters in force for this database, key mode, and (passphrase-only mode) the salt. Created at init, rewritten only by rotation, and copied alongside every backup. See [ADR-0028](adr/0028-key-derivation-and-rotation.md).

**Trust layer**
The security model defines three trust layers: **storage** (the encrypted file — zero-knowledge for the provider), **processing** (the machine running Core Service — must be trusted), **transport** (communication between processes — requires HTTPS if crossing a machine boundary). See [security.md](security.md).

---

## Observability

**Health endpoint**
An HTTP endpoint (`GET /v1/health` on Core Service, `GET /health` on other processes) that returns process readiness status. Used by the launcher to gate startup ordering. See [observability.md](observability.md).

**Request ID**
A UUID assigned to every HTTP request on receipt, included in the response header (`X-Request-ID`) and all associated log entries. Enables correlating log entries across a request lifecycle. See [observability.md](observability.md).

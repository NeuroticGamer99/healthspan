# ADR-0025: Plugin Host-Process Matrix and Core Service Isolation

## Status
Proposed

## Context and Problem Statement
Which process loads which plugin type — and may third-party code ever execute inside the Core Service process?

The existing documents answer this question four different ways:

- [security.md](../security.md) states plugins execute "in the CLI process"
- [ADR-0010](0010-cli-plugin-model.md) has `mcp_tool` plugins registering on `context.mcp` — an object that exists only in the MCP Server process
- [ADR-0011](0011-event-bus.md) makes transport adapters and the scheduler plugins that run inside the Core Service process
- [ADR-0012](0012-job-abstraction.md) runs lightweight jobs — whose handlers are plugin-registered — as asyncio tasks inside the Core Service process
- [ADR-0013](0013-encryption-at-rest.md) guarantees: "Plugins never access the encryption key… The key never crosses the process boundary"

These cannot all be true. ADR-0010's security boundary states that a plugin has access to everything its host process can reach. The Core Service process holds the Argon2id-derived database key and every decrypted query result in memory. If any plugin loads into Core Service, a malicious plugin can read the key and the plaintext database, and ADR-0013's isolation guarantee — the claim the entire two-factor key design presents to users — is silently false.

Each document was written with a different plugin type in mind, and two "plugins" (ADR-0011's transport adapters and scheduler) were never given entries in ADR-0010's plugin type catalog at all — which is exactly how they escaped host-process analysis.

## Decision Drivers
- ADR-0013's plugin-isolation guarantee must be made true **by architecture**, not by auditing plugin code
- Plugins are *invited* code: "install this plugin" is a social-engineering and supply-chain delivery path the platform itself creates and blesses. ADR-0013's threat model excludes same-user malware, but the platform's own supported extension mechanism must never be the exfiltration path
- The micro-kernel principle (ADR-0006) must survive: first-party business logic ships as plugins against the same interfaces available to third parties
- Least privilege per process (security.md): a plugin's blast radius should be bounded by its host process's credentials, which requires host assignments to be nailed down
- Automations must be resident and reliable — "alert me when fasting insulin is high" must not depend on the user remembering to run a command
- Loadability must be explicit in code and enforceable by the loader — no magic values, no behavior implied by the *absence* of a declaration

## Considered Options
1. **Core Service is a plugin-free zone** — no code from the plugins directory ever executes in Core Service; event-driven plugin code runs in a dedicated Automation Host process; plugins influence Core Service only through data
2. **Allow plugins into Core Service and weaken ADR-0013** — keep in-process loading and rewrite the isolation guarantee honestly ("plugins you install can read your encryption key")
3. **Sandbox plugins inside/beside Core Service** — restricted subprocesses with brokered capabilities (the trust-tier design sketched in ADR-0020)

## Decision Outcome
Chosen option: **Option 1 — no plugin code ever executes in the Core Service process.**

The decision has four parts:

1. Two **derivation principles** that generated the host matrix and against which all future plugin types must be classified
2. The **host-process matrix** assigning every plugin type to its host process(es)
3. **Enforcement in code**: a `PluginType` enumeration and explicit per-host allowlists
4. A new fourth supervised process, the **Automation Host**, where event-driven plugin code runs

Option 3 remains the right long-term direction for *relaxing* this decision (running community plugins with brokered, reduced capabilities) — see ADR-0020. It is premature now. Option 2 is rejected: it trades the platform's strongest security claim for implementation convenience.

### Positive Consequences
- ADR-0013's "the key never crosses the process boundary" guarantee becomes architecturally true and stays true without auditing any plugin
- A plugin's maximum capability is exactly its host process's bearer-token scope — composable with per-client scoped tokens (ADR-0026)
- The plugin type catalog becomes complete: everything that uses the plugin interface has an explicit host and an explicit loadability status
- Automations gain an honest reliability story: a supervised resident process with event catch-up, instead of in-process coupling
- The micro-kernel principle survives intact — first-party in-core components still implement the plugin interface; they are simply not *loadable*

### Negative Consequences / Tradeoffs
- One more supervised process (Automation Host) to start, monitor, and document (extends ADR-0008)
- Event-driven plugins pay SSE + REST latency instead of in-process calls — milliseconds, irrelevant at health-data timescales
- Out-of-process event consumption requires delivery guarantees (reconnect + replay) that the in-process bus got for free — a real requirement pushed onto ADR-0011
- Plugin-provided job handlers can never run as in-process asyncio tasks — always child processes (see Job Execution below)
- Analysis results computed by plugins are not directly servable by the Core REST API; clients obtain them via the job system or by hosting the plugin themselves

---

## The Two Derivation Principles

Every host assignment below follows from two principles. They — not the table — are the durable content of this ADR. A future plugin type is placed by applying them; if a proposed design seems to require plugin *code* resident in Core Service, the design is wrong, or it must explicitly supersede this ADR and revisit ADR-0013's guarantee in the same change.

### Principle 1: Plugins influence Core Service through data, never resident code

Anything a plugin contributes to Core Service behavior arrives as **data through the validated REST API** and is stored in the database like any other data. Data is inert: Core Service interprets it with first-party code, and the validation boundary (security.md) applies to it identically to any other write.

Worked examples:
- A `reference_ranges` plugin does not run range-comparison code inside Core Service. It **upserts rows** into the framework range tables (ADR-0005) via the REST API at registration time. "Queryable via the Core REST API" is satisfied because the *data* lives in the database; the comparison logic is first-party Core code.
- Declarative automation rules (trigger/condition/action definitions — ADR-0016) are data. They may be stored and validated by Core Service; they are never *executed* by it (the rule engine runs in the Automation Host).

### Principle 2: Plugins that react to events do so out-of-process

Event-driven plugin code subscribes to the event stream over SSE (`GET /v1/events`) and acts through the REST API — the cross-process subscriber path ADR-0011 already defines. No plugin subscribes to the in-process bus directly, because the in-process bus lives in Core Service memory.

This is why the Automation Host exists: it is the supervised process where event-reactive plugin code (`automation`, `notification_channel`) runs.

---

## Host-Process Matrix

| Plugin type | Host process(es) | Third-party allowed | Mechanism |
|---|---|---|---|
| `cli` | CLI | Yes | Code in-process; acts via REST |
| `import_adapter` | CLI (invocation); job child process (heavyweight execution) | Yes | Parse/validate/normalize client-side; writes via REST bulk import |
| `mcp_tool` | MCP Server | Yes | Code in-process; reads/acts via REST |
| `analysis` | MCP Server, CLI, Automation Host | Yes | Code in-process with its consumer; heavy computation via job child processes |
| `query` | MCP Server, CLI, Automation Host | Yes | Code in-process with its consumer; queries via REST |
| `reference_ranges` | CLI (registration only) | Yes | **Data provider** — upserts framework range rows via REST; no resident code anywhere |
| `automation` | Automation Host | Yes | Subscribes via SSE, acts via REST |
| `notification_channel` | Automation Host | Yes | Subscribes to `alert.*` via SSE; delivers via its channel |
| `provider` | Wherever its consumer loads | Inherits host's rule | Service registry is per-process |
| — *(no loadable type)* | **Core Service** | **No — nothing loads** | See "Internal Components" below |

A composite plugin declaring multiple types (e.g. `["cli", "mcp_tool"]`) is loaded independently by each host whose allowlist intersects its declared types; each host registers only the interfaces it supports. Plugin authors should keep module-level side effects minimal, since the module may be imported by several processes.

### Internal components are not plugin types

The event bus, the scheduler, the transport adapters (SSE, MQTT, ZeroMQ — ADR-0011), and the job orchestrator (ADR-0012) run inside Core Service. Consistent with the micro-kernel principle, they **implement the plugin interface** (`HealthspanPlugin`, `register(context, api_version)`) so their contracts stay uniform and replaceable by contributors *at build time*.

They are **not loadable plugins**:

- They have no `PluginType` enumeration member. A plugin in the plugins directory declaring `PLUGIN_TYPES = ["transport_adapter"]` or `["scheduler"]` fails validation as an unknown type — loudly, by construction.
- They ship inside the `healthspan` package and are imported explicitly by Core Service code. They are never discovered by directory scanning.
- Replacing one means contributing to the Healthspan codebase (or forking), not dropping a file in a directory. This is deliberate: the plugins directory is the *untrusted-code* channel, and Core Service does not consume it.

---

## Enforcement: `PluginType` Enumeration and Per-Host Allowlists

Loadability is explicit in code. There are no magic values and no behavior implied by absence.

```python
import enum

class PluginType(enum.StrEnum):
    """Every loadable plugin type. Internal Core Service components
    (event bus, scheduler, transport adapters, job orchestrator) are
    deliberately absent: they are not loadable. See ADR-0025."""
    CLI                  = "cli"
    MCP_TOOL             = "mcp_tool"
    IMPORT_ADAPTER       = "import_adapter"
    ANALYSIS             = "analysis"
    REFERENCE_RANGES     = "reference_ranges"
    QUERY                = "query"
    AUTOMATION           = "automation"
    NOTIFICATION_CHANNEL = "notification_channel"
    PROVIDER             = "provider"
```

Every process that embeds the plugin loader declares an explicit allowlist, defined in one place:

```python
HOST_LOADABLE_TYPES: dict[Host, frozenset[PluginType]] = {
    Host.CLI: frozenset({
        PluginType.CLI, PluginType.IMPORT_ADAPTER, PluginType.REFERENCE_RANGES,
        PluginType.ANALYSIS, PluginType.QUERY, PluginType.PROVIDER,
    }),
    Host.MCP_SERVER: frozenset({
        PluginType.MCP_TOOL, PluginType.ANALYSIS, PluginType.QUERY,
        PluginType.PROVIDER,
    }),
    Host.AUTOMATION_HOST: frozenset({
        PluginType.AUTOMATION, PluginType.NOTIFICATION_CHANNEL,
        PluginType.ANALYSIS, PluginType.QUERY, PluginType.PROVIDER,
    }),
    Host.JOB_CHILD: frozenset({
        PluginType.IMPORT_ADAPTER, PluginType.ANALYSIS,
        PluginType.QUERY, PluginType.PROVIDER,
    }),
    Host.CORE_SERVICE: frozenset(),   # explicitly empty — see ADR-0025
}
```

Enforcement rules:

1. The loader validates every string in a plugin's `PLUGIN_TYPES` against the `PluginType` enumeration. An unknown type **fails that plugin's load with a clear error** naming the unknown type and listing the valid ones. It is not skipped silently.
2. The loader loads a plugin only if `PLUGIN_TYPES ∩ LOADABLE_TYPES` for the current host is non-empty, and registers only the intersecting interfaces.
3. `Host.CORE_SERVICE` maps to `frozenset()` — an explicit, greppable, testable empty set. It exists precisely so that "Core Service loads nothing" is a stated fact in code rather than an omission.
4. Defense in depth: Core Service **does not import the plugin loader module at all**. The empty allowlist is a declaration; the absent import is the mechanism. A CI test asserts both — that `HOST_LOADABLE_TYPES[Host.CORE_SERVICE]` is empty, and that the Core Service package has no import path to the loader.

The job child (ADR-0012's heavyweight execution process) embeds the loader in **single-plugin mode**: it loads exactly the plugin that registered the executing job type's handler — never a directory scan — and the `Host.JOB_CHILD` allowlist bounds which plugin *types* may ever execute there. `automation` and `notification_channel` are deliberately absent: both are Automation Host residency concerns (a long-lived SSE subscriber and a delivery channel respectively), not batch job execution. The allowlist governs what code may load; the child's credential is unchanged — the ephemeral single-job token of ADR-0026, never a standing credential.

---

## The Automation Host

The fourth launcher-supervised process (extending ADR-0008's Core Service + MCP Server + GUI). It is the single execution locus for event-driven plugin code.

### Contract

- **Loads:** `automation`, `notification_channel`, `analysis`, `query`, `provider` plugins from the plugins directory
- **Also hosts (first-party, shipped in the `healthspan` package):**
  - the declarative rule engine — interprets trigger/condition/action rules (ADR-0016); rules are data, the engine is code, and the code runs here, not in Core Service
  - the watch-folder importer — the one import concern that genuinely needs residency (see review item 1.D); a watch-folder import *is* an automation: trigger = file appears, action = submit import job. It holds the dedicated `watch-import` token (`jobs import`) rather than the process credential ([ADR-0026](0026-named-scoped-tokens.md)). Post-import file handling is specified below.
- **Subscribes:** `GET /v1/events` (SSE). On reconnect it sends the standard SSE `Last-Event-ID` header; Core Service replays events after that ID from a retained window (requirement levied on ADR-0011, below). If replay begins with a `gap` marker, the host must reconcile — re-query recent imports and job states via REST — before resuming normal trigger processing (ADR-0011). The host persists its last-processed event ID locally — the full epoch-qualified SSE ID ([ADR-0011](0011-event-bus.md), review item 2.2), so a cursor from a prior Core Service run is detected as an epoch mismatch and answered with an all-partition `gap`, never a silently empty replay (the cursor file contains only the event ID — no health data).
- **Acts:** exclusively via the Core REST API with its own bearer token, carrying `read`, `events`, and `jobs` scopes — never `admin` (ADR-0026). The sole exception to the shared process credential is the first-party watch-folder importer's dedicated `watch-import` token (`jobs import`), which keeps the mass-mutation `import` scope out of the credential handed to directory-loaded plugins.
- **Never:** opens the database, touches the encryption key, or loads code on behalf of Core Service.

### Watch-folder post-import handling

The watch folder is the platform's one *unattended* import flow (review 2026-07-07 item 2.4), so the two policies that assume a human at a prompt — [ADR-0034](0034-clinical-document-storage.md)'s source-disposal offer and [ADR-0033](0033-plaintext-artifact-disposal.md)'s dispose/keep prompt — need an unattended answer, and something must prevent the importer from re-triggering forever on a file that stays in the watched directory (the dedup/conflict policy protects the *data*; the trigger loop is a separate problem).

The watch-folder configuration declares a post-import action, applied when a file's import job reaches a terminal **success** state — including a dedup no-op success, or duplicate drops would loop:

- **`move` (default)** — the file is atomically renamed into a `processed/` subfolder of the watched directory (same filesystem, so the rename is atomic; a name collision gains a timestamp suffix). Loop prevention is structural: only the watched root triggers, and subfolders are never scanned. The user's file is kept.
- **`keep`** — the file stays in place; the importer tracks content hashes (the same SHA-256 identity as ADR-0034's `source_file_hash`) and never re-submits unchanged content. Changed content under the same name is a new document and does re-trigger. The tracking file contains hashes only — no health data (the same discipline as the SSE cursor file above).
- **`dispose`** — best-effort disposal per ADR-0033 after verified success, with the honest caveat and the full-disk-encryption recommendation printed to the log (there is no TTY). The config declaration *is* the explicit choice ADR-0033's rule 6 demands of non-interactive runs — the unattended analog of `--dispose-plaintext`. Defaulting to `move` does not violate that rule: rule 6 refuses to guess between two choices that both have teeth (destroy vs. retain plaintext); `move` destroys nothing and leaks nothing new.

Uniform across all three modes: a file whose import job **fails** is never disposed — it moves to a `failed/` sibling subfolder, visible and out of the trigger path (a poison file cannot loop), with the failure surfaced through normal job history and `job.*` events; disposal only ever follows verified success (ADR-0034's ordering). A file whose import job is still in flight is not re-submitted. And stated honestly: under `move` and `keep`, the watch directory tree holds plaintext health exports indefinitely — user-custody plaintext like any export, with ADR-0033's full-disk-encryption backstop as the standing recommendation.

### Lifecycle

- Started by the launcher after Core Service is healthy; stoppable/startable independently (`healthspan automations start`, `healthspan automations status`)
- If the Automation Host is down, automations do not fire — honestly and visibly (status command, `system.*` events, GUI indicator — the launcher's supervision reports become Core-emitted `system.process.*` events, [ADR-0042](0042-process-supervision-and-single-instance-locking.md)), not silently. On restart it resumes from its cursor and processes the replayed window, so brief outages do not lose triggers whose events are still retained.
- Execution tracing per ADR-0016 remains a requirement on the automation engine and is unaffected by where the engine runs.

### Why a resident daemon rather than a CLI mode

Automations are inherently resident — a rule that only fires while the user remembers to run a command is a correctness lie. Supervision, an SSE cursor, and event replay give a bounded, honest reliability story. The marginal cost (one more supervised subprocess) is trivial at personal scale, and the process boundary is exactly what makes third-party automation code compatible with Core Service isolation.

---

## Job Execution (constraint on ADR-0012)

ADR-0012's lightweight execution model — asyncio tasks inside Core Service — would carry plugin-registered handler code into Core Service through the back door. Therefore:

- **Plugin-provided job handlers always use the heavyweight model**: a separate child process. Only first-party internal handlers may run as in-process asyncio tasks.
- Job child processes are created with the **`spawn` start method, explicitly, on all platforms** — never `fork`. A forked child inherits a copy of the parent's memory, including the derived encryption key; a spawned child starts from a fresh interpreter. (On Linux, `fork` is the historical default in Python ≤ 3.13; do not rely on defaults.)
- A job child receives an ephemeral, single-job bearer token for REST access (ADR-0026). It never receives the key and never opens the database.

---

## Security Invariants

These four invariants summarize this ADR's contract together with ADR-0013's. They are mirrored in [security.md](../security.md) as standing requirements. **A future ADR that touches one of these must cite it by number and explicitly supersede or extend the ADR that establishes it** — this makes breaking the reasoning a review-visible event rather than an accident.

| # | Invariant | Why |
|---|---|---|
| INV-1 | The derived key exists only in the memory of the single process currently holding the database open — the Core Service at runtime, or the CLI during an explicitly invoked `db`/`keys` maintenance command run while Core Service is stopped. It is never transmitted, logged, or inherited by child processes (spawn, not fork). | The key is the single secret the entire encryption-at-rest story rests on (ADR-0013). |
| INV-2 | Core Service never executes code from the plugins directory. First-party in-core components ship inside the `healthspan` package and are imported explicitly. | The plugins directory is the platform's invited-code channel; keeping it out of the key-holding process makes ADR-0013's plugin isolation true by architecture. |
| INV-3 | A plugin's maximum capability is its host process's credentials. | Bounds the blast radius of any malicious or compromised plugin to a knowable, revocable token scope (refined by ADR-0026's credential tiers: directory-loaded plugins are handed the plugin-tier token). |
| INV-4 | Plugins alter Core Service behavior only via data submitted through the validated REST API. | Data is inert and validated at the boundary (security.md); code is not. This is Principle 1 in enforceable form. |

---

## Consequences for Other Documents

Conforming edits required by this ADR (all target documents are Proposed or freely editable, except where noted):

- **ADR-0011** (Proposed): reclassify transport adapters and the scheduler as internal components (not loadable plugins); add the event replay window and `Last-Event-ID` reconnect semantics; name the Automation Host as the canonical cross-process subscriber
- **ADR-0012** (Proposed): plugin-provided handlers always heavyweight; explicit `spawn`; scoped token for children
- **ADR-0016 / ADR-0017** (stubs): host process is the Automation Host; declarative rules are data (engine is first-party); code automations are `automation` plugins
- **security.md**: replace "plugins execute in the CLI process" with the host matrix; add the Security Invariants section
- **ADR-0010** (Accepted): navigation link only — `Extended by ADR-0025`
- **ADR-0008** (Accepted): navigation link only — `Extended by ADR-0025` (fourth supervised process)
- **README / observability.md diagrams**: the fourth process box is the Automation Host, not an "Import Pipeline" daemon (resolves review item 1.D alongside ADR-0012's job-based imports)
- **ADR-0020**: trust tiers + sandboxing remain the mechanism by which a future ADR may *relax* this matrix (community plugins with brokered capabilities); any such change must supersede or extend this ADR and address INV-2 explicitly

## Pros and Cons of the Options

### Option 1 — Core Service is a plugin-free zone (chosen)
- Pro: ADR-0013's guarantee holds by architecture; zero reliance on plugin audits
- Pro: complete, enforceable type catalog; explicit allowlists; testable in CI
- Pro: micro-kernel principle preserved via build-time-replaceable internal components
- Con: one more supervised process; event replay requirement on ADR-0011; plugin job handlers always pay subprocess overhead

### Option 2 — allow plugins in Core Service, weaken ADR-0013
- Pro: simplest implementation; in-process latency for automations
- Con: converts the platform's strongest security claim into "trust every plugin you install"; a single malicious plugin exfiltrates the key and the plaintext database
- Con: contradicts the trust model users are told (two-factor key, zero-knowledge storage) in a way most users cannot evaluate

### Option 3 — sandboxed plugins inside/beside Core Service
- Pro: eventually enables community plugins with reduced capabilities (ADR-0020 trust tiers)
- Con: sandbox design, capability brokering, and promotion paths are a large project; premature before the ecosystem exists
- Con: even a sandboxed in-process plugin shares an address space with the key — the honest sandbox is a separate process, which is what Option 1 already builds

## Links
- Extends: [ADR-0010](0010-cli-plugin-model.md) — completes the plugin type catalog with host assignments and loadability enforcement
- Extends: [ADR-0008](0008-process-lifecycle.md) — adds the Automation Host as the fourth supervised process
- Preserves: [ADR-0013](0013-encryption-at-rest.md) — makes the "Plugin isolation" guarantee architecturally true
- Constrains: [ADR-0011](0011-event-bus.md), [ADR-0012](0012-job-abstraction.md), [ADR-0016](0016-automation-plugin-type.md), [ADR-0017](0017-notification-channels.md)
- Extends: [ADR-0006](0006-application-architecture.md) — process isolation and the micro-kernel principle; replaces the Import Pipeline daemon in the process roster with the Automation Host
- Related: [ADR-0020](0020-plugin-registry.md) — trust tiers as the future relaxation path
- Related: [ADR-0026](0026-named-scoped-tokens.md) — named scoped tokens; per-host credentials and credential tiers
- Related: [ADR-0042](0042-process-supervision-and-single-instance-locking.md) — supplies the restart-with-backoff mechanism that makes this ADR's "supervised resident process" language honest for the Automation Host
- Related: [ADR-0033](0033-plaintext-artifact-disposal.md) — disposal policy behind the watch folder's `dispose` action; its rule 6 (non-interactive runs must choose) is why the action is config-declared
- Related: [ADR-0034](0034-clinical-document-storage.md) — the source-disposal offer whose unattended form is the watch folder's config-declared post-import action
- Related: [specs/security.md](../security.md) — Security Invariants
- Resolves: [architecture review 2026-06-10](../architecture-review-2026-06-10.md), items 1.A and 3.H

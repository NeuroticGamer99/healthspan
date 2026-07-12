# ADR-0049: Core-Service-Skeleton Implementation Decisions (Phase 2 WI-1)

## Status
Proposed

## Context and Problem Statement
Implementing the Phase-2 Core Service skeleton — the FastAPI process, its direct-start entry (`healthspan service start`), the unauthenticated liveness endpoint, single-instance locking, and structured logging — surfaces decisions the governing ADRs deliberately leave open, plus one new runtime dependency that needs a spec record:

- [ADR-0006](0006-application-architecture.md) names FastAPI and lists "service ports, binding address, log level" as shared-config content, but fixes no `[service]` section shape or defaults. It is Accepted and immutable; [ADR-0046](0046-filesystem-layout-and-config-discovery.md) owns the config schema and is also Accepted. New config keys and their defaults therefore need a home per the config-defaults-in-the-owning-ADR pattern ([CLAUDE.md](../../CLAUDE.md), rule 4), and since both owners are Accepted that home is a new Proposed ADR.
- [observability.md](../observability.md) requires JSON-structured logs with named fields but names no logging library. Selecting one is a new dependency ([CLAUDE.md](../../CLAUDE.md), rule 1).
- [ADR-0040](0040-health-endpoint-authentication.md) requires the liveness endpoint to carry "a plain per-source-address request-rate cap" but fixes no number.
- [ADR-0039](0039-startup-sequence-and-passphrase-handoff.md) specifies five passphrase deployment variants; Phase 2's minimum needs an explicit statement of *which* land now.

Following the [ADR-0047](0047-crypto-surface-implementation-decisions.md) precedent, the accumulated WI-1 decisions land as one batched Proposed ADR in the same PR as the implementing change.

## Decision Drivers
- Every externally observable contract (config keys a user edits, the log shape a parser consumes, the port a client connects to) must be recoverable from the specs without reading source
- Dependency additions are supply-chain surface (the pip-audit gate, [ADR-0045](0045-repository-workflow-and-ci-enforcement.md)); each is a deliberate, recorded choice
- Phase 2 is the Core Service *minimum* — decisions defer everything a later phase owns rather than pulling it forward (the launcher, ADR-0039; supervision, ADR-0042; full-auto-unlock, ADR-0013)
- Config stays minimal and strict — the ADR-0046 unknown-key-rejecting posture extends to the new section unchanged

## Considered Options
1. Record each decision in its own small ADR
2. **One batched Proposed ADR for the WI-1 Core-Service skeleton** (chosen)

## Decision Outcome
Chosen: **option 2** — the decisions share one context (the WI-1 implementation) and one review, matching the ADR-0047 batched-extension pattern.

### 1. Structured-logging backend: `structlog` (new dependency)
Structured logging is built on **`structlog`** (new runtime dependency) rather than a hand-rolled `logging.Formatter`. It renders **JSON to stdout** and carries the [observability.md](../observability.md) required fields on every entry: `timestamp` (ISO-8601 UTC, millisecond precision, `Z`-suffixed), `level`, `process` (the process name, `core_service` for the Core Service), `message`, and `request_id`. The `request_id` is carried in a `contextvars.ContextVar` bound per request by the Core Service's request-ID middleware ([observability.md](../observability.md) request tracing), so log calls need not thread it manually. structlog integrates with the stdlib `logging` root so uvicorn's and third-party libraries' records pass through the same JSON renderer and never emit a second, unstructured format to the same stream. The configured minimum level comes from `[logging] level` (ADR-0046). The choice buys contextvar binding and a processor pipeline that the health-data-in-logs discipline ([security.md](../security.md); the canary gate, [testing-strategy.md](../testing-strategy.md)) builds on; the cost is one pinned, hash-verified dependency, accepted with eyes open.

### 2. The `[service]` configuration section
The shared config ([ADR-0006](0006-application-architecture.md), [ADR-0046](0046-filesystem-layout-and-config-discovery.md)) gains an optional `[service]` table, parsed under the same strict, unknown-key-rejecting rules as every other section:

| Key | Type | Default | Meaning |
|---|---|---|---|
| `host` | string | `"127.0.0.1"` | Bind address for the Core Service HTTP listener |
| `port` | integer | `8464` | Core Service listener port |
| `passphrase_file` | string (path) | *(unset)* | Optional path to an OS-secret-facility file holding the master passphrase ([ADR-0039](0039-startup-sequence-and-passphrase-handoff.md) channel c); relative paths resolve against the config-file directory, per ADR-0046 |

**Binding posture.** The default `host` is `127.0.0.1` — loopback only, the local-first default ([ADR-0006](0006-application-architecture.md), [security.md](../security.md)). A LAN binding (`0.0.0.0` or a specific interface) is an explicit, user-typed opt-in; the platform never binds beyond loopback by default. `port` covers the Core Service alone; other processes (the MCP Server, Phase 4) add their own keys when they arrive rather than presuming a shape now.

### 3. Default Core Service port: `8464`
`8464` is the default listener port — in the registered range, clear of the common-collision ports (3000, 5000, 8000, 8080). It is a default, not a constant: `[service] port` overrides it, and no other component hardcodes it.

### 4. Liveness rate cap
The unauthenticated `GET /v1/health` liveness endpoint ([ADR-0040](0040-health-endpoint-authentication.md)) carries a fixed per-source-address request-rate cap of **30 requests per rolling 1-second window**; requests beyond it receive `429`. The window is generous for every legitimate poller at one address combined — the launcher, a Docker `HEALTHCHECK`, a systemd watchdog, and the GUI all poll from `127.0.0.1` on the default deployment — while bounding an unauthenticated flood against the key-holding process. The cap is an internal constant, not config surface; a deployment that needs it tuned is a revisit trigger, not a knob shipped speculatively. Because liveness answers from a cached readiness flag (no database work, ADR-0037), the cap protects against request volume, not query cost.

### 5. Phase-2 passphrase-channel scope (of ADR-0039's five variants)
Phase 2 ships [ADR-0039](0039-startup-sequence-and-passphrase-handoff.md)'s **direct-start Core Service** (variant 2) only, over the three interactive channels: an interactive **TTY prompt** (`getpass`), a **stdin pipe** (one line, then closed), and a **`passphrase_file`** (config key above or `--passphrase-file` flag). A start finding none of these available fails with an error listing them — never an environment-variable fallback. Deferred, with their owning work:
- **The launcher** (ADR-0039 variant 1, migration ownership + downward stdin handoff) → Phase 4, when the MCP Server makes a second process to orchestrate. Phase 2 keeps migration ownership with the operator: `service start` refuses on a stale `schema_version` and names `healthspan db migrate`, exactly as ADR-0039 specifies for direct start.
- **Full-auto-unlock mode** (ADR-0039 variant 5; [ADR-0013](0013-encryption-at-rest.md)) → Phase 6, where it is the zero-touch reliability path paired with supervised restart ([ADR-0042](0042-process-supervision-and-single-instance-locking.md) gates unattended key-holder restart on exactly this mode). It is spec-only today; both current key modes (two-factor, passphrase-only) prompt.
- **systemd and Docker** credential files (variants 3–4) are the `passphrase_file` channel with an orchestrator-managed path; the deployment snippets are documentation, deferred to the distribution milestone.

### 6. The single-instance lock guards the sanctioned direct-database commands
The Phase-2 single-instance advisory lock ([ADR-0042](0042-process-supervision-and-single-instance-locking.md)) on `<database-path>.lock` is what the three sanctioned direct-database commands ([ADR-0006](0006-application-architecture.md)) — `healthspan db migrate`, `db backup`, and `db restore` — consult to satisfy the "refuse while Core Service is up" rule ([ADR-0033](0033-plaintext-artifact-disposal.md) §db-encrypt names `db migrate` and the rotation commands; [ADR-0038](0038-backup-execution-and-verification.md) names backup/restore). Each acquires the advisory lock fail-fast before prompting, holds it for its duration, and refuses if it is held (the Core Service, or another instance, owns the database) — a shared `exclusive_database_access` helper. This resolves the [open-questions.md](../open-questions.md) Operations entry deferring the backup/restore guard from Phase 1.

Two related items stay deferred, deliberately:
- **The `keys` rotation commands** (`change-passphrase`, `rotate-secret-key`, `convert-mode`) should acquire the same guard per [ADR-0033](0033-plaintext-artifact-disposal.md), but they live in the crypto CLI that WI-2 revisits (token minting at `init`); the guard is applied to them there, tracked in [open-questions.md](../open-questions.md).
- **The `psutil` PID-identity refinement** ([ADR-0042](0042-process-supervision-and-single-instance-locking.md)) is not pulled in: the kernel advisory lock is the correctness guarantee, the sentinel records the holder PID only for a human-readable message, and the reused-PID hygiene check lands with the supervision work (Phase 6). The rekey crash-durability `fsync` barrier ([ADR-0047](0047-crypto-surface-implementation-decisions.md) §7) stays deferred too.

### 7. OpenAPI schema and docs UIs are disabled in Phase 2
The Core Service app is built with `openapi_url=None`, `docs_url=None`, `redoc_url=None` — FastAPI's auto-generated `/openapi.json`, `/docs` (Swagger), and `/redoc` are **off**. Two reasons: they would be additional unauthenticated routes (violating the "exactly one `public` route" property decision 4/§enforcement rests on), and an unauthenticated `/openapi.json` publishes the full API surface — fingerprinting material the same driver in [ADR-0040](0040-health-endpoint-authentication.md) ("minimize what an unauthenticated caller can learn") argues against. How to expose API docs *securely* (behind auth, or `monitor`-gated) is a WI-2 question decided with the auth layer; [api-reference.md](../api-reference.md) remains the design-time API reference in the meantime. This does not contradict api-reference.md's note that FastAPI "will produce" OpenAPI at runtime — it defers *when* and *behind what auth*, not *whether*.

### Positive Consequences
- The log shape, the config keys, the port, and the passphrase channels are all recoverable from the specs, not the source
- Config stays minimal and strict; the new section inherits ADR-0046's unknown-key rejection with no new parsing posture
- The Phase-2 scope is explicit about what it defers and to which phase, so the launcher/supervision/full-auto-unlock work is not accidentally pulled forward or forgotten
- One new dependency, recorded with its rationale and subject to the pip-audit gate

### Negative Consequences / Tradeoffs
- `structlog` is a new supply-chain link — mitigated by pinning + hash verification (the lockfile discipline) and the pip-audit gate, and recorded here rather than discovered later
- The liveness rate cap being a constant means a many-poller deployment cannot tune it without a code change — accepted at single-user scale, named as a revisit trigger
- The default port is one more value a second client must know — mitigated by its being config-discoverable (`healthspan config show`)

## Consequences for Other Documents
- **[api-reference.md](../api-reference.md)**: the `GET /v1/health` liveness row is confirmed as implemented (status word, `public`, unauthenticated); the rate cap is noted
- **[open-questions.md](../open-questions.md)**: the Operations entry for the `db backup`/`db restore` service-up guard and `.lock` acquisition moves to Resolved (decision 6); the Recovery-Kit orphan-sweep entry moves to Resolved for the sweep hook (the print pathways remain deferred); the rekey `fsync` barrier stays open
- **[development-plan.md](../development-plan.md)**: the Phase-2 "launcher stays minimal in this phase (foreground execution)" wording is corrected to direct-start-only, launcher deferred to Phase 4 (decision 5)
- **[ADR-0046](0046-filesystem-layout-and-config-discovery.md)**: navigation link — the config gains a `[service]` section (its keys and defaults live here)

## Links
- Extends: [ADR-0006](0006-application-architecture.md) — supplies the `[service]` config-section shape and defaults ADR-0006 leaves open
- Extends: [ADR-0046](0046-filesystem-layout-and-config-discovery.md) — the `[service]` section parsed under its strict discovery/validation rules
- Related: [ADR-0037](0037-core-service-concurrency-and-driver.md) — the concurrency model the skeleton is built to (async liveness off a cached flag; sync repository + threadpool bridge for later WIs)
- Related: [ADR-0039](0039-startup-sequence-and-passphrase-handoff.md) — the passphrase channels; decision 5 records the Phase-2 subset and the launcher/full-auto-unlock deferrals
- Related: [ADR-0040](0040-health-endpoint-authentication.md) — the liveness exemption and rate-cap requirement decision 4 gives a number
- Related: [ADR-0042](0042-process-supervision-and-single-instance-locking.md) — the single-instance advisory lock; decision 6 makes it the backup/restore service-up guard
- Related: [ADR-0033](0033-plaintext-artifact-disposal.md) — the orphan-plaintext sweep whose startup hook lands here (print pathways deferred)
- Related: [observability.md](../observability.md) — the structured-logging fields decision 1 renders
- Related: [ADR-0045](0045-repository-workflow-and-ci-enforcement.md) — the pip-audit gate covering the new dependency

# ADR-0040: Health Endpoint Authentication — the Liveness Exemption and the Monitor Scope

## Status
Proposed

## Context and Problem Statement

The platform's documents contradict each other about health endpoints:

- [api-reference.md](../api-reference.md) and [security.md](../security.md): every endpoint requires a bearer token — *"No endpoint is unauthenticated, including health and metrics endpoints."*
- [observability.md](../observability.md): the launcher polls each process's health endpoint to gate startup order — but [ADR-0026](0026-named-scoped-tokens.md)'s default token set contains no launcher credential, and nothing says which token the launcher presents. The MCP Server compounds it: its endpoints authenticate via the separate client-facing secret, which the launcher also does not hold.
- Docker Compose healthchecks and systemd watchdog timers have the same problem with no credential story at all.

Additionally, ADR-0026 requires every REST route to declare its required scope, and `/v1/health` and `/v1/metrics` have none assigned.

Who may ask "are you up?", with what credential, and what does the answer reveal? (Architecture review 2026-07-06, item 1.E.)

## Decision Drivers

- The launcher, Docker `HEALTHCHECK`, and systemd watchdogs must all be able to poll readiness — simultaneously, without per-orchestrator special cases
- [ADR-0039](0039-startup-sequence-and-passphrase-handoff.md)'s channel discipline: secrets never in argv or environment variables, because both are inspectable. A Docker `HEALTHCHECK` line and a systemd healthcheck `ExecStart` **are argv** — visible in `docker inspect` and `systemctl show`
- ADR-0026's storage rule: no token in any shared config file — a compose file or unit file is exactly such a file
- Minimize what an unauthenticated caller can learn: version strings and `schema_version` are fingerprinting material
- An unauthenticated endpoint on the key-holding process must not be a lever for making it do work
- Observability tooling (a metrics scraper, an uptime monitor) should be grantable ops visibility without being granted health-data access — the INV-5 blast-radius principle
- Every route must declare its scope (ADR-0026); an exemption must be declared, not omitted

## Considered Options

1. **Minimal unauthenticated liveness endpoint** — `200`/`503` and a status word only; all detail moves behind auth
2. **Credentialed health checks** — the launcher uses `cli-admin` (it is the CLI's sibling); Docker/systemd deployments get a documented low-scope token
3. **No HTTP liveness at all** — process-level signals (`sd_notify`, exit codes, pid checks) replace HTTP polling

## Decision Outcome

Chosen option: **Option 1 — minimal unauthenticated liveness, detail behind an authenticated endpoint gated by a new `monitor` scope.**

Option 2 fails on secret distribution: every place the healthcheck credential would live — a `HEALTHCHECK CMD curl -H "Authorization: …"` line, a systemd unit's `ExecStart`, a compose file — is an inspectable surface. ADR-0039 spent an entire decision banning exactly this for the passphrase, and ADR-0026 banned tokens in shared config files; issuing a token whose *documented storage location* is a compose file would reverse both. Option 1 removes the secret-distribution problem instead of managing it, and is the industry-standard shape (Kubernetes probes, Docker healthchecks, and load-balancer checks all assume an unauthenticated status endpoint).

Option 3 fails the same drivers from the other side: Docker and orchestrator convention is HTTP; the launcher's readiness gating (observability.md) needs a readiness answer, not just "process exists"; and `sd_notify` is systemd-only.

### The liveness endpoints

`GET /v1/health` (Core Service) and `GET /health` (MCP Server, Automation Host, and any future HTTP process) are **the platform's only unauthenticated endpoints**. The response is a status word and nothing else:

```
200 {"status": "ok"}
503 {"status": "unavailable"}
```

- **`200` means ready to serve**, not merely alive: for the Core Service, the database is open and queryable. This is what the launcher's startup gating and a Docker healthcheck actually need to know. `503` covers everything else; the *reason* is not disclosed.
- **No version, no `schema_version`, no uptime, no error detail, no product name in the body.** What the endpoint leaks — a service exists on this port and is up or down — is already observable to anything that can `connect()`, and the default binding is `127.0.0.1`.
- **O(1) and allocation-free in spirit**: the handler reports a cached readiness flag the service maintains through normal operation — never a per-request database query. An unauthenticated caller must not be able to make the key-holding process do meaningful work.
- **Exempt from bearer-token verification, not from rate limiting**: the generic per-source-address limits still apply.

### The detail endpoint

The rich health response previously specified in observability.md moves to:

```
GET /v1/health/detail        (requires: monitor)
```

```json
{
  "status": "healthy",
  "version": "1.0.0",
  "schema_version": 5,
  "db_connected": true,
  "uptime_seconds": 3600
}
```

Version strings and `schema_version` identify what software, at what revision, holds the health data — fingerprinting material that has no business being free.

### The `monitor` scope

ADR-0026's flat scope list gains a seventh entry:

| Scope | Grants |
|---|---|
| `monitor` | `GET /v1/health/detail` and `GET /v1/metrics` — read-only operational metadata, never health data |

The tempting alternative was reusing `read`, but `read` grants biomarker queries: a metrics scraper or uptime monitor given `read` so it can see `/v1/metrics` could also read lab results. `monitor` keeps observability tooling structurally outside health data — a user runs `healthspan token create metrics-scraper --scopes monitor` and the resulting credential can never touch a biomarker. This is INV-5's blast-radius principle applied to ops tooling.

Default holders: **`cli-admin` and `gui`** gain `monitor` (CLI diagnostics and the GUI status page keep working with no user action). **`mcp` does not** — the AI client has no default need for ops detail, and read-only-minimal is that token's design stance (ADR-0026); a user who wants the AI client to answer "what version am I running" issues a token carrying `monitor` deliberately.

`/v1/metrics` was previously scope-less like the health endpoint; it now requires `monitor`. Its payload (request counts, status-code histogram, job counts) is operational metadata, but it profiles usage patterns of a health database — authenticated is the right default.

### The exemption is declared, not omitted

ADR-0026 requires every route to declare its required scope(s). The liveness route declares an explicit **`public`** marker — a named declaration in the route definition, not an absent one. Consequences:

- "Exactly one `public` route per process" is a grep-able, testable property (testing-strategy.md security tests; the docs-consistency generation test planned in review item 4.B)
- An endpoint added without any scope declaration remains a hard error — forgetting to declare is still impossible; *choosing* `public` is visible in review

### What each poller does

| Poller | Endpoint | Credential |
|---|---|---|
| Launcher startup gating (ADR-0008/0039) | each process's liveness endpoint | none |
| Docker Compose `HEALTHCHECK` | `GET /v1/health` | none |
| systemd watchdog / healthcheck timer | `GET /v1/health` | none |
| GUI status page, CLI diagnostics | `GET /v1/health/detail`, `/v1/metrics` | `gui` / `cli-admin` (carry `monitor`) |
| User-installed scraper or uptime monitor | `GET /v1/health/detail`, `/v1/metrics` | user-minted token with `monitor` only |

### Positive Consequences

- The launcher, Docker, and systemd all gain a working, uniform readiness story with zero secret distribution
- The api-reference/observability contradiction is resolved; "no endpoint is unauthenticated" becomes a precisely true statement with one named, argued exemption
- Fingerprinting detail (version, `schema_version`) moves behind auth — stricter than the status quo, which served it on the (nominally authenticated, practically credential-less) health endpoint
- Observability tooling gets a minimal-privilege credential shape; granting ops visibility no longer means granting health-data access
- `/v1/health` and `/v1/metrics` now have explicit scope assignments, closing ADR-0026's declare-every-route gap

### Negative Consequences / Tradeoffs

- Any local process (or LAN peer, in an opt-in LAN deployment) can observe that the service exists and whether it is up — accepted: this is port-scan-visible anyway, and the body identifies nothing
- A seventh scope to document and enforce — mechanical, one-time
- Two health endpoints (liveness + detail) instead of one — the split is the point: the free one says nothing
- The `mcp` token cannot read version/uptime by default — deliberate; revisit only if an MCP diagnostics tool proves genuinely useful

## Pros and Cons of the Options

### Option 1 — minimal unauthenticated liveness (chosen)
- Pro: no secret in any compose file, unit file, or launcher config; industry-standard probe shape; strictly less disclosure than today's rich health payload
- Pro: works identically for every poller, present and future
- Con: one deliberate exception to "every endpoint authenticates" — bounded to a status word and made testable via the `public` marker

### Option 2 — credentialed health checks
- Pro: preserves the unqualified "no endpoint is unauthenticated" claim
- Con: the credential must live in argv or a shared config file — the surfaces ADR-0039 and ADR-0026 banned; `docker inspect` and `systemctl show` would print it
- Con: per-orchestrator distribution and rotation ceremony for a credential that guards a status word
- Con: the launcher polling the MCP Server would additionally need the client-facing secret, entangling two credential systems

### Option 3 — no HTTP liveness (process signals)
- Pro: nothing to expose
- Con: Docker healthchecks and readiness-gated startup need an in-band readiness answer; `sd_notify` is systemd-only; exit codes say "alive," not "ready"

## Links

- Extends: [ADR-0026](0026-named-scoped-tokens.md) — adds the `monitor` scope, amends the default token set (`cli-admin`, `gui`), and adds the `public` route-declaration marker
- Related: [ADR-0039](0039-startup-sequence-and-passphrase-handoff.md) — the argv/env channel discipline that rules out option 2; the launcher polls liveness after passphrase handoff
- Related: [ADR-0008](0008-process-lifecycle.md) — launcher readiness gating
- Related: [specs/observability.md](../observability.md) — health endpoint and metrics specifications
- Related: [specs/security.md](../security.md) — Authentication section wording
- Resolves: [architecture review 2026-07-06](../architecture-review-2026-07-06.md), item 1.E

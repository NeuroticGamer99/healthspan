# ADR-0026: Named Scoped Bearer Tokens

## Status
Proposed

## Context and Problem Statement
Authentication today ([security.md](../security.md)) is a single bearer token, generated on first run, stored in the shared TOML config, and used by every client: GUI, MCP Server, CLI, plugins, job children, and the inbound webhook. Consequences:

- The MCP server — and therefore the AI client, and therefore anything that prompt-injects the AI client — holds full write/delete/import capability. security.md's claim that "the MCP server has no write access beyond what the Core REST API permits" is technically true and practically empty: the API permits everything to the one token.
- The inbound webhook (`POST /v1/events/inbound`) shares the same god-token.
- Compromise of any client credential is compromise of all of them, and there is no way to rotate one client's access without breaking every client.
- ADR-0025's INV-3 ("a plugin's maximum capability is its host process's credentials") is only as meaningful as the credential design behind it — with one shared token, every host's credential is `everything`.

How should clients authenticate to the Core Service, and with what granularity of capability?

## Decision Drivers
- Bound the blast radius of a prompt-injected AI client: injected instructions should at most exfiltrate what a read-only token can query, never destroy or corrupt data
- Give INV-3 teeth: each host process should hold — and hand to its plugins — only the capability it needs
- Prevent silent control-plane subversion: a malicious plugin must not be able to mint its own credentials and persist after removal
- Rotation and revocation must be per-client, not all-or-nothing
- Keep it proportionate: a local-first, single-user platform does not need OAuth2/OIDC infrastructure
- Be honest about what per-client tokens cannot do on a single-user machine (same-user processes can read each other's stored credentials — the ADR-0013 platform asymmetry)

## Considered Options
1. **Single shared bearer token** (status quo) — one credential for everything
2. **Named, scoped bearer tokens per client** — flat scope list, hashed server-side storage, per-client storage of the plaintext
3. **OAuth2/OIDC with a local identity provider** — full authorization framework
4. **mTLS per client** — client certificates instead of bearer tokens

## Decision Outcome
Chosen option: **Option 2 — named, scoped bearer tokens per client.**

Every credential the platform issues is **named** (identifies its holder), **scoped** (grants an explicit subset of capability), and **revocable** (individually, without disturbing other clients). No anonymous shared credential exists anywhere in the system.

Option 3 is disproportionate: OAuth2's machinery (flows, refresh tokens, an IdP) solves multi-user delegation problems this platform does not have. Option 4 secures the channel but does not express capability, and certificate lifecycle management is a heavier user burden than token strings; it remains available as a transport hardening layer for LAN deployments independent of this decision.

### Positive Consequences
- Prompt-injection blast radius through the MCP server drops from "destroy the database" to "read what the read scope permits"
- The webhook endpoint is bounded to event publication — it can no longer read or write health data
- Forged internal events (`data.imported`, `alert.resolved`, `job.complete`) are structurally impossible from outside the Core Service — the event system cannot be used to launder scopes through automations
- A malicious directory-loaded plugin cannot mint tokens, rewrite config, or trigger migrations: control-plane subversion and post-removal persistence require `admin`, which is never handed to directory-loaded code
- Per-client rotation and revocation; a leaked GUI token does not force reconfiguring the AI client
- Token names (never values) make audit logging meaningful

### Negative Consequences / Tradeoffs
- Six default credentials to issue, store, and document instead of one
- The CLI carries two credentials (admin and plugin-tier), and the loader must assign them by plugin provenance — a deliberate refinement of ADR-0010's parity principle (see below)
- Scope enforcement adds a required-scope declaration to every REST route (mechanical, one-time)
- Legitimate admin-extension plugins need a documented escape hatch (deliberate named-token issuance) and will 403 by default

---

## Scope Model

Six flat scopes. No hierarchy, no wildcards, no resource-level granularity — scopes gate *capability classes*, and the REST API's validation gates everything finer.

| Scope | Grants |
|---|---|
| `read` | GET on data endpoints, and SSE subscription (`GET /v1/events`) — event payloads can carry health data, so subscribing *is* reading |
| `write` | Create, correct, and delete individual records |
| `import` | Bulk import endpoints — separated from `write` because it is the mass-mutation path |
| `events` | Event publication via `POST /v1/events/inbound`, bounded by the token's publish-namespace allowlist (see "No scope laundering through the event system" below) |
| `jobs` | Submit and cancel jobs (`POST /v1/jobs`, `DELETE /v1/jobs/{id}`), and read job status |
| `admin` | Token management, config mutation, migration triggers |

**No scope laundering through the job system:** submitting a job requires `jobs` *plus* every scope the job type declares. An import job requires `jobs` + `import`. A token holding only `jobs` can submit only jobs whose types declare no additional scopes. The job type's required scopes are part of its registration metadata (ADR-0012).

**No scope laundering through the event system:** the `events` scope does not grant publish-anything. Without the following rules, the weakest credential in the system — the webhook's `events`-only token — could forge a trusted event type (`data.imported`), fire an automation, and have the Automation Host execute the automation's actions with *its* credentials: a confused-deputy path around every scope above. Forgery must be structurally impossible, not merely discouraged:

1. **Source stamping** — the `source` field of every inbound event is set by the Core Service from the authenticated token's name; a payload that attempts to supply `source` is rejected. Events from internal components are stamped by the bus itself. Subscribers may treat provenance as trustworthy data.
2. **Reserved internal namespaces** — `data.*`, `job.*`, `schedule.*`, `schema.*`, `system.*`, and `plugin.*` are statements of platform fact. They are never publishable through the generic `/v1/events/inbound` endpoint, for any token: the facts they represent enter the system only through purpose-built, validated REST endpoints (bulk import → `data.imported`; the job progress endpoint → `job.progress`; loader status reports → `plugin.loaded`), from which the Core Service emits the event itself. A forged `data.imported` or `job.complete` cannot exist.
3. **Per-token publish namespaces** — the token record carries an event-namespace allowlist consulted whenever `events` scope is exercised. The webhook token may publish only `external.*`; the Automation Host token may publish `alert.*`, `sync.*`, and `external.*`.

Rule 2 also closes a safety hole specific to a health platform: a forged `alert.resolved` could otherwise mask a genuine clinical alert. Under rule 3, `alert.*` publication is confined to the Automation Host and Core internals.

**Residual, accepted:** an automation whose rule explicitly triggers on `external.*` events acts with host credentials on external input — by design. The trust decision is visible in the rule definition, the triggering event is source-stamped, and the execution trace (ADR-0016) records it.

Every REST route declares its required scope(s) in the route definition — enforcement is a FastAPI dependency, not per-endpoint hand-rolled checks. An authenticated request lacking a required scope receives `403` with the token *name*, the missing scope, and no echo of request content.

## Default Token Set

Issued at first run (extends ADR-0008's first-run sequence):

| Token name | Holder | Scopes |
|---|---|---|
| `cli-admin` | CLI (built-in commands) | `read write import events jobs admin` |
| `cli-plugins` | CLI (handed to directory-loaded plugins) | `read write import jobs` |
| `gui` | GUI | `read write import jobs` |
| `mcp` | MCP Server → Core | **`read`** |
| `automation-host` | Automation Host | `read events jobs` (`write` opt-in) |
| `webhook` | Inbound webhook callers | `events` |

- **MCP is read-only by default.** Granting the AI client write capability is a deliberate act: the user issues a second named token (e.g. `mcp-write`) and configures the MCP server to use it. It is never a config flag that silently upgrades the existing token.
- **Automation Host** automations that flag or annotate results need `write`; the default omits it so a fresh install's automation surface is read-and-react only.
- **Event namespaces:** tokens carrying `events` also carry a publish-namespace allowlist — `webhook`: `external.*`; `automation-host`: `alert.*`, `sync.*`, `external.*` (see the event-laundering rules above). `cli-admin` holds `events` with the non-reserved namespaces for scripting.
- **Job children** do not appear in this table — they receive ephemeral tokens (below).

## Token Format, Verification, and Storage

**Format:** `hsp_<name>_<secret>` — `hsp_` for secret-scanner recognition, the token name for identification in errors and audit logs, and a 32-byte cryptographically random secret (base64url). The name embedded in the token is advisory (for humans and logs); authorization derives solely from the server-side record.

**Server-side storage:** The Core Service stores only `SHA-256(token)` alongside the name, scopes, created-at, last-used-at, and revoked flag, in a `tokens` table in the database. A stolen copy of the token table reveals nothing usable. Verification hashes the presented token and compares with `secrets.compare_digest`. Plaintext token values exist only at issuance (displayed/stored once) and in each client's own storage.

**Client-side storage:** Each client stores **only its own token** in the OS keychain via `keyring` (service `healthspan`, username `token:<name>`) — the same mechanism and platform caveats as ADR-0013. A per-client config file fallback (owner-read-only) exists for headless deployments. **The shared TOML config no longer contains any token** — the shared file is exactly how one token became everyone's token. This changes the shared-configuration content decided in ADR-0006 (navigation link added there).

**Bootstrap:** `healthspan init` creates the database (ADR-0013), then mints the default token set: hashes into the `tokens` table, plaintexts into the keyring. Nothing is printed except confirmation. The CLI's direct-database exception for `db migrate`/`db backup` (ADR-0006) is unaffected — those paths do not authenticate through the REST API.

## CLI Credential Tiers (refinement of ADR-0010's parity principle)

The CLI runs the most third-party code of any process. It holds two credentials, assigned by **plugin provenance** — a fact the loader already knows (ADR-0010 loads package-shipped built-ins and directory plugins distinctly):

- **Package-shipped first-party plugins** (built-in commands, including `healthspan token …`) receive the process credential: `cli-admin`.
- **Directory-loaded plugins** receive `cli-plugins` via `context.api` — no `admin`, no `events`.

This is a deliberate refinement of ADR-0010's "no privileged distinction" principle, consistent with ADR-0025: first-party and third-party plugins have **interface parity** — same `PluginContext`, same registration contract, same capabilities surface — but not **placement parity** (ADR-0025) and not **credential parity** (this ADR). What the principle has always protected is the contributor experience and replaceability, not a right of arbitrary dropped-in code to hold admin credentials.

What this buys, stated honestly: the `cli-plugins` token still carries `write` and `import` — a malicious CLI plugin **can still destroy data** (recoverable from backups). What it cannot do is subvert the control plane: mint its own tokens (persistence after removal), rewrite config, or trigger migrations. Data damage is recoverable; silent credential persistence defeats the auth system.

**Escape hatch for legitimate admin-extension plugins:** the 403 error names the token (`cli-plugins`), the missing scope, and the remedy — the user runs `healthspan token create <name> --scopes …` and configures the plugin to use that token explicitly (a keyring entry named in the plugin's TOML section). Elevation is thereby a visible, deliberate, audited act.

The same provenance rule applies in the MCP Server and Automation Host for uniformity, though it is only load-bearing in the CLI (the other hosts' process credentials carry no `admin`).

## Ephemeral Job Tokens

Heavyweight job children (ADR-0012) never receive a standing credential:

- At spawn, the Core Service mints a single-job token carrying the job type's declared scopes, bound to the job ID. Progress reporting goes through the dedicated `POST /v1/jobs/{id}/progress` endpoint (validated against the token's job binding); Core emits the corresponding `job.progress` events. Job children hold no generic event publication rights
- Delivered to the child via stdin at handoff — never via command line or environment (both inspectable)
- Expires automatically when the job reaches a terminal state (`complete`, `failed`, cancelled); also revocable with the job
- Recorded in the `tokens` table like any token (named `job:<uuid>`), so audit logging covers job children uniformly

## MCP Server Client-Facing Credential

The MCP Server has two credential relationships: its own token to Core (`mcp`, read-only), and the credential AI clients present *to it* (per security.md, every HTTP endpoint requires auth). The client-facing credential is a separate local secret generated at init and printed once for AI-client configuration; it is not a Core token and grants nothing beyond what the MCP server itself can do — which is bounded by the `mcp` Core token. Rotating either is independent of the other.

## Lifecycle Commands

```
healthspan token create <name> --scopes read,write   Mint a named token; print once
healthspan token list                                Names, scopes, created, last-used, status — never values
healthspan token revoke <name>                       Immediate revocation
healthspan token rotate <name>                       Revoke + reissue same name/scopes; update keyring if local
```

All require `admin`. Rotation of a locally-held token (e.g. `gui`) updates the keyring entry in place; rotating `mcp` requires restarting the MCP Server to pick up the new value. There is no grace overlap — revocation is immediate; the platform is local-first and clients are restartable.

## Auth Failure Rate Limiting and Audit Logging

Consolidated here from the standing requirements (security review item 2.10):

**Rate limiting:** Failed authentication attempts are rate-limited per source address with exponential backoff (including localhost — a compromised local process brute-forcing token values should hit a wall). Authentication failure responses are uniform and generic: no distinction between "unknown token" and "revoked token" leaks to the caller.

**Audit log:** An append-only `auth_audit` table records: timestamp, token *name* (or `invalid` for unrecognized credentials), source address, endpoint, method, and outcome (`ok`, `denied:scope`, `denied:invalid`, `denied:revoked`, `rate-limited`). Never token values, never request bodies, never health data — consistent with the logging rules in security.md. All `admin`-scoped actions are always audited, successful or not: token issuance is exactly the event whose absence from an audit trail would make persistence invisible. Retention is configurable; pruning follows the jobs-table pattern (ADR-0012).

## What This Does Not Protect (honest limitations)

On a single-user machine, all processes run as the same user, and Windows Credential Manager / Linux keyrings do not enforce per-application ACLs (the ADR-0013 platform asymmetry). A sufficiently deliberate malicious plugin can read *another* client's token from the keyring and escalate to that token's scopes. Scoped tokens therefore robustly bound:

- **The protocol surface** — what an AI client, a prompt-injected AI client, a webhook caller, or a LAN client can do from outside the machine's trust boundary
- **The handed-credential surface** — what plugin code wields without going hunting, and what the control plane accepts without a visible, audited issuance event

They do not defeat a determined same-user attacker — that remains the province of OS full-disk encryption, platform keychain ACLs where they exist (macOS), and not installing untrusted code. This is the same honesty standard as ADR-0013's threat-model table.

## Security Invariant

This ADR adds **INV-5** to the invariants table in [security.md](../security.md), and refines **INV-3**:

| # | Invariant | Why |
|---|---|---|
| INV-5 | Every issued credential is named, scoped, and revocable; no anonymous shared credential exists, and `admin` scope is never handed to directory-loaded plugin code. | Bounds prompt-injection blast radius to the holder's scopes; makes control-plane subversion require a visible, audited issuance event rather than a silent copy. |
| INV-3 (refined) | A plugin's maximum handed capability is its host process's **plugin-tier** credential; escalation requires deliberate, named token issuance by the user. | The host assignment (ADR-0025) bounds *where* plugin code runs; the credential tier bounds *what it is given*. |

## Consequences for Other Documents

- **security.md**: rewrite the Authentication section (this model), correct the Principles claim about MCP write access, add INV-5 and refine INV-3, note read-only default under Prompt injection awareness
- **ADR-0006** (Accepted): navigation link — shared config no longer carries a bearer token
- **ADR-0008** (Accepted): navigation link — first run issues the default token set
- **ADR-0010** (Accepted): navigation link — credential tiers refine the parity principle
- **ADR-0011** (Proposed): webhook inbound adapter authenticates callers with the `events`-scoped token and may publish only `external.*`; the envelope `source` is stamped from token identity; reserved namespaces are marked internal-only in the event catalog
- **ADR-0012** (Proposed): job children use ephemeral single-job tokens; progress reporting moves to the dedicated `POST /v1/jobs/{id}/progress` endpoint
- **ADR-0025** (Proposed): Automation Host and job-child token references now cite this ADR

## Pros and Cons of the Options

### Single shared bearer token (status quo)
- Pro: one secret, zero ceremony
- Con: MCP/AI-client compromise = full data destruction capability; webhook holds god-token; no per-client rotation; INV-3 is hollow

### Named scoped tokens (chosen)
- Pro: blast-radius bounding at every trust seam the platform actually has; per-client lifecycle; meaningful audit identity
- Con: six credentials, dual-token CLI, per-route scope declarations — all mechanical, one-time costs

### OAuth2/OIDC local IdP
- Pro: standard flows, standard libraries, delegation and expiry built in
- Con: an identity provider, token refresh, and flow UX for a single-user localhost platform; the complexity exceeds the entire rest of the auth surface

### mTLS per client
- Pro: strong mutual authentication; no bearer secrets on the wire
- Con: expresses identity, not capability — scopes still needed on top; certificate lifecycle is heavier for users than token strings; remains available as optional LAN transport hardening orthogonal to this decision

## Links
- Extends: [ADR-0006](0006-application-architecture.md) — shared configuration no longer carries the bearer token
- Extends: [ADR-0008](0008-process-lifecycle.md) — first-run behavior issues the default token set
- Extends: [ADR-0010](0010-cli-plugin-model.md) — credential tiers as a refinement of the parity principle
- Related: [ADR-0025](0025-plugin-host-process-matrix.md) — host-process matrix; INV-3; per-host credentials
- Related: [ADR-0012](0012-job-abstraction.md) — ephemeral job-child tokens
- Related: [ADR-0013](0013-encryption-at-rest.md) — keyring storage mechanism and platform asymmetries
- Related: [specs/security.md](../security.md) — Authentication requirements; Security Invariants
- Resolves: [architecture review 2026-06-10](../architecture-review-2026-06-10.md), item 2.1 and the auth items of 2.10

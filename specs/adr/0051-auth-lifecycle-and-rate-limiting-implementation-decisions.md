# ADR-0051: Auth Lifecycle, Rate-Limiting, and Bootstrap Implementation Decisions (Phase 2 WI-2b)

## Status
Accepted

## Context and Problem Statement

Phase 2 WI-2b completes [ADR-0026](0026-named-scoped-tokens.md)'s auth surface: the default-set bootstrap whose timing [ADR-0050](0050-token-store-and-auth-implementation-decisions.md) §1 decided, the auth-failure rate limiter (ADR-0026 rules 1–4), and the lifecycle surface (`token create/list/revoke/rotate`, `auth reset-limits`, `mcp rotate-client-secret`). Implementation surfaced decisions the ADRs left open: concrete limiter defaults and their config home, where limiter state lives, how rotation coexists with the `tokens.name` UNIQUE constraint, a self-lockout hazard in revocation, the transport for MCP-secret rotation, the exact members of `cli-admin`'s publish-namespace allowlist, and the failure-ordering of the bootstrap's keyring and database writes.

Per the CLAUDE.md decision-capture convention these land as one Proposed ADR extending the Accepted ADR-0026 (the [ADR-0049](0049-core-service-skeleton-implementation-decisions.md)/ADR-0050 batching pattern). Four of these were owner decisions taken at WI-2b kickoff (2026-07-12): the limiter defaults (§1), in-memory limiter state (§2), the self-revocation refusal (§4), and REST-routed MCP-secret rotation (§5).

## Decision Drivers

- ADR-0026's four limiter rules are fixed; only tuning, state placement, and response shape were open
- A misconfigured client must recover in bounded time; a brute-forcing local process must hit a wall (ADR-0026 rule 4)
- No admin path may be able to destroy the last admin credential irrecoverably — bootstrap never re-mints into a non-empty table and no direct-database escape hatch exists
- Every admin action must land in `auth_audit` (ADR-0026: issuance is exactly the event whose absence would make persistence invisible)
- The bootstrap must be all-or-nothing: a partially-minted default set would defeat the emptiness-check idempotence guard forever

## Decision Outcome

### 1. Limiter defaults: 5 free failures, 1 s doubling backoff, aggregate at 5×, knobs in `[auth]`

Per bucket — keyed on (source address, advisory token-name prefix), per ADR-0026 rule 2 — the first **5** failures are free; the next arms an exponential backoff of 1 s, doubling per further failure, capped at **60 s** (the ADR-0026 rule 4 default). The per-address aggregate threshold (rule 3) is a **fixed 5× multiple** of the per-bucket one (25 by default), with the same backoff curve past it. Name-cycling trips the aggregate quickly; stated honestly, a single very persistent failing client also reaches it over time — rule 1 bounds the impact either way to that address's *failing* requests. Two knobs land in a new **`[auth]` config section**: `failure_threshold` (default 5) and `max_backoff_seconds` (default 60), both integers ≥ 1, strict unknown-key validation as everywhere ([ADR-0046](0046-filesystem-layout-and-config-discovery.md)). The aggregate multiplier is deliberately not a knob.

A throttled failure answers `429` with body `{"detail": "too many failed authentication attempts"}` and a `Retry-After` header (integer seconds, rounded up) — revealing that the limiter fired discloses nothing about token state (ADR-0026), and neither does the retry horizon. **A throttled attempt is not a new failure**: while a window is armed, the limiter reports the honest remaining time without recording, extending, or allocating anything — so `Retry-After` is accurate, a client that honors it escapes when the window serves out, and hammering a 429 neither doubles the backoff nor grows state. Check-and-record is one atomic locked operation, so concurrent failures at a threshold boundary cannot each read a stale verdict. The audit row records the `rate-limited` outcome under the server-side token name for a recognized-but-revoked credential, `invalid` otherwise (the [ADR-0050](0050-token-store-and-auth-implementation-decisions.md) §3 rule; the attacker-supplied advisory name feeds only the bucket key). A `403` is an *authenticated* request and never counts as a failure.

**No clear-on-success; decay is time-based only.** A valid credential is never throttled (the limiter is consulted only on the failure path), so nothing legitimate needs its state cleared. The limiter deliberately offers *no* success hook: because the advisory name is unauthenticated (ADR-0026 rule 2), a success under a name that erased the failures accrued under it would let a co-resident attacker — every loopback client shares `127.0.0.1` — launder brute-force attempts against a live token's name through that token's ordinary traffic, defeating exactly the localhost wall ADR-0026 rule 1 requires. A recovering client's stale failures instead age out of their bucket by the idle prune below; they never throttle its valid requests in the meantime.

**Bounded state:** the advisory name is attacker-supplied text, so the bucket key is truncated to 64 characters and each address holds at most 256 buckets. Overflow past the cap shares the per-address `invalid` bucket — and the overflow reroute is resolved *before* the throttle check, so an overflowing attempt honors that shared bucket's armed window rather than slipping a free 401 in and then recording into a blocked bucket. Buckets idle for 15 minutes with no live block are pruned by a sweep that runs at most once a minute. Constants, not knobs: they bound resources rather than tune behavior.

**Reserved names.** `invalid` (the audit sentinel and the limiter's shared unrecognized/overflow bucket key) and `mcpclient` (the MCP client-facing secret's format segment, which ADR-0026 states is never a Core token name) are refused at mint: a real token under either would corrupt `token_name = 'invalid'` audit queries and, through the shared bucket, hand an attacker a way to influence limiter state by using a legitimately-issued token.

### 2. Limiter state is in-memory only

No table, no persistence: a service restart clears all limiter state, and `POST /v1/auth/reset-limits` clears it on demand. Persistence would buy nothing real — a local attacker who can restart the service already owns the machine, backoff caps at 60 s anyway, and only failing requests are ever affected — while costing a schema table, pruning, and a write per failed attempt on top of the audit row.

### 3. `token rotate` is an atomic in-place hash replacement

`tokens.name` is UNIQUE (migration 0002), so ADR-0026's "revoke + reissue same name/scopes" cannot be a revoked row plus a fresh insert. Rotation is realized as a single `UPDATE`: replace `token_hash`, reset `created_utc`, clear `last_used_utc`/`revoked`/`revoked_utc` — the old value dead and the new one live with no window between, scopes and attributes untouched. Two consequences, both deliberate: a **revoked name stays reserved** (its row is the revocation record; `create` answers `409` for it), and **rotation is the sanctioned reissue path for a revoked name** (it returns the name to live under a fresh secret). If a `token:<name>` keyring entry exists (every default token's does, from bootstrap), rotation updates it in place per ADR-0026; a keychain failure does not undo the committed rotation — the response still carries the plaintext, flagged `keyring_updated: false`.

### 4. Revocation carries two lockout guards: never the requester's own token, never the last live `admin` token

`POST /v1/tokens/{name}/revoke` answers `409` when `{name}` is the requester's own token. Bootstrap never re-mints into a non-empty table (ADR-0050 §1) and no direct-database escape hatch exists (the Phase 2 milestone allows none), so losing the final admin credential is irreversible. The request-layer self-check alone cannot guarantee that — two requests authenticated by two *different* admin tokens could revoke each other — so the invariant lives where it belongs, in the store: `revoke_token` refuses, inside the same `BEGIN IMMEDIATE` transaction as the update, to revoke the last live token carrying `admin`. Concurrent mutual revocations serialize on the transaction; the second is refused, and at least one live admin credential always survives. Rotation — which both refusal messages point to — covers the compromised-token case without the lockout.

### 5. `mcp rotate-client-secret` goes through the Core REST API

The MCP client-facing secret is not a Core token — its SHA-256 lives in the OS keyring as the verifier-side record (ADR-0026) — so the CLI *could* rotate it locally with the service down. It instead calls `POST /v1/mcp/rotate-client-secret` (`admin`) like every other lifecycle command: the act lands in `auth_audit` (all admin actions are audited), enforcement is the same scope check as everywhere, and the keyring write is one Core already performs at bootstrap. Uniformity over offline availability: nothing consumes the secret while the Core Service is down, because the MCP Server verifies against the same keyring at its own startup.

### 6. `cli-admin` publishes `external.*` and `sync.*` — not `alert.*`

ADR-0026 gives `cli-admin` "the non-reserved namespaces for scripting" but also confines `alert.*` publication "to the Automation Host and Core internals" — and the two clauses conflict, since `alert.*` is not in the reserved list. Resolved in favor of the confinement: a forged `alert.resolved` masking a clinical alert is the safety hazard ADR-0026's rule 2 discussion names, so the operator-scripting allowlist is **`external.*`, `sync.*`**. An operator who genuinely needs to script alert publication mints a dedicated token for it — a visible, audited act.

### 7. Bootstrap ordering: keyring first, then one all-or-nothing insert

The first-start mint (ADR-0050 §1) generates all eight default plaintexts and the MCP secret up front, writes the keyring entries (`token:<name>` plaintexts, `mcp-client-secret` hash) **first**, then inserts all eight hashes in a **single transaction**. A failure between the two leaves orphaned keyring entries and an empty table — the next start overwrites and retries; the reverse order could commit hashes whose plaintexts were lost, an unrecoverable lockout the emptiness check would never repair. Any failure aborts startup (nothing serves without credentials existing). The one-time MCP plaintext prints to **stderr** — the operator's console under direct-start — never through the stdout JSON log stream, which may be redirected or shipped.

### 8. The lifecycle CLI is a REST client with no offline path

`healthspan token …`, `auth reset-limits`, and `mcp rotate-client-secret` authenticate with the `cli-admin` plaintext from its keyring entry and call the admin endpoints; with the Core Service down they fail with guidance to `service start`. This keeps the Phase 2 milestone invariant — nothing but `db migrate`/`db backup`/`db restore` touches the database directly — and costs nothing real: revocation urgency is low while nothing is serving.

This makes the CLI an HTTP client, promoting **`httpx` from a dev dependency to a runtime dependency**. Not a new selection — `httpx` is on the Phase 2 pre-decided dependency list ([ADR-0006](0006-application-architecture.md)/[ADR-0029](0029-mcp-streamable-http.md) client stack, adopted with the WI-1 skeleton) — but this is the first shipped consumer, so the promotion is recorded here.

### Positive Consequences

- ADR-0026's limiter rules land with concrete, tested semantics and two operator knobs, defaulted safely
- The self-lockout and partial-bootstrap failure modes are structurally impossible, not merely unlikely
- Every credential-lifecycle act — mint, revoke, rotate, limiter reset, MCP rotation — is scope-checked and audited through one path
- The ADR-0026 allowlist ambiguity is resolved on the side its own safety argument supports

### Negative Consequences / Tradeoffs

- A revoked token name is permanently reserved; reissue requires rotation rather than delete-and-recreate (accepted: the row is the revocation record, and `auth_audit` keys on names)
- Keyring entries (`token:<name>`, `mcp-client-secret`) are machine-global per the ADR-0026 convention, with no per-database namespacing: bootstrapping a *second* database on the same machine overwrites the first database's stored plaintexts. Single-database operation is the design center; the multi-database question is recorded in [open-questions.md](../open-questions.md) rather than silently changing an Accepted convention here
- Limiter state vanishing on restart means a determined local brute-forcer can reset its wall by restarting the service — but that capability already implies key-holder-level access
- Token lifecycle management requires a running Core Service; there is no break-glass offline revocation (bounded: nothing serves while it is down)
- `failure_threshold` couples the bucket and aggregate thresholds through the fixed multiplier — tuning them independently would need a code change

## Consequences for Other Documents

- **[ADR-0026](0026-named-scoped-tokens.md)** (Accepted): navigation link — lifecycle, limiter, and allowlist concretizations recorded here
- **[api-reference.md](../api-reference.md)**: the `429` error contract and the Auth administration endpoint group — same PR
- **[open-questions.md](../open-questions.md)**: the `keys` rekeying commands joined the WI-1 exclusive-access guard (the deferred item) — same PR
- **pyproject.toml**: `httpx` moves from the dev group to `[project.dependencies]` (§8) — same PR
- **[security.md](../security.md)**: the lifecycle-command list gains `auth reset-limits` and `mcp rotate-client-secret` — same PR

## Links

- Extends: [ADR-0026](0026-named-scoped-tokens.md) — limiter defaults and response shape, rotation semantics, self-revocation guard, MCP-rotation transport, `cli-admin` allowlist members
- Extends: [ADR-0050](0050-token-store-and-auth-implementation-decisions.md) — realizes its §1 bootstrap-timing decision with the keyring-first ordering
- Related: [ADR-0046](0046-filesystem-layout-and-config-discovery.md) — the `[auth]` section follows its strict-parsing rules
- Related: [ADR-0049](0049-core-service-skeleton-implementation-decisions.md) — the WI-decisions-bundle pattern; the `[service]` config precedent the `[auth]` section follows
- Related: [ADR-0033](0033-plaintext-artifact-disposal.md), [ADR-0038](0038-backup-execution-and-verification.md), [ADR-0042](0042-process-supervision-and-single-instance-locking.md) — the exclusive-access guard the `keys` rekeying commands now hold (a WI-1 deferral closed here, not a new decision)

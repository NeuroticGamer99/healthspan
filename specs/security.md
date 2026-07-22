# Security Requirements

This is a standing requirements document. All implementation work is measured against these requirements. Security is designed in from the start — not retrofitted.

This document covers the platform's own security posture. It does not cover the security of third-party services (AI providers, cloud backup, data sources).

---

## Trust Model

The platform operates across three distinct trust layers, each with different properties. Understanding these layers is essential for reasoning about what is and is not protected.

**Storage layer — zero-knowledge for the provider**
The SQLCipher-encrypted database file is opaque ciphertext. Any party that holds the file — a cloud backup provider, a sync service, a drive — never sees plaintext. The decryption key never leaves the user's control (OS keychain + Recovery Kit). Cloud backup and sync of the encrypted file is explicitly safe for the output of `healthspan db backup`; the live database file must never be synced ([ADR-0019](adr/0019-multi-device-sync.md)). The storage provider is in the "do not trust" tier and the encryption model handles this correctly.

**Processing layer — must trust the Core Service machine**
Decryption happens inside the Core Service process. The machine running Core Service must be trusted — its operator has potential access to decrypted health data. This is inherent: querying data requires decrypting it. The Core Service cannot be delegated to an untrusted host without accepting that the host operator can read the data. There is no software-only solution to this constraint.

**Transport layer — conditional on deployment**
Communication between processes on the same trusted machine (localhost HTTP) is safe. Any communication that crosses a machine boundary — LAN, Docker, remote clients — requires HTTPS. This is not negotiable: health data in transit over an unencrypted network connection is a serious vulnerability regardless of how well the file at rest is protected.

### Implications for deployment

| Deployment | Storage trust | Processing trust | Transport requirement |
|---|---|---|---|
| Single local machine (default) | Cloud sync safe (backups only) | User's own machine | None (localhost) |
| Backup file on cloud storage | Cloud provider untrusted — safe | Core Service machine | HTTPS if Core Service remote |
| Core Service on a home server | File sync safe (backups only) | Home server operator | HTTPS between devices |
| Core Service on a cloud VM | File sync safe (backups only) | Cloud VM provider can see plaintext | HTTPS required |

**The local-first default is not primarily a data protection decision.** ADR-0013 makes the encrypted database file safe for cloud storage regardless. Local-first is the right default for simplicity and zero infrastructure dependency — not because remote storage is inherently dangerous for an encrypted file.

---

## Principles

**Least privilege per process.** Each process has access only to what it needs. The GUI has no database credentials. The Automation Host token carries no `admin` scope. The MCP server holds a read-only token by default — write capability for an AI client is a deliberately issued, named credential, never a default (ADR-0026). See ADR-0006.

**Defense in depth.** No single control is relied upon alone. Authentication, host header validation, and CORS work together. A bypass of one does not grant access.

**Secure by default.** The default configuration is the most secure configuration. Users must explicitly opt into less restrictive settings (e.g. changing the binding address for LAN deployment). Security is never traded for convenience in the defaults.

**Health data never leaves the process boundary unintentionally.** Logs, error messages, and diagnostic output must not contain health data values. Metadata (timestamps, biomarker names, query counts) is acceptable in logs; result values are not.

---

## Security Invariants

Numbered, testable invariants the architecture must preserve. **Any ADR that would change one of these must cite it by number and explicitly supersede or extend the ADR that establishes it.** Breaking an invariant must be a visible, deliberate decision — never a side effect of an unrelated design change.

| # | Invariant | Why | Established by |
|---|---|---|---|
| INV-1 | The derived key exists only in the memory of the single process currently holding the database open — the Core Service at runtime, or the CLI during an explicitly invoked `db`/`keys` maintenance command run while Core Service is stopped. It is never transmitted, logged, or inherited by child processes (jobs use `spawn`, never `fork`). | The key is the single secret the entire encryption-at-rest story rests on. | [ADR-0013](adr/0013-encryption-at-rest.md), [ADR-0025](adr/0025-plugin-host-process-matrix.md) |
| INV-2 | The Core Service never executes code from the plugins directory. First-party in-core components ship inside the `healthspan` package and are imported explicitly. | The plugins directory is the platform's invited-code channel; keeping it out of the key-holding process makes plugin isolation true by architecture, not by audit. | [ADR-0025](adr/0025-plugin-host-process-matrix.md) |
| INV-3 | A plugin's maximum handed capability is its host process's plugin-tier credential; escalation requires deliberate, named token issuance by the user. | The host assignment bounds *where* plugin code runs; the credential tier bounds *what it is given*. | [ADR-0025](adr/0025-plugin-host-process-matrix.md), [ADR-0026](adr/0026-named-scoped-tokens.md) |
| INV-4 | Plugins alter Core Service behavior only via data submitted through the validated REST API. | Data is inert and validated at the boundary; code is not. | [ADR-0025](adr/0025-plugin-host-process-matrix.md) |
| INV-5 | Every issued credential is named, scoped, and revocable; no anonymous shared credential exists, and `admin` scope is never handed to directory-loaded plugin code. | Bounds prompt-injection blast radius to the holder's scopes; control-plane subversion requires a visible, audited issuance event rather than a silent copy. | [ADR-0026](adr/0026-named-scoped-tokens.md) |
| INV-6 | Interpretation and derivation never enter source-data tables: content class is determined structurally by table, authorship on interpretive rows is stamped from token identity (never caller-supplied), and no AI-client credential carries source-data write scope by default. | Source data's evidentiary value depends on knowing nothing in it was synthesized; contamination is silent and, once mixed, irreversible. | [ADR-0043](adr/0043-ai-authored-analyses-and-annotate-scope.md), [ADR-0044](adr/0044-derived-data-points.md) |
| INV-7 | The live database's audit surfaces — `audit_log` and `auth_audit` — are append-only: no platform operation updates or deletes an audit row, enforced in-schema by `RAISE(ABORT)` triggers. Value corrections supersede (a new data row plus appended audit records) and designated metadata repairs update the data row in place, fully audited — neither ever edits what an existing audit row says happened. Sole sanctioned exception: `auth_audit` retention pruning under its configured policy, whose enforcement-mechanism reconciliation is deferred — tracked in [open-questions.md](open-questions.md), "`auth_audit` retention pruning vs. the shipped append-only trigger"; `audit_log` is never pruned. | The audit trail is the evidentiary record: "what changed, when, by what" must stay answerable through every later feature — especially ones that merge or delete data, whose "cleanup" would otherwise silently destroy exactly the record that explains them. | [ADR-0027](adr/0027-audit-trail-and-corrections.md), [ADR-0026](adr/0026-named-scoped-tokens.md), [ADR-0050](adr/0050-token-store-and-auth-implementation-decisions.md) |
| INV-8 | No process stores the plaintext of a credential presented to it for verification, and no log or audit surface records one: verifiers hold only digests, and a credential's plaintext exists only at its one-time issuance handoff and in the holder's own local storage. | Reading a verifier's storage must not yield usable credentials: impersonating a client has to require compromising that client, and neither a database exfiltration nor a decrypted backup can hand an attacker every caller's identity. | [ADR-0026](adr/0026-named-scoped-tokens.md) |

---

## Authentication

**Bearer token on every HTTP endpoint, with one named exemption.** The Core REST API and MCP Server require a valid `Authorization: Bearer <token>` header on every request, including requests from localhost. The single exemption is each process's liveness endpoint (`GET /v1/health` on the Core Service, `GET /health` on other HTTP processes), which returns only `200`/`503` and a status word — no version, no `schema_version`, no detail — so the launcher, Docker healthchecks, and systemd watchdogs can poll readiness without a credential landing in argv, a compose file, or a unit file (all inspectable surfaces; same discipline as [ADR-0039](adr/0039-startup-sequence-and-passphrase-handoff.md)). The liveness route declares an explicit `public` marker; every other route declares a required scope. Detailed health (`/v1/health/detail`) and metrics (`/v1/metrics`) require the `monitor` scope. See [ADR-0040](adr/0040-health-endpoint-authentication.md).

**Named, scoped, revocable tokens — no shared credential.** Every credential the platform issues is named (identifies its holder), scoped (grants an explicit capability subset: `read`, `write`, `annotate`, `import`, `events`, `jobs`, `monitor`, `supervise`, `admin`), and individually revocable. Each client — GUI, MCP Server, CLI, Automation Host, the launcher, webhook callers, job children — holds its own token with default scopes per the matrix in [ADR-0026](adr/0026-named-scoped-tokens.md) (ADR-0026 is authoritative for scopes and defaults). The MCP server's token is read-only by default. The inbound webhook token can only publish events. Job children receive ephemeral single-job tokens. See INV-5.

**Event publication is namespace-bound.** Tokens carrying `events` publish only within their declared event namespaces; the event `source` field is stamped from token identity, never caller-supplied; and reserved namespaces (`data.*`, `job.*`, `schedule.*`, `schema.*`, `system.*`, `plugin.*`) are emitted only by the Core Service itself — facts originating elsewhere, such as the launcher's supervision reports, enter through purpose-built validated endpoints from which Core emits the event ([ADR-0042](adr/0042-process-supervision-and-single-instance-locking.md)). Forged internal events are structurally impossible — the event system cannot be used to launder scopes through automations, and a forged `alert.resolved` cannot mask a genuine clinical alert. See ADR-0026.

**Token generation and verification.** Tokens are minted at first run (cryptographically random, minimum 32 bytes, base64url-encoded, `hsp_<name>_` prefixed). The Core Service stores only SHA-256 hashes of token values; verification uses `secrets.compare_digest`. Plaintext values exist only at issuance and in each client's own storage. See INV-8.

**Token storage.** Each client stores only its own token, in the OS keychain via `keyring` (per-client config file fallback with owner-read-only permissions for headless deployments). **No token is stored in the shared TOML config file** — a shared file is how a single token becomes every client's token.

**MCP Server's two credentials.** The MCP Server holds its own read-only `mcp` token to Core *and* verifies a separate client-facing bearer that AI clients present *to it* (`hsp_mcpclient_…`). Because the MCP Server owns no database, that client-facing secret is stored hashed (`SHA-256`, verified with `compare_digest`) in the MCP Server's own keyring rather than in the `tokens` table, and is rotated with `healthspan mcp rotate-client-secret`. It is a static bearer: the MCP Server does not advertise OAuth discovery and returns a plain `401` on a missing or invalid credential ([ADR-0029](adr/0029-mcp-streamable-http.md)). See ADR-0026.

**Credential tiers for plugins.** Directory-loaded plugins receive their host process's plugin-tier token via `context.api` — never a credential carrying `admin`. Package-shipped first-party plugins receive the process credential. See ADR-0026 and INV-3.

**Lifecycle.** `healthspan token create | list | revoke | rotate`, `healthspan auth reset-limits`, and `healthspan mcp rotate-client-secret` (all admin scope; `list` shows names and scopes, never values) — thin REST clients over the Core Service's `admin`-scoped endpoints, so every lifecycle act is scope-checked and lands in the auth audit log ([ADR-0051](adr/0051-auth-lifecycle-and-rate-limiting-implementation-decisions.md)). Revocation is immediate; there is no grace overlap; revoking the token that authenticates the request is refused (rotate instead — self-lockout guard).

**Rate limiting and audit.** Failed authentication attempts are rate-limited with bounded exponential backoff, including from localhost — keyed on (source address, advisory token-name prefix) with a per-address aggregate cap, and throttling failures only: a valid credential is never delayed, so one misconfigured client cannot lock out the other local clients (ADR-0026). Auth events (token *name*, endpoint, outcome — never token values, never health data) are recorded in an append-only audit log; all `admin`-scoped actions are always audited. See ADR-0026 and INV-7.

**Config file permissions.** The TOML config file must be created with owner-read-only permissions (`chmod 600` equivalent). The platform should warn on startup if the config file has broader permissions.

**No token in URLs.** Bearer tokens must only appear in the `Authorization` header, never in query strings or URL paths (where they would appear in server logs and browser history).

---

## Network Security

**Binding address.** Default binding is `127.0.0.1` (localhost only), and loopback-only operation is the design center: **non-loopback (LAN or Docker-bridge) binding is out of scope for the initial development plan** ([development-plan.md](development-plan.md), Phases 0–8). The config parser accepts a non-loopback `service.host` as an explicit, user-typed opt-in ([ADR-0049](adr/0049-core-service-skeleton-implementation-decisions.md) §Binding posture), but the platform does not *support* it until the gated controls below land — tracked in [open-questions.md](open-questions.md), "Non-loopback binding hardening".

**Gated controls — required before any non-loopback bind is supported.** The following are requirements on the future LAN-deployment work item, not descriptions of current behavior:

1. **Host header validation** (DNS-rebinding defense) — the server rejects any request whose `Host` header does not match a configured expected value. Configured in TOML. Default: `localhost` and `127.0.0.1`.
2. **CORS allowlist** — an explicit allowlist of permitted origins, default empty (deny all cross-origin requests). The server denies preflights from non-allowlisted origins; combined with bearer-token authentication (browsers cannot send custom `Authorization` headers cross-origin without a preflight), this blocks DNS rebinding even if host validation were bypassed.
3. **HTTPS** — required for any non-localhost binding, on both the server listener and the CLI client path (a non-loopback `service.host` today would put the bearer token on the wire in cleartext — CWE-319). For LAN deployment, a self-signed certificate with pinning or a local CA (mkcert). For internet-exposed deployment (not a supported use case for personal health data), a trusted CA. For localhost, HTTPS stays recommended but not required.

LAN or Docker deployment additionally requires documented network-level controls (firewall, VPN) — a documentation obligation of the same gated work item.

---

## Encryption at Rest

The database file contains sensitive personal health data and must be encrypted. Encryption must be decided and implemented before any data is written — retrofitting it onto an existing database is destructive and error-prone.

**SQLCipher** is the recommended solution: AES-256-CBC, transparent to the application (query code is unchanged), cross-platform, and compatible with the SQLite backup API. See [ADR-0013](adr/0013-encryption-at-rest.md).

**Key management** uses a two-factor hybrid model inspired by 1Password's Secret Key design: a randomly generated secret key stored in the OS keychain (`keyring`) combined with a user master passphrase. Neither component alone decrypts the database. Cross-device portability is provided by a printable **Recovery Kit** — a QR code of the secret key with a blank for the handwritten passphrase — generated at init and stored securely offline. A passphrase-only mode is available for users who want full portability without any OS keychain dependency. See [ADR-0013](adr/0013-encryption-at-rest.md) for the full threat model and implementation requirements.

**Key derivation** ([ADR-0028](adr/0028-key-derivation-and-rotation.md)): `db_key = Argon2id(password = NFC-normalized UTF-8 passphrase, salt = 32-byte secret key)`, 32-byte output, parameters at or above the OWASP floor and recorded per database in a non-secret `.keyparams` sidecar that travels with the database file. The key is handed to SQLCipher in raw-hex form (`PRAGMA key = "x'<64 hex>'"`), skipping SQLCipher's internal PBKDF2 — the single sanctioned exception to the no-interpolation rule below, as only locally generated, format-validated hex reaches it. The key is derived once at Core Service startup, held in process memory (`SecretStr` discipline) with a persistent connection pool, and zeroed best-effort at shutdown — per-request re-derivation defends nothing the threat model protects.

**Rotation** ([ADR-0028](adr/0028-key-derivation-and-rotation.md)): `healthspan keys change-passphrase` and `healthspan keys rotate-secret-key` rekey the database in place after taking a mandatory verified backup. Rotation is not retroactive — old backups open only with the credentials in force when they were made. Secret-key rotation invalidates all previously printed Recovery Kits. In passphrase-only mode the sidecar salt is the secret key's analog: `rotate-secret-key` rotates it (fresh salt + rekey) and `change-passphrase` regenerates it alongside the new passphrase. `healthspan keys convert-mode --to two-factor|passphrase-only` converts between key modes in place under the same mandatory-backup discipline; converting to passphrase-only warns that it is a downgrade to single-factor protection.

**Backups** ([ADR-0038](adr/0038-backup-execution-and-verification.md)): scheduled backups run *inside* Core Service — the only process holding the key (INV-1) — as the first-party `backup.database` job on a dedicated worker thread; on-demand runs require `admin` scope. Every backup is verified (opens with the current key, `PRAGMA integrity_check`, `schema_version` match) and published atomically with its `.keyparams` sidecar before a sync client can see it; a backup that fails verification is deleted and the job fails loudly. `healthspan db backup` is the offline path and refuses to run while Core Service is up — the same exclusive-access discipline as the rotation commands. `healthspan db restore` is the same pipeline mirrored: offline-only (it refuses against a live service and holds the ADR-0042 advisory lock), it verifies the backup by key-open and full `integrity_check` on a temporary copy before anything takes the live name, never migrates implicitly, and moves the displaced live file aside rather than deleting it.

**Platform note:** under the `uv tool install` distribution (ADR-0023), the OS keychain client is the Python interpreter — on every platform, including macOS, same-user code can access the stored secret key without a prompt. The passphrase (never stored, standard mode) is what keychain compromise alone does not yield. See ADR-0028 for the correction of ADR-0013's macOS per-app ACL claim.

**The encryption key must never be:**
- Stored in plaintext in the TOML config file
- Hardcoded in source code
- Committed to version control
- Logged
- Passed between processes — each process that legitimately needs it derives it independently ([ADR-0039](adr/0039-startup-sequence-and-passphrase-handoff.md))

**The master passphrase** crosses a process boundary only via an interactive TTY prompt, a stdin pipe, or a permission-restricted file provided by an OS secret facility (systemd credentials, Docker secrets) — **never argv, never environment variables**, both of which are inspectable ([ADR-0039](adr/0039-startup-sequence-and-passphrase-handoff.md)). Entry surfaces (TTY prompt, GUI masked dialog) are distinct from transport: whatever collects the passphrase hands it on over one of the sanctioned channels and drops its copy after handoff. Migrations run in the launcher before Core Service starts; the launcher and Core Service hold the database sequentially, never simultaneously.

**Migration path:** For users with an existing unencrypted database, `healthspan db encrypt` migrates using SQLCipher's `sqlcipher_export()`, verifies the encrypted copy (opens with the derived key, `PRAGMA integrity_check`, row-count comparison), and then requires an explicit decision about the plaintext original: user-confirmed best-effort disposal (the default), or deliberate retention via `--keep-plaintext` with a prominent warning. A plaintext health database is never silently left on disk. See [ADR-0033](adr/0033-plaintext-artifact-disposal.md).

**Clinical document originals:** Imported source documents (lab PDFs, CCDA exports, FHIR document payloads) are stored as content-addressed BLOBs inside the encrypted database — there is no plaintext document directory. After a verified import, the importer offers ADR-0033 disposal of the source file (interactively for CLI imports; via the watch folder's config-declared post-import action for unattended imports, [ADR-0025](adr/0025-plugin-host-process-matrix.md)), so the only durable copy can be the encrypted one. See [ADR-0034](adr/0034-clinical-document-storage.md).

---

## Temporary Files

Any temporary files created during import, export, migration, or processing that contain health data must be handled with the same care as the database itself. The full disposal policy — avoidance first, best-effort disposal, full-disk encryption as the backstop — is [ADR-0033](adr/0033-plaintext-artifact-disposal.md); in summary:

- **Avoidance first.** The primary control is never writing plaintext health data to disk at all. Prefer in-memory processing and streaming; create a plaintext file only when a platform pathway genuinely requires one, for the shortest workable lifetime.
- **Restricted permissions.** Temporary files must be written to a directory with owner-read-write-only permissions (`chmod 600` equivalent on the file, `chmod 700` on the directory). Never write to `/tmp`, `%TEMP%`, or any world-readable location.
- **Best-effort disposal.** When a plaintext file is disposed of, it is overwritten with zeroes and then unlinked — as defense-in-depth, not as guaranteed erasure. SSD wear leveling, copy-on-write and journaling filesystems, filesystem snapshots, and cloud-sync version history can all preserve the original blocks beyond the platform's reach. OS full-disk encryption (BitLocker, FileVault, LUKS) is the real backstop for residual plaintext; it is recommended prominently in user documentation and printed by every command that creates a plaintext artifact.
- **No shared directories.** Temporary files must not be written to a directory that any other user or process can read, including shared system temp directories.
- **Failure cleanup.** If a process exits abnormally while a temporary file exists, the startup sequence must detect and dispose of any orphaned temporary files — including Recovery Kit renders — before proceeding.

This applies to: import staging files, export staging files, database migration intermediaries, SQLCipher `sqlcipher_export()` output files, Recovery Kit renders (secret-key material — same discipline as health data), and any in-process buffer written to disk. The plaintext original left aside by `healthspan db encrypt` is not a temporary file but follows the same disposal policy ([ADR-0033](adr/0033-plaintext-artifact-disposal.md)).

---

## Database Security

**Parameterized queries only.** No SQL statement in the codebase may be constructed by string interpolation or concatenation of user-supplied values. All user input reaches the database exclusively through parameterized query placeholders. This is enforced by code review convention and, where possible, by linting.

**Single database owner.** Only the Core Service process holds a runtime database connection. The CLI holds a connection only for an explicitly invoked `db`/`keys` maintenance subcommand — `db migrate`, `db backup`, `db encrypt` ([ADR-0033](adr/0033-plaintext-artifact-disposal.md)), `keys change-passphrase`, `keys rotate-secret-key` — and never while Core Service is running (each of these refuses to start against a live service — [ADR-0038](adr/0038-backup-execution-and-verification.md), [ADR-0028](adr/0028-key-derivation-and-rotation.md), [ADR-0033](adr/0033-plaintext-artifact-disposal.md)). No other process accesses the database directly, and no two processes hold connections to the live file at the same time.

**Database file permissions.** The SQLite file must be created with owner-read-write-only permissions (`chmod 600` equivalent). The platform should warn on startup if the database file has broader permissions.

**No database credentials in logs.** The database path may appear in startup logs. Query contents and result values must not.

---

## Input Validation

**Validate at the boundary.** All data entering the system through the REST API is validated before any database interaction. The Core Service is the validation boundary; no process assumes that data it receives from another process has already been validated.

**Bulk import validation.** Import requests are fully parsed and validated before any writes begin. Validation errors are collected across the entire batch and returned together — not one at a time. A dry-run mode is available that performs full validation without writing. Writes are wrapped in a transaction; a failure at any point rolls back the entire batch.

**Conflict policy must be explicit.** Bulk import requests must specify a conflict policy (`reject`, `skip`, or `upsert`). There is no default that silently mutates existing data.

**No raw filesystem paths from callers.** A caller-supplied path is a path-traversal primitive (read side) or an arbitrary-file-write primitive (write side). File-typed job parameters are relative paths contained inside configured directories (`[jobs.files]` import/export roots), validated centrally by the job framework — resolved real path (symlinks followed) must sit inside a resolved root; absolute paths rejected; rejection errors reveal nothing about whether the target exists. Ad-hoc local imports upload file *content* via the bulk import endpoint instead of passing a server-side path. See [ADR-0012](adr/0012-job-abstraction.md), File Path Validation.

---

## Dependency Security

**Pin all dependencies.** Production dependencies are pinned to exact versions in the lockfile. Unpinned dependencies are a supply chain risk.

**Audit regularly.** Run `pip-audit` (or equivalent) as part of the development workflow and before releases. Known vulnerabilities in dependencies are treated as bugs.

**Minimize dependencies.** Each process pulls in only the dependencies it needs. The Core Service, MCP Server, and CLI have separate dependency sets where practical.

---

## Plugin Security

Plugin code executes in a host process determined by the plugin's type — **never in the Core Service**. The host-process matrix, its enforcement mechanism, and the full reasoning are defined in [ADR-0025](adr/0025-plugin-host-process-matrix.md). In summary:

*Summary of [ADR-0025](adr/0025-plugin-host-process-matrix.md)'s `HOST_LOADABLE_TYPES` — that ADR is authoritative.*

| Host process | Plugin types loaded |
|---|---|
| CLI | `cli`, `import_adapter`, `reference_ranges`, `analysis`, `query`, `provider` |
| MCP Server | `mcp_tool`, `analysis`, `query`, `provider` |
| Automation Host | `automation`, `notification_channel`, `analysis`, `query`, `provider` |
| Job child process | `import_adapter`, `analysis`, `query`, `provider` (single-plugin load — only the executing job type's handler) |
| Core Service | **None — the Core Service never loads plugin code** (INV-2) |

The security boundary must be clearly communicated:

- Plugins are a **trusted-user feature**. Only install plugins you have read and trust.
- The platform does not sandbox plugins. A malicious plugin has full access to anything its **host process** can reach — including that process's bearer token and the config file — but never the encryption key or the database, which are reachable only from the Core Service (INV-1, INV-2, INV-3).
- Plugin authors must not store or transmit health data outside the local system without explicit user consent and documentation.
- This boundary must be documented prominently in any user-facing plugin documentation. See ADR-0010 and ADR-0025.

**Plugin package supply chain.** Catalog-governed `PLUGIN_PACKAGES` install from a hash-pinned lockfile in `--require-hashes` mode — a version pin alone does not authenticate content; the sha256 hash does. A hash mismatch is a hard failure, never a warning. The catalog is generated mechanically at release time, and that resolution honors the publication age gate ([ADR-0020](adr/0020-plugin-registry.md)) so freshly published versions cannot be locked in. Off-catalog packages are content-unauthenticated unless the declaration supplies hashes; the confirmation warning says so explicitly. The plugin loader validates every plugin (API version, dependency graph, cycles, conflicts) before installing any package, and reads declaration metadata statically — a plugin that fails validation neither executes code nor installs anything. See [ADR-0036](adr/0036-plugin-package-installation-integrity.md).

---

## Logging

**No health data values in logs.** Log entries may contain: timestamps, endpoint names, HTTP status codes, biomarker names, request counts, error types, and process lifecycle events. Log entries must not contain: biomarker result values, reference ranges, clinical notes, intervention details, or any other health data payload.

**Log levels.** `INFO` is the default. `DEBUG` may include request/response metadata but must still exclude health data values. No log level permits health data in log output.

---

## AI Client Interface

**No health data in error responses beyond what is necessary.** Error responses from the Core REST API and MCP Server should describe what went wrong without echoing back sensitive input values.

**The MCP server does not trust the AI client.** Tool call inputs from the AI client are validated by the Core REST API identically to any other client. The AI client is not granted elevated trust.

**Prompt injection awareness.** Data retrieved from the database and returned to the AI client may contain user-authored text (clinical notes, intervention descriptions). This text could contain prompt injection attempts if the database has been compromised or if data from untrusted sources has been imported. The MCP server should not construct tool responses in ways that amplify injected instructions. This requirement is made concrete and testable by the MCP tool output contract in [api-reference.md](api-reference.md): untrusted free text is returned inside delimited, instruction-shielded data blocks with per-response random boundaries, and every tool is row-capped and paginated. The MCP server's default read-only token (ADR-0026) bounds the impact of any injected instructions to data exfiltration — they cannot mutate or delete data — and the row caps mean exfiltration requires many visible, auditable tool calls rather than one.

---

## Versioning and Interface Stability

**"AI client" not a specific product name.** No interface definition, configuration key, API endpoint, or documentation may reference a specific AI product by name. "AI client" is the correct term. See ADR-0007.

**Versioned interfaces reduce attack surface drift.** REST API versioning (`/v1/`), plugin interface versioning, and config schema versioning ensure that security controls are not accidentally bypassed when interfaces evolve. See design-rationale.md for the full list of versioning surfaces.

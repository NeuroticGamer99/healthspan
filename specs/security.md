# Security Requirements

This is a standing requirements document. All implementation work is measured against these requirements. Security is designed in from the start — not retrofitted.

This document covers the platform's own security posture. It does not cover the security of third-party services (AI providers, cloud backup, data sources).

---

## Trust Model

The platform operates across three distinct trust layers, each with different properties. Understanding these layers is essential for reasoning about what is and is not protected.

**Storage layer — zero-knowledge for the provider**
The SQLCipher-encrypted database file is opaque ciphertext. Any party that holds the file — a cloud backup provider, a sync service, a drive — never sees plaintext. The decryption key never leaves the user's control (OS keychain + Recovery Kit). Cloud backup and sync of the encrypted file is explicitly safe. The storage provider is in the "do not trust" tier and the encryption model handles this correctly.

**Processing layer — must trust the Core Service machine**
Decryption happens inside the Core Service process. The machine running Core Service must be trusted — its operator has potential access to decrypted health data. This is inherent: querying data requires decrypting it. The Core Service cannot be delegated to an untrusted host without accepting that the host operator can read the data. There is no software-only solution to this constraint.

**Transport layer — conditional on deployment**
Communication between processes on the same trusted machine (localhost HTTP) is safe. Any communication that crosses a machine boundary — LAN, Docker, remote clients — requires HTTPS. This is not negotiable: health data in transit over an unencrypted network connection is a serious vulnerability regardless of how well the file at rest is protected.

### Implications for deployment

| Deployment | Storage trust | Processing trust | Transport requirement |
|---|---|---|---|
| Single local machine (default) | Cloud sync safe | User's own machine | None (localhost) |
| Encrypted file on cloud storage | Cloud provider untrusted — safe | Core Service machine | HTTPS if Core Service remote |
| Core Service on a home server | File sync safe | Home server operator | HTTPS between devices |
| Core Service on a cloud VM | File sync safe | Cloud VM provider can see plaintext | HTTPS required |

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
| INV-1 | The derived database key exists only in Core Service memory. It is never transmitted, logged, or inherited by child processes (jobs use `spawn`, never `fork`). | The key is the single secret the entire encryption-at-rest story rests on. | [ADR-0013](adr/0013-encryption-at-rest.md), [ADR-0025](adr/0025-plugin-host-process-matrix.md) |
| INV-2 | The Core Service never executes code from the plugins directory. First-party in-core components ship inside the `healthspan` package and are imported explicitly. | The plugins directory is the platform's invited-code channel; keeping it out of the key-holding process makes plugin isolation true by architecture, not by audit. | [ADR-0025](adr/0025-plugin-host-process-matrix.md) |
| INV-3 | A plugin's maximum handed capability is its host process's plugin-tier credential; escalation requires deliberate, named token issuance by the user. | The host assignment bounds *where* plugin code runs; the credential tier bounds *what it is given*. | [ADR-0025](adr/0025-plugin-host-process-matrix.md), [ADR-0026](adr/0026-named-scoped-tokens.md) |
| INV-4 | Plugins alter Core Service behavior only via data submitted through the validated REST API. | Data is inert and validated at the boundary; code is not. | [ADR-0025](adr/0025-plugin-host-process-matrix.md) |
| INV-5 | Every issued credential is named, scoped, and revocable; no anonymous shared credential exists, and `admin` scope is never handed to directory-loaded plugin code. | Bounds prompt-injection blast radius to the holder's scopes; control-plane subversion requires a visible, audited issuance event rather than a silent copy. | [ADR-0026](adr/0026-named-scoped-tokens.md) |

---

## Authentication

**Bearer token on every HTTP endpoint.** The Core REST API and MCP Server require a valid `Authorization: Bearer <token>` header on every request, including requests from localhost. No endpoint is unauthenticated.

**Named, scoped, revocable tokens — no shared credential.** Every credential the platform issues is named (identifies its holder), scoped (grants an explicit capability subset: `read`, `write`, `import`, `events`, `jobs`, `admin`), and individually revocable. Each client — GUI, MCP Server, CLI, Automation Host, webhook callers, job children — holds its own token with default scopes per the matrix in [ADR-0026](adr/0026-named-scoped-tokens.md). The MCP server's token is read-only by default. The inbound webhook token can only publish events. Job children receive ephemeral single-job tokens. See INV-5.

**Event publication is namespace-bound.** Tokens carrying `events` publish only within their declared event namespaces; the event `source` field is stamped from token identity, never caller-supplied; and reserved namespaces (`data.*`, `job.*`, `schedule.*`, `schema.*`, `system.*`, `plugin.*`) are emitted only by the Core Service itself. Forged internal events are structurally impossible — the event system cannot be used to launder scopes through automations, and a forged `alert.resolved` cannot mask a genuine clinical alert. See ADR-0026.

**Token generation and verification.** Tokens are minted at first run (cryptographically random, minimum 32 bytes, base64url-encoded, `hsp_<name>_` prefixed). The Core Service stores only SHA-256 hashes of token values; verification uses `secrets.compare_digest`. Plaintext values exist only at issuance and in each client's own storage.

**Token storage.** Each client stores only its own token, in the OS keychain via `keyring` (per-client config file fallback with owner-read-only permissions for headless deployments). **No token is stored in the shared TOML config file** — a shared file is how a single token becomes every client's token.

**Credential tiers for plugins.** Directory-loaded plugins receive their host process's plugin-tier token via `context.api` — never a credential carrying `admin`. Package-shipped first-party plugins receive the process credential. See ADR-0026 and INV-3.

**Lifecycle.** `healthspan token create | list | revoke | rotate` (admin scope; `list` shows names and scopes, never values). Revocation is immediate; there is no grace overlap.

**Rate limiting and audit.** Failed authentication attempts are rate-limited per source address with exponential backoff, including from localhost. Auth events (token *name*, endpoint, outcome — never token values, never health data) are recorded in an append-only audit log; all `admin`-scoped actions are always audited. See ADR-0026.

**Config file permissions.** The TOML config file must be created with owner-read-only permissions (`chmod 600` equivalent). The platform should warn on startup if the config file has broader permissions.

**No token in URLs.** Bearer tokens must only appear in the `Authorization` header, never in query strings or URL paths (where they would appear in server logs and browser history).

---

## Network Security

**DNS rebinding protection.** The platform defends against DNS rebinding attacks (malicious websites making requests to localhost services) with two independent controls:

1. **Host header validation** — the server rejects any request whose `Host` header does not match a configured expected value. Configured in TOML. Default: `localhost` and `127.0.0.1`.
2. **Bearer token authentication** — browsers cannot send custom `Authorization` headers cross-origin without a CORS preflight. The server denies preflights from non-allowlisted origins, blocking the attack even if host validation were bypassed.

**CORS.** All HTTP services use an explicit allowlist of permitted origins. Default: empty (deny all cross-origin requests). Users configure allowed origins in TOML for their specific AI client or web UI.

**Binding address.** Default binding is `127.0.0.1` (localhost only). LAN or Docker deployment requires explicitly setting the binding address in config. Binding to `0.0.0.0` on a non-localhost address requires HTTPS and is documented as requiring additional network-level controls (firewall, VPN).

**HTTPS.** Required for any non-localhost binding. For localhost development, HTTPS is recommended but not required. For LAN deployment, use a self-signed certificate with pinning, or a local CA (mkcert). For internet-exposed deployment (not a supported use case for personal health data), use a trusted CA.

---

## Encryption at Rest

The database file contains sensitive personal health data and must be encrypted. Encryption must be decided and implemented before any data is written — retrofitting it onto an existing database is destructive and error-prone.

**SQLCipher** is the recommended solution: AES-256-CBC, transparent to the application (query code is unchanged), cross-platform, and compatible with the SQLite backup API. See [ADR-0013](adr/0013-encryption-at-rest.md).

**Key management** uses a two-factor hybrid model inspired by 1Password's Secret Key design: a randomly generated secret key stored in the OS keychain (`keyring`) combined with a user master passphrase. Neither component alone decrypts the database. Cross-device portability is provided by a printable **Recovery Kit** — a QR code of the secret key with a blank for the handwritten passphrase — generated at init and stored securely offline. A passphrase-only mode is available for users who want full portability without any OS keychain dependency. See [ADR-0013](adr/0013-encryption-at-rest.md) for the full threat model and implementation requirements.

**Key derivation** ([ADR-0028](adr/0028-key-derivation-and-rotation.md)): `db_key = Argon2id(password = NFC-normalized UTF-8 passphrase, salt = 32-byte secret key)`, 32-byte output, parameters at or above the OWASP floor and recorded per database in a non-secret `.keyparams` sidecar that travels with the database file. The key is handed to SQLCipher in raw-hex form (`PRAGMA key = "x'<64 hex>'"`), skipping SQLCipher's internal PBKDF2 — the single sanctioned exception to the no-interpolation rule below, as only locally generated, format-validated hex reaches it. The key is derived once at Core Service startup, held in process memory (`SecretStr` discipline) with a persistent connection pool, and zeroed best-effort at shutdown — per-request re-derivation defends nothing the threat model protects.

**Rotation** ([ADR-0028](adr/0028-key-derivation-and-rotation.md)): `healthspan keys change-passphrase` and `healthspan keys rotate-secret-key` rekey the database in place after taking a mandatory verified backup. Rotation is not retroactive — old backups open only with the credentials in force when they were made. Secret-key rotation invalidates all previously printed Recovery Kits.

**Platform note:** under the `uv tool install` distribution (ADR-0023), the OS keychain client is the Python interpreter — on every platform, including macOS, same-user code can access the stored secret key without a prompt. The passphrase (never stored, standard mode) is what keychain compromise alone does not yield. See ADR-0028 for the correction of ADR-0013's macOS per-app ACL claim.

**The encryption key must never be:**
- Stored in plaintext in the TOML config file
- Hardcoded in source code
- Committed to version control
- Logged

**Migration path:** For users with an existing unencrypted database, `healthspan db encrypt` migrates using SQLCipher's `sqlcipher_export()`, verifies the encrypted copy (opens with the derived key, `PRAGMA integrity_check`, row-count comparison), and then requires an explicit decision about the plaintext original: user-confirmed best-effort disposal (the default), or deliberate retention via `--keep-plaintext` with a prominent warning. A plaintext health database is never silently left on disk. See [ADR-0033](adr/0033-plaintext-artifact-disposal.md).

**Clinical document originals:** Imported source documents (lab PDFs, CCDA exports, FHIR document payloads) are stored as content-addressed BLOBs inside the encrypted database — there is no plaintext document directory. After a verified import, the importer offers ADR-0033 disposal of the source file, so the only durable copy can be the encrypted one. See [ADR-0034](adr/0034-clinical-document-storage.md).

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

**Single database owner.** Only the Core Service process holds a runtime database connection. The CLI holds a connection only for migrations and backup, and only when those subcommands are explicitly invoked. No other process accesses the database directly.

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

| Host process | Plugin types loaded |
|---|---|
| CLI | `cli`, `import_adapter`, `reference_ranges`, `analysis`, `query`, `provider` |
| MCP Server | `mcp_tool`, `analysis`, `query`, `provider` |
| Automation Host | `automation`, `notification_channel`, `analysis`, `query`, `provider` |
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

**Prompt injection awareness.** Data retrieved from the database and returned to the AI client may contain user-authored text (clinical notes, intervention descriptions). This text could contain prompt injection attempts if the database has been compromised or if data from untrusted sources has been imported. The MCP server should not construct tool responses in ways that amplify injected instructions. The MCP server's default read-only token (ADR-0026) bounds the impact of any injected instructions to data exfiltration — they cannot mutate or delete data.

---

## Versioning and Interface Stability

**"AI client" not a specific product name.** No interface definition, configuration key, API endpoint, or documentation may reference a specific AI product by name. "AI client" is the correct term. See ADR-0007.

**Versioned interfaces reduce attack surface drift.** REST API versioning (`/v1/`), plugin interface versioning, and config schema versioning ensure that security controls are not accidentally bypassed when interfaces evolve. See design-rationale.md for the full list of versioning surfaces.

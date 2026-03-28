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

**Least privilege per process.** Each process has access only to what it needs. The GUI has no database credentials. The import pipeline has no MCP configuration. The MCP server has no write access beyond what the Core REST API permits. See ADR-0006.

**Defense in depth.** No single control is relied upon alone. Authentication, host header validation, and CORS work together. A bypass of one does not grant access.

**Secure by default.** The default configuration is the most secure configuration. Users must explicitly opt into less restrictive settings (e.g. changing the binding address for LAN deployment). Security is never traded for convenience in the defaults.

**Health data never leaves the process boundary unintentionally.** Logs, error messages, and diagnostic output must not contain health data values. Metadata (timestamps, biomarker names, query counts) is acceptable in logs; result values are not.

---

## Authentication

**Bearer token on every HTTP endpoint.** The Core REST API and MCP Server require a valid `Authorization: Bearer <token>` header on every request, including requests from localhost. No endpoint is unauthenticated.

**Token generation.** The bearer token is generated automatically on first run (cryptographically random, minimum 32 bytes, base64url-encoded). It is stored in the shared TOML config file. It is never hardcoded, never committed to version control, and never logged.

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

**Key management** uses a two-factor hybrid model inspired by 1Password's Secret Key design: a randomly generated secret key stored in the OS keychain (`keyring`) combined with a user master passphrase, derived together via Argon2id. Neither component alone decrypts the database. Cross-device portability is provided by a printable **Recovery Kit** — a QR code of the secret key with a blank for the handwritten passphrase — generated at init and stored securely offline. A passphrase-only mode is available for users who want full portability without any OS keychain dependency. See [ADR-0013](adr/0013-encryption-at-rest.md) for the full threat model, platform asymmetries, and implementation requirements.

**The encryption key must never be:**
- Stored in plaintext in the TOML config file
- Hardcoded in source code
- Committed to version control
- Logged

**Migration path:** For users with an existing unencrypted database, `healthspan db encrypt` migrates in place using SQLCipher's `sqlcipher_export()`, verifies integrity, and retains the original as a backup.

---

## Temporary Files

Any temporary files created during import, export, migration, or processing that contain health data must be handled with the same care as the database itself:

- **Restricted permissions.** Temporary files must be written to a directory with owner-read-write-only permissions (`chmod 600` equivalent on the file, `chmod 700` on the directory). Never write to `/tmp`, `%TEMP%`, or any world-readable location.
- **Secure deletion.** Temporary files must be overwritten with zeroes (or equivalent) before deletion — not merely unlinked. Unlinking a file on most filesystems does not erase its contents from disk; the data remains recoverable until the blocks are reallocated.
- **No shared directories.** Temporary files must not be written to a directory that any other user or process can read, including shared system temp directories.
- **Failure cleanup.** If a process exits abnormally while a temporary file exists, the startup sequence must detect and securely delete any orphaned temporary files before proceeding.

This applies to: import staging files, export staging files, database migration intermediaries, SQLCipher `sqlcipher_export()` output files, and any in-process buffer written to disk.

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

---

## Dependency Security

**Pin all dependencies.** Production dependencies are pinned to exact versions in the lockfile. Unpinned dependencies are a supply chain risk.

**Audit regularly.** Run `pip-audit` (or equivalent) as part of the development workflow and before releases. Known vulnerabilities in dependencies are treated as bugs.

**Minimize dependencies.** Each process pulls in only the dependencies it needs. The Core Service, MCP Server, and CLI have separate dependency sets where practical.

---

## Plugin Security

Plugins execute arbitrary Python code in the CLI process. This is intentional and enables full extensibility, but the security boundary must be clearly communicated:

- Plugins are a **trusted-user feature**. Only install plugins you have read and trust.
- The platform does not sandbox plugins. A malicious plugin has full access to anything the CLI process can reach, including the bearer token and config file.
- Plugin authors must not store or transmit health data outside the local system without explicit user consent and documentation.
- This boundary must be documented prominently in any user-facing plugin documentation. See ADR-0010.

---

## Logging

**No health data values in logs.** Log entries may contain: timestamps, endpoint names, HTTP status codes, biomarker names, request counts, error types, and process lifecycle events. Log entries must not contain: biomarker result values, reference ranges, clinical notes, intervention details, or any other health data payload.

**Log levels.** `INFO` is the default. `DEBUG` may include request/response metadata but must still exclude health data values. No log level permits health data in log output.

---

## AI Client Interface

**No health data in error responses beyond what is necessary.** Error responses from the Core REST API and MCP Server should describe what went wrong without echoing back sensitive input values.

**The MCP server does not trust the AI client.** Tool call inputs from the AI client are validated by the Core REST API identically to any other client. The AI client is not granted elevated trust.

**Prompt injection awareness.** Data retrieved from the database and returned to the AI client may contain user-authored text (clinical notes, intervention descriptions). This text could contain prompt injection attempts if the database has been compromised or if data from untrusted sources has been imported. The MCP server should not construct tool responses in ways that amplify injected instructions.

---

## Versioning and Interface Stability

**"AI client" not a specific product name.** No interface definition, configuration key, API endpoint, or documentation may reference a specific AI product by name. "AI client" is the correct term. See ADR-0007.

**Versioned interfaces reduce attack surface drift.** REST API versioning (`/v1/`), plugin interface versioning, and config schema versioning ensure that security controls are not accidentally bypassed when interfaces evolve. See design-rationale.md for the full list of versioning surfaces.

# ADR-0013: Encryption at Rest

## Status
Accepted

## Context and Problem Statement
The database contains years of sensitive personal health data: lab results, medications, clinical events, CGM readings, body composition measurements, and contextual logs. If the device is stolen, the drive is imaged, or an attacker has physical access without knowing the user's login credentials, this data must be unreadable. Encryption must be applied from the first migration — retrofitting encryption onto an existing plaintext database is invasive and error-prone.

A related problem is key management: encryption is only meaningful if the key is stored securely. The key must not appear in config files, source code, environment variables, or application logs.

## Decision Drivers
- Encryption must be transparent — no change to query logic, schema, or migrations after connection is established
- Key must be stored securely without burdening the user with manual key management in the common case
- Must work on Windows, macOS, and Linux
- Must be applied from day one — not a future enhancement
- The threat model and its limits must be honestly documented so users understand what they are and are not protected against

## Considered Options

### Database encryption
- SQLCipher — transparent AES-256 SQLite encryption extension
- Filesystem-level encryption (BitLocker, FileVault, LUKS) — OS-managed, not application-managed
- No encryption

### Key management
- OS keychain via `keyring` Python library
- Passphrase-derived key via Argon2
- Key file stored separately from the database

## Decision Outcome

**Database encryption:** SQLCipher
**Key management:** OS keychain via `keyring`, with passphrase-derived key as a supported alternative

These are independent choices that combine: the database is always encrypted with SQLCipher; how the key is stored is user-configurable, defaulting to the OS keychain.

### Positive Consequences
- The database file is opaque ciphertext at rest — unreadable without the key regardless of how the file is obtained
- OS keychain default requires zero user action beyond normal OS login — transparent to non-technical users
- `keyring` is a well-audited, widely deployed library (used by pip, poetry, AWS CLI, GitHub CLI, Jupyter) — not an experimental choice
- Passphrase option gives technically sophisticated users explicit control and cross-device portability
- Encryption is transparent after connection open — no changes to queries, schema, migrations, or the ORM layer

### Negative Consequences / Tradeoffs
- `sqlcipher3` replaces `sqlite3` as the database driver — a build dependency change, not a logic change
- OS keychain does not protect against malware running as the same user (see Threat Model section)
- Headless Linux deployments require additional keyring configuration (see Linux section)

---

## Database Encryption: SQLCipher

SQLCipher is an open source extension to SQLite that applies transparent 256-bit AES encryption to the database file. The Python binding is `sqlcipher3`.

The API is identical to `sqlite3` with one additional call at connection open time:

```python
import sqlcipher3

conn = sqlcipher3.connect("biocontext.db")
conn.execute(f"PRAGMA key='{encryption_key}'")
# All subsequent operations are identical to standard SQLite
```

After the `PRAGMA key` call, the connection behaves exactly like an unencrypted SQLite connection. Queries, schema, and migrations require no changes.

The database file on disk is opaque ciphertext. It cannot be read by SQLite tools, hex editors, or forensic tools without the key.

---

## Key Management: OS Keychain via `keyring`

The `keyring` Python library provides a single cross-platform API over each operating system's native credential storage:

| Platform | Backend | Security basis |
|---|---|---|
| Windows | Windows Credential Manager (DPAPI) | User account + login password |
| macOS | macOS Keychain | Login keychain + per-app ACL |
| Linux (GNOME) | libsecret / GNOME Keyring | User session login |
| Linux (KDE) | KWallet | User session login |

```python
import keyring
import secrets

# First run: generate and store
key = secrets.token_hex(32)
keyring.set_password("biocontext", "db_encryption_key", key)

# Every subsequent run: retrieve
key = keyring.get_password("biocontext", "db_encryption_key")
```

The key never appears in a config file, environment variable, source file, or log. It is generated once, stored by the OS, and retrieved automatically after user login.

### Why `keyring` is the right choice

The alternatives are either the same OS APIs accessed directly (what `keyring` wraps), or worse approaches:

- Direct platform SDKs (`win32crypt`, `pyobjc`, `secretstorage`) — `keyring` already calls these internally; bypassing it gains nothing and loses portability
- Config file — plaintext or weakly obfuscated; readable by any process
- Environment variable — readable via `/proc/self/environ` on Linux; not persistent
- Custom encrypted file — reinventing the OS keychain, worse in every respect

`keyring` is used by pip, poetry, AWS CLI, GitHub CLI, Jupyter, and the majority of Python tooling that requires credential storage. It is not experimental. The security properties come from the OS, not the library — `keyring` is a thin, well-audited wrapper that calls the same underlying OS functions as any direct implementation would.

### macOS vs Windows/Linux security asymmetry

**macOS**: Keychain enforces a per-application access control list (ACL). An application not on the ACL receives a system-level "Allow?" prompt before access is granted. A Nuitka-compiled binary with hardened runtime enabled receives this protection automatically. macOS Gatekeeper and app notarization further reduce the risk of malicious code reaching the keychain.

**Windows and Linux**: Windows Credential Manager and GNOME Keyring do not enforce per-application ACL for programmatically accessed credentials. Any process running as the current user can call `keyring.get_password()` and retrieve the key without a prompt. The protection is against external attackers — theft, drive imaging, other user accounts — not against same-user malicious processes.

This asymmetry is a platform characteristic, not a `keyring` deficiency. There is no Python library that provides per-application credential ACL on Windows or Linux because the OS does not expose that capability for generic credential storage.

---

## Key Management: Two-Factor Hybrid with Recovery Kit (Recommended)


Inspired by 1Password's Secret Key model, the recommended approach combines the OS keychain with a user passphrase to derive the encryption key from two independent components:

```
db_key = Argon2id(master_passphrase + secret_key)
```

The **secret key** is a randomly generated 32-byte value stored in the OS keychain — never typed by the user under normal operation. The **master passphrase** is known only to the user — never stored anywhere by default. Neither component alone is sufficient to reconstruct the database key.

### First run

```
biocontext init
```

1. Generate a random secret key; store in OS keychain
2. Prompt user to set a master passphrase
3. Derive `db_key = Argon2id(passphrase + secret_key)`
4. Initialize SQLCipher database with `db_key`
5. Prompt user to print or save the Recovery Kit before proceeding

### Daily operation

On the primary machine the secret key is retrieved automatically from the OS keychain. The user enters their passphrase at startup (or optionally caches it in the keychain for full auto-unlock — see modes below). Zero setup; startup is fast.

### Recovery Kit

The Recovery Kit is a printable document generated once at `biocontext init` and on demand via `biocontext keys recovery-kit`. It contains:

- The secret key encoded as a **QR code** (for scanning with a phone camera) and as a **Base32 string** (for manual entry if QR fails)
- A blank line to **handwrite the master passphrase** — it is never printed
- Step-by-step recovery instructions for a new machine
- A warning that the kit must be stored securely (safe, safety deposit box, etc.)

The kit is useless without the passphrase. The passphrase alone is useless without an enrolled device or the kit. An attacker needs both.

### New machine setup

```
biocontext init --restore
```

1. User scans the Recovery Kit QR code (or types the Base32 string)
2. User enters master passphrase
3. `db_key = Argon2id(passphrase + secret_key)` — database unlocks
4. Secret key is stored in the new machine's OS keychain for subsequent auto-retrieval

### Operation modes

| Mode | Daily experience | Security |
|---|---|---|
| **Standard (recommended)** | OS keychain holds secret key; user types passphrase at startup | Two-factor: both must be compromised |
| **Full auto-unlock** | OS keychain holds both secret key and passphrase | Convenient; collapses to single-factor protection |
| **Passphrase-only** | No secret key component; no OS keychain dependency | Single-factor; fully portable without a kit |

Full auto-unlock stores the passphrase in the OS keychain for zero-friction startup. It sacrifices the two-factor security benefit but retains the Recovery Kit portability benefit.

### Comparison with prior approaches

| | Pure OS keychain | Pure passphrase | Two-factor hybrid |
|---|---|---|---|
| Daily friction | None | Enter passphrase | Enter passphrase |
| Survives OS reinstall | No | Yes | Yes — via Recovery Kit |
| Cross-device portability | No | Yes | Yes — via Recovery Kit |
| Single component compromise sufficient | Yes | Yes | **No — needs both** |
| Recovery if all credentials lost | None | None | None |

Neither option has a recovery path if all credentials are lost (forgotten passphrase + lost Recovery Kit). This is intentional and standard for encrypted local storage. It must be documented prominently in user-facing documentation.

## Key Management: Passphrase-Only (Alternative)

For users who want full portability without any OS keychain dependency, the passphrase-only mode is available:

```
biocontext init --key-from-passphrase
```

The key is derived solely from the passphrase using Argon2id. No secret key is generated. No OS keychain is used. The passphrase alone unlocks the database on any machine. This is the lowest-friction cross-device option but provides only single-factor protection.

---

## Threat Model

Honest documentation of security limits is itself a security requirement. Users who misunderstand their protection may take risks they would not otherwise take.

### Protected against

| Threat | Protection |
|---|---|
| Device theft or loss | Database file is ciphertext; reconstruction requires both the secret key (from Recovery Kit or enrolled device) and the passphrase |
| Drive imaging with physical access | Same — attacker cannot read DB without both components |
| Other user accounts on the same machine | OS keychain is per-user; credentials are isolated |
| Credentials left in config files or source code | Neither component is ever written to a file other than the OS keychain |
| Casual forensic analysis of the database file | Ciphertext is opaque without SQLCipher and the correct key |
| OS keychain compromise alone (Windows/Linux) | Attacker has the secret key but still needs the passphrase (standard mode) |
| Recovery Kit found by an attacker | Kit is useless without the passphrase |

### Not protected against

| Threat | Reason |
|---|---|
| Malware running as the current user (Windows/Linux) | Same-user processes can access the OS keychain without a prompt on these platforms |
| Malware running as the current user (macOS) | Harder due to per-app ACL and Gatekeeper, but not impossible for a sophisticated attacker |
| Memory scraping while the database is open | The key and decrypted data exist in process memory during active queries |
| An attacker with existing user-level access | At this level, data can be exfiltrated directly without touching the database |
| User who forgets their passphrase (passphrase mode) | No recovery path — intentional |

### Context: this is the industry standard

1Password, Bitwarden, Obsidian, Signal Desktop, and every other local-first encrypted application operate under the same fundamental constraints. Encryption at rest using an OS keychain is the established, widely-reviewed approach for this threat model. It is not a compromise — it is the correct solution for the threats it addresses.

Software-only solutions cannot protect against malware running with the same user privileges. This is a platform-level limitation, not an application design failure. Hardware-backed key storage (see Future Hardening Path) can close this gap but is not viable cross-platform today.

---

## Implementation Requirements

**Hot backups are encrypted.** SQLCipher uses the standard SQLite Online Backup API (`sqlite3_backup`). Backups produced by `biocontext db backup` are encrypted ciphertext — not plaintext copies. The backup file requires the same key as the source database to open. There is no path from a hot backup to a plaintext database file without the key.

**Connection lifetime**: The SQLCipher connection must be closed when not actively in use. The Core Service opens a connection per request session and closes it on completion. The key must not be held in memory indefinitely.

**Logging prohibition**: The encryption key must never appear in any log output, error message, stack trace, or diagnostic output. Enforced by treating the key as a `SecretStr` type (Pydantic) from the moment it is retrieved.

**Plugin isolation**: Plugins never access the encryption key. Plugins call the Core REST API; the Core Service manages all database connections. The key never crosses the process boundary.

**Code signing**: The Nuitka-compiled binary should be signed and notarized for macOS distribution. A signed binary with hardened runtime receives improved Keychain ACL behavior automatically.

**`context.credentials` reuse**: The same `keyring` backend used for the database key is used by `PluginContext.credentials` for external service credentials (Dexcom OAuth tokens, Fitbit credentials, etc.). One library, one security model, consistently applied.

---

## Headless Linux

On Linux without a desktop session (Docker, VPS, SSH-only), no GNOME Keyring or KWallet daemon is running. `keyring` falls back to the `keyrings.alt` file-based backend.

For containerized deployment the recommended approach is passphrase-derived key mode with the passphrase injected via a Docker secret at startup. This is a documented deployment variant, not the default. See ADR-0008 for the process lifecycle and deployment variants.

---

## Future Hardening Path

Hardware-backed key storage — where key material never leaves a secure hardware enclave, providing protection even against processes running as the same user — is the meaningful next step:

- **Windows**: TPM via Windows CNG API
- **macOS**: Secure Enclave via CryptoKit / Security framework
- **Android**: Android Keystore

Cross-platform Python access to these systems is not mature today. When the ecosystem matures, a hardware-backed keyring backend can be added without changing the SQLCipher layer — the interface remains `keyring.get_password()`; the backend changes underneath it.

---

## Pros and Cons of the Options

### SQLCipher (chosen)
- Pro: Transparent — no changes to queries, schema, or migration logic after connection open
- Pro: Industry standard for SQLite encryption, widely audited, actively maintained
- Pro: Cross-platform (Windows, macOS, Linux)
- Con: Requires `sqlcipher3` build dependency instead of stdlib `sqlite3`

### Filesystem-level encryption (BitLocker, FileVault, LUKS)
- Pro: No application code changes required
- Con: Not controlled by the application — depends entirely on user having enabled it
- Con: Does not protect against other users with filesystem access on the same machine
- Con: Cannot be enforced or verified at application startup

### No encryption
- Con: Database is plaintext; readable by any process and by any attacker with physical access
- Con: Unacceptable for a health data platform

---

## Links
- Related: [specs/security.md](../security.md) — platform-wide security requirements
- Related: [ADR-0006](0006-application-architecture.md) — Core Service owns all DB connections; plugins never access the DB directly
- Related: [ADR-0008](0008-process-lifecycle.md) — headless Linux deployment variant
- Related: [ADR-0010](0010-cli-plugin-model.md) — `context.credentials` uses the same `keyring` backend
- Resolves: key management open question in [open-questions.md](../open-questions.md)

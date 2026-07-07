# ADR-0028: Key Derivation, Rotation, and Key Lifetime

## Status
Proposed

## Context and Problem Statement
ADR-0013 (Accepted) decided SQLCipher encryption with a two-factor key model. The 2026-06-10 architecture review found four defects in its specification — none reversing the decision, all in how it is realized. ADR-0013 is immutable under ADR governance, so this ADR extends it:

1. **The KDF construction is underspecified** (review 2.2). `Argon2id(passphrase + secret_key)` is naive concatenation with no salt, no encodings, and no parameters — not implementable deterministically, and re-derivation *must* be deterministic across machines, library versions, and years. The `PRAGMA key='{key}'` f-string example is a quoting hazard that contradicts security.md's no-string-interpolation rule, and passing a passphrase-style key makes SQLCipher run its internal PBKDF2 over the output of a stronger KDF that already ran.
2. **There is no rotation story** (review 2.3). Neither the passphrase nor the secret key can be changed.
3. **The connection-lifetime requirement is self-contradictory** (review 3.C). "Open a connection per request and don't hold the key in memory" is impossible: opening the next connection requires the key — so either it is held anyway, or Argon2id (hundreds of milliseconds, by design) runs on every request. The threat model already concedes memory scraping.
4. **The Nuitka assumptions are load-bearing** (review 1.C). The code-signing requirement and the macOS Keychain per-app ACL claim both assume a signed, hardened-runtime compiled binary. ADR-0023 replaced Nuitka with `uv tool install`: the keychain client is now the Python interpreter in a uv venv, and any same-user Python script presents the same identity.

## Decision Drivers
- Key derivation must be exactly reproducible: same inputs → same key, on any machine, any library version, forever
- Argon2id parameters must meet the OWASP floor and be upgradeable without locking the user out
- The database file must stay portable: everything needed to re-derive (except the two credentials) travels with it
- Rotation must exist, and must not be able to destroy the only copy of the database
- Runtime behavior must match the threat model actually documented in ADR-0013 — no security theater that costs hundreds of milliseconds per request
- Platform claims must reflect the real distribution mechanism (ADR-0023), to the same honesty standard as ADR-0013's threat-model table and ADR-0026's limitations section

## Considered Options
1. **Status quo** — leave the construction to implementation-time interpretation
2. **Secret-key-as-salt Argon2id, raw-hex key handoff, parameter sidecar, rekey-based rotation, derive-once key lifetime** (chosen)
3. **Separate stored salt + HKDF combine step** — a random non-secret salt in both modes, with the secret key mixed in as additional keying material

## Decision Outcome
Chosen option: **Option 2.**

Option 3 adds a stored non-secret salt even in two-factor mode for no gain: the secret key is already random, per-user, 32 bytes, and secret — it satisfies every property a salt needs and better (this is 1Password's construction). Option 1 is the defect being fixed.

### Positive Consequences
- Derivation is specified to the byte: implementable, testable (deterministic known-answer tests — testing-strategy.md already lists Argon2id determinism as a unit target), and stable across versions
- SQLCipher's internal PBKDF2 is skipped — startup pays for one strong KDF, not two chained ones
- The quoting hazard is gone: the raw-key PRAGMA accepts only locally generated, format-validated hex
- Passphrase and secret key are both rotatable, with a mandatory verified backup making rekey failure recoverable
- Argon2id parameters can be raised over time (rotation rewrites the sidecar) without breaking old databases
- The macOS claim now matches what `uv tool install` actually delivers

### Negative Consequences / Tradeoffs
- A `.keyparams` sidecar file must travel with the database — one more file in backups and restores (mitigated: `healthspan db backup` handles it; a missing sidecar produces an explanatory error, and the file is reconstructable from documented defaults if the parameters were never changed)
- The derived key resides in Core Service memory for the process lifetime — an explicit acceptance, not a new exposure (the threat model never protected against memory scraping)
- The macOS threat-model row gets *weaker* on paper — because it was overstated, not because anything regressed

---

## Key Derivation Construction

```
db_key = Argon2id(
    password  = UTF-8 encoding of the NFC-normalized master passphrase,
    salt      = the 32-byte secret key (raw bytes),
    hash_len  = 32,
    m, t, p   = per the .keyparams record
)
```

- **Passphrase encoding:** Unicode-normalize to NFC (`unicodedata.normalize("NFC", ...)`), then UTF-8 encode. Without pinned normalization, the same passphrase typed on two machines can differ at the byte level (composed vs decomposed accents) and silently fail to unlock.
- **Secret key:** generated once as `secrets.token_bytes(32)`. Raw bytes are the KDF salt. Human-facing representations — the OS keychain entry and the Recovery Kit — use RFC 4648 Base32 (no padding, grouped for readability); the Base32 decodes back to the same 32 bytes. The Recovery Kit QR encodes the Base32 string.
- **Two-factor mode needs no stored salt** — the secret key *is* the salt, and does double duty: salt properties (random, per-user, unique) plus second-factor secrecy. **Passphrase-only mode** substitutes a random 32-byte salt generated at init and stored in the `.keyparams` sidecar — salts are not secrets; the mode's security still rests on the passphrase alone, exactly as ADR-0013 documents.
- **Parameters:** initial values are argon2-cffi's current defaults (t=3, m=64 MiB, p=4) — comfortably above the recorded floor of the OWASP minimums (m ≥ 19 MiB, t ≥ 2, p ≥ 1), below which `healthspan init` and rotation refuse to write a `.keyparams` record. The parameters *in force for this database* are read from the sidecar, never from library defaults: a library upgrade that shifted defaults would otherwise silently derive a different key and lock the user out.

**Key handoff to SQLCipher** uses the raw-key form:

```python
conn.execute(f"""PRAGMA key = "x'{db_key.hex()}'";""")
```

This is deliberate on two counts. First, SQLCipher's passphrase form runs PBKDF2 internally — pointless latency after Argon2id has already run, and weaker than what it post-processes. The raw form uses the 32 bytes directly. Second, this replaces ADR-0013's `PRAGMA key='{encryption_key}'` example, which interpolated an arbitrary string into SQL. The raw form interpolates only the output of `.hex()` on locally generated bytes, validated against `^[0-9a-f]{64}$` before use — this is the single sanctioned exception to security.md's no-interpolation rule, and it is an exception in appearance only: no user-supplied data can reach it.

## The `.keyparams` Sidecar

A small plaintext file created by `healthspan init` next to the database (`healthspan.db.keyparams`), recording what re-derivation needs but must not guess:

| Field | Content |
|---|---|
| `format` | Sidecar format version |
| `kdf` | `argon2id`, with the Argon2 version number (19) |
| `m`, `t`, `p`, `hash_len` | The parameters in force for this database |
| `mode` | `two-factor` or `passphrase-only` |
| `salt` | Base64; present only in passphrase-only mode |
| `created_utc`, `rotated_utc` | Provenance timestamps |

**Nothing in the sidecar is secret** — KDF parameters and salts are safe in plaintext; what they are not safe from is *loss*. The sidecar therefore lives with the database and follows it everywhere: `healthspan db backup` copies it alongside every backup, restore requires it, and new-machine setup (`healthspan init --restore`) expects the database file and its sidecar together. It still gets owner-only permissions like every platform file. It is rewritten only by `healthspan init` and the rotation commands below.

A missing sidecar fails with an error that says exactly how to recover (restore the sidecar from any backup of the same key generation; or, if parameters were never changed from the documented initial defaults, regenerate it).

## Rotation

Two commands, both new:

```
healthspan keys change-passphrase     New passphrase; secret key unchanged
healthspan keys rotate-secret-key     New secret key; passphrase unchanged; new Recovery Kit
```

Shared mechanics:

1. **Exclusive access required** — the commands refuse to run while the Core Service is up (single-writer, security.md). This is a CLI direct-database operation like `db migrate` (ADR-0006).
2. **Mandatory verified backup first** — the command runs `healthspan db backup`, then *verifies the backup* per [ADR-0038](0038-backup-execution-and-verification.md)'s definition (opens with the current key, `PRAGMA integrity_check` passes, `schema_version` matches the source), before touching the live file. `PRAGMA rekey` rewrites every page of the database in place; a crash mid-rekey can corrupt the whole file, and unlike a bad row deletion the blast radius is everything. A `--no-backup` flag exists for scripted expert use and prints what it is skipping.
3. Derive the old key, open and verify; derive the new key; `PRAGMA rekey = "x'<64 hex>'"` (same raw-hex discipline as `PRAGMA key`); update the sidecar's `rotated_utc`.
4. **Parameter upgrades ride along**: rotation re-reads the recommended parameters and, if the floor or defaults have risen, writes the new values to the sidecar — the natural moment, since the key is being re-derived anyway.

`change-passphrase` additionally: prompts for the new passphrase twice; the Recovery Kit remains valid (it holds the secret key; the passphrase line is handwritten — the user updates it).

`rotate-secret-key` additionally: generates fresh `secrets.token_bytes(32)`, replaces the keychain entry, regenerates the Recovery Kit and prompts to print it — **every previously printed kit is now invalid**, and the command says so explicitly before proceeding.

**Rotation is not retroactive.** Backups open with the credentials in force when they were made — an encrypted backup is ciphertext under the old key; nothing about rotating the live database re-encrypts old files. Both commands print this consequence and the choice it implies: re-create backups under the new credentials, or retain the old credentials (old Recovery Kit, old passphrase) until old backups have aged out. User documentation must carry the same warning prominently.

## Key and Connection Lifetime

This section replaces ADR-0013's "Connection lifetime" implementation requirement, which was unimplementable as written:

- **Derive once, at Core Service startup.** The user's passphrase is read, the key derived, and the passphrase buffer discarded immediately. Argon2id's cost is paid exactly once per process lifetime. (How the passphrase reaches the Core Service — TTY, stdin pipe, or OS-secret-facility file, never argv or environment — is specified in [ADR-0039](0039-startup-sequence-and-passphrase-handoff.md), which also accepts the launcher's own derivation for the migration phase as a second, discarded, KDF run per platform start.)
- **Hold the raw 32-byte key in Core Service process memory** for the life of the process, wrapped in the same `SecretStr` discipline ADR-0013 already requires (never logged, never in tracebacks). This is honest, not lax: ADR-0013's own threat-model table lists memory scraping of the open database as *not protected against* — releasing the key between requests defended nothing while costing a full KDF run or a held key anyway.
- **Maintain a persistent connection pool** keyed once at connection creation. Per-request open/close forfeits SQLite's page cache and adds keying overhead for zero threat-model benefit; a pool is also how SQLite serves concurrent reads well under FastAPI. (Per-connection pragma discipline — `foreign_keys`, journal mode, `synchronous`, `busy_timeout` — is recorded in [ADR-0035](0035-migration-execution-semantics.md); the pool's structure — thread-affine, one connection per worker thread, sized in [ADR-0037](0037-core-service-concurrency-and-driver.md) — is decided there.)
- **Best-effort zeroization on shutdown**: the key buffer is overwritten before process exit. Stated honestly: CPython's memory model (immutable bytes, GC copies) makes guaranteed zeroization impossible — this is hygiene, not a boundary.
- Everything downstream is unchanged: the key never crosses a process boundary (ADR-0025, INV-1), job children are spawned — never forked — precisely so they do not inherit this held key (ADR-0012), and plugins reach data only through the REST API.

## Distribution Reality: Code Signing and the macOS Keychain

ADR-0013's "Code signing" implementation requirement — sign and notarize "the Nuitka-compiled binary" — is **withdrawn**: under ADR-0023 there is no application binary to sign. With it goes the macOS advantage it anchored:

- **Corrected claim:** under `uv tool install`, the process requesting keychain access is the Python interpreter in a uv-managed venv. The macOS Keychain ACL grants access to that binary identity — which means *any* same-user code that can execute Python holds the same identity. The per-app ACL still exists, but it no longer distinguishes Healthspan from other Python running as the user.
- **Threat-model correction** (to ADR-0013's "Not protected against" table): malware running as the current user is not meaningfully harder on macOS than on Windows/Linux for keychain access specifically. Gatekeeper and notarization still raise the bar for malware *getting onto* the machine; they no longer gate access to Healthspan's keychain entries once it is there.
- **What restores it:** a future packaged, signed distribution (an app bundle with hardened runtime) would re-establish a distinct ACL identity — a future ADR alongside ADR-0023 if pursued. The TPM / Secure Enclave hardening path in ADR-0013 is unaffected and remains the real answer to same-user attackers.

The two-factor model is what actually carries the weight here, unchanged: keychain compromise alone still yields only the secret key; the passphrase is still required (standard mode). This correction is about not claiming a platform advantage the distribution mechanism no longer delivers — the same standard as ADR-0026's "What This Does Not Protect."

## Consequences for Other Documents

- **ADR-0013** (Accepted): navigation link only — `Extended by ADR-0028`; content untouched per governance
- **security.md**: Encryption at Rest section gains the precise construction, sidecar, rotation commands, and key-lifetime statements; the macOS asymmetry language is corrected where it appears
- **testing-strategy.md**: already lists Argon2id determinism as a unit target; gains known-answer derivation vectors, sidecar round-trip, and rekey tests
- **Architecture review**: items 1.C, 2.2, 2.3, 3.C resolved

## Pros and Cons of the Options

### Status quo
- Pro: nothing to write
- Con: underivable spec, interpolation hazard, double KDF, no rotation, self-contradictory lifetime rule, overstated macOS claim — the review's findings stand unaddressed

### Secret-key-as-salt + raw-hex + sidecar + rekey + derive-once (chosen)
- Pro: byte-exact and testable; one KDF; rotation with a safety net; runtime matches the threat model; honest platform posture
- Con: sidecar file to carry; withdrawn macOS claim reads as a downgrade (it is a correction)

### Separate stored salt + HKDF combine
- Pro: uniform salt handling across both key modes
- Con: extra stored state in two-factor mode that the secret key already provides; a second derivation primitive (HKDF) in the audit surface for no security gain

## Links
- Extends: [ADR-0013](0013-encryption-at-rest.md) — the two-factor SQLCipher decision this ADR makes precise
- Related: [ADR-0023](0023-distribution-mechanism.md) — `uv tool install` is what invalidates the signing assumptions
- Related: [ADR-0026](0026-named-scoped-tokens.md) — keyring usage and the shared honesty standard for same-user limits
- Related: [ADR-0012](0012-job-abstraction.md) / [ADR-0025](0025-plugin-host-process-matrix.md) — spawn-not-fork exists because this key is held in Core memory (INV-1)
- Related: [ADR-0019](0019-multi-device-sync.md) — backup files and their credential generations
- Related: [ADR-0033](0033-plaintext-artifact-disposal.md) — disposal rules for Recovery Kit renders (regenerated by `rotate-secret-key`) and the verified-then-dispose `db encrypt` flow
- Extended by: [ADR-0037](0037-core-service-concurrency-and-driver.md) — the connection pool's thread-affine structure and sizing
- Extended by: [ADR-0038](0038-backup-execution-and-verification.md) — backup verification definition adopted by the pre-rekey backup; CLI exclusive-access discipline extended to `db backup`
- Extended by: [ADR-0039](0039-startup-sequence-and-passphrase-handoff.md) — the passphrase handoff channel (TTY/stdin/secret-file, never argv/env) that derive-once-at-startup presupposes
- Related: [specs/security.md](../security.md) — encryption requirements; no-interpolation rule and its single sanctioned exception
- Resolves: [architecture review 2026-06-10](../architecture-review-2026-06-10.md), items 1.C, 2.2, 2.3, 3.C

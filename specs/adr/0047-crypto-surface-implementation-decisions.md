# ADR-0047: Crypto-Surface Implementation Decisions (WI-2)

## Status
Accepted

## Context and Problem Statement
Implementing the Phase-1 crypto core (ADR-0013/0028/0033: KDF, sidecar, connection factory, `healthspan init`, the `keys` commands) surfaced decisions those ADRs deliberately left open, plus one owner decision made at Phase-1 planning that needs a spec record. ADR-0028 and ADR-0033 are Accepted and immutable, so per governance these land as one batched extension ADR (the ADR-0035/0037/0038 pattern) in the same PR as the implementing change.

## Decision Drivers
- Every externally observable contract (file formats, names, prompts a script must answer) must be recoverable from the specs without reading source
- Immutable ADRs are extended, not edited; batching accumulated defaults into one extension ADR is the accepted cost
- Phase sequencing: WI-2 lands before the migration runner (WI-3) and the backup/restore CLI (WI-4); the decisions below make that ordering explicit rather than accidental

## Considered Options
1. Record each decision in a separate small ADR
2. **One batched extension ADR for the WI-2 crypto surface** (chosen)

## Decision Outcome
Chosen: **option 2** — the decisions share one context (the WI-2 implementation) and one review; fine-grained ADRs would add ceremony without adding recoverable history.

### 1. Passphrase advisory policy (owner decision 2026-07-10)
Passphrase length is advisory, never enforced: below **12 characters**, `healthspan init` and `keys change-passphrase` warn and require an explicit confirmation, but never refuse. Rationale: the owner is the only user; a refused passphrase pushes toward writing one down insecurely, and the two-factor mode's security does not rest on passphrase entropy alone. The threshold is a constant (`PASSPHRASE_ADVISORY_MIN`), not config surface.

### 2. `.keyparams` sidecar file format
ADR-0028 fixed the sidecar's fields; this ADR fixes the encoding: **TOML**, UTF-8, read with `tomllib`, written by the platform only (`init`, rotation, conversion). Concrete key names: `format` (integer, currently `1`), `kdf` (`"argon2id"`), `argon2_version` (`19`), `m`/`t`/`p`/`hash_len` (integers; `m` in KiB), `mode` (`"two-factor"` | `"passphrase-only"`), `salt` (Base64, passphrase-only mode only), `created_utc`/`rotated_utc` (ISO-8601 UTC; `rotated_utc` omitted until first rotation). **Unknown keys are read errors** — the same strictness rationale as ADR-0046's config loading, sharpened here because the sidecar is integrity-sensitive (ADR-0028's floor-enforcement reasoning). TOML over JSON for consistency with the platform's one existing config format and its comment-friendliness for a file users may inspect.

### 3. OS keychain entry naming
The secret key is stored under service **`healthspan`**, entry name **`secret-key`**, as the same grouped Base32 string the Recovery Kit shows (ADR-0028's human-facing representation) — what a user inspects in their OS keychain matches what they would type from paper. This concretizes ADR-0013's illustrative `db_encryption_key` example, which predates the two-factor terminology: the entry holds the *secret key* (one factor), not the database key.

### 4. Recovery Kit rendering: WI-2 scope and the qrcode dependency
The kit renders **in memory to text** (ADR-0033's avoidance-first rule): grouped Base32 secret key, a handwritten-passphrase line, custody instructions, and a **QR code drawn with Unicode half-block characters** encoding the Base32 string — scannable from the terminal screen and from a monospace printout. Rendering uses the **`qrcode`** library (new dependency; pure-Python, no image stack — the half-block render needs no Pillow/pypng). Display is the default; a deliberate digital copy exists only via `--output <path>` with ADR-0033's encrypted-storage warning, owner-only permissions, and the `healthspan-recovery-kit-<date>.txt` naming matched by the repository's existing `*recovery-kit*` gitignore pattern.

**Kit display can never be lost to I/O.** When the output stream's encoding cannot carry the half-block cells (stdout redirected under a legacy Windows code page), the kit renders QR-free with a note instead of crashing; a `--output` file-write failure warns and keeps the terminal render as the user's copy. The terminal render always precedes the file write.

**Deferred, explicitly:** ADR-0033's OS print pathways (`lp`/`lpr` streaming, Windows temp-file shell print with verified disposal) and the orphan startup sweep. The sweep's natural home is the startup failure-cleanup that arrives with the Core Service (Phase 2); the print pathways land with it or with the user-documentation milestone, whichever comes first (tracked in [open-questions.md](../open-questions.md), Operations).

### 5. Pre-rekey backup: internal primitive in WI-2, CLI in WI-4
ADR-0028 mandates a verified backup before every rekey, but `healthspan db backup` is a later work item. Resolution: WI-2 implements the **verified-backup primitive** (native backup API into a `.partial` file, sidecar copied and byte-compared, verification = opens with the current key + full `PRAGMA integrity_check` + `schema_version` match, atomic rename to final names — ADR-0038's pipeline ordering) as an internal routine the rotation commands call. WI-4's `db backup` command wraps the same routine with retention and scheduling polish. Backup pairs are published as `healthspan-<UTC timestamp>Z.db` + `.db.keyparams` in the configured `[backup] directory`; before the schema_version table exists (pre-WI-3), "version match" is vacuously `None = None`. `--no-backup` exists on every rekey command and prints what it is skipping, per ADR-0028.

### 6. `healthspan init` surface
- **Creates an empty encrypted database** (keyed file, `journal_mode=WAL` and `application_id` set per ADR-0035) and the sidecar; the schema arrives via `healthspan db migrate` (WI-3). Init prints that next step. Rationale: credentials and provisioning are independent of the schema, and WI ordering should not force users through a half-specified combined flow.
- **Creates a skeleton config file** at the platform-default location if none exists (creation is the writer's job, ADR-0046): `config_version = 1`, the **database path pinned as an active value** to the location init actually created the file (so a future change in platform-default resolution can never orphan the encrypted database), and the remaining defaults as comments.
- **Two-factor ordering: keychain first.** The secret key is stored in the OS keychain *before* any file is created — a keychain outage aborts init with nothing on disk, instead of leaving an encrypted database whose key was never stored or shown. An orphaned entry from a later failure is harmless (the next successful init overwrites it). If file creation fails after that, init removes the partial database/sidecar (safe: the database is empty at init) so a re-run is not wedged on the overwrite guard.
- **Owner-only protection on everything it writes** (config, database, sidecar, kit renders, backup directory): POSIX `0600`/`0700`; on Windows, ACL replacement via `icacls` — inheritance removed, a single full-control grant to the current user (ADR-0046's write-side enforcement).
- **Refuses to overwrite**: an existing database or sidecar aborts init with guidance, never silently re-keys.
- **`init --restore`** (adopt an existing database + sidecar + kit on a new machine, ADR-0013) is **deferred to WI-4** with `db restore`, which owns the verify-then-install mechanics it needs.

### 7. Rekey durability ordering
ADR-0028's rotation mechanics list the sidecar update after `PRAGMA rekey` as a sequence of effects; realized naively, a write failure after the rekey leaves the database encrypted under parameters no file on disk records (in passphrase-only mode the new salt would exist only in memory). The implementation therefore orders for durability:

1. **Stage first**: the new sidecar is written to `<sidecar>.pending` *before* the rekey — every refusable write (disk full, ACLs) fires while the old credentials still open the database, and the operation aborts cleanly with the pending file removed.
2. **Rekey**, then **install atomically**: `os.replace(pending, sidecar)` — a same-directory atomic swap. A crash in the narrow window between rekey and install leaves the new parameters on disk in the pending file; the unlock error path detects a stray `.pending` and says how to recover.
3. **Keychain writes never outrank the kit**: after a successful rekey, a failure storing the new secret key (or deleting the old one on downgrade) is downgraded to a prominent warning on the result — the Recovery Kit is always rendered, so a keychain outage can never discard the only copy of a live key.

### 8. Exclusive-access guard timing
ADR-0028 requires rekey commands to refuse while the Core Service is up. No Core Service exists in Phase 1; the enforceable check arrives with ADR-0042's single-instance lock (Phase 2), and the rekey commands adopt it then. Until that lands, protection is what SQLite provides: the rekey's write transaction fails against a concurrent writer rather than corrupting.

### Positive Consequences
- Every WI-2 contract a client, script, or future implementer needs is on the record; the deferred obligations (print pathways, orphan sweep, `init --restore`, the ADR-0042 guard) have named owners and arrival points
- The ADR-0028 rekey-safety net exists from the first rekey ever executed, despite `db backup` landing later

### Negative Consequences / Tradeoffs
- A batched ADR is less granular history than per-topic ADRs — accepted, per the routing rules' batching allowance
- The terminal QR (half-block cells) prints acceptably only in monospace contexts; a print-grade kit (PDF or image QR) is deferred with the print pathways and may add an imaging dependency then

## Pros and Cons of the Options

### Separate small ADRs per topic
- Pro: independently supersedable records
- Con: seven ADRs for one implementation's defaults; shared context repeated seven times

### One batched extension ADR (chosen)
- Pro: one review, one context, matches the established ADR-0035/0037/0038 batching pattern
- Con: a future reversal of any single item must supersede this ADR in part (precedent exists: partial supersession by section)

## Links
- Extends: [ADR-0028](0028-key-derivation-and-rotation.md) — passphrase advisory, sidecar encoding, keychain entry name, pre-rekey backup locus, exclusive-access timing
- Extends: [ADR-0033](0033-plaintext-artifact-disposal.md) — kit render scope in WI-2; print pathways and orphan sweep deferral
- Extends: [ADR-0046](0046-filesystem-layout-and-config-discovery.md) — init's skeleton-config creation and Windows ACL mechanics
- Related: [ADR-0013](0013-encryption-at-rest.md) — key modes, keychain storage, `init --restore` (deferred here)
- Related: [ADR-0038](0038-backup-execution-and-verification.md) — verification definition adopted by the internal primitive
- Related: [ADR-0035](0035-migration-execution-semantics.md) — persistent pragmas set at provisioning
- Related: [ADR-0042](0042-process-supervision-and-single-instance-locking.md) — the lock the rekey guard adopts in Phase 2

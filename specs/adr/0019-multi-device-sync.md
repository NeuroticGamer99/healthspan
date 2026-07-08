# ADR-0019: Multi-Device Sync

## Status
Proposed

## Context and Problem Statement
A user who works on both a desktop and a laptop needs the database to stay in sync across devices. The current local-first SQLite design has no sync mechanism. ADR-0003 decided SQLite-only for v1, so a PostgreSQL backend on a shared server is not an available answer without a new ADR (which must also revisit migrations and encryption); sync semantics (conflict resolution, offline use, which device is authoritative) are unaddressed either way.

ADR-0013 changes the sync picture materially on confidentiality: the SQLCipher-encrypted database file is opaque ciphertext, so a cloud storage provider or sync service that holds it never sees plaintext. But confidentiality is not the same as safety to sync. The live database runs in WAL mode (`*.db-wal` / `*.db-shm` are gitignored, and Core Service holds a persistent connection per ADR-0028) — a sync client can snapshot `db` and `-wal` at different instants mid-write, capturing a torn, internally inconsistent copy. Encryption makes this worse, not better: a torn ciphertext file cannot be partially salvaged or diagnosed the way a torn plaintext file sometimes can. **Only the output of `healthspan db backup` — a checkpointed, single-file, self-consistent snapshot — is safe to sync continuously. The live database file must never be synced while Core Service may be writing to it.**

## Decision Drivers
- Local-first is the primary deployment model; sync is an enhancement, not a requirement
- The encrypted database file is confidentiality-safe for any untrusted storage provider (ADR-0013) — but confidentiality does not imply the file is safe to sync while live; WAL mode makes an in-progress sync of the live file a torn-copy hazard, independent of encryption
- Only `healthspan db backup` output is a checkpointed, self-consistent snapshot safe to sync continuously
- SQLite has a single-writer constraint: concurrent writes from multiple machines corrupt the database; only one Core Service instance may write at a time
- Offline use (editing on a device without network access) requires a merge/conflict model
- Health data conflicts (same biomarker result entered on two devices) have clear resolution rules (last write wins is dangerous; human review is safer)
- A server-based backend is the natural path for true multi-master access, but ADR-0003 decided SQLite-only for v1 — that path requires a new ADR

## Near-Term Approach: Single-Writer + Cloud Sync

The recommended near-term approach for multi-device use:

1. **One active Core Service at a time.** Only one machine runs the Core Service and holds a write connection to the database. Other devices read via the Core REST API over the LAN/VPN, or wait until the primary machine is accessible.

2. **Sync backup output only, never the live database file.** The live `db` file (plus its WAL-mode `-wal`/`-shm` companions) must not be placed in Dropbox, iCloud Drive, OneDrive, or any similar sync service — a sync client can capture it mid-write and deliver a torn, unrecoverable copy on the receiving machine. Instead, the scheduled in-service `backup.database` job ([ADR-0038](0038-backup-execution-and-verification.md)) writes a checkpointed, self-consistent, **verified**, encrypted snapshot to a designated backup directory, published atomically so a sync client never observes a partial or unverified file; that directory — and only that directory — is what gets synced. (`healthspan db backup` is the offline/manual path, for use while Core Service is stopped.) Cloud sync of backup output provides:
   - Continuous off-site backup (the cloud provider cannot read the ciphertext, and each synced file is internally consistent)
   - A mechanism to transfer the database between machines: restore from the latest synced backup (`healthspan db restore --latest`, [ADR-0038](0038-backup-execution-and-verification.md)), never by copying the live file
   - Version history (most sync services retain deleted/overwritten versions)

3. **Single-writer enforcement.** Only one Core Service instance may hold the write connection to the live database file at a time; that file stays local to the machine running Core Service and is never itself a sync target. Core Service enforces this locally by holding an **OS advisory lock** on a sentinel file alongside the database for its entire process lifetime ([ADR-0042](0042-process-supervision-and-single-instance-locking.md)) — a second instance attempting to start fails to acquire the lock and refuses. The kernel releases the lock when the holder exits, so a crash or power loss cannot leave a stale lock that blocks the next legitimate start. Moving the active database to a new machine is a restore operation — stop Core Service, transfer the latest backup (via sync or manual copy), restore it as the new live file with `healthspan db restore` ([ADR-0038](0038-backup-execution-and-verification.md)), start Core Service there.

4. **Backup is the only sync-safe artifact.** The `backup.database` job — and, offline, `healthspan db backup` — produces a checkpointed, verified, encrypted backup file (ADR-0013, [ADR-0038](0038-backup-execution-and-verification.md)) that is safe to sync continuously. The live database file is never safe to sync while Core Service may be writing to it, regardless of encryption.

## Future Approach: Multi-Master Sync

True offline multi-master sync (both machines write, later reconcile) requires either:
- A purpose-built CRDT or operational transform layer on top of SQLite
- Switching to a backend that natively handles distributed writes (PostgreSQL) — a new ADR superseding ADR-0003's SQLite-only decision, which must also revisit ADR-0009 (migrations) and ADR-0013/0028 (encryption)

A PostgreSQL backend over a LAN or VPN with a single authoritative server would be the natural path when more than one machine needs simultaneous write access, at the cost of that ADR chain. CRDT-based SQLite sync is a more complex future direction.

## Decision Outcome
TBD — deferred for multi-master. Near-term recommendation is single-writer, with cloud sync limited to `healthspan db backup` output — never the live database file. See above for the recommended pattern.

## Links
- Related: [ADR-0003](0003-database-backend.md) — SQLite-only for v1; multi-master write access requires a new ADR revisiting migrations and encryption
- Related: [ADR-0008](0008-process-lifecycle.md) — Docker Compose deployment supports LAN access
- Related: [ADR-0013](0013-encryption-at-rest.md) — encrypted file is cloud-safe from a confidentiality standpoint; hot backups are the sync-safe artifact
- Related: [ADR-0028](0028-key-derivation-and-rotation.md) — persistent connection held for the life of Core Service; the live file is under active write at any time it runs
- Related: [ADR-0038](0038-backup-execution-and-verification.md) — how the sync-safe artifact is actually produced and verified
- Related: [ADR-0042](0042-process-supervision-and-single-instance-locking.md) — the OS advisory lock that enforces this ADR's single-writer rule locally, replacing the plain lock file
- Related: [specs/security.md](../security.md) — Trust Model section; storage layer is zero-knowledge

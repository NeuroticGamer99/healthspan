# ADR-0019: Multi-Device Sync

## Status
Proposed — stub

## Context and Problem Statement
A user who works on both a desktop and a laptop needs the database to stay in sync across devices. The current local-first SQLite design has no sync mechanism. ADR-0003 (pluggable database) partially addresses this — a PostgreSQL backend on a shared server provides multi-device access — but the sync semantics (conflict resolution, offline use, which device is authoritative) are not addressed.

ADR-0013 changes the sync picture materially: the SQLCipher-encrypted database file is opaque ciphertext. A cloud storage provider or sync service that holds the file never sees plaintext. Cloud backup and cloud sync of the encrypted file are explicitly safe — the storage provider is untrusted and the encryption model handles this correctly.

## Decision Drivers
- Local-first is the primary deployment model; sync is an enhancement, not a requirement
- The encrypted database file is cloud-safe (ADR-0013) — no data exposure risk from cloud backup or sync
- SQLite has a single-writer constraint: concurrent writes from multiple machines corrupt the database; only one Core Service instance may write at a time
- Offline use (editing on a device without network access) requires a merge/conflict model
- Health data conflicts (same biomarker result entered on two devices) have clear resolution rules (last write wins is dangerous; human review is safer)
- The pluggable database backend (ADR-0003) is the natural path for true multi-master access

## Near-Term Approach: Single-Writer + Cloud Sync

The recommended near-term approach for multi-device use:

1. **One active Core Service at a time.** Only one machine runs the Core Service and holds a write connection to the database. Other devices read via the Core REST API over the LAN/VPN, or wait until the primary machine is accessible.

2. **Cloud sync as backup and transfer.** The encrypted database file can be safely placed in Dropbox, iCloud Drive, OneDrive, or any similar sync service. Cloud sync provides:
   - Continuous off-site backup (the cloud provider cannot read the ciphertext)
   - A mechanism to transfer the database between machines without manual copy
   - Version history (most sync services retain deleted/overwritten versions)

3. **Single-writer enforcement.** To prevent concurrent write corruption, Core Service writes a lock file alongside the database. If another machine's sync client delivers an updated database file while Core Service is running, it must not be swapped in while the connection is open. The recommended pattern: sync service syncs the file; Core Service checks modification time at startup and warns if the file changed unexpectedly while it was offline.

4. **Hot backup is encrypted.** `healthspan db backup` produces an encrypted backup file (ADR-0013). Backup files can be safely synced to cloud storage alongside the live database.

## Future Approach: Multi-Master Sync

True offline multi-master sync (both machines write, later reconcile) requires either:
- A purpose-built CRDT or operational transform layer on top of SQLite
- Switching to a backend that natively handles distributed writes (PostgreSQL via ADR-0003)

The PostgreSQL backend (ADR-0003) over a LAN or VPN with a single authoritative server is the recommended path when more than one machine needs simultaneous write access. CRDT-based SQLite sync is a more complex future direction.

## Decision Outcome
TBD — deferred for multi-master. Near-term recommendation is single-writer + cloud sync of the encrypted file. See above for the recommended pattern.

## Links
- Related: [ADR-0003](0003-database-backend.md) — PostgreSQL backend is the recommended path for true multi-master write access
- Related: [ADR-0008](0008-process-lifecycle.md) — Docker Compose deployment supports LAN access
- Related: [ADR-0013](0013-encryption-at-rest.md) — encrypted file is cloud-safe; hot backups are encrypted
- Related: [specs/security.md](../security.md) — Trust Model section; storage layer is zero-knowledge

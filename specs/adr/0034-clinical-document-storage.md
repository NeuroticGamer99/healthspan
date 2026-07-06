# ADR-0034: Clinical Document Original File Storage

## Status
Proposed

## Context and Problem Statement
The clinical documents data type ([data-model.md](../data-model.md)) stores a `source_file_hash` — the SHA-256 of the original imported file — which implies the original (lab PDF, CCDA export, FHIR DocumentReference payload) is retained somewhere. Nothing specified where. The [architecture review](../architecture-review-2026-06-10.md) (item 2.9) flagged the default failure mode: a plain `documents/` directory next to the database would be plaintext PHI outside the SQLCipher encryption boundary, silently voiding the trust model's guarantees ([security.md](../security.md)) — "cloud sync of the encrypted file is safe" and "device theft yields ciphertext" are both false the moment a folder of original PDFs sits beside the database.

Originals are worth retaining: extraction into the `body` text column is lossy (layout, signatures, charts in lab reports), parser improvements motivate re-extraction, and the source document itself has reference value. So the question is not *whether* to keep them but *where* — inside which encryption boundary.

## Decision Drivers
- Every guarantee the platform makes about data at rest is scoped to the SQLCipher boundary ([ADR-0013](0013-encryption-at-rest.md)); plaintext PHI outside it is the exact class of defect the review's section 2 targets
- One security surface is cheaper than two: backup ([ADR-0019](0019-multi-device-sync.md)), rotation ([ADR-0028](0028-key-derivation-and-rotation.md)), and disposal ([ADR-0033](0033-plaintext-artifact-disposal.md)) are already specified for the database; a second store would need all three answered again
- Deletes and corrections are transactional and audited ([ADR-0027](0027-audit-trail-and-corrections.md)); document originals must not be able to diverge from their rows
- This is a personal health archive: realistically hundreds of documents over years at 0.1–10 MB each — on the order of a gigabyte worst case, not a clinic's document volume
- Imaging *reports* are text documents in scope; DICOM imaging data is explicitly out of scope for this table

## Considered Options
1. **BLOBs inside the main SQLCipher database, content-addressed** (chosen)
2. **Second SQLCipher database** (`documents.db`) keyed from the same credentials
3. **Per-file encrypted store** — content-addressed `docs/<sha256>.enc`, write-once files
4. **Do not retain originals** — extracted text and hash only

## Decision Outcome
Chosen: **option 1 — originals are stored as BLOBs inside the main encrypted database.**

A content-addressed `document_files` table (`sha256` UNIQUE, content bytes, MIME type, byte size, import timestamp) holds each original exactly once; clinical-document rows reference it by hash. There is no plaintext document directory, ever.

- **Deduplication** is the UNIQUE constraint on `sha256` — the job `source_file_hash` was already doing, now enforced by the schema. Re-importing the same file attaches to the existing blob.
- **Transactional integrity**: document row and original are inserted, hard-deleted, and audited in one transaction under [ADR-0027](0027-audit-trail-and-corrections.md)'s rules. No DB-says-yes-filesystem-says-no divergence, no orphan class, no new startup sweep.
- **Size guardrail**: imports warn above 25 MB per file and refuse above 100 MB (both configurable). The caps exist to keep the accepted tradeoffs below honest — a refused file is a signal the user is storing something (DICOM series, scanned book) this table was not designed for.
- **Retrieval**: originals are served through the Core REST API (streaming; SQLite's incremental blob I/O supports this). The GUI prefers in-memory rendering (avoidance first, [ADR-0033](0033-plaintext-artifact-disposal.md)); saving an original back to disk is an explicit user export, warned as leaving the encryption boundary like any [ADR-0015](0015-data-export.md) output.
- **MCP exposure**: the queryable surface for AI clients is the extracted `body` text; binary originals are not exposed through MCP tools by default.

### Closing the ingestion loop (ADR-0033 tie-in)
After a successful import — blob stored, hash verified against the source — the importer (CLI or watch-folder) offers disposal of the *source* file under the [ADR-0033](0033-plaintext-artifact-disposal.md) policy: user-confirmed best-effort disposal, honestly caveated, FDE recommendation printed. Declining is fine (the source is user-custody plaintext, like an export); the point is that the platform makes "the only durable copy is encrypted" a one-keystroke outcome instead of leaving a plaintext copy behind by default silence.

### Accepted tradeoffs (stated honestly)
- **Backups grow with the archive.** `healthspan db backup` copies every stored document every time; there is no incremental backup. At the expected scale this means backups grow from megabytes toward a gigabyte over years — cheap to store, slower to copy.
- **Rotation slows with the archive.** `PRAGMA rekey` ([ADR-0028](0028-key-derivation-and-rotation.md)) rewrites every page, and the mandatory pre-rekey backup copies them all. Rotation is a rare, deliberate operation; seconds-to-minutes is acceptable.
- Both costs scale with document volume, which is why the guardrail and the escape hatch below exist.

### The escape hatch — encrypted cold store (future, if ever needed)
If document volume ever outgrows the single-file model, the designed escape is option 3 as an **extension ADR**: a content-addressed per-file encrypted store (`docs/<sha256>.enc`, write-once, keys wrapped by the database key), holding the blob bytes while the `document_files` row keeps hash and metadata. Its genuine attractions — immutable files are torn-write-proof and therefore live-sync-friendly (unlike the live database, [ADR-0019](0019-multi-device-sync.md)), and incremental backup becomes free — are real but purchased with a second encryption surface: rotation must re-wrap keys, referential integrity spans DB and filesystem, and restore becomes multi-artifact. Not justified at personal scale. The API contract is deliberately storage-agnostic (documents are fetched by hash through the REST API), so the split would change no client, no schema consumer, and no MCP tool — only the blob's location.

### Positive Consequences
- No plaintext PHI outside the encryption boundary — the trust model's at-rest guarantees stay true with documents in the system
- Zero new security machinery: encryption, rotation, backup, sync safety, and disposal are all inherited decisions
- Originals survive parser improvements; re-extraction is always possible
- Ingestion can end with no plaintext copy anywhere, by user choice

### Negative Consequences / Tradeoffs
- Backup size and rekey duration scale with the document archive (bounded by the guardrail; escape hatch designed)
- Viewing an original outside the GUI requires an explicit export step — mild friction that is the boundary working as intended

## Pros and Cons of the Options

### BLOBs in the main SQLCipher database (chosen)
- Pro: one boundary, one backup artifact, transactional deletes/audit, dedup by constraint, all prior security decisions inherited
- Con: backup and rekey costs scale with archive size; no incremental backup

### Second SQLCipher database
- Pro: keeps the hot database lean; rekey of primary stays fast
- Con: second boundary — rotation, backup, restore, and integrity all need re-answering; a second file to lose or desync

### Per-file encrypted store
- Pro: write-once files are sync-friendly and incrementally backupable
- Con: key-wrapping design, cross-filesystem integrity, orphan sweeps, multi-artifact restore — clinic-scale architecture for a personal archive; retained as the documented escape hatch

### Do not retain originals
- Pro: nothing new to store or secure
- Con: extraction is lossy and re-extraction impossible; users keep a plaintext PHI folder themselves anyway — the review's concern relocated outside the platform's care, by design

## Links
- Extends: [ADR-0013](0013-encryption-at-rest.md) — the encryption boundary documents now live inside
- Related: [ADR-0027](0027-audit-trail-and-corrections.md) — transactional delete + audit covers the stored original
- Related: [ADR-0028](0028-key-derivation-and-rotation.md) — rekey cost scales with the archive (accepted)
- Related: [ADR-0033](0033-plaintext-artifact-disposal.md) — import-source disposal offer; in-memory render preference; export warnings
- Related: [ADR-0019](0019-multi-device-sync.md) — backup remains the single sync-safe artifact; cold-store sync properties noted for the escape hatch
- Related: [ADR-0015](0015-data-export.md) — saving an original to disk is an export
- Related: [data-model.md](../data-model.md) — clinical documents schema considerations; [open-questions.md](../open-questions.md) FTS strategy is unaffected (FTS indexes extracted text, not the binary)
- Resolves: [architecture review 2026-06-10](../architecture-review-2026-06-10.md), item 2.9

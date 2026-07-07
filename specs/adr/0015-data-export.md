# ADR-0015: Data Export and Portability

## Status
Proposed

## Context and Problem Statement
Health data ownership means nothing without the ability to get your data out. Import and export are mirrors of each other — users who can import from a source should be able to export to a portable format. Export is also necessary for sharing with physicians, migrating to a different platform, and complying with data portability expectations. What export formats and mechanisms should the platform support?

## Decision Drivers
- Export must be a first-class feature, not an afterthought
- The export format must be human-readable and machine-parseable without proprietary tooling
- Export and import formats should be compatible — a full export should be re-importable
- Export should support partial export (by date range, by data type, by biomarker)
- Healthcare interoperability (FHIR) is a future consideration (see ADR-0018)
- The CLI must support export as a first-class command
- Export of encrypted data must produce the decrypted content — the export is the user's data
- Plaintext export is deliberate, but the "share with physician" path needs an encrypted transport option — an export that leaves the machine (email, USB stick, patient portal upload) should not have to travel as plaintext health data

## Considered Options
- JSON (platform-native format)
- CSV (per data type)
- FHIR R4 (healthcare interoperability standard)
- SQLite database copy (full backup, not portable)

## Decision Outcome
Chosen option: **[TBD]**

Two formats are clearly needed: a platform-native JSON format for full round-trip import/export, and CSV per data type for human-readable export and spreadsheet use. FHIR is deferred to ADR-0018. The decision outcome is TBD pending format specification work.

## Export Formats Under Consideration

### Platform-native JSON
A JSON envelope containing all exported data with the platform's schema version, export timestamp, and data type sections. Designed for round-trip compatibility with the bulk import endpoint.

```json
{
  "export_version": 1,
  "exported_at": "2026-03-21T14:00:00Z",
  "schema_version": 5,
  "labs": [ ... ],
  "events": [ ... ],
  "interventions": [ ... ],
  "body_composition": [ ... ]
}
```

### CSV (per data type)
One CSV file per data type, with a manifest file listing the included files and metadata. Human-readable, importable into Excel/Sheets, compatible with most data tools.

### SQLite backup
`healthspan db backup` produces a hot backup of the database file. Not portable (requires the platform to read), but useful for disaster recovery. This is distinct from the export command and already partially addressed in ADR-0006.

## CLI Interface

```
healthspan export                          # full export, platform-native JSON
healthspan export --format csv             # full export, CSV per data type
healthspan export --since 2024-01-01       # date-filtered
healthspan export --type labs,events       # data-type filtered
healthspan export --biomarker insulin      # biomarker-filtered
healthspan export --output ./my_export/    # output directory
healthspan export --encrypt                # passphrase-protected archive (see below)
```

The `--output` flag is the CLI writing locally under the user's own filesystem authority. When an export runs as a *job* submitted through the REST API, the output location is a file-typed job parameter subject to [ADR-0012](0012-job-abstraction.md)'s File Path Validation — a relative path contained inside the configured `export_dir`, never a caller-chosen arbitrary path (an unconstrained output path would be an arbitrary-file-write primitive).

## Export Encryption Option

Exports are decrypted plaintext by default — that is the point of an export, and the default stays that way. But the primary sharing scenario (send a filtered export to a physician) moves health data across email, USB media, or an upload portal, where plaintext is the wrong transport format. `--encrypt` addresses this:

- Wraps the export output (any format) in a single passphrase-protected archive
- The passphrase is a **one-time sharing passphrase** entered at export time and communicated to the recipient out-of-band — it is never the master passphrase, never stored, and never written to config or logs
- Candidate mechanism: AES-256 encrypted ZIP (e.g. `pyzipper`) — chosen for recipient ergonomics, since a physician's office can open it with common tools (7-Zip, WinZip, macOS Archive Utility alternatives) without installing platform software. Legacy ZipCrypto is explicitly unacceptable. Stronger tools with worse recipient ergonomics (`age`, GPG) can be offered later via plugin if demand exists.
- The unencrypted staging files created while building the archive fall under security.md's temp-file handling rules (create with restrictive permissions, delete on completion)

Final mechanism choice is part of the format specification work gating this ADR's decision outcome.

## Round-Trip Guarantee

A full export re-imported to a fresh database must produce an identical database. This is a testable invariant and should be part of the test suite.

## Open Questions
- Should the export format version be independent of the schema version, or tied to it?
- Should export be a job (ADR-0012) for large datasets, or synchronous for typical sizes?
- FHIR R4 export format — deferred to ADR-0018

## Links
- Related: [ADR-0004](0004-data-ingestion-strategy.md) — export format mirrors import format
- Related: [ADR-0012](0012-job-abstraction.md) — large exports may use the job system
- Related: [ADR-0018](0018-fhir-interoperability.md) — FHIR export deferred here
- Related: [specs/security.md](../security.md) — export produces decrypted plaintext by default (`--encrypt` wraps it for transport); temp-file handling rules apply to staging files

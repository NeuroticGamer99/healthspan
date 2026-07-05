# Architecture & Security Review — 2026-06-10

Full review of README, all design documents, and all 24 ADRs, plus packaging and CI files. Items are formatted as checklists for working through over time.

**Overall verdict:** The fundamental architecture is sound — no major changes needed. Process isolation + micro-kernel + REST core is the right shape, and the documentation discipline is above average. The findings below are almost all "the specs evolved and left stale guarantees behind" rather than wrong thinking. The two items to resolve before writing migration 0001: the plugin host-process matrix (1.A) and bearer token scoping (2.1).

---

## 1. Inconsistencies between documents

### A. Plugin host-process model is contradictory ⚠️ most important finding

- [x] Resolve which process loads which plugin type, and update all four documents to agree. — *Resolved by [ADR-0025](adr/0025-plugin-host-process-matrix.md) (Proposed), with conforming edits to ADR-0011, ADR-0012, ADR-0016, ADR-0017, and security.md (host matrix + Security Invariants).*

The contradiction:

- [security.md](security.md) says plugins execute "in the CLI process"
- [ADR-0010](adr/0010-cli-plugin-model.md) has `mcp_tool` plugins registering on `context.mcp` — that's the MCP Server process
- [ADR-0011](adr/0011-event-bus.md) makes transport adapters and the scheduler plugins that run *inside the Core Service*
- [ADR-0013](adr/0013-encryption-at-rest.md) guarantees "Plugins never access the encryption key… The key never crosses the process boundary"

These can't all be true. If transport-adapter/automation/analysis plugins load into Core Service, a malicious plugin can read the derived key (and all plaintext) from process memory, and ADR-0013's isolation guarantee is void.

**Recommendation:** Write an explicit per-plugin-type host matrix. Prefer option (a): no third-party plugins ever load into Core Service — first-party only, with third-party automation running out-of-process via SSE + REST. This preserves the only process where the key lives as a no-plugin zone. Option (b) is rewriting the ADR-0013 guarantee honestly. Cross-link with ADR-0020's trust-tier/sandboxing design (see 3.H).

### B. ADR-0001's supersession orphans the language decision

- [x] Restore an authoritative record of the Python language decision. — *Resolved by restatusing [ADR-0001](adr/0001-mcp-server-language.md) as `Accepted (partially superseded by ADR-0023)` (a permitted status-field correction): ADR-0023 replaced only the Nuitka distribution choice; ADR-0001 remains the authoritative Python record. Status variant added to the ADR README status table.*

[ADR-0023](adr/0023-distribution-mechanism.md) explicitly replaces only the Nuitka distribution choice ("language choice (Python) is unchanged"), yet ADR-0001's status is fully `Superseded by ADR-0023`, and [open-questions.md](open-questions.md) still cites ADR-0001 as the resolved record for "Implementation language." There is now no Accepted ADR recording that the platform is Python.

**Options:** a new ADR restating the language decision, or status the original as `Accepted (partially superseded by ADR-0023)`.

### C. ADR-0013 (Accepted) still contains Nuitka

- [x] Write an extending/superseding ADR correcting the code-signing requirement and the macOS keychain claim (per ADR governance — not an in-place edit). — *Resolved by [ADR-0028](adr/0028-key-derivation-and-rotation.md) (Proposed): code-signing requirement withdrawn (no binary exists to sign under uv); macOS keychain ACL claim corrected to same-user parity with Windows/Linux; a future signed app-bundle distribution noted as what would restore it.*

[ADR-0013](adr/0013-encryption-at-rest.md) requires signing "the Nuitka-compiled binary." Beyond the stale reference, this matters substantively: the macOS keychain per-app ACL story assumed a signed hardened-runtime binary. Under `uv tool install`, the keychain client is the Python interpreter in a uv venv — any same-user Python script gets the same ACL identity, which materially weakens the claimed macOS asymmetry advantage.

### D. What is the Import Pipeline, actually?

- [x] Decide: long-running process, or CLI/job-system operations? Correct the diagrams and startup order. — *Resolved: decision made in ADR-0025 — imports are job-system operations, there is no Import Pipeline daemon, and the fourth supervised process is the Automation Host (which hosts the watch-folder importer, the one import concern needing residency). Diagrams and startup order corrected: README diagram + new Automation Host bullet, observability.md health-endpoint heading and startup step 4, glossary (client rosters, new Automation Host entry, "Import Pipeline" section retitled "Data Import" with an entry stating the pipeline is a pattern not a process, stale "transport adapters are plugins" entry fixed), design-rationale.md and security.md client/process mentions. Accepted ADR-0006 got a navigation link to ADR-0025 only; its diagram stands as a historical record.*

The README diagram and [ADR-0006](adr/0006-application-architecture.md) draw it as a first-class long-running process; [observability.md](observability.md) has the launcher starting it fourth; but [ADR-0008](adr/0008-process-lifecycle.md) starts only Core + MCP + GUI, and [ADR-0012](adr/0012-job-abstraction.md) implies imports are jobs submitted to Core Service.

**Recommendation:** Imports are CLI-invoked or job-system operations; the "Import Pipeline" is not a daemon. The watch-folder importer for Levels exports is the one thing that genuinely needs a resident process — name that explicitly if kept.

### E. ADR-0003 and ADR-0002 are decided in practice but not on paper

- [x] Accept ADR-0003 as SQLite-only for v1 (PostgreSQL as a future ADR that must also revisit ADR-0009 and ADR-0013). — *Done: decision outcome written (SQLite-specific features freely usable; a PostgreSQL ADR must revisit ADR-0009 and ADR-0013/0028), status Accepted.*
- [x] Accept ADR-0002 as "MCP-based pluggability." — *Done: decision outcome written (MCP server is the provider interface; client choice is configuration, local LLMs supported), status Accepted.*

[ADR-0006](adr/0006-application-architecture.md)'s diagram says "Database (pluggable)," but [ADR-0013](adr/0013-encryption-at-rest.md) (Accepted) is SQLCipher-specific — encryption at rest does not transfer to PostgreSQL (entirely different model: TDE/pgcrypto/disk encryption), and [ADR-0009](adr/0009-database-migration.md)'s custom migration runner was justified on a single-dialect assumption. Likewise [ADR-0007](adr/0007-mcp-transport.md) (Accepted) already chose AI-client-agnostic MCP, and the README asserts it.

### F. ADR-0022 has two wrong cross-references

- [x] Fix (permitted as minor link fixes under ADR governance):
  - `PLUGIN_VERSION` attributed to ADR-0010 — it's defined in [ADR-0024](adr/0024-plugin-extensions.md) — *fixed in the decision driver and the Links section*
  - Links section credits "ADR-0001 — uv tool install as the distribution mechanism" — that's [ADR-0023](adr/0023-distribution-mechanism.md) — *fixed*

### G. ADR-0009 cites SQLite syntax that doesn't exist

- [ ] Correct: SQLite has no `ALTER TABLE ... ADD COLUMN IF NOT EXISTS`.
- [ ] Drop the per-file idempotency requirement — it's redundant with the runner's own `schema_version` tracking (applied files are skipped).

*Deferred into item 3.F's migration ADR: dropping the idempotency requirement changes decision content in an Accepted ADR, so it needs an extending ADR — which 3.F (migration transaction discipline, pragmas) will create anyway. One ADR will extend ADR-0009 once, covering both.*

### H. "Cloud sync of the live file is safe" is overstated

- [x] Update [open-questions.md](open-questions.md) resolved item and [ADR-0019](adr/0019-multi-device-sync.md): sync only `healthspan db backup` outputs (checkpointed, consistent), never the live file. — *Done: both rewritten to state the live WAL-mode file must never be synced (torn-copy hazard, unrelated to encryption); only backup output is sync-safe. security.md's trust table and design-rationale.md's pattern reference were checked and don't repeat the overstated claim — no change needed there.*

With WAL mode (implied by `.gitignore`), a sync client snapshotting `db` + `-wal` mid-write can capture a torn, unrecoverable copy — encrypted ciphertext makes partial copies worse, not better. ADR-0019's mtime check is weak protection.

### I. Minor items

- [x] README presents several still-Proposed ADRs (event bus adapters, reference range frameworks, jobs) as settled — add a "designed, not final" caveat. — *Resolved: added inline "designed, not final" caveats with ADR links at the three spots that overstated design status — event bus adapters (ADR-0011), reference range frameworks/automation rules/notification channels (ADR-0005/0016/0017), and the job system backing long-running imports (ADR-0012).*
- [x] Verify [pyproject.toml](../pyproject.toml) author email `neuroticgamer01@…` vs repo URL `NeuroticGamer99` — possible typo. — *Not a typo: `NeuroticGamer99` is Matthew's deliberately public GitHub handle; `neuroticgamer01@zaleski.net` is the real, non-alias email PyPI requires for publishing. No change needed.*

---

## 2. Security specification robustness (ordered by impact)

### 2.1 Replace the single shared bearer token with per-client, scoped tokens ⚠️

- [x] Named tokens per client with scopes (`read`, `write`, `import`, `admin`)
- [x] MCP server defaults to read-only
- [x] `healthspan token rotate` command
- [x] Constant-time comparison (`secrets.compare_digest`) specified in security.md

*Resolved by [ADR-0026](adr/0026-named-scoped-tokens.md) (Proposed): six scopes (adds `events`, `jobs`), per-client default matrix, hashed server-side storage, keyring-per-client storage (tokens removed from shared TOML), CLI credential tiers by plugin provenance, ephemeral single-job tokens, and INV-5. security.md Authentication section rewritten.*

[security.md](security.md) claims "The MCP server has no write access beyond what the Core REST API permits" — but with one token shared by GUI, MCP, CLI, plugins, and the inbound webhook, the MCP server (and therefore the AI client, and therefore anything that prompt-injects the AI client) has full write/delete/import capability. A read-only MCP token bounds the blast radius of injected instructions to data exfiltration rather than data destruction.

### 2.2 Specify the key derivation construction precisely

- [x] Use the 32-byte secret key as the Argon2id salt (essentially 1Password's construction) — current spec says `Argon2id(passphrase + secret_key)`, naive concatenation, no salt mentioned at all
- [x] Specify encodings
- [x] Pin Argon2id parameters to OWASP minimums (≥19 MiB memory, t≥2) in the spec
- [x] Pass the derived key as raw hex: `PRAGMA key = "x'<64 hex>'"` — skips SQLCipher's internal PBKDF2 (a stronger KDF already ran) and eliminates the quoting hazard in ADR-0013's `f"PRAGMA key='{key}'"` example, which contradicts security.md's "no SQL by string interpolation" rule

*All four resolved by [ADR-0028](adr/0028-key-derivation-and-rotation.md) (Proposed): secret key as salt (passphrase-only mode gets a random salt in a `.keyparams` sidecar); NFC-normalized UTF-8 passphrase, raw-byte salt, Base32 human representations; OWASP minimums as the recorded floor with argon2-cffi defaults (64 MiB, t=3) as initial values, parameters pinned per database in the sidecar; raw-hex `PRAGMA key` as the single sanctioned interpolation exception.*

### 2.3 Key/passphrase rotation is entirely missing

- [x] Extension ADR to ADR-0013: `healthspan keys change-passphrase` via `PRAGMA rekey`; secret key rotation; document that old backups still open with old credentials. — *Resolved by [ADR-0028](adr/0028-key-derivation-and-rotation.md): both commands specified; mandatory verified backup before rekey (`--no-backup` override); secret-key rotation invalidates printed Recovery Kits; non-retroactivity documented and printed by the commands.*

### 2.4 `healthspan db encrypt` retains a plaintext backup

- [ ] [security.md](security.md) says the migration "retains the original as a backup" — a plaintext health database deliberately left on disk, contradicting the temp-file secure-deletion policy. Require explicit user-confirmed secure disposal after verification.

### 2.5 The secure-deletion requirement overpromises

- [ ] Restate honestly: overwriting with zeroes is ineffective on SSDs (wear leveling) and CoW/journaling filesystems. Primary control is *never writing plaintext health data to disk*; overwrite-before-unlink is best-effort defense-in-depth; recommend OS full-disk encryption as the real backstop.

### 2.6 Recovery Kit digital artifact handling is unaddressed

- [ ] `healthspan keys recovery-kit` presumably renders a file (PDF/PNG with the secret key QR). Specify: render to memory or temp file under the same handling rules, prompt to delete after printing, add kit-file patterns to `.gitignore`.

### 2.7 Runtime pip installs need hash pinning

- [ ] ADR-0024 catalog should carry sha256 hashes; loader installs with `--require-hashes` (version pins alone don't authenticate content)
- [ ] Reorder the loader: dependency graph / cycle / conflict validation (steps 5–7) before package installation (step 4), so a plugin that will fail validation never gets its packages installed

### 2.8 Job/import file paths are an unvalidated surface

- [ ] ADR-0012's example has the API accepting `"file": "export_2026.csv"` — caller-supplied paths are a path-traversal/arbitrary-file-read primitive (especially from a write-scoped MCP token). Require paths to resolve inside configured import directories.

### 2.9 Imported clinical documents' original files have no specified home

- [ ] [data-model.md](data-model.md) stores `source_file_hash`, implying the original PDF lives somewhere — if a plain directory, that's plaintext PHI outside the encryption boundary. Specify: BLOBs inside SQLCipher, or an explicitly-encrypted document store.

### 2.10 Smaller items

- [x] Auth-failure rate limiting + audit logging for LAN deployments — *consolidated into [ADR-0026](adr/0026-named-scoped-tokens.md) and security.md's Authentication section (rate limiting applies to localhost too; `auth_audit` table records token names, never values)*
- [x] Harden [publish.yml](../.github/workflows/publish.yml): pin actions to commit SHAs, add explicit `permissions: contents: read` — *Done: `actions/checkout` pinned to `34e11487…` (v4.3.1), `astral-sh/setup-uv` pinned to `d4b2f3b6…` (v5.4.2), `contents: read` added alongside `id-token: write`. Note: setup-uv's current major is v8; staying on the pinned v5.4.2 until a deliberate upgrade.*
- [x] Expand `.gitignore`: export output directories, `*.sqlite` — *Done: added `*.sqlite`, `*.sqlite3`, and `exports/`/`export/` (annotated as plaintext-health-data output per ADR-0015).*
- [x] ADR-0015: add an export-encryption option (passphrase-protected archive) for the "share with physician" path — exports are deliberate plaintext — *Done: ADR-0015 (Proposed, freely editable) gained a decision driver and an "Export Encryption Option" section — `--encrypt` wraps any export format in a passphrase-protected archive; one-time sharing passphrase (never the master passphrase, never stored), candidate mechanism AES-256 ZIP for recipient ergonomics (ZipCrypto explicitly unacceptable), staging files under security.md temp-file rules. Plaintext stays the default. Final mechanism folded into the ADR's existing TBD format-specification work.*

---

## 3. Architecture, frameworks, and ADR-specific improvements

**No fundamental change needed.** Process isolation for a single-user desktop platform is heavier than a monolith, but ADR-0006 justifies it well and the Home Assistant analogy holds.

### 3.A Resolve the event-sourcing question as "no" — write the ADR now

- [x] Blocks migration 0001 per [open-questions.md](open-questions.md). — *Resolved by [ADR-0027](adr/0027-audit-trail-and-corrections.md) (Proposed): event sourcing rejected; append-only `audit_log` written in the same transaction as every mutation (application-layer capture, trigger-enforced immutability), `superseded_by` supersession for value corrections (in-place updates only for designated metadata repairs like the timezone workflow), hard delete + mandatory audit row with client backup prompt, CQRS-lite with ADR-0021 aggregates as rebuildable read models invalidated by `data.*` events. open-questions.md entries moved to Resolved.*

Full event sourcing is the wrong trade for a single-user analytical store — materialized-view consistency complexity forever, for replay rarely used. The lighter pattern gets 90%:

- Append-only `audit_log` written in the same transaction as every mutation (so it can't drift)
- `superseded_by` self-FK on data tables for corrections
- CQRS-lite via ADR-0021's aggregate tables

"What did we believe on date X" still works via audit_log + superseded chains.

### 3.B ADR-0007 refresh: MCP HTTP+SSE transport is deprecated

- [x] Extending ADR before implementation: the MCP spec (since the 2025-03-26 revision) replaced HTTP+SSE with **Streamable HTTP**; `fastmcp` supports it. None of the reasoning changes — still a long-lived HTTP server. (ADR-0011's *internal* SSE event stream for the GUI is unaffected — plain SSE, not MCP.) — *Resolved by [ADR-0029](adr/0029-mcp-streamable-http.md) (Proposed): Streamable HTTP only, no legacy HTTP+SSE compatibility mode (no deployed clients to break); ADR-0007's five security requirements carry over unchanged (token storage per ADR-0026; the spec's new origin-validation requirement is already met by the Host/CORS rules); ADR-0011's internal SSE stream explicitly called out as non-impacted. ADR-0007 got an "Extended by" navigation link; open-questions.md's resolved MCP-transport entry and ADR-0001's link annotation updated.*

### 3.C Fix ADR-0013's connection-lifetime requirement

- [x] "Open a connection per request and don't hold the key in memory" is self-contradictory and slow: opening the next connection requires holding the derived key (or re-running Argon2id, ~hundreds of ms per request, by design). The threat model already accepts memory scraping as unprotected. — *Resolved by [ADR-0028](adr/0028-key-derivation-and-rotation.md): derive once at startup, hold the raw key in Core memory under `SecretStr` discipline, persistent connection pool, best-effort zeroization at shutdown; replaces ADR-0013's connection-lifetime requirement.*

**Honest and fast:** derive once at startup, hold the raw key in process memory (optionally mlock-style best effort), keep a persistent connection/pool, zero the key on shutdown. SQLite + WAL + a connection pool is also how to get decent concurrent-read behavior under FastAPI.

### 3.D ADR-0005: pick option 2, and add a `unit` column — the sketch has a real safety bug

- [ ] `framework_ranges(range_low, range_high)` with no unit is dangerous: an Attia ApoB target in mg/dL compared against a result in g/L silently produces garbage flags. Every range row needs a unit; comparison must unit-normalize. The `effective_date NULL` column already gives option 3 for free when needed.

### 3.E Ground the biomarker model in LOINC and UCUM

- [ ] Canonical *identifier* = LOINC code (human-readable name as display); labs print different names but report LOINC on most results — collapses the alias problem
- [ ] Units as UCUM strings, with a canonical unit per biomarker — gives unit conversion a standard to validate against
- [ ] Makes ADR-0018's FHIR work nearly free (`Observation.code` *is* LOINC)
- [ ] Plan value representation for real lab data now: numeric value + comparator (`<0.1` below-detection results) + text value for qualitative results — FHIR's `valueQuantity.comparator` model is the proven shape; a bare `REAL` column fails on the first thyroid antibody panel

### 3.F Python specifics

- [ ] Verify before implementation that `sqlcipher3`/`sqlcipher3-wheels` and PySide6 publish Python 3.14 wheels on all three OSes — both are compiled, and ADR-0013/0001 depend on them; most likely forced compromise
- [ ] ADR-0009 runner: specify driver transaction discipline explicitly (`isolation_level=None`/autocommit + explicit `BEGIN IMMEDIATE`…`COMMIT`) — the stdlib-style driver's implicit transaction handling otherwise silently auto-commits between DDL statements and breaks "atomic per migration"
- [ ] Require `PRAGMA foreign_keys=ON` per connection (off by default) and `journal_mode=WAL` as recorded decisions

### 3.G Testing strategy additions

- [ ] Property-based testing (Hypothesis) for the hardest pure-logic targets: unit conversion, timezone quadruple round-trips, Argon2id determinism
- [ ] CI secret-scanning/log-grep gate to mechanize the "no health values in logs" requirement that [testing-strategy.md](testing-strategy.md) already sketches

### 3.H ADR-0020's sandboxing note deserves promotion

- [x] The trust-tier/sandboxed-subprocess design buried in the registry stub is the long-term answer to finding 1.A (which plugins may live in Core Service). Cross-link from ADR-0010's security boundary so the two evolve together. — *Done: ADR-0010 ↔ ADR-0020 cross-links added; [ADR-0025](adr/0025-plugin-host-process-matrix.md) names ADR-0020's trust tiers as the only sanctioned path to relaxing the matrix (must address INV-2 explicitly).*

---

## Strongest parts (for the record)

The three-layer trust model in security.md, the timestamp quadruple convention, the lab-source-first schema rationale, and ADR-0013's honest threat-model table are all better than what most shipped products document.

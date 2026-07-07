# Testing Strategy

Standards and approach for testing the Healthspan platform. Testing is a design concern — the multi-process architecture, encryption layer, plugin system, and health data sensitivity all create challenges that must be addressed before implementation begins.

---

## Principles

**No real health data in tests.** Test fixtures use synthetic data that is structurally realistic but contains no personally identifying or clinically meaningful values. Synthetic fixtures are committed to the repository; real data never is. See [CLAUDE.md](../CLAUDE.md) for the personal data containment policy.

**Test at the boundary.** The process-isolated architecture (ADR-0006) means each process communicates over a defined interface. Tests should primarily exercise these interfaces — the REST API, the plugin interface contract, the event bus protocol — rather than reaching into implementation internals. This keeps tests stable as internals evolve.

**Tests run without infrastructure.** The default test suite must run with no external services — no PostgreSQL, no MQTT broker, no running Core Service (unless the test starts one). SQLite (SQLCipher) is the default backend and is file-based; tests create ephemeral databases.

**Encryption is always on in tests.** Tests must exercise the same SQLCipher code path as production. Testing against an unencrypted SQLite database hides bugs in the encryption layer. Test databases use a hardcoded test passphrase — never the user's real credentials.

---

## Test Layers

### Unit tests

Isolated tests of individual functions and classes. No database, no HTTP, no filesystem (except temp files).

Two distinct sub-categories of function belong here:

**Pure logic (no data source dependency):** functions whose inputs are primitive values or simple arguments — no connection to the database or any I/O. These are straightforward to test because the inputs are constructed from scratch.

**In-memory data transforms:** functions that process data structures already retrieved from the database. Once a query result is held in Python objects, dicts, or lists, any function that transforms, enriches, or aggregates that data is a pure unit test target — the database is not involved and does not need to be mocked. Construct the input data inline as a fixture dict or object; call the function; assert on the output. These tests are stable because they couple only to the data contract, not to the database layer.

**Coverage targets — pure logic:**
- Biomarker alias resolution and canonical name mapping
- Timestamp convention: UTC conversion, timezone inference, correction workflow
- Import record parsing and validation (per-source importer modules)
- Reference range comparison logic
- Plugin compatibility checking (API version range validation, dependency resolution)
- Argon2id key derivation (ADR-0028): known-answer vectors — fixed passphrase + secret key + parameters produce the expected 32-byte key; NFC normalization (composed and decomposed passphrase forms derive identically); Base32 secret-key round-trip; `.keyparams` sidecar parse/serialize round-trip
- Configuration parsing and validation

**Coverage targets — in-memory data transforms:**
- Trend computation over a time series of lab results
- CGM aggregation: fasting baseline, time-in-range, postprandial statistics
- Multi-source deduplication and merge logic (same biomarker, multiple labs)
- Reference range annotation applied to result sets
- Unit conversion (e.g., mg/dL → mmol/L, weight units)
- Response shaping and serialization before API delivery
- Computed field enrichment on domain objects (derived ratios, z-scores, delta from prior draw)

### Property-based tests

Hypothesis-driven tests for the pure-logic targets whose input spaces are too large for example-based tests: unit conversion, timezone handling, and key derivation. Hypothesis is a dev dependency with two registered settings profiles: `dev` (small `max_examples`, fast inner loop) and `ci` (larger example counts, `derandomize=True` so a CI failure reproduces deterministically).

**Unit conversion ([ADR-0031](adr/0031-units-and-ucum.md)).** Properties are written against the internal units-module API, never against a specific engine — the suite doubles as the acceptance harness for ADR-0031's open conversion-engine sub-decision: any candidate engine (ucumvert+pint, curated table) must pass the suite unchanged. Relative tolerance for float comparisons is fixed suite-wide at `1e-9`.

- *Identity*: converting a value to its own unit returns it exactly
- *Round-trip*: A→B→A recovers the original within tolerance
- *Composition*: A→B→C agrees with A→C within tolerance
- *Order preservation*: x < y before conversion implies converted x < converted y (linear conversions)
- *Molar conversions*: mg/dL ↔ mmol/L parameterized by molar mass, exercising ADR-0031's biomarker-context requirement — a molar conversion attempted without biomarker context must fail loudly, never fall back to a scalar factor

**Timestamp quadruple round-trips.** Strategies: `st.timezones()` × `st.datetimes()` (stdlib `zoneinfo`), which deliberately generate DST-gap and fold-ambiguous instants.

- *Reconstruction*: for any (instant, IANA zone), storing the quadruple and recomputing local time from `*_utc` + `*_local_tz` reproduces `*_local_recorded` for unambiguous times
- *Documented edge behavior*: DST-ambiguous (fold) and nonexistent (gap) local times must match the platform's documented convention — this property forces that convention to be written down before implementation
- *UTC fixed point*: quadruple → UTC → quadruple is stable

**Argon2id determinism ([ADR-0028](adr/0028-key-derivation-and-rotation.md)).** Generalizes the known-answer and NFC unit tests above.

- *Determinism*: arbitrary Unicode passphrase + secret key + parameters → identical 32-byte key on repeated derivation
- *Normalization invariance*: generated passphrases containing combining characters derive identically in NFC and NFD forms — much stronger than fixed hand-picked forms

Property tests run with minimal test-only Argon2id parameters (KDFs are deliberately slow) and Hypothesis `deadline` disabled; the known-answer vectors at production parameters remain in the plain unit tests.

### Integration tests

Tests that exercise a real database and/or the REST API. Each test gets its own ephemeral SQLCipher database.

**Coverage targets:**
- Core REST API endpoint behavior: CRUD operations, authentication enforcement, input validation, error responses
- Bulk import endpoint: full-batch validation, atomic transactions, dry-run mode, conflict policies (reject, skip, upsert)
- Database migration runner: forward migration from empty database, runner-level idempotency (applied files skipped via `schema_version`), schema version tracking, connection pragma verification (`foreign_keys` off + `foreign_key_check` during migration, on at runtime — [ADR-0035](adr/0035-migration-execution-semantics.md))
- Event bus: event publishing, SSE delivery to subscribers, event schema validation
- Job system: submission, status transitions, progress events, cancellation
- Audit trail (ADR-0027): **mutation-matrix test** — every repository mutation path against every data table writes exactly one `audit_log` row in the same transaction, with correct operation, row images, and provenance (actor, batch, job); a rolled-back mutation leaves no audit row; `audit_log` immutability triggers reject UPDATE and DELETE
- Corrections (ADR-0027): value correction inserts the new row, sets `superseded_by`, and emits `data.corrected`; correction chains resolve correctly through `*_current` views; supersession-chain rows are not deletable; hard delete preserves the full row image in `audit_log` and emits `data.deleted`
- Health and metrics endpoints

### Plugin tests

Tests for the plugin loader and the plugin interface contract.

**Coverage targets:**
- Plugin discovery: `.py` files and packages in the plugins directory
- Static metadata extraction ([ADR-0036](adr/0036-plugin-package-installation-integrity.md)): declarations read without importing the module — a plugin whose body raises at import time still yields its metadata during scan, and no plugin code executes before validation passes
- Compatibility validation: API version range checking, dependency graph resolution, cycle detection
- Load order: topological sort, providers before consumers
- Service registry: registration, retrieval, version constraints, namespace queries
- `PLUGIN_PACKAGES` installation ([ADR-0036](adr/0036-plugin-package-installation-integrity.md)): catalog-governed resolution installs the hash-pinned closure in `--require-hashes` mode; a hash mismatch hard-fails with the offending package named; off-catalog warning states that a version pin does not authenticate content
- Validation-before-install ([ADR-0036](adr/0036-plugin-package-installation-integrity.md)): a plugin that fails API-version, cycle, or conflict validation installs **zero** packages — the reorder's observable property
- Error handling: missing dependencies, incompatible versions, malformed plugins
- First-party plugin loading: built-in plugins load before user plugins; user overrides work

A test plugin fixture set should include: a minimal single-file plugin, a package plugin, a provider plugin, a consumer plugin with declared dependencies, and intentionally broken plugins (bad version range, missing dependency, syntax error).

### End-to-end tests

Tests that exercise the full multi-process stack: Core Service + MCP Server + CLI.

**Coverage targets:**
- MCP tool calls: AI client sends a tool call → MCP server translates to REST API request → Core Service queries database → result returned
- CLI commands: `healthspan import`, `healthspan export`, plugin-registered commands
- Process lifecycle: launcher starts processes in correct order; health endpoint readiness gating works; graceful shutdown
- First-run flow: `healthspan init` generates secret key, prompts for passphrase, creates encrypted database, runs migrations

### Security tests

Tests that specifically validate security properties.

**Coverage targets:**
- **Authentication**: requests without bearer token are rejected; requests with invalid or revoked tokens are rejected; every endpoint enforces auth (no accidental unauthenticated endpoints)
- **Authorization**: requests authenticated with a token lacking a required scope are rejected with 403; every route declares its required scope (no accidental scope-free endpoints); job submission enforces the job type's declared scopes (ADR-0026)
- **Event forgery**: `/v1/events/inbound` rejects reserved namespaces (`data.*`, `job.*`, `schedule.*`, `schema.*`, `system.*`, `plugin.*`) for every token; rejects payloads supplying `source`; rejects publication outside the token's namespace allowlist; a job-child token cannot report progress for a different job ID (ADR-0026)
- **SQL injection**: parameterized queries only — test with known injection payloads against all user-input-accepting endpoints
- **Host header validation**: requests with unexpected Host headers are rejected
- **CORS**: cross-origin requests from non-allowlisted origins are rejected; preflight requests are denied
- **Input validation**: malformed import payloads are rejected with full error details; oversized payloads are handled gracefully
- **Encryption round-trip**: create database with SQLCipher → close → reopen with correct key → data intact; reopen with wrong key → failure
- **Rekey (ADR-0028)**: `change-passphrase` and `rotate-secret-key` flows — old credentials fail after rotation, new credentials open, data intact; the pre-rekey backup still opens with the *old* credentials; rotation refuses to run without a verified backup unless `--no-backup`; missing `.keyparams` sidecar fails with the documented recovery guidance
- **Recovery Kit flow**: init → generate Recovery Kit → simulate new machine → restore from kit → database unlocks
- **Health data in logs**: mechanized by the log canary gate (see [CI Gates](#ci-gates)) — all log output captured during the full test run is scanned against the canary manifest; no fixture health value may appear

### Migration tests

Tests that validate the database migration system.

**Coverage targets:**
- Fresh database: all migrations apply in sequence from empty
- Incremental: each migration applies cleanly to a database at the previous version (migrations assume exact predecessor state — [ADR-0035](adr/0035-migration-execution-semantics.md))
- Runner idempotency: running `healthspan db migrate` twice produces no errors and no duplicate rows in `schema_version` (per-file idempotent SQL is prohibited, not tested for)
- Failure recovery: a deliberately broken migration rolls back cleanly; subsequent valid migrations still apply
- **Mid-file atomicity ([ADR-0035](adr/0035-migration-execution-semantics.md))**: a multi-statement migration that fails partway leaves *none* of its statements applied and no `schema_version` row — this is the test that catches the driver's implicit-commit-around-DDL behavior
- Foreign-key integrity: a migration that introduces an FK violation is rejected by the pre-commit `foreign_key_check` and rolls back entirely
- Schema integrity: after all migrations, the database schema matches the expected table/column/index set

---

## Synthetic Test Data

### Fixture design

Test fixtures should be realistic enough to exercise edge cases but contain no real health data:

- **Lab results**: use plausible biomarker names and values within and outside reference ranges; include multi-source scenarios (same biomarker from different labs)
- **CGM data**: generate synthetic glucose readings at 5-minute intervals with realistic patterns (fasting baseline, postprandial spikes, overnight stability)
- **Body composition**: synthetic InBody-like readings with a mix of device capabilities (some fields NULL for simpler devices)
- **Clinical events and interventions**: fictional but structurally complete entries with dose history records
- **Clinical documents**: synthetic visit notes with realistic structure but no real clinical content

**Canary rule.** Every synthetic health value must be grep-distinctive, because the log canary gate (see [CI Gates](#ci-gates)) works by scanning captured log output for fixture values — and realistic values like a glucose of `95` collide with timestamps, ports, and status codes. Text fields (clinical notes, medication names in notes) embed a `CANARY-` marker token; numeric health values use high-entropy decimals with at least six significant digits (e.g. `104.73921`) that cannot collide with infrastructure numbers. The fixture loader derives the **canary manifest** — the complete list of health values present in the fixtures — programmatically from the fixture files themselves, so there is no hand-maintained list to drift out of sync.

### Fixture management

Fixtures live in `tests/fixtures/` as JSON or SQL files. A fixture loader creates an ephemeral SQLCipher database, applies all migrations, and loads the specified fixtures before each test or test suite.

---

## Cross-Platform Testing

The platform targets Windows, macOS, and Linux. CI must test on all three. Platform-specific concerns:

- **OS keychain**: `keyring` backend differs per platform (DPAPI, macOS Keychain, libsecret). Tests that exercise key storage must run on each platform or mock the keyring backend with `keyring.testing`.
- **File permissions**: `chmod 600` behavior differs on Windows. Tests that validate config file or database file permissions must account for platform differences.
- **Path handling**: forward vs backslash; tests should use `pathlib.Path` consistently.
- **SQLCipher and PySide6 wheel availability**: both are compiled dependencies (see [ADR-0013](adr/0013-encryption-at-rest.md), [ADR-0001](adr/0001-mcp-server-language.md)); the platform's `>=3.14` requirement (`pyproject.toml`) only works uncompiled if prebuilt wheels exist for Python 3.14 on Windows, macOS, and Linux. Verified 2026-07 on PyPI: `sqlcipher3` 0.6.2 and `sqlcipher3-wheels` 0.5.7 both ship `cp314`/`cp314t` wheels for `win_amd64`/`win_arm64`/`win32`, `manylinux_2_28`, and `macosx` across all three targets; `PySide6` 6.11.1 and `shiboken6` 6.11.1 ship stable-ABI (`cp310-abi3`) wheels with `requires-python = "<3.15,>=3.10"`, covering 3.14 by construction. No forced compromise (older Python pin, source build, or compiler toolchain in CI) is needed today. CI should still pin exact versions and re-verify wheel availability before bumping the Python floor in the future, since this is external package-maintainer state, not a platform guarantee.

---

## CI Gates

Checks that run as distinct CI steps and fail the build outright. These mechanize requirements that would otherwise depend on code-review vigilance.

### Log canary gate (mandatory)

Turns [observability.md](observability.md)'s "never log health data values" prohibition from a review norm into a mechanized invariant:

1. The full test run — including E2E tests, whose spawned Core Service / MCP Server / CLI processes have their stdout and stderr captured too — writes all log output to files.
2. A final CI step scans everything captured against the canary manifest (see the canary rule under Fixture design).
3. Any hit fails the build, printing the matched canary value and the offending log line.

The gate is only as strong as the canary rule: a fixture value that is not grep-distinctive is invisible to it. Fixture review enforces the rule; the gate enforces the logs.

### Structured-log field allowlist (recommended)

Because all log output is structured JSON, a second check can assert that log entries use only the permitted-field vocabulary from [observability.md](observability.md) — catching a leak through a novel field name that no canary value would match. Recommended rather than mandatory: it can false-positive on legitimate new fields, and the canary scan is the hard gate. When a legitimate field is added, the allowlist is updated in the same PR.

### Repository secret scanning (mandatory)

A pinned `gitleaks` step scans the full tree on every CI run for committed credentials — tokens, keys, passphrases. The hardcoded test passphrase is allowlisted. To be honest about scope: this catches *credential patterns*, not health data — there is no reliable pattern for a lab value. The personal-data containment policy ([CLAUDE.md](../CLAUDE.md)) is enforced by the `specs/personal/` gitignore and review discipline; secret scanning backstops only its credential-shaped failure modes (e.g. a Recovery Kit render or bearer token pasted into a spec).

---

## Links
- Related: [security.md](security.md) — security requirements that tests must validate
- Related: [observability.md](observability.md) — health data logging prohibition must be tested
- Related: [ADR-0004](adr/0004-data-ingestion-strategy.md) — bulk import behavior to test
- Related: [ADR-0009](adr/0009-database-migration.md) — migration runner behavior to test
- Related: [ADR-0010](adr/0010-cli-plugin-model.md) — plugin loader behavior to test
- Related: [ADR-0013](adr/0013-encryption-at-rest.md) — encryption round-trip to test
- Related: [ADR-0027](adr/0027-audit-trail-and-corrections.md) — audit trail and correction behavior to test
- Related: [ADR-0031](adr/0031-units-and-ucum.md) — the unit-conversion property suite is the acceptance harness for its open conversion-engine sub-decision

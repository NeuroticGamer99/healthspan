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
- Argon2id key derivation (deterministic output for known inputs)
- Configuration parsing and validation

**Coverage targets — in-memory data transforms:**
- Trend computation over a time series of lab results
- CGM aggregation: fasting baseline, time-in-range, postprandial statistics
- Multi-source deduplication and merge logic (same biomarker, multiple labs)
- Reference range annotation applied to result sets
- Unit conversion (e.g., mg/dL → mmol/L, weight units)
- Response shaping and serialization before API delivery
- Computed field enrichment on domain objects (derived ratios, z-scores, delta from prior draw)

### Integration tests

Tests that exercise a real database and/or the REST API. Each test gets its own ephemeral SQLCipher database.

**Coverage targets:**
- Core REST API endpoint behavior: CRUD operations, authentication enforcement, input validation, error responses
- Bulk import endpoint: full-batch validation, atomic transactions, dry-run mode, conflict policies (reject, skip, upsert)
- Database migration runner: forward migration from empty database, idempotency of individual migrations, schema version tracking
- Event bus: event publishing, SSE delivery to subscribers, event schema validation
- Job system: submission, status transitions, progress events, cancellation
- Health and metrics endpoints

### Plugin tests

Tests for the plugin loader and the plugin interface contract.

**Coverage targets:**
- Plugin discovery: `.py` files and packages in the plugins directory
- Compatibility validation: API version range checking, dependency graph resolution, cycle detection
- Load order: topological sort, providers before consumers
- Service registry: registration, retrieval, version constraints, namespace queries
- `PLUGIN_PACKAGES` installation: catalog-governed resolution, off-catalog warning
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
- **Authentication**: requests without bearer token are rejected; requests with invalid token are rejected; every endpoint enforces auth (no accidental unauthenticated endpoints)
- **SQL injection**: parameterized queries only — test with known injection payloads against all user-input-accepting endpoints
- **Host header validation**: requests with unexpected Host headers are rejected
- **CORS**: cross-origin requests from non-allowlisted origins are rejected; preflight requests are denied
- **Input validation**: malformed import payloads are rejected with full error details; oversized payloads are handled gracefully
- **Encryption round-trip**: create database with SQLCipher → close → reopen with correct key → data intact; reopen with wrong key → failure
- **Recovery Kit flow**: init → generate Recovery Kit → simulate new machine → restore from kit → database unlocks
- **Health data in logs**: after a test run that exercises all endpoints, grep all log output for known test biomarker values — none should appear

### Migration tests

Tests that validate the database migration system.

**Coverage targets:**
- Fresh database: all migrations apply in sequence from empty
- Incremental: each migration applies cleanly to a database at the previous version
- Idempotency: running `healthspan db migrate` twice produces no errors and no duplicate rows in `schema_version`
- Failure recovery: a deliberately broken migration rolls back cleanly; subsequent valid migrations still apply
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

### Fixture management

Fixtures live in `tests/fixtures/` as JSON or SQL files. A fixture loader creates an ephemeral SQLCipher database, applies all migrations, and loads the specified fixtures before each test or test suite.

---

## Cross-Platform Testing

The platform targets Windows, macOS, and Linux. CI must test on all three. Platform-specific concerns:

- **OS keychain**: `keyring` backend differs per platform (DPAPI, macOS Keychain, libsecret). Tests that exercise key storage must run on each platform or mock the keyring backend with `keyring.testing`.
- **File permissions**: `chmod 600` behavior differs on Windows. Tests that validate config file or database file permissions must account for platform differences.
- **Path handling**: forward vs backslash; tests should use `pathlib.Path` consistently.
- **SQLCipher build**: `sqlcipher3` has platform-specific build requirements. CI must verify the build succeeds on each target.

---

## Links
- Related: [security.md](security.md) — security requirements that tests must validate
- Related: [observability.md](observability.md) — health data logging prohibition must be tested
- Related: [ADR-0004](adr/0004-data-ingestion-strategy.md) — bulk import behavior to test
- Related: [ADR-0009](adr/0009-database-migration.md) — migration runner behavior to test
- Related: [ADR-0010](adr/0010-cli-plugin-model.md) — plugin loader behavior to test
- Related: [ADR-0013](adr/0013-encryption-at-rest.md) — encryption round-trip to test

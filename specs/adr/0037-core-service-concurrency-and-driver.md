# ADR-0037: Core Service Concurrency Model and Database Driver Choice

## Status
Accepted

## Context and Problem Statement
The [2026-07-06 architecture review](../reviews/architecture-review-2026-07-06.md) (item 3.C) found that nothing in the specs says how the synchronous SQLCipher driver meets async FastAPI. The default failure mode — calling the driver inside `async def` endpoints — blocks the event loop, stalling the SSE stream (ADR-0011) and every concurrent request while query work runs. ADR-0028 decided a persistent connection pool exists; it did not decide the pool's structure, the threading model, or where blocking work (blob streaming, backups) runs. Every endpoint inherits this decision, which is why it must be written down before anything downstream assumes an answer.

A second question was folded in during resolution: whether to move from the Python DB-API driver (`sqlcipher3`, named in ADR-0013) to **apsw** for more direct SQLite control — apsw has no implicit transaction management (the defect class ADR-0035 had to specify around), exposes exact SQLite error codes, and gives fine-grained control of the backup and incremental-blob APIs. SQLite lock-in was accepted as a non-issue going in: portability is already fictional (SQLCipher, the pragma discipline, WAL semantics, FTS5, and the supersession SQL are all SQLite-specific, and design-rationale.md no longer offers PostgreSQL as an option).

## Decision Drivers
- The event loop must never block on database work: SSE liveness (ADR-0011) and request concurrency depend on it
- SQLite connections are thread-affine in practice; the model must make misuse structurally hard, not merely documented
- Single-user scale: the design should be boring and correct, not throughput-optimized machinery
- The encryption layer guards the entire platform (ADR-0013); crypto provenance outweighs driver ergonomics
- Distribution is `uv tool install` from PyPI wheels (ADR-0023) — any driver requiring a C toolchain on user machines is disqualified
- Every apsw capability the design actually needs (explicit transactions, native backup stepping, incremental blob I/O) must be either present in the chosen driver or consciously given up

## Considered Options

**Concurrency model:**
1. Async endpoints calling the driver directly (status quo failure mode)
2. **Synchronous repository layer bridged by FastAPI's threadpool; thread-affine connection pool** (chosen)
3. Async wrapper layer (aiosqlite-style connection actor thread)

**Driver:**
- (a) **DB-API `sqlcipher3` with canonical Zetetic SQLCipher, wheels from `sqlcipher3-wheels`** (chosen)
- (b) apsw built from source against the SQLCipher amalgamation
- (c) `apsw-sqlite3mc` — apsw prebuilt against SQLite3MultipleCiphers in its SQLCipher-compatible cipher mode

## Decision Outcome
Chosen: **concurrency option 2, driver option (a).**

### Concurrency model: synchronous repository, threadpool bridge

No async database layer exists anywhere in the platform. The rules:

1. **Sync repository, `def` endpoints.** All database access goes through synchronous repository functions. Every DB-touching endpoint is declared `def`; FastAPI runs it on the AnyIO worker threadpool automatically. `async def` is reserved for endpoints that do not touch the database. The invariant, stated for testing: **the driver is never called on the event loop thread.** An `async def` code path that must reach the database (SSE catch-up/replay) goes through the threadpool bridge (`anyio.to_thread.run_sync` / Starlette `run_in_threadpool`) — never the repository directly.
2. **Thread-affine connection pool.** One connection per worker thread, created lazily and thread-locally by ADR-0035's single shared connection factory, keyed once at creation with the held raw key (ADR-0028). `check_same_thread=True` stays **on**: with thread-local connections it is no longer an obstacle but the enforcement mechanism for the affinity rule — a connection leaking across threads fails loudly at the driver.
3. **Deliberately small threadpool.** The AnyIO worker limit (default 40) is capped at **8**. Pool size equals connection count; each connection carries its own page cache; this is a single-user platform where WAL provides many readers plus one writer — more threads buy write contention and memory, not throughput.
4. **Write discipline: `BEGIN IMMEDIATE` + `busy_timeout`.** Repository write transactions take the write lock up front (the same fail-fast rationale ADR-0035 gives the migration runner); `busy_timeout = 5000` ms joins ADR-0035's connection-factory pragma set. A timeout maps to HTTP 503 — at this platform's bimodal write volume (a few imports per week plus rare corrections, ADR-0027) it should be unobservable in practice.
5. **Where the known blockers run:**
   - **Argon2id** — once, at Core Service startup, before the server accepts connections (the startup sequencing itself is the T1.4 decision, not this one).
   - **Blob streaming (ADR-0034)** — synchronous incremental-blob reads, iterated in chunks through Starlette's threadpool iterator into the streaming response. The event loop only ever forwards completed chunks.
   - **Backups** — the driver's native backup API (`sqlite3_backup`) stepped in small page increments on a worker thread, sleeping between steps so it never starves writers. This ADR guarantees the primitive; where scheduled backups execute is decided by the backup-locus work (review 2.1).
   - **SSE (ADR-0011)** — stays `async def`, fed by an in-process, thread-safe event bus. Synchronous repository code on worker threads publishes into the loop via `call_soon_threadsafe`; the bus is the single object that crosses the sync/async boundary and is thread-safe by construction.
6. **Rejected: async wrapper layer.** An aiosqlite-style connection-actor adds machinery with no throughput gain at single-user scale, and it *hides* the affinity discipline inside a wrapper instead of enforcing it at the driver.

### Driver: stay on DB-API `sqlcipher3`; source wheels from `sqlcipher3-wheels`

ADR-0013 named `sqlcipher3` as the driver; this ADR re-affirms it after evaluating the apsw routes, and adds the wheel-channel decision ADR-0013 never made. The deciding facts (research summary below):

- **Option (b) is not actually available.** Official apsw wheels bundle vanilla SQLite only; there is no apsw+SQLCipher build channel. Building apsw from source against the SQLCipher amalgamation requires a C compiler and OpenSSL on every user machine — disqualified outright by ADR-0023's `uv tool install` distribution.
- **Option (c) is real but fails on crypto provenance.** `apsw-sqlite3mc` (first-party wheels by the SQLite3MultipleCiphers author) is production-stable, covers every platform, and tracks SQLite releases within days. Its problems are documented in full under Pros and Cons — the decisive one: the page-level crypto is an independent *reimplementation* of the SQLCipher scheme, not Zetetic's audited, Signal-deployed code, on a database whose entire security story is encryption at rest.
- **Option (a) keeps canonical crypto, and every apsw capability the design needs exists in the fork**: the native backup API with page-stepped copying, incremental blob I/O (`blobopen`), and explicit transaction control via `isolation_level=None` — which ADR-0035 already mandates, neutralizing DB-API's implicit-transaction behavior, its worst defect.

**Wheel channel:** the official `sqlcipher3-binary` package publishes Linux x86_64 wheels only. Windows and macOS — including this platform's primary machine — are served by **`sqlcipher3-wheels`**, a third-party fork whose GitHub Actions build canonical Zetetic SQLCipher source into wheels for all three OSes. This is a single-maintainer supply-chain link and is accepted with eyes open: the dependency is **pinned and hash-verified** in the platform lockfile (the same integrity discipline ADR-0036 applies to plugins), and the tradeoff is recorded here rather than discovered later.

**Named revisit triggers** (any one reopens the driver choice, with option (c) the designated fallback):
- The `sqlcipher3-wheels` channel goes stale (no wheels for a needed Python version or platform within a release cycle)
- A driver defect surfaces that the fork cannot or will not fix
- A future packaged, signed distribution (the ADR-0028 macOS discussion's future path) makes source-building apsw against canonical SQLCipher viable

### Positive Consequences
- The event-loop-never-blocks rule is testable (SSE heartbeat stays live during a slow pooled query — testing-strategy.md) instead of aspirational
- Thread affinity is enforced by the driver (`check_same_thread=True` + thread-local pool), not by convention
- ADR-0028's "persistent connection pool" hand-wave now has a concrete shape: 8 thread-local connections, keyed once, single factory
- Backup and blob-streaming work have a stated home (worker threads, stepped/chunked) that the backup-locus and document-retrieval designs can build on
- Canonical Zetetic SQLCipher remains the crypto layer; ADR-0013 is confirmed, not extended
- The apsw evaluation is on the record with dated facts, so the question is not re-litigated from scratch when a revisit trigger fires

### Negative Consequences / Tradeoffs
- Windows/macOS binaries come from a single-maintainer fork channel (`sqlcipher3-wheels`) — mitigated by pinning + hash verification, honestly recorded, and monitored via the revisit triggers
- The DB-API driver's legacy transaction behavior remains a live hazard anywhere the ADR-0035 discipline (`isolation_level=None`, explicit `BEGIN`/`COMMIT` in one factory) is not followed — one factory and the mid-file-atomicity test are the containment
- A hard cap of 8 worker threads bounds request concurrency; a long-running synchronous request occupies a scarce slot (acceptable: single user, and jobs — the actually-long work — run in child processes per ADR-0012)
- `def` endpoints make "accidentally async-blocking" impossible but cost a thread hop on every request — negligible against SQLCipher page decryption, and the boring-correct tradeoff this scale wants

## Research Summary (2026-07-07)

Facts established while resolving the driver question, recorded so future revisits start from evidence, not memory:

| Package | Finding |
|---|---|
| `apsw` (official PyPI) | Wheels bundle vanilla SQLite ("embedded privately inside"); no SQLCipher/SEE build option or channel. Source builds accept a custom amalgamation (`sqlite3.c` dropped into the build tree) but require a C toolchain — and SQLCipher additionally OpenSSL. The apsw docs themselves point encryption users to `apsw-sqlite3mc`. |
| `apsw-sqlite3mc` | apsw statically compiled against SQLite3MultipleCiphers by its author (Ulrich Telle). v3.53.2.0 released 2026-06-06, tracking SQLite 3.53.2; wheels for Windows (x86, x86-64, ARM64), macOS (x86-64, ARM64), Linux (manylinux/musllinux); CPython 3.10–3.14; Development Status: Production/Stable. SQLite3MC's `sqlcipher` cipher scheme is file-format compatible with SQLCipher 4. |
| `sqlcipher3` (coleifer) | pysqlite3 fork bound to SQLCipher. Exposes the native backup API and incremental blob I/O (`blobopen`). Whether it exposes CPython 3.12's PEP 249 `autocommit` attribute was not confirmed — immaterial, since ADR-0035 already specifies `isolation_level=None` as the required semantics with `autocommit` as an optional alternative. |
| `sqlcipher3-binary` (official wheels) | v0.6.0 (2025-12-31): **manylinux x86_64 wheels only** — no Windows, no macOS. |
| `sqlcipher3-wheels` (laggykiller fork) | v0.5.7 (2026-01-07): 128 wheels covering Windows (win32/amd64/arm64), macOS, and Linux, built from canonical SQLCipher 4 source via the fork's GitHub Actions. |

## Pros and Cons of the Options

### Concurrency: async endpoints calling the driver directly
- Pro: no bridge to explain
- Con: the review's defect — event loop stalls on every query; SSE and concurrent requests freeze behind Argon2id-adjacent or slow query work

### Concurrency: sync repository + threadpool bridge + thread-affine pool (chosen)
- Pro: boring, correct, enforced by the driver itself; one place opens connections; testable liveness contract
- Con: capped concurrency (8 threads); per-request thread hop

### Concurrency: async wrapper layer
- Pro: uniformly `async` call sites
- Con: an actor thread per connection re-implements what the threadpool already does; affinity discipline hidden rather than enforced; more machinery for zero gain at this scale

### Driver (a): DB-API `sqlcipher3` + `sqlcipher3-wheels` (chosen)
- Pro: canonical Zetetic SQLCipher (audited, massively deployed — Signal); ADR-0013 confirmed rather than extended; stdlib-familiar API; ADR-0035's transaction discipline already written against it; backup + blob I/O present
- Con: Windows/macOS wheels from a single-maintainer fork channel; DB-API legacy transaction behavior needs the ADR-0035 discipline to stay contained; fork tracks CPython's `sqlite3` with some lag

### Driver (b): apsw from source against SQLCipher
- Pro: canonical crypto *and* the best driver
- Con: no wheels exist or are planned; requires C toolchain + OpenSSL on user machines — breaks ADR-0023 distribution; disqualified

### Driver (c): `apsw-sqlite3mc`
- Pro: structurally superior driver (no implicit transaction management at all — ADR-0035's defect class cannot occur; exact SQLite error codes; first-class backup/blob control); first-party wheels from a long-established maintainer on every platform including win_arm64; tracks SQLite releases within days; arguably *fewer* supply-chain parties on Windows than option (a)
- Con, documented in full:
  - **Crypto provenance**: the AES page encryption, per-page HMAC, and KDF plumbing are SQLite3MC's independent reimplementation of the SQLCipher scheme — compatible at the file-format level, but not Zetetic's audited and battle-deployed code. For a database whose entire security posture rests on encryption at rest, this is the load-bearing objection.
  - **Concentration risk**: one maintainer authors the crypto implementation *and* builds the wheels — a single point of trust for the platform's most security-critical dependency.
  - **Governance cost**: ADR-0013 (Accepted) decided "SQLCipher"; changing the implementing library requires an extension ADR and a re-verification that its threat-model claims survive the substitution.
  - **Key-handoff re-verification**: ADR-0028's byte-exact raw-key construction (`PRAGMA key = "x'…'"`, internal KDF skipped) is specified against SQLCipher's pragma semantics; SQLite3MC's equivalents (`hexkey`, cipher-scheme configuration pragmas) differ in surface and defaults and would need the same known-answer-test rigor re-established.
  - **Compatibility is a mode, not an identity**: SQLCipher-4 compatibility is one configurable cipher scheme among several; a misconfiguration writes a file canonical SQLCipher tooling cannot open. The compatibility claim is tested by its author but is not a Zetetic guarantee.

## Consequences for Other Documents
- **ADR-0035**: pragma table gains `busy_timeout` (connection factory); Links gains this ADR
- **ADR-0028**: connection-pool bullet gains the thread-affine structure cross-reference
- **testing-strategy.md**: gains the event-loop liveness contract test and the thread-affinity test
- **Backup-locus design (review 2.1)**: inherits "stepped native backup on a worker thread" as its starting primitive
- **Architecture review 2026-07-06**: item 3.C resolved

## Links
- Extends: [ADR-0028](0028-key-derivation-and-rotation.md) — gives the "persistent connection pool" its concrete threading structure
- Extends: [ADR-0035](0035-migration-execution-semantics.md) — adds `busy_timeout` to the connection-factory pragma discipline
- Confirms: [ADR-0013](0013-encryption-at-rest.md) — `sqlcipher3` driver re-affirmed after the apsw evaluation; content untouched
- Related: [ADR-0011](0011-event-bus.md) — the SSE stream whose liveness the loop-never-blocks rule protects
- Related: [ADR-0034](0034-clinical-document-storage.md) — blob streaming runs on the worker-thread side of the bridge
- Related: [ADR-0012](0012-job-abstraction.md) — long-running work belongs in job children, which is why a small threadpool suffices
- Related: [ADR-0023](0023-distribution-mechanism.md) — the wheel-availability constraint that disqualified source-built apsw
- Related: [ADR-0036](0036-plugin-package-installation-integrity.md) — the pin-and-hash-verify discipline applied to the wheel channel
- Resolves: [architecture review 2026-07-06](../reviews/architecture-review-2026-07-06.md), item 3.C — sync/async bridging model (and the apsw driver question raised during resolution)

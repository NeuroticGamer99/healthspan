# Architecture & Security Review — 2026-07-06

Full review of README, all design documents (security, design-rationale, data-model, testing-strategy, observability, api-reference, glossary, open-questions), all 36 ADRs, and the repo-level files (pyproject.toml, .gitignore, publish.yml). Follows the same format as [architecture-review-2026-06-10.md](architecture-review-2026-06-10.md); items are checklists for working through over time.

**Remediation check:** every item from the 2026-06-10 review was verified against the current documents. All are genuinely resolved — the remediation ADRs (0025–0036) are consistent with each other and with the rewritten sections of security.md, testing-strategy.md, and the glossary. The findings below are almost entirely *new seams created by that remediation* (scope matrices, invariants, and process rosters that now exist and can therefore now disagree), plus implementation-facing gaps that matter before migration 0001 and the first line of Core Service code.

**Overall verdict:** No fundamental architecture change is needed. Process isolation + plugin-free Core Service + named scoped tokens + in-transaction audit is a coherent, mutually reinforcing design — the security architecture is now notably stronger than most shipped products in this space. The two items to resolve before implementation starts: the migration-ownership contradiction (1.C) and the watch-folder credential gap (1.A), because both sit on the startup path and the first automation path respectively.

---

## 1. Inconsistencies between documents

### A. The watch-folder importer cannot run under the default token set ⚠️ most important finding

- [ ] Reconcile ADR-0026's default `automation-host` scopes with ADR-0012/ADR-0025's watch-folder import flow.

The contradiction:

- [ADR-0026](adr/0026-named-scoped-tokens.md) default token matrix: `automation-host` holds `read events jobs` (`write` opt-in) — **no `import`**.
- [ADR-0026](adr/0026-named-scoped-tokens.md) scope-laundering rule: "submitting a job requires `jobs` *plus* every scope the job type declares. An import job requires `jobs` + `import`."
- [ADR-0025](adr/0025-plugin-host-process-matrix.md) makes the watch-folder importer the canonical Automation Host resident ("trigger = file appears, action = submit import job"), and [ADR-0012](adr/0012-job-abstraction.md) names it the canonical path-based import client.

Under the default token set, the flagship automation — the one ADR-0025 uses to justify the Automation Host's existence — fails with 403 on its first file.

**Recommendation:** do *not* widen the `automation-host` default (keeping the default automation surface read-and-react is right). Instead, mint a dedicated named token at init (e.g. `watch-import`, scopes `jobs import`) held by the first-party watch-folder component specifically, and document it in ADR-0026's default table. This keeps the mass-mutation capability out of reach of directory-loaded automation plugins (which receive the plugin-tier credential per INV-3), stays visible/revocable per INV-5, and makes the flow actually work.

### B. ADR-0011's publish example uses a reserved namespace its own rules forbid

- [ ] Fix the `context.events.publish("data.imported", …)` example in [ADR-0011](adr/0011-event-bus.md)'s Event API section.

The example is presented as "uniform in every host," but `data.*` is a reserved namespace that no token may publish through `/v1/events/inbound` (ADR-0011's own catalog and ADR-0026 rule 2). From an Automation Host plugin, the exact call shown is structurally rejected. Replace with a namespace a plugin can actually publish (`sync.complete` or `external.*`). Doc-example bug, not a design bug — but this is exactly the example plugin authors will copy.

### C. Who runs migrations — the launcher/CLI or the Core Service?

- [ ] Decide and align: [ADR-0008](adr/0008-process-lifecycle.md) (launcher step 3: "runs any pending database migrations") and [ADR-0035](adr/0035-migration-execution-semantics.md) ("the launcher runs migrations before the Core Service starts... why exclusive access holds") vs. [observability.md](observability.md) startup order ("1. Core Service **(runs migrations on startup)**") and its health endpoint returning 503 for "migration pending."

These describe two different designs. ADR-0035's `BEGIN IMMEDIATE`/exclusive-access reasoning and security.md's "the CLI holds a connection only for migrations and backup" both assume the launcher/CLI path; observability.md assumes in-process migration at Core startup. If Core Service ran migrations itself, the migration runner's foreign-keys-off connection would coexist with the runtime pool's foreign-keys-on factory in one process, and the 503-while-migrating state would be real; if the launcher runs them, "migration pending" can never be observed via the health endpoint (the service isn't up yet) and that response field is dead. Recommend: launcher/CLI runs migrations (matches ADR-0035's locking argument); fix observability.md's startup list and drop or repurpose the "migration pending" 503 example.

### D. INV-1 as worded is falsified by the platform's own CLI maintenance commands

- [ ] Reword INV-1 in [security.md](security.md) (and its statement in [ADR-0025](adr/0025-plugin-host-process-matrix.md)) to be precisely true.

INV-1: "The derived database key exists only in Core Service memory." But `healthspan db migrate`, `db backup`, `db encrypt` (ADR-0033), and `keys change-passphrase` / `keys rotate-secret-key` (ADR-0028) all derive and hold the key **in the CLI process** — that is the documented direct-database exception (ADR-0006), and ADR-0028 even requires exclusive access *because* the CLI opens the database. An invariant that is testably false in five supported flows will erode trust in the whole invariants table. Suggested wording: "The derived key exists only in the memory of the single process currently holding the database open — the Core Service at runtime, or the CLI during an explicitly invoked `db`/`keys` maintenance command run while Core Service is stopped. It is never transmitted, logged, or inherited by child processes (spawn, never fork)."

### E. Health endpoints: "no endpoint is unauthenticated" vs. the launcher's polling

- [ ] Decide the health-endpoint authentication model and give the launcher a specified credential (or a specified exemption).

[api-reference.md](api-reference.md) and [security.md](security.md): every endpoint requires a bearer token, *including health and metrics*. [observability.md](observability.md): the launcher polls each process's health endpoint to gate startup. But ADR-0026's default token set contains no launcher credential, and nothing says which token the launcher presents (it also needs to poll the MCP Server's `/health`, which authenticates via the separate client-facing secret). Docker Compose healthchecks and systemd watchdogs have the same problem. Options: (a) minimal unauthenticated liveness endpoint returning only `200`/`503` and a status word — no version, no `schema_version`, no uptime (that detail moves to an authenticated `/v1/health/detail`); (b) the launcher uses `cli-admin` (it is the CLI) and container healthchecks get a documented low-scope token. Either is defensible; (a) is the industry-standard shape and removes secret-distribution problems from container orchestration. Also assign explicit scopes to `/v1/health` and `/v1/metrics` whichever way this lands (every route must declare a scope per ADR-0026).

### F. The job child process is a plugin host with no entry in the enforcement matrix

- [ ] Add the job child to [ADR-0025](adr/0025-plugin-host-process-matrix.md)'s `HOST_LOADABLE_TYPES` (and to security.md's host-matrix summary table).

ADR-0025's prose matrix row for `import_adapter` says "job child process (heavyweight execution)", and ADR-0012 says heavyweight children load plugin handler code. But the `Host` enumeration/allowlist sketch defines only CLI, MCP_SERVER, AUTOMATION_HOST, and CORE_SERVICE. The process that runs the most third-party code at the highest data-mutation privilege (import jobs) is the one host with no declared allowlist. Suggested: `Host.JOB_CHILD: frozenset({IMPORT_ADAPTER, ANALYSIS, QUERY, PROVIDER})`. security.md's summary table should gain the same row so the two matrices can't drift.

### G. design-rationale.md contains three stale claims the ADRs have since overruled

- [ ] "…or by switching to the PostgreSQL backend (ADR-0003) for true multi-master write access" — ADR-0003 (Accepted) decided **SQLite-only for v1**; PostgreSQL is explicitly *not* an available backend and requires a new ADR chain. The sentence presents it as a current option.
- [ ] "Direct integration with the MCP server and Core Service without a network hop" and "The MCP server queries SQLite and returns focused result sets" (Why SQLite section) — the MCP server has **no** database access (ADR-0006) and reaches data over REST; the same document's MCP Server Design section says so. Rewrite the Why SQLite bullets to attribute database access to the Core Service only.
- [ ] The Adapting This Project section references extending "MCP server tools … with additional query patterns" — fine, but consider pointing at the plugin route (ADR-0010) rather than implying core edits.

design-rationale.md is a living document, so these are ordinary edits.

### H. Trust model still says "cloud backup and sync of the encrypted file is explicitly safe" without the backup-only qualifier

- [ ] Add the ADR-0019 qualifier to [security.md](security.md)'s Trust Model (storage-layer paragraph and the deployment table's "Cloud sync safe" cells).

The 2026-06-10 review (item 1.H) fixed this claim in open-questions.md and ADR-0019 and judged security.md's wording acceptable as a confidentiality statement. On re-read it is still the sentence a user will quote to justify putting the live WAL-mode database in Dropbox. One clause fixes it: "…is explicitly safe *for the output of `healthspan db backup`; the live database file must never be synced (ADR-0019)*."

### I. data-model.md specifies "FK arrays," which SQLite does not have

- [ ] Clinical Documents: "optional FK arrays to `lab_results` draw IDs, `clinical_events`, `interventions`" — there is no array type and no enforceable FK-in-JSON. Specify junction tables (`document_lab_draws`, `document_events`, `document_interventions`) so the links are real foreign keys that participate in `foreign_key_check` (ADR-0035) and the audit model (ADR-0027).

### J. The denormalized "current dose" column conflicts with the correction model

- [ ] data-model.md: interventions' "current dose is a denormalized convenience column derived from the latest `intervention_dose_history` row." Under [ADR-0027](adr/0027-audit-trail-and-corrections.md), every in-place update must be either a supersession or a *designated metadata repair* — a derived cache column is neither, and updating it on every dose change generates audit rows for data that is not source data. Recommend replacing the stored column with a view (or computing it in the repository layer): dose history is the truth; "current dose" is a query. If a stored column is kept for query ergonomics, ADR-0027 needs a third category ("derived denormalizations — excluded from audit/supersession"), which is more machinery than a view costs.

### K. Minor staleness and drift

- [ ] [specs/README.md](README.md) arc42 table: "9. Architecture Decisions | adr/ (24 ADRs)" — there are 36. Consider dropping the count entirely so it can't drift.
- [ ] [security.md](security.md) Database Security: "The CLI holds a connection only for migrations and backup" — now also `db encrypt` (ADR-0033) and the two `keys` rotation commands (ADR-0028). Say "explicitly invoked `db`/`keys` maintenance subcommands."
- [ ] [.gitignore](../.gitignore): recovery-kit patterns are `*recovery-kit*.pdf` / `*recovery-kit*.png`; ADR-0033 says "`*recovery-kit*` patterns" and doesn't fix the render format. Broaden to bare `*recovery-kit*`.
- [ ] [ADR-0019](adr/0019-multi-device-sync.md) is statused "Proposed — stub" but contains a full near-term decision (single-writer + backup-only sync) that other documents cite as settled (open-questions.md Resolved, ADR-0035, security.md). Restatus to Proposed (or Accepted, see 4.A) so the index reflects reality.
- [ ] [ADR-0008](adr/0008-process-lifecycle.md) Option Details still describe first-run generating "a new random bearer token" and printing it — superseded by ADR-0026's token set. The `Extended by` link exists; fine under governance, but the launcher section of any future user-facing doc should be written from ADR-0026, not ADR-0008.

---

## 2. Security specification robustness (ordered by impact)

### 2.1 Give scheduled backups an execution locus — the current design has no process that can run them

- [ ] Specify how scheduled backups actually execute.

ADR-0019 prescribes "a scheduled `healthspan db backup`" as the sync-safe artifact producer, and ADR-0027 makes backups the platform's entire recovery story. But: heavyweight job children **never receive the key** (INV-1/ADR-0012), so backup cannot be a plugin-style job child; the Automation Host cannot run `db backup` (no key, and the CLI command requires the passphrase); and the CLI path requires the user to be present to type the passphrase (or full auto-unlock). The only process that can open the database while the platform runs is the Core Service. **Recommendation:** backup is a first-party *lightweight* job inside Core Service using the SQLite Online Backup API on its existing keyed connection (run on a worker thread — see 3.C), triggered by the scheduler (`schedule.*` events) and on-demand via an admin-scoped endpoint; `healthspan db backup` remains the offline/manual path. This also resolves the tension of a CLI backup opening the database while Core Service holds the write connection.

- [ ] While specifying it: make verification part of *every* backup (open the copy with the current key + `PRAGMA integrity_check`), not only the mandatory pre-rekey backup (ADR-0028). A recovery story built on unverified backups is a hope, not a story. Copy the `.keyparams` sidecar alongside, as ADR-0028 already requires.

### 2.2 Specify the passphrase handoff channel to Core Service

- [ ] The passphrase travels: user → (launcher?) → Core Service at startup (ADR-0028: derive once at startup). Nothing specifies the channel. Require: interactive TTY prompt or stdin pipe — **never argv, never environment variables** (both inspectable; same reasoning that put job tokens on stdin in ADR-0026). Cover the variants: launcher-started (launcher prompts, pipes to child stdin, then drops its copy), directly-started Core Service (prompts itself), headless/systemd (systemd credentials / `LoadCredential=`), Docker (secrets file, already sketched in ADR-0013). Also state that the launcher must not retain the passphrase after handoff. This is a one-section addition to ADR-0028 or ADR-0008's extension chain.

### 2.3 Bound job lifetime so ephemeral tokens cannot live forever

- [ ] ADR-0026's job tokens "expire automatically when the job reaches a terminal state" — a hung or orphaned child never reaches one, so its token (potentially `import`-scoped) lives indefinitely. Add to ADR-0012: per-job-type execution timeout with a default; heartbeat via the progress endpoint (a child silent past the deadline is transitioned to `failed`, which expires the token); Core Service kills the child process on timeout/cancellation; a startup sweep transitions `running` jobs from a previous Core Service run to `failed` (their children are gone), expiring their tokens. Also record a max-concurrent-heavyweight-children limit (resource control, and it bounds token proliferation).

### 2.4 Auth rate limiting keyed on source address alone locks out every local client at once

- [ ] On the default deployment every client is `127.0.0.1`. One misconfigured client (e.g. GUI holding a rotated-out token, retrying on a timer) triggers exponential backoff that also blocks the MCP server, CLI, and Automation Host — a self-inflicted local DoS. Refine ADR-0026: key the limiter on (source address, token *name prefix* parsed from `hsp_<name>_…` — advisory but good enough for bucketing); keep a per-address aggregate cap so name-cycling doesn't evade it; bound the maximum backoff so legitimate clients recover in bounded time; note `healthspan token list` + an admin reset as the operator escape hatch. Keep the uniform failure responses.

### 2.5 Event-flood hardening on the inbound webhook and the replay window

- [ ] `/v1/events/inbound` has no size or rate cap, and ADR-0011's replay window is a single shared ring (10,000 events / 24 h). An `events`-scoped caller flooding `external.*` evicts `data.*`/`job.*` events from the window; an Automation Host reconnecting after a brief outage then gets a `gap` marker instead of its triggers. The gap marker makes the loss *visible*, but ADR-0025's "brief outages do not lose triggers" claim quietly depends on nobody flooding. Add to ADR-0011: payload size cap and per-token rate cap on inbound publication; partition the replay window (reserved namespaces vs. externally publishable ones) or reserve a fraction for Core-emitted events; and state the Automation Host's mandatory reconciliation behavior on `gap` (re-query recent imports via REST) as a requirement, not an aside.

### 2.6 The MCP client-facing credential is underspecified

- [ ] ADR-0026 defines it in three sentences: generated at init, printed once, independent of Core tokens. Specify: where the MCP Server stores it (keyring, same discipline as everything else), whether it is hashed at rest (it should be — same `SHA-256` + `compare_digest` pattern), a rotation command (`healthspan token rotate` covers Core tokens only), and its format (give it the `hsp_` prefix for secret-scanner recognition). Also verify before implementation that mainstream MCP clients can actually present a static bearer header to a Streamable HTTP server — the MCP ecosystem is moving toward OAuth for HTTP transports, and fastmcp's auth options should be checked against the clients Matthew actually uses; if OAuth becomes unavoidable, that is an ADR-0029 extension, better discovered now than after the token plumbing is built.

### 2.7 Enforce the Argon2id parameter floor at derive time, not only at write time

- [ ] ADR-0028: init and rotation refuse to *write* below-floor parameters into `.keyparams`, but nothing refuses to *derive* with a below-floor sidecar (tampered, corrupted, or hand-edited). Cheap hardening: the reader validates parameters against the recorded OWASP floor before deriving and refuses (with the documented recovery guidance) below it. The sidecar is integrity-sensitive even though it is not secret; this is the one check that makes tampering with it unprofitable.

### 2.8 Off-catalog `PLUGIN_PACKAGES` can displace Healthspan's own pinned dependencies

- [ ] All plugin packages install into the single `uv tool` environment (ADR-0023/0024). An off-catalog pin like `cryptography==41.0.0` or `pandas==2.2.3` doesn't just conflict with other plugins — it can downgrade or displace a version Healthspan's own lockfile pinned, including security-relevant transitive deps of the Core Service. ADR-0024 assigns off-catalog conflicts to the user, but the platform should at minimum detect and refuse (or loudly warn on) an off-catalog requirement that conflicts with the platform's own locked versions — the loader has the catalog (now the full transitive closure per ADR-0036) to check against. One paragraph in ADR-0036's scope.

### 2.9 Mechanize two more review-convention requirements as CI gates

- [ ] security.md's "no SQL by string interpolation… enforced by code review convention and, where possible, by linting" — make it mechanical now: ruff/bandit rule S608 (or a semgrep rule) as a mandatory CI gate, with the single sanctioned `PRAGMA key` exception annotated inline (`# noqa: S608 — ADR-0028 sanctioned`). Add alongside the canary and gitleaks gates in testing-strategy.md.
- [ ] security.md's "run `pip-audit` … before releases" — pin it as a scheduled + release-blocking CI step, same treatment as gitleaks. Unpinned advice rots.

### 2.10 Smaller items

- [ ] Broaden `.gitignore` recovery-kit pattern (see 1.K).
- [ ] [publish.yml](../.github/workflows/publish.yml): pin the uv *version* in the pinned setup-uv action (`with: version:`) so releases are reproducible against a known resolver; SHA-pinning the action alone doesn't pin the tool it installs at runtime.
- [ ] ADR-0015's `--encrypt` recipient-ergonomics claim: Windows Explorer opens only legacy ZipCrypto archives, not AES-256 ZIPs — a physician's office on stock Windows *will* need 7-Zip or equivalent. Keep AES ZIP (correct choice), but the ADR and the command's output should say so and offer one-line recipient instructions; "common tools" currently overstates it.
- [ ] ADR-0034's document BLOBs: note that MCP tools must also not expose the extracted `body` of arbitrary size unpaginated (ties into 3.G).

---

## 3. Architecture, frameworks, and best practices (Python, AI, database)

**No fundamental change needed.** The remediated architecture is coherent: the plugin-free Core Service (ADR-0025), scoped tokens (ADR-0026), in-transaction audit (ADR-0027), and precise KDF/pragma specs (ADR-0028/0035) reinforce each other, and the honesty standard (threat-model tables, "what this does not protect" sections) is applied consistently. The items below are refinements and pre-implementation decisions, not redesigns.

### 3.A Decide bulk-import audit granularity before migration 0001 — per-row audit will not survive the first CGM backfill

- [ ] [ADR-0027](adr/0027-audit-trail-and-corrections.md) + testing-strategy's mutation-matrix test currently imply: every inserted row writes one `audit_log` row containing the full row image as JSON. ADR-0027 sized the design for "a few imports per week," but data-model.md forecasts CGM backfills of **millions of rows in a single import**. Per-row insert audit would roughly triple the write volume and permanently store a JSON duplicate of every CGM reading — in a table that is deliberately never pruned.

**Recommendation:** batch-level audit for bulk-import *inserts* — one `audit_log` row per import batch (operation `import`, row count, source, adapter, dry-run/conflict-policy metadata), relying on the `import_batch_id` provenance column that every imported row already carries to answer "where did this row come from." Keep per-row audit (with images) for `update`, `correct`, and `delete`, where the old/new images are the whole point. This preserves every question the audit trail exists to answer ("what changed, when, by what" — for inserts, the batch row + the data row itself answer it) at a storage cost that scales with *mutations* rather than with data volume. Needs an ADR-0027 extension (it changes decision content) and a matching adjustment to the mutation-matrix test contract — cheap now, disruptive after migration 0001.

### 3.B Use SQLite STRICT tables and CHECK constraints — ADR-0003 explicitly paid for them

- [ ] ADR-0003's "SQLite-specific features freely usable" should be cashed in where it matters most for clinical data integrity:
  - `CREATE TABLE … STRICT` on every data table — real type enforcement, so a `'95'` string can never sit in a REAL column (SQLite's default flexible typing is exactly wrong for lab values).
  - CHECK constraints encoding [ADR-0030](adr/0030-biomarker-identity.md)'s value model: `CHECK (value_num IS NOT NULL OR value_text IS NOT NULL)`, `CHECK (comparator IS NULL OR value_num IS NOT NULL)`, `CHECK (comparator IN ('<','<=','>=','>'))`.
  - `PRAGMA application_id` set at init so the file self-identifies.
  - A `UNIQUE(framework_id, biomarker_id, effective_date)` constraint on `framework_ranges` (ADR-0005), plus a documented lookup rule (latest `effective_date` ≤ draw date; NULL rows as the dateless default) so point-in-time resolution is deterministic.
- [ ] Record these in the migration-0001 design notes or a short data-integrity section of an existing Proposed schema ADR — they are the database-level analog of the validation boundary.

### 3.C Decide the sync/async bridging model for the Core Service — the classic FastAPI + SQLite pitfall

- [ ] `sqlcipher3` is a synchronous DB-API driver; FastAPI is async. Nothing yet specifies how they meet, and the default failure mode (calling the driver inside `async def` endpoints) blocks the event loop — stalling the SSE stream (ADR-0011) and every concurrent request while Argon2id-adjacent or query work runs. Options: (a) declare the repository layer synchronous and use FastAPI's `def` endpoints / `run_in_threadpool`, with the ADR-0028 connection pool made **thread-affine** (SQLite connections are per-thread; `check_same_thread` discipline); (b) an async wrapper thread-pool layer. Recommend (a) stated explicitly — it is boring and correct at this scale. This belongs in a short ADR or an implementation-notes section beside ADR-0028/0035; it also determines where the backup job (2.1) and blob streaming (ADR-0034) run.

### 3.D Resolve the clinical-documents FTS question as FTS5 — and decide it with the documents table

- [ ] open-questions.md lists FTS5 vs. app-level search vs. embeddings. Recommend **FTS5 external-content table** over `body` now: it is compiled into standard SQLCipher builds, the index pages are encrypted like every other page, external-content mode avoids storing the text twice, and the sync triggers are mechanical. App-level scan is a dead end the moment "summarize all provider guidance" spans years of notes; embedding search is genuinely valuable but is a *plugin-layer addition* (ADR-0010 `analysis`/`query` types) on top of FTS, not a replacement — and its vector store must live inside the encryption boundary if it stores note content, which is a future ADR's problem. Deciding FTS5 now lets the documents migration ship its triggers with the table instead of retrofitting.

### 3.E The launcher's supervision story is weaker than the reliability claims built on it

- [ ] ADR-0008 concedes the launcher has "no automatic restart on crash," but ADR-0025 sells the Automation Host as "a supervised resident process" whose honest reliability story justifies the fourth process. A crashed Automation Host (or Core Service) currently stays down until the user notices. Add restart-with-backoff (bounded retries, `system.*` event on restart, refuse-loop detection) to the launcher for Core Service and Automation Host at minimum, or explicitly document systemd/OS service wrappers as *the* reliability path and downgrade the "supervised" language. Also: ADR-0019's second-instance detection via lock file is racy and stale-file-prone — hold an OS advisory lock (`fcntl`/`msvcrt` on a sentinel file) for the process lifetime instead; it cannot go stale.

### 3.F Time-series pragmatics: pair ADR-0021's design with the CGM importer, and set index expectations now

- [ ] ADR-0021 (stub) is correctly deferred, but two things should be stated when it lands: SQLite has no materialized views, so aggregates are plain summary tables rebuilt by first-party jobs on `data.*` events (the invalidation contract ADR-0027 already fixed); and the `cgm_readings` baseline index should be `(reading_timestamp_utc)` — with the `superseded_by IS NULL` partial-index pattern applied only if CGM rows are actually correctable (they are bulk-imported facts; consider exempting CGM from supersession entirely and treating re-import as the correction mechanism, which also simplifies 3.A). Note the timestamp quadruple costs ~4 columns × millions of rows; that is acceptable (~tens of MB), but say so deliberately rather than discovering it.

### 3.G Specify the MCP tool output contract — the AI-facing half of the value model

- [ ] ADR-0030's comparator/qualitative value model must survive the trip through MCP tools, or the AI client will re-introduce the bugs the schema fixed. When the tool surface is finalized (api-reference.md), require:
  - Censored values render as strings (`"<0.1"`), never bare numerics; qualitative results render as their text; units and the applicable reference range accompany every value (the AI should never guess units).
  - Every tool declares MCP **tool annotations** (`readOnlyHint: true` for the default read-only set) so clients display and gate them correctly under the current MCP spec.
  - Pagination / row-count caps on every tool (context-window hygiene *and* exfiltration friction — a prompt-injected client should need many visible calls, not one, to dump years of data).
  - Untrusted free text (clinical-note `body`, intervention notes) is returned inside clearly delimited data blocks with an instruction-shielding preamble, per current prompt-injection best practice — this makes security.md's "should not amplify injected instructions" requirement concrete and testable.
  - Document originals (ADR-0034 BLOBs) stay unexposed by default, as already decided.

### 3.H Honest zeroization: hold the key in a mutable buffer

- [ ] ADR-0028's best-effort shutdown zeroization cannot work on an immutable `bytes` object (CPython will have copied it, and `SecretStr` wraps `str`). If zeroization is to be more than a comment, hold the 32-byte key in a `bytearray` (mutable, zeroable in place) inside the pool/keying component, produce the transient hex only at connection-open, and keep `SecretStr` semantics for repr/log protection via a tiny wrapper. Still best-effort (the hex string and SQLCipher's own copy exist transiently), but it makes the shutdown overwrite real rather than symbolic. One paragraph in ADR-0028's implementation notes.

### 3.I Frameworks: no changes recommended

FastAPI + fastmcp + typer + PySide6 + sqlcipher3 + keyring + argon2-cffi remain the right stack; the 2026-07 wheel verification for Python 3.14 is recorded in testing-strategy.md and holds. `>=3.14` is consistent with the project's stay-current posture. Do not add an ORM: the repository layer + plain SQL + the audit/pragma discipline of ADR-0027/0035 is a better fit than SQLAlchemy for a single-dialect, audit-critical schema, and Alembic's trigger (multi-backend) hasn't fired. GraphQL stays correctly deferred. `pyproject.toml`'s empty `dependencies` is fine at this phase; when the first real dependency lands, the Dependency Security requirements (pinning, pip-audit — see 2.9) apply from day one.

---

## 4. Governance and documentation hygiene

### 4.A Promote the load-bearing Proposed ADRs — the invariants table currently rests on "not yet binding" documents

- [ ] 20 of 36 ADRs are Proposed, but several are cited as *standing requirements*: security.md's INV-1…INV-5 are established by ADR-0025/0026 (Proposed), the Encryption at Rest section is normatively ADR-0028 (Proposed), and migration 0001's schema shape is fixed by ADR-0027/0030/0031/0035 (all Proposed). Per the ADR README's own status table, Proposed means "under discussion; not yet binding" — so the platform's strongest security claims are formally non-binding. After working through this review: batch-accept the settled set — **0005, 0011, 0012, 0025, 0026, 0027, 0028, 0029, 0030, 0033, 0034, 0035, 0036** (0031 stays Proposed pending its conversion-engine sub-decision, or accept it with the sub-decision explicitly carved out; 0015 stays Proposed pending format specification). Acceptance is the review gate that makes INV-1…5 binding before code is measured against them.
- [ ] Update the index and any "currently Proposed" caveats in README.md accordingly (the README currently flags ADR-0005/0011/0012/0016/0017/0025 as designed-not-final — correct today, revisit after the batch acceptance).

### 4.B Keep the two host/scope matrices single-sourced

- [ ] The host-process matrix now exists in ADR-0025 (normative) and security.md (summary), and the scope matrix in ADR-0026 (normative) and security.md (summary). Items 1.A and 1.F show these can drift. Add a one-line "summary of ADR-00XX — that ADR is authoritative" header over each security.md table, and when the enforcement code exists, generate-or-test the doc tables against `HOST_LOADABLE_TYPES` and the default-token fixture (a docs-consistency test in CI, same spirit as the ADR-0025 no-loader-import test).

### 4.C Small fixes list (mechanical)

- [ ] specs/README.md arc42 ADR count (1.K)
- [ ] security.md CLI direct-DB command enumeration (1.K)
- [ ] observability.md startup order and 503 example after 1.C is decided
- [ ] ADR-0011 publish example (1.B)
- [ ] design-rationale.md stale lines (1.G)
- [ ] security.md trust-model sync qualifier (1.H)
- [ ] ADR-0019 status (1.K)
- [ ] .gitignore recovery-kit pattern (2.10)

---

## Strongest parts (for the record)

The remediation work since 2026-06-10 is the strongest part of the project. Specifically: the two derivation principles in ADR-0025 (data-not-code, events-out-of-process) give future plugin types a *generative* rule rather than a table to imitate; ADR-0026's event-forgery analysis (source stamping, reserved namespaces, per-token publish allowlists) closes a confused-deputy class most platforms never even name; ADR-0028 and ADR-0035 turn two hand-wavy areas (KDF construction, driver transaction semantics) into byte-exact, testable specifications; and ADR-0027's batch of decisions (in-transaction audit, supersession, hard-delete-with-image) is exactly proportioned to a single-writer analytical store. The consistent "what this does not protect" honesty sections remain better than industry norm and should be preserved as a hard convention in every future security-relevant ADR.

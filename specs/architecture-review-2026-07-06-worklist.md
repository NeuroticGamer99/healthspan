# Work Plan — Architecture Review 2026-07-06

Execution ordering for [architecture-review-2026-07-06.md](architecture-review-2026-07-06.md), sorted by reasoning difficulty: open architecture decisions first (work these with a high-reasoning model — Fable, high thinking), bounded design work second (Fable/Opus, normal effort), mechanical edits last (Sonnet-level). Item numbers refer to the review document.

**Global sequencing rule:** every task that edits a still-**Proposed** ADR (0005, 0011, 0012, 0025, 0026, 0027, 0028, 0030, 0031, 0033–0040, plus 0015/0019) must land **before** the batch acceptance flip (4.A). While Proposed, these are direct edits; after acceptance, the same change costs a full extension ADR under governance. This is why 4.A is deliberately last despite being mechanically trivial.

---

## Tier 1 — Fable, high thinking: open architecture decisions

These have real tradeoffs, cross-document blast radius, and (for the first three) sit on the migration-0001 or core-runtime critical path. Do them first, one per session, in this order.

### T1.1 — Bulk-import audit granularity (review 3.A) ⚠️ hardest, most consequential

- [x] Decide batch-level vs per-row audit for bulk-import inserts; edit [ADR-0027](adr/0027-audit-trail-and-corrections.md) directly (still Proposed — no extension ADR needed yet). — *Done 2026-07-07: batch-level `import` audit rows per (batch × table); upserts resolve as no-op or supersession (`correct`, per-row images); see review 3.A resolution note.*

Why hard: it reopens a decided ADR's core contract. Must reason through what batch-level insert audit preserves and loses ("what changed, when, by what" for inserts vs. corrections), how the mutation-matrix test contract changes, whether `upsert`-conflict-policy imports count as inserts or updates (they mutate existing rows — probably per-row), and how the batch audit row composes with `import_batch_id` provenance. Fan-out: testing-strategy.md mutation-matrix wording, data-model.md source-provenance note.
Interacts with: T1.5's CGM question (whether CGM rows are supersession-exempt changes the volume math).

### T1.2 — Core Service concurrency model: sync driver under async FastAPI (review 3.C)

- [x] Decide the sync/async bridge (recommend: synchronous repository layer on threadpool via `def` endpoints; thread-affine connection pool; `check_same_thread` discipline). Record as a short new ADR or an implementation-semantics section extending the ADR-0028/0035 cluster. — *Done 2026-07-07: new [ADR-0037](adr/0037-core-service-concurrency-and-driver.md) (sync repository, `def` endpoints, thread-affine pool of 8, `BEGIN IMMEDIATE` + `busy_timeout`); also re-affirmed DB-API `sqlcipher3` over apsw/`apsw-sqlite3mc` with the research on record and named revisit triggers; see review 3.C resolution note. ADR-0037 joins the T3.4 flip list.*

Why hard: it's a structural decision every endpoint, the SSE stream, blob streaming (ADR-0034), and the backup job inherit. Requires reasoning about SQLite connection/thread affinity, pool sizing under WAL, and where Argon2id and `sqlite3_backup` block. Nothing else in Tier 1 should assume an answer before this is written down.

### T1.3 — Scheduled backup execution locus + routine verification (review 2.1)

- [x] Specify where scheduled backups run (recommend: first-party lightweight job inside Core Service on a worker thread, scheduler-triggered, admin endpoint for on-demand; CLI `db backup` stays the offline path) and make verification part of every backup. — *Done 2026-07-07: new [ADR-0038](adr/0038-backup-execution-and-verification.md) — `backup.database` first-party lightweight job on a dedicated worker thread, Core-internal scheduler + on-demand via `POST /v1/jobs` (admin scope, no new endpoint), verify-then-publish (full `integrity_check`, atomic rename, sidecar alongside, failed backups never prune), CLI `db backup` refuses while Core Service runs; ADR-0012 lightweight wording corrected per ADR-0037; see review 2.1 resolution notes. ADR-0038 joins the T3.4 flip list.*

Why hard: the current design literally has no process that can run them (children never get the key; Automation Host has no key; CLI needs a human). Must reconcile with INV-1 wording (T3.1), ADR-0019's sync story, and the `.keyparams` sidecar copy requirement. Depends on T1.2 (threading).
Edits: ADR-0019 (Proposed), ADR-0012 or a new backup section, security.md.

### T1.4 — Startup flow: migration ownership + passphrase handoff (reviews 1.C + 2.2, one combined pass)

- [x] Decide who runs migrations (recommend launcher/CLI, matching ADR-0035's locking argument) and specify the passphrase channel (TTY/stdin only, never argv/env; launcher-piped, direct-start, systemd `LoadCredential=`, Docker secret variants; launcher drops its copy after handoff). — *Done 2026-07-07: new [ADR-0039](adr/0039-startup-sequence-and-passphrase-handoff.md) — launcher owns migrations definitively (Core Service verifies `schema_version` and exits on mismatch; "migration pending" 503 removed from observability.md); channel rule TTY/stdin/secret-file, never argv/env, with all deployment variants + GUI (PySide6 dialog) as a sanctioned entry surface piping to the launcher's stdin; double Argon2id per start accepted to keep the derived key untransmitted; T2.8 retain-vs-reprompt constraint named. See review 1.C + 2.2 resolution notes. ADR-0039 joins the T3.4 flip list.*

Why hard: these are the same design surface — the sequence between "user types passphrase," "migrations run against the encrypted DB," and "Core Service derives and holds the key" must be one coherent story. Fan-out: observability.md startup order + the dead "migration pending" 503, ADR-0028 addendum, ADR-0008 extension chain.

### T1.5 — Health endpoint authentication model (review 1.E)

- [x] Decide: minimal unauthenticated liveness (`200`/`503`, status word only, detail behind auth) vs. launcher/orchestrator credentials. Assign explicit scopes to `/v1/health` and `/v1/metrics` either way. — *Done 2026-07-07: new [ADR-0040](adr/0040-health-endpoint-authentication.md) — minimal unauthenticated liveness chosen (credentialed checks rejected on the ADR-0039 argv/inspectability argument: `docker inspect` and `systemctl show` would print the token); liveness declares an explicit `public` route marker; detail moves to `/v1/health/detail`; new seventh scope `monitor` gates detail + `/v1/metrics` (`cli-admin`/`gui` defaults, `mcp` excluded). ADR-0026 tables edited directly (still Proposed). See review 1.E resolution note. ADR-0040 joins the T3.4 flip list.*

Why hard: it's a deliberate exception to (or credential extension of) "no endpoint is unauthenticated," and the answer must work for the launcher, Docker healthchecks, and systemd watchdogs simultaneously. Small in lines, but it's a security-posture decision that should be argued, not defaulted.
Edits: security.md, api-reference.md, observability.md, ADR-0026 (Proposed) if a credential is chosen.

---

## Tier 2 — Fable/Opus, normal effort: bounded design work

Real design content, but the review already narrowed the option space. Each is a self-contained session; order within the tier is by value, and the first two should come early because they complete the security matrices the flip (4.A) will freeze.

### T2.1 — Complete the scope/host matrices (reviews 1.A + 1.F, one pass)

- [x] Add the `watch-import` token (`jobs import`) to ADR-0026's default table and describe its holder; do **not** widen `automation-host`. — *Done 2026-07-07: table row + holder bullet added (default credential count 6 → 7); keyring entry confirmed as `token:watch-import` per the existing convention; no `read`/`events`; provenance-rule deviation noted in the CLI Credential Tiers section as deliberately narrower.*
- [x] Add `Host.JOB_CHILD: frozenset({IMPORT_ADAPTER, ANALYSIS, QUERY, PROVIDER})` to ADR-0025's enforcement sketch; mirror the row in security.md's summary table. — *Done 2026-07-07: added, with a single-plugin-load paragraph (only the executing job type's handler; `automation`/`notification_channel` deliberately absent — delivery is Automation Host residency; credential stays the ephemeral job token); security.md row mirrored; explicit host-allowlist assertion added to testing-strategy.md's plugin tests. See review 1.A + 1.F resolution notes.*

Both ADRs still Proposed — direct edits. The remaining thought: confirm the watch-folder component's token storage (keyring entry name) and that the job-child allowlist shouldn't also carry `notification_channel` (it shouldn't — delivery is Automation Host residency).

### T2.2 — Auth/event abuse hardening (reviews 2.4 + 2.5, one pass)

- [ ] Rate limiter: key on (source address, token-name prefix), per-address aggregate cap, bounded max backoff, admin reset. Edit ADR-0026.
- [ ] Event flood: payload size + per-token rate caps on `/v1/events/inbound`; partition or reserve replay-window capacity for reserved namespaces; make Automation Host `gap` reconciliation a stated requirement. Edit ADR-0011.

Moderate subtlety (evasion reasoning, window-capacity reasoning) but the shapes are given.

### T2.3 — MCP tool output contract (review 3.G)

- [ ] Specify: censored values as strings with units and ranges, qualitative as text, tool annotations (`readOnlyHint`), pagination/row caps on every tool, delimited untrusted free text with instruction-shielding. Lands in api-reference.md's MCP section (+ a note in security.md's prompt-injection paragraph).

Needs current MCP-spec knowledge and care about the ADR-0030 value model surviving serialization — genuine design writing, not transcription.

### T2.4 — MCP client-facing credential spec + ecosystem check (review 2.6)

- [ ] Specify storage (keyring), hashing at rest, rotation command, `hsp_` format; **research** whether the target MCP clients can present static bearer headers to Streamable HTTP servers or whether OAuth pressure forces an ADR-0029 extension.

The research half is why this is Tier 2: the conclusion isn't known in advance.

### T2.5 — Schema-shape decisions for migration 0001 (reviews 3.B + 1.I + 1.J, one pass)

- [ ] STRICT tables, ADR-0030 value-model CHECK constraints, `application_id`, `framework_ranges` UNIQUE + effective-date lookup rule.
- [ ] Replace data-model.md's "FK arrays" with junction tables (`document_lab_draws`, `document_events`, `document_interventions`).
- [ ] Resolve "current dose": recommend view/computed read over stored column; if a column survives, ADR-0027 needs a third mutation category (argue against).

Mostly settled recommendations, but the CHECK design and the current-dose call want one careful pass by someone holding the whole schema in their head.

### T2.6 — Clinical-document search: FTS5 decision (review 3.D)

- [ ] Resolve the open-questions.md entry as FTS5 external-content over `body`; note encryption inheritance, trigger mechanics, and embeddings as a future plugin layer inside the boundary. Short ADR or a decided open-questions entry.

### T2.7 — Job lifetime bounds (review 2.3)

- [ ] Per-job-type timeout, heartbeat-or-fail via the progress endpoint, kill on timeout/cancel, startup sweep of orphaned `running` jobs, max concurrent children. Edit ADR-0012 (Proposed).

The recommendations are nearly complete; the remaining design is default timeout values and sweep semantics.

### T2.8 — Launcher supervision + single-instance lock (review 3.E)

- [ ] Restart-with-backoff for Core Service and Automation Host (or explicitly demote "supervised" and document systemd as the reliability path); replace ADR-0019's lock file with an OS advisory lock held for process lifetime.

Constraint from ADR-0039 (T1.4): the launcher drops its passphrase copy after handoff *because* it never auto-restarts Core Service. Restart-with-backoff must decide re-prompt vs. retain vs. directing users to full auto-unlock — explicitly, not by accident.

---

## Tier 3 — Sonnet-level: specified fixes and mechanical edits

The thinking is already done in the review; these are careful transcription. Safe to batch several per session.

### T3.1 — Precision rewrites with provided wording

- [ ] INV-1 reword in security.md + ADR-0025 (review 1.D — suggested text is in the review).
- [ ] Trust-model sync qualifier, one clause (review 1.H).
- [ ] security.md CLI direct-DB command enumeration → "explicitly invoked `db`/`keys` maintenance subcommands" (review 1.K).
- [ ] Derive-time Argon2id floor enforcement paragraph in ADR-0028 (review 2.7).
- [ ] Off-catalog-vs-platform-lockfile conflict refusal paragraph in ADR-0036 (review 2.8).
- [ ] `bytearray` key-buffer note in ADR-0028 implementation notes (review 3.H).

### T3.2 — Doc-example and staleness fixes

- [ ] ADR-0011 publish example → non-reserved namespace (review 1.B).
- [ ] design-rationale.md: PostgreSQL-as-option sentence; "MCP server queries SQLite" / "without a network hop" bullets (review 1.G).
- [ ] specs/README.md arc42 ADR count — drop the number (review 1.K).
- [ ] ADR-0019 status: "Proposed — stub" → "Proposed" (review 1.K).
- [ ] "Summary of ADR-00XX — that ADR is authoritative" headers over security.md's two matrix tables (review 4.B).

### T3.3 — Repo hygiene and CI gates

- [ ] .gitignore: broaden to bare `*recovery-kit*` (review 2.10).
- [ ] publish.yml: pin uv version via `with: version:` (review 2.10).
- [ ] testing-strategy.md CI Gates: add mandatory ruff/bandit S608 gate (with the ADR-0028 sanctioned-exception annotation convention) and pinned `pip-audit` scheduled + release-blocking step (review 2.9).
- [ ] ADR-0015: AES-ZIP recipient-ergonomics honesty note (Windows Explorer can't open AES ZIP; recipient instructions) (review 2.10).
- [ ] ADR-0034: note MCP body-text pagination expectation, cross-ref T2.3's contract (review 2.10).

### T3.4 — Governance close-out (deliberately last)

- [ ] 4.A batch acceptance flip: 0005, 0011, 0012, 0025, 0026, 0027, 0028, 0029, 0030, 0033, 0034, 0035, 0036, 0037, 0038, 0039, 0040 → Accepted (+ index; 0031 stays Proposed pending the conversion-engine sub-decision; 0032 stays stub; 0019 per T2.8 outcome). Update README's "designed, not final" caveats.
- [ ] 4.B docs-consistency CI test note (generate/verify matrix tables against `HOST_LOADABLE_TYPES` and the default-token fixture) — record as a testing-strategy line item; implementation comes with the code.

**Gate:** T3.4 runs only after every Tier 1/Tier 2 task that edits a Proposed ADR has landed (T1.1, T1.2, T1.3, T1.4, T1.5, T2.1, T2.2, T2.4, T2.7).

---

## Parked (not schedulable yet)

- **3.F — ADR-0021 aggregates + CGM supersession exemption**: deferred by design until the CGM importer exists. When picked up, decide the supersession exemption *with* T1.1's audit-granularity outcome in hand — they're the same volume argument.
- **2.6 OAuth follow-up**: only if T2.4's research concludes static bearer auth won't hold — then an ADR-0029 extension.

## Dependency summary

```
T1.2 (concurrency) ──→ T1.3 (backup locus)
T1.4 (startup flow) ──→ T3.2 observability fixes
T1.1 (audit granularity) ──→ parked 3.F (CGM)
T2.3 (tool contract) ──→ T3.3 ADR-0034 note
T2.8 (supervision) ──→ T3.4 ADR-0019 status decision
All Proposed-ADR edits (T1.1–T1.5, T2.1, T2.2, T2.4, T2.7) ──→ T3.4 (acceptance flip)
```

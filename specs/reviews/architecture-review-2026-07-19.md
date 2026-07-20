# Architecture & Security Review — 2026-07-19

Full review of README, all design documents (security, design-rationale, data-model, testing-strategy, observability, api-reference, glossary, open-questions, lab-data-dimensions, development-plan), all 61 ADRs, and — for the first time — the implemented code: `src/healthspan/` (Phases 0–3 complete; migrations 0001–0005; 51 test files), the CI workflows, and the repo-level files. Follows the same format as [architecture-review-2026-07-07.md](architecture-review-2026-07-07.md); items are checklists for working through over time. Point-in-time state: **between Phase 3 (complete 2026-07-17) and Phase 3.5 (inserted 2026-07-18, not started)** — the same kind of seam the 07-07 review sat on before the batch acceptance flip.

**Remediation check:** every resolved item from the 2026-07-07 review and the 2026-07-17 ADR sweep was verified against the current documents and code. All are genuinely resolved — the supervision-reporting endpoint (`POST /v1/system/process-reports`, `supervise` scope, `launcher` token), the reworded delete flow, `healthspan db restore`, the SSE per-run epoch, passphrase-only rotation/`convert-mode`, watch-folder post-import handling, `foreign_key_check` in backup verification, the strict-typing and lint gates, and the 4.A batch acceptance flip all exist as their resolution notes claim; the 07-17 sweep's A/B/D dispositions (docs-exposure deferral, limiter-claim reword, Extended-by back-links, per-phase status markers) are all present. Deliberately still open from 07-07 §4.B: 1.G third bullet and 1.K's ADR-0008 note (soft, unchanged); 3.F CGM aggregation (parked, trigger still Phase 7); and the docs-consistency generation test — whose precondition has now been met without the item being revisited (see 4.D). From 07-17: C1's non-loopback (a)-vs-(b) decision remains consciously open with the sharpened trigger now load-bearing; C3 (ADR-0014 disposition) correctly waits for Phase 4.

**Overall verdict:** No fundamental architecture change is needed — the fourth review in a row, and the first able to say so against *implemented code* rather than specs alone. The load-bearing decisions survive contact with reality: ADR-0037's single-writer model is implemented as enforcement, not convention; the ADR-0030/0005 value and range models are DB-level `CHECK`s; the ranges engine handles the unit-conversion boundary-coincidence trap explicitly. The findings below are forward-looking seams, concentrated where the next two phases will press: Phase 3.5's catalog **merge** collides with two Accepted invariants and needs its Proposed ADR before the work item starts (3.A — the most important finding), the security invariant table should gain the two properties that merge/delete will press on *before* that ADR is written (2.2), and Phase 4 as planned is overloaded and should be re-sliced (4.A).

---

## 1. Inconsistencies between documents (and code)

### A. data-model.md has no Migration 0005 section and does not document the shipped reference-data seeds

- [ ] Add a `## Migration 0005` section mirroring the 0001–0004 pattern, and record the 0004/0005 seeds.

`src/healthspan/migrations/0004_categories_and_aliases.sql` seeds 4 labs and a ~40-entry starter biomarker catalog; `0005_molar_mass_and_frameworks.sql` seeds 19 molar masses, 3 range frameworks, and 5 framework ranges. [data-model.md](../data-model.md) has dedicated sections for migrations 0001–0004 but none for 0005 (it appears only inline inside the 0004 section), and the 0004 section never mentions its catalog seed — while [manual-entry-quickstart.md](../manual-entry-quickstart.md) ("seeds the reference data") and [api-reference.md](../api-reference.md) ("Three frameworks ship…") both treat the seeds as shipped facts. The 0005 `framework_ranges` INSERTs structurally depend on the 0004 biomarker seed, so the dependency is worth stating too. Owning layer: data-model.md, direct edit — this documents already-shipped structure per the doc's own charter; no ADR needed.

### B. security.md's "Defense in depth" principle cites the gated network controls as if live

- [ ] One-line qualifier on the principles bullet.

The Network Security section is properly framed — its gated-controls preamble says explicitly that Host-header validation, CORS, and HTTPS "are requirements on the future LAN-deployment work item, not descriptions of current behavior" ([security.md](../security.md), Network Security), which is the 07-17 C1 reword applied correctly. But the earlier design-principles bullet ("Authentication, host header validation, and CORS work together", security.md line 39) still reads as a description of working controls; `service.py` installs neither middleware. Same class as the 07-07 bearer-token-qualifier fix: don't let the platform's own docs overstate what its code enforces. Direct edit.

### C. Minor staleness and drift

- [ ] [glossary.md](../glossary.md) offers "Lab Standard" and "Attia" as example frameworks; migration 0005 deliberately seeds neither, with documented sourcing reasons (no citable Attia source; no Lab Standard framework — lab ranges live per-row). Mark the examples as illustrative or cite the actually-seeded frameworks (`nih_medlineplus_lipid_targets`, `ada_standards_of_care`, `aha_cdc_hscrp_risk_strata`).
- [ ] [manual-entry-quickstart.md](../manual-entry-quickstart.md) documents ADR-0060's commands (`biomarkers add`, `labs add`, `categories list`) but omits [ADR-0060](../adr/0060-cli-catalog-add-commands.md) from its owning-ADR list. Add it.
- [ ] [ADR-0060](../adr/0060-cli-catalog-add-commands.md) declares "Builds on: ADR-0057" but [ADR-0057](../adr/0057-reference-data-and-catalog-import-implementation-decisions.md) carries no reciprocal link — the same asymmetry class the 07-17 B-series cleaned up. One navigation back-link on 0057 (permitted in-place edit on an Accepted ADR).

---

## 2. Security specification robustness (ordered by impact)

### 2.1 The specified startup permission warnings are implemented for the config file only — the database, sidecar, and passphrase file are never checked

- [ ] Implement the on-open permission checks security.md already requires, and add the testing-strategy targets.

security.md requires a startup warning when the config file (line 82) or the database file (line 153) has broader-than-owner permissions, and describes the `passphrase_file` channel as "a permission-restricted file." The implementation checks the **config file only**, and only on POSIX (`_check_permissions` in `src/healthspan/config.py`); nothing ever stats the database file, the `.keyparams` sidecar, or the passphrase file on open — owner-only permissions are set only on files the platform *creates*. Failure scenario: a database restored from a tar/cloud-sync copy arrives mode 644, or a hand-created passphrase file sits world-readable in a home directory; startup proceeds silently, and another local account can read ciphertext + sidecar (in passphrase-only mode, the complete offline-attack input) or the passphrase itself — exactly the drift the warn-on-startup clauses exist to catch, with a real health database now on the machine. Fix: check database + sidecar + passphrase file at `build_runtime`/`exclusive_database_access` time (POSIX mode check; on Windows, an "other principals hold grants" check — `fsperm.py` already enumerates explicit principals). Warn-vs-refuse and Windows-check depth are implementation defaults owned by [ADR-0049](../adr/0049-core-service-skeleton-implementation-decisions.md) (Accepted) → batch into the next extension ADR, the ADR-0050/0051 pattern.

### 2.2 Table the two invariants Phase 3.5's merge/delete will press on — INV-7 (append-only audit) and INV-8 (no server-side credential plaintext)

- [ ] Add INV-7 and INV-8 to security.md's invariant table **before** the Phase 3.5 catalog-correction ADR is written.

INV-1…6 cover key custody, plugin isolation, credential discipline, and provenance — but two properties the code now enforces exist only in prose and ADRs, so no future ADR is *forced* to cite them before weakening them: (a) `audit_log` and `auth_audit` are append-only ([ADR-0027](../adr/0027-audit-trail-and-corrections.md); ADR-0050 §6), enforced by `RAISE(ABORT)` triggers; (b) the server stores only credential hashes — plaintexts exist at issuance and in holder-local storage only ([ADR-0026](../adr/0026-named-scoped-tokens.md), implemented in `tokens.py`). security.md's own rule is that breaking an invariant "must be a visible, deliberate decision" — a mechanism that only protects properties in the table. Phase 3.5's merge/delete is the platform's first row-destroying feature; a merge design that "cleans up" audit rows referencing a merged-away entity would pass every existing INV citation check while destroying the evidentiary record ADR-0027 calls the recovery story. Both properties are already decided in Accepted ADRs, so this is a direct edit to security.md (standing-requirements doc) with established-by citations — no new ADR. Sequencing is the point: land it first, so the 3.A ADR must cite INV-7.

### 2.3 FastAPI's default 422 body echoes submitted input — including health values on the import route

- [ ] Decide and record the input-echo posture in [api-reference.md](../api-reference.md)'s error conventions; recommend stripping `input`.

The engine's own validation messages are value-disciplined (they name columns, never values), but a *shape*-level failure never reaches the engine: Pydantic v2's `RequestValidationError` detail includes an `input` field echoing the offending submitted value, and `service.py` installs no override. A malformed `lab_results` row in a 422 response echoes health values back in the error body. Today that returns only to the loopback submitter; in Phase 4 an import-capable AI client would put the echo into an LLM context, and any client that logs error bodies verbatim writes health values outside the canary gate's reach. security.md line 216 already promises "without echoing back sensitive input values." Fix: a custom validation-error handler returning `loc`/`msg`/`type` only (sufficient for a machine-fixable error), recorded in api-reference.md's error-format section in the same PR (decision-capture rule 2).

### 2.4 The Phase-4 MCP server's client-credential verification has no specified throttle or audit trail

- [ ] One paragraph now, so the Phase-4 work item inherits the requirement instead of discovering it.

ADR-0026/0029 specify the `hsp_mcpclient_…` static bearer, hashed-in-keyring verification, and a plain 401 — but the ADR-0051 limiter and `auth_audit` trail are **Core Service** mechanisms; the MCP server verifies its client secret itself, with no specified failure wall and no record of failed attempts, on the endpoint whose purpose is to face the least-trusted client class. Brute-forcing a 32-byte secret is infeasible; the loss is observability — a probing or misconfigured client is invisible. State the requirement: MCP-side failed-verification throttling (the AuthFailureRateLimiter shape; one bucket, one credential) and a metadata-only failure log/counter. Route: the Phase-4 MCP work item's decisions ADR, with a pointer added to [open-questions.md](../open-questions.md) (Operations) so it cannot be missed at kickoff.

### 2.5 Smaller items

- [ ] `publish.yml` publishes whatever commit the release tag points at without requiring `ci-ok` to have passed on it. The ruleset guards `main`, but a tag can point at a commit that never passed the aggregate gate, and the trusted publisher will ship it. Add a pre-publish check that the released commit has a successful `ci-ok` (and/or a tag ruleset). Route: extension of [ADR-0045](../adr/0045-repository-workflow-and-ci-enforcement.md) (Accepted) — batchable with the next CI decisions ADR.
- [ ] `LivenessRateLimiter._hits` (`api_security.py`) is keyed by source address with per-address bounded deques but **no key eviction** — one or two entries forever on loopback, but an unbounded-cardinality unauthenticated memory surface under a future non-loopback bind (contrast `AuthFailureRateLimiter._maybe_sweep`, which prunes). Add one line to the "Non-loopback binding hardening" gated-controls list in [open-questions.md](../open-questions.md), or fix opportunistically with an idle sweep (local implementation detail, rule 6, once the requirement is on the gate list).

---

## 3. Architecture, frameworks, and best practices (Python, AI, database)

**No fundamental change needed.** Tested against the five pressure points the new code and data create: (a) the cohort-dimension range gap is an additive migration, not a redesign (3.B); (b) the audit/supersession model survives Phase 3.5's merge — but does not *answer* it without an ADR (3.A); (c) ADR-0037's single-writer model remains right as MCP/jobs arrive — jobs run in child processes, MCP is a stateless REST client, so the 8-thread cap is never the long-work bottleneck; (d) the event-bus-inside-Core + SSE design stands, with the stale-cursor→`gap` epoch already specified; (e) the MCP tool output contract is current against the 2025-11-25 spec revision, not stale (3.E).

### 3.A Phase 3.5 catalog merge needs its Proposed ADR before the work item — it collides with two Accepted decisions ⚠️ most important finding

- [ ] Write the catalog-correction ADR (Phase 3.5's first work item, or before it), resolving both collisions; cite INV-7 (2.2).

[development-plan.md](../development-plan.md) specifies merge as "re-point every `biomarker_id`-dependent row (`lab_results`, `biomarker_aliases`, `framework_ranges`) onto a surviving row." Two problems the audit model *survives* but does not *answer*:

1. **Supersede-vs-in-place routing.** [ADR-0027](../adr/0027-audit-trail-and-corrections.md)'s correction model gives every content-table change one of two routes: value corrections supersede (new row + `superseded_by` + `correct` audit row); only *designated metadata columns* update in place. Re-pointing a result's `biomarker_id` is neither cleanly — it is an identity fix, a category the ADR deliberately does not have. Whichever route the merge takes must be argued, not improvised: supersession preserves the mistaken-identity history (and bloats chains for a bulk merge); in-place declares `biomarker_id` a repairable metadata column (and rewrites what the row *meant*).
2. **Natural-key collision.** `ux_lab_results_natural_key` is a partial unique index on `(lab_draw_id, biomarker_id) WHERE superseded_by IS NULL` (`migrations/0003_import_conflict_keys.sql` line 28). If one draw holds current results for both the orphan and the survivor — precisely the same-concept-duplicate scenario that motivates merge — re-pointing violates the index. The ADR must define the outcome (refuse? supersede the orphan's result as a duplicate? merge values?), because the DB will otherwise define it as an `IntegrityError` at runtime.

The model itself needs no change — merge decomposes into audited `update`/`delete`/`correct` operations the schema already enumerates. But per the decision-capture discriminator this is an architectural decision (touches an Accepted ADR's invariants): a design that exists only in the merge code would be a spec bug. Routing: new Proposed ADR, landed with or before the merge work item.

### 3.B The cohort-dimension range model: additive, but it earns its own ADR and a profile entity

- [ ] When the range-model PR is taken up (its trigger: first hormone-panel range), write the ADR — see also 4.B.

`framework_ranges` is keyed `(framework_id, biomarker_id, effective_date)`; real reference data needs sex/age/fasting dimensions, and the already-seeded hormone biomarkers (testosterone, estradiol, FSH, LH) make the trigger near. Adding nullable cohort columns is an **additive migration** — the table survives; no fundamental change. What makes it more than a drive-by: `ranges.resolve_ranges` deliberately fetches the top-2 ranked candidates and raises on a tie *because the schema currently guarantees a single winner* — cohort columns convert that guarantee into reader-supplied filtering, and no read path today knows the owner's cohort, which means a **profile entity that does not yet exist**. [open-questions.md](../open-questions.md) already stages the cluster correctly as one range-model PR; the point here is confirming it is Proposed-ADR-sized, not migration-sized.

### 3.C The enriched read path runs in autocommit — pull the snapshot fix into Phase 4, where its trigger fires

- [ ] Wrap enriched reads in a `BEGIN DEFERRED` transaction (one WAL snapshot) as part of Phase 4, rather than leaving the open-questions entry waiting.

`reads.list_lab_results` issues four independent queries (framework resolution, page query, catalog lookup, range resolution) on an autocommit connection (`isolation_level=None`), so each sees its own WAL snapshot; a catalog import committing mid-read can produce a page comparing a new-state range against an old-state `canonical_unit`/`molar_mass`. Correctly identified and deferred in open-questions.md with the trigger "a second concurrent writer (Phase 4's job/event surface)" — but Phase 4 is *next*: the trigger fires the moment imports become jobs. The fix is transaction discipline the write path already has, not architecture.

### 3.D Code polish (local detail, rule 6 — fix, no spec record, except where noted)

- [ ] `ConnectionPool` installs `set_trace_callback` counting every statement across all 8 worker threads under one global `Lock`, purely to feed the `db_query_count` metric — a hot-path global mutex for an observability counter. Per-thread counters summed on read (or `itertools.count`) remove it.
- [ ] The `draw_utc` shape backstop — the `GLOB` CHECK already spelled out in open-questions.md — is worth landing with Phase 3.5 while that area is open: the CLI validates format (`_validate_draw_utc`) but `POST /v1/import` does not, and a malformed value mis-resolves `[:10]` lexical range resolution silently. One additive constraint converts a silent wrong-flag into an `IntegrityError`. (Routing: data-model.md / owning ADR when landed.)

### 3.E AI surface: the MCP tool output contract is current — one forward nit

- [ ] When Phase 4 implements the contract, extend the instruction-shield to the structured triple's `value_text`.

The [api-reference.md](../api-reference.md) contract is written against MCP revision 2025-11-25, uses `outputSchema`/`structuredContent`, correctly states that `readOnlyHint`/`destructiveHint` annotations are not a security boundary, and specifies a per-response random delimiter for instruction-shielding — ahead of typical practice, nothing stale. The nit: the shield covers free-text bodies/notes, but `value_text` (qualitative results) is also attacker-influenceable text and should receive the same data-block framing when serialized into `structuredContent`.

### 3.F Frameworks: no changes recommended

Reaffirming 07-07's 3.D against the implemented code: FastAPI + typer + `sqlcipher3-wheels` + keyring + argon2-cffi + `ucumvert`/`pint` (correctly confined to `units.py` behind the ADR-0031 surface, with molar-mass context failing loud rather than falling back) all stand. The no-ORM decision has now paid for itself in the audit/pragma discipline of the repository layer. Nothing in the Python 3.14 baseline is under-used in a way worth a finding.

---

## 4. Governance, documentation hygiene, and the development plan

### 4.A The development plan still makes sense — but re-slice Phase 4 before starting it

- [ ] Split Phase 4 into 4a (launcher + event bus + jobs; milestone: imports run as jobs emitting `data.*`/`job.*` over SSE) and 4b (MCP + exports + ADR-0014 disposition; milestone: AI client queries real data with full fidelity).

Phases 1–3 were each a single vertical; Phase 4 as written bundles five-to-six first-of-their-kind subsystems — the launcher (first multi-process orchestration), event bus + SSE (first async surface), jobs (first long-running-work surface), the full MCP server and tool-output contract, export endpoints, and the ADR-0014 disposition — behind one milestone. Each has its own security surface (2.4 above is one). The split respects the immutable phase-number bindings (both halves remain "Phase 4 = the event-bus/MCP surface" for ADR-0052/0053 reference purposes) and puts the ADR-0014 disposition in 4a with the SSE work it depends on, rather than stranded at the end of a mega-phase. Otherwise the plan verifies cleanly: the Phase 3.5 in/out-of-scope boundary is sound; its survey instrument ([lab-data-dimensions.md](../lab-data-dimensions.md)) exists and matches what the plan says it produces, with the validated/known-needed distinction intact; the decimal-numbering rationale is correct and the constraint real; the parallel track matches the still-open Data Ingestion entries.

### 4.B The decision-gates table is missing its largest accumulated gate

- [ ] Add a row: "Range-model expressiveness (cohort dimensions, exclusive bounds, `physical_min`, non-numeric comparison) — trigger: first hormone-panel range / first visibly-wrong flag — owner: database owner."

The table lists ADR-0014, ADR-0016/0017, and per-source investigations — all still real. But real-data entry has grown a coherent decision cluster that open-questions.md and lab-data-dimensions.md both say should land as one range-model PR, whose trigger is near (3.B), and it is discoverable only across five open-questions entries. The plan treats it correctly as trigger-deferred and out of Phase 3.5 scope; it just isn't visible in the one table meant to summarize gates.

### 4.C Guard the Phase 3.5 catalog-seed bullet against becoming the scope-creep seam

- [ ] Tie the "fuller built-in catalog" bullet explicitly to the correction work: seed the concepts the source survey found duplicated or aliased, so the seed exercises the merge/alias tooling — rather than an open-ended "more useful" seed.

The bullet already defends itself well (names/categories risk-free; units earn UCUM validation; range values stay conservative), but it is the one Phase 3.5 item whose discriminator is growth, not correction — the phase's own "not a second Phase 3" rule needs it bounded by intent, not just by prudence.

### 4.D Governance findings

- [ ] [ADR-0060](../adr/0060-cli-catalog-add-commands.md) and [ADR-0061](../adr/0061-markdown-link-check-gate.md) are Proposed with merged implementations — not a violation (CLAUDE.md permits landing Proposed with the implementing PR), but they are the current lagging pair; flip at the next lock-in sweep, index updated in the same change.
- [ ] The 07-07 §4.B docs-consistency carryover's precondition has been met without the item being revisited: enforcement code now exists (`scripts/check_adr_index.py`, `scripts/check_spec_links.py`), and `check_spec_links.py` has tests while **`check_adr_index.py` has none**. Write the missing test; then the carryover can finally close.
- Verified clean, for the record: the ADR index matches all 61 files' `## Status` fields exactly, including the partial-status cases (0001, 0004) and all 13 Proposed/stub entries — the `check_adr_index.py` gate is visibly working; api-reference.md corresponds exactly to the implemented routes, scopes, and TBD markers, and the nine scope names match `tokens.py` verbatim; open-questions.md correctly struck Phase-3-resolved entries and re-owned the catalog-editing entry to Phase 3.5; recent merged PRs all carry `Decisions:` sections, including a correct "none" (#42).

### 4.E Small fixes list (mechanical)

- [ ] data-model.md Migration 0005 section + seed documentation (1.A)
- [ ] security.md defense-in-depth bullet qualifier (1.B)
- [ ] glossary framework examples (1.C)
- [ ] manual-entry-quickstart owning-ADR list += ADR-0060 (1.C)
- [ ] ADR-0057 back-link to ADR-0060 (1.C)
- [ ] open-questions.md: MCP-verification pointer (2.4), liveness-limiter line on the non-loopback gate list (2.5)

---

## Strongest parts (for the record)

The headline is that the specs turned out to be *true*: this is the first review able to test five ADR-heavy phases of specification against running code, and the correspondence is unusually tight — the ADR index matches all 61 files, api-reference matches the implemented surface exactly, and both prior reviews' resolution notes verify against reality for the third consecutive cycle. Specific standouts: **the auth surface is enforced structurally, not by convention** — every route must declare a scope or `public` at assembly or the app refuses to build, and the rate limiter's hardest design point (no clear-on-success, because the advisory name is attacker-suppliable) is both implemented and *reasoned in the docstring*. **The rekey durability ordering** (stage sidecar → rekey → atomic install, with keyring-failure paths that can never discard the only copy of a live key) leaves no crash window in which the database is encrypted under parameters no file records. **The ranges engine** (`ranges.py`) does real interval arithmetic with per-bound open/closed semantics and a relative-tolerance boundary-coincidence test that specifically kills the "same physical quantity verdicted differently by its reporting unit" bug — and raises loudly on a resolution tie instead of silently picking a winner. **The log-canary discipline is real**: the manifest derives from parsed fixture records, the scan covers test-failure output, and no implemented call site logs a value, token, or key. And **Phase 3.5 itself is the plan's anti-speculative-layering principle demonstrably working** — a horizontal slice argued from a demonstrated gap, bounded against becoming a second Phase 3, with a survey instrument that distinguishes corpus-validated from known-needed dimensions instead of guessing.

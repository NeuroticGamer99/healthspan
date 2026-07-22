# Work Plan — Architecture Review 2026-07-19

Execution ordering for [architecture-review-2026-07-19.md](architecture-review-2026-07-19.md), sorted by reasoning difficulty: open architecture decisions first (work these with a high-reasoning model — Fable, high thinking), bounded design work second (Fable/Opus, normal effort), mechanical edits last (Sonnet-level). Item numbers refer to the review document.

**Global sequencing rule:** this review has no pending acceptance flip to race (the 07-06 worklist's gate); the clock here is **phase kickoffs**. Everything that shapes Phase 3.5 lands before its first work item starts: T2.1 (INV-7/INV-8) before T1.1 is *written*, so the merge ADR must cite INV-7; T1.1 before (or with) the merge work item; T3.4's `draw_utc` CHECK rides the first Phase 3.5 migration. Everything that shapes Phase 4 lands before Phase 4 planning: T2.4 (the development-plan pass) before the phase is sliced into work items, and T3.1's two inheritance paragraphs (MCP verification, `value_text` shielding) before the MCP work item is scoped. Nothing in this worklist edits a Proposed ADR ahead of a flip — the only flip in play (T3.5, ADR-0060/0061) is itself deliberately last.

## PR mapping

One PR per Tier-1/2 item (each is a self-contained session), batched PRs for Tier 3, executed as sequential sessions. Each PR ticks its own checkboxes in this worklist in the same PR, so `main` always shows accurate remediation state. R-numbers are shorthand for this table only — PRs and commits reference the T-numbers.

| PR | Worklist items | Contents |
|----|----------------|----------|
| R1 | — | This review + worklist + `reviews/README.md` index rows (pulled forward from T3.2) |
| R2 | T2.1 | security.md INV-7/INV-8 — **first, gates R3** |
| R3 | T1.1 | Catalog-correction Proposed ADR (spec-only, before the merge work item) |
| R4 | T2.4 | Development-plan pass (Phase 4 → 4a/4b, gates-table row, seed bullet) |
| R5 | T2.2 | Permission checks: code + ADR-0049 extension ADR + testing-strategy targets |
| R6 | T2.3 | 422 handler: code + api-reference error conventions |
| R7 | T2.5 | publish.yml `ci-ok` gate + ADR-0045 extension ADR |
| R8 | T3.1 + T3.2 | Spec/doc mechanical batch |
| R9 | T3.3 | Code-polish batch (pool counter, `check_adr_index.py` test, liveness sweep) |
| — | T3.4 | Not a standalone PR — rides the first Phase 3.5 migration |
| R10 | T3.5 | ADR-0060/0061 flip sweep — last, after R3's outcome is known |

Deliberate calls: R3 is a standalone spec-only PR (the review allows "with or before" the merge work item; *before* is better — it shapes how Phase 3.5 is sliced, and a focused ADR PR reviews better than an ADR buried in an implementation diff). R5 and R7 stay separate despite both being small — their extension ADRs are owned by different Accepted ADRs (0049 vs 0045) and don't batch. Hard ordering is only R2 → R3, plus R4 before Phase 3.5's catalog work item and Phase 4 planning; R4–R9 are otherwise independent.

---

## Tier 1 — Fable, high thinking: open architecture decisions

### T1.1 — Catalog-correction ADR: merge vs. the audit model (review 3.A) ⚠️ hardest, most consequential

- [ ] Write the Phase 3.5 catalog-correction Proposed ADR, resolving both collisions before the merge work item exists in code.

Why hard: it reopens the semantics of an Accepted ADR's core contract without being allowed to edit it. Must reason through (1) **supersede-vs-in-place routing** for an identity fix — ADR-0027 has exactly two categories (value corrections supersede; designated metadata columns update in place) and re-pointing `biomarker_id` is deliberately neither: supersession preserves the mistaken-identity history but bloats chains on a bulk merge, in-place declares `biomarker_id` a repairable metadata column and rewrites what the row *meant*; and (2) the **natural-key collision** — when one draw holds current results for both orphan and survivor (the very duplicate scenario motivating merge), `ux_lab_results_natural_key` forbids the re-point, and the ADR must define the outcome (refuse / supersede the orphan's result as a duplicate / merge values) or the DB defines it as a runtime `IntegrityError`. Also in scope: what merge writes to `audit_log` (per-row images vs. a batch shape — the T1.1-2026-07-06 precedent), delete's unreferenced-rows-only guarantee, and alias handling for the merged-away name.
Sequencing: **after T2.1** (must cite INV-7); **before the Phase 3.5 merge/remove work item**. Interacts with T2.4's seed-bullet guard (the seed should exercise this tooling).

---

## Tier 2 — Fable/Opus, normal effort: bounded design work

Real design content, but the review already narrowed the option space. Each is a self-contained session.

### T2.1 — INV-7 and INV-8 in security.md's invariant table (review 2.2)

- [x] Add INV-7 (audit surfaces are append-only: `audit_log`, `auth_audit`; corrections supersede, never rewrite; no platform operation deletes or mutates an audit row) and INV-8 (no server-side credential plaintext; plaintexts exist only at issuance and in holder-local storage), with established-by citations to ADR-0027/0050 and ADR-0026.

Small in lines but load-bearing in wording — invariant text is what every future ADR must argue against, so the phrasing pass deserves care (e.g. INV-7 must not accidentally forbid ADR-0038's backup pruning or legitimate supersession chains). Both properties are already decided in Accepted ADRs: direct edit to security.md, no new ADR. **Do this first — it gates T1.1.**

### T2.2 — Startup permission verification for the database, sidecar, and passphrase file (review 2.1)

- [ ] Decide the defaults (warn vs. refuse per file class; Windows-check depth via `fsperm.py`'s explicit-principal enumeration), implement the checks at `build_runtime`/`exclusive_database_access` time, and add the testing-strategy targets.

The design space is small but real: a warning that scrolls past on service start protects nobody, while a hard refusal on an inherited-ACL quirk locks the owner out of their own data — the warn-vs-refuse line per file (database, `.keyparams` sidecar, `passphrase_file`, config) has to be argued. Decisions are implementation defaults owned by ADR-0049 (Accepted) → record in the next extension ADR, the ADR-0050/0051 batching pattern.

### T2.3 — 422 input-echo posture (review 2.3)

- [ ] Install a validation-error handler returning `loc`/`msg`/`type` only (no `input`), and record the input-echo posture in api-reference.md's error-format conventions in the same PR.

Bounded: the review recommends the answer (strip `input`), the remaining design is confirming machine-fixability without the echo (the CLI's retry UX consumes these errors) and stating the posture as a contract so Phase 4's MCP error path inherits it. Decision-capture rule 2 — api-reference.md, same PR.

### T2.4 — Development-plan pass: re-slice Phase 4, surface the range-model gate, bound the seed bullet (reviews 4.A + 4.B + 4.C, one pass)

- [ ] Split Phase 4 into 4a (launcher + event bus + jobs; milestone: imports run as jobs emitting `data.*`/`job.*` over SSE) and 4b (MCP + exports + ADR-0014 disposition; milestone: AI client queries real data with full fidelity), keeping both halves inside the immutable "Phase 4" binding.
- [ ] Add the range-model expressiveness row to the decision-gates table (trigger: first hormone-panel range / first visibly-wrong flag; owner: database owner).
- [ ] Reword the Phase 3.5 catalog-seed bullet to tie the seed to the correction work (seed the concepts the source survey found duplicated or aliased, exercising the merge/alias tooling).

One document, three edits, one session. The genuine design content is the 4a/4b milestone wording — each half must end at a crisp, testable milestone per the plan's own rules, and 4a's milestone is the one that makes the ADR-0014 disposition land naturally with the SSE work. **Land before Phase 4 planning; the seed-bullet edit before Phase 3.5's catalog work item.**

### T2.5 — Release gate: `ci-ok` verification in publish.yml (review 2.5, first bullet)

- [ ] Add a pre-publish step verifying the released commit carries a successful `ci-ok` check (and consider a tag ruleset), recorded as an extension of ADR-0045 — batchable with the next CI decisions ADR.

Bounded: the mechanism is named (check-runs lookup on the release commit before `uv publish`); the remaining thought is failure UX (a release cut from a never-gated commit should fail loudly with a pointer, not silently skip) and whether the tag ruleset is worth its ongoing friction. The extension-ADR cost is why this is Tier 2 rather than repo hygiene — batch it with T2.2's extension ADR only if the owning ADRs align (they don't: 0045 vs 0049 — keep separate, both can ride one PR each).

---

## Tier 3 — Sonnet-level: specified fixes and mechanical edits

The thinking is already done in the review; these are careful transcription. Safe to batch several per session.

### T3.1 — Spec precision edits with provided wording

- [ ] security.md defense-in-depth bullet: qualify host-header validation and CORS as gated future controls (review 1.B).
- [ ] MCP client-credential verification requirement — one paragraph (failed-verification throttling, AuthFailureRateLimiter shape, single bucket; metadata-only failure log/counter) staged where the Phase-4 MCP work item will inherit it, plus the open-questions.md (Operations) pointer (review 2.4).
- [ ] api-reference.md tool output contract: extend the instruction-shield/data-block framing to the structured triple's `value_text` (review 3.E).
- [ ] open-questions.md "Non-loopback binding hardening": add the liveness-limiter address-table eviction line to the gated-controls list (review 2.5, second bullet).

### T3.2 — Doc staleness batch

- [ ] data-model.md: add the `## Migration 0005` section (0001–0004 pattern) and document the 0004/0005 reference-data seeds, including 0005's structural dependency on the 0004 biomarker seed (review 1.A — the largest item in this batch, but pure transcription of shipped structure).
- [ ] glossary.md: framework examples — mark "Lab Standard"/"Attia" as illustrative or cite the seeded frameworks (review 1.C).
- [ ] manual-entry-quickstart.md: add ADR-0060 to the owning-ADR list (review 1.C).
- [ ] ADR-0057: navigation back-link to ADR-0060 (permitted in-place edit on an Accepted ADR) (review 1.C).
- [x] reviews/README.md: index rows for the 2026-07-19 review and this worklist (housekeeping surfaced by the review's own delivery). *Resolved: pulled forward into R1, the PR that landed the review itself.*

### T3.3 — Code polish (rule 6 — no spec record)

- [ ] `ConnectionPool`: replace the global-`Lock` trace-callback statement counter with per-thread counters summed on read (or `itertools.count`) (review 3.D).
- [ ] `scripts/check_adr_index.py`: write the missing test, mirroring `test_check_spec_links.py` — this finally closes the 07-07 §4.B docs-consistency carryover (review 4.D).
- [ ] `LivenessRateLimiter`: opportunistic idle sweep mirroring `AuthFailureRateLimiter._maybe_sweep` (review 2.5 — optional now; required before any non-loopback bind via T3.1's gate-list line).

### T3.4 — `draw_utc` shape backstop (review 3.D) — rides the first Phase 3.5 migration

- [ ] Add the `GLOB` shape-CHECK on `draw_utc` (per the open-questions.md sketch) to whatever migration Phase 3.5 ships first, closing the API-side gap the CLI's `_validate_draw_utc` already covers; record in data-model.md (and the migration's owning ADR) in the same PR.

Mechanical, but deliberately scheduled: it needs a migration to ride, and Phase 3.5 provides one — don't mint a migration for a CHECK alone.

### T3.5 — Governance close-out (deliberately last)

- [ ] Flip ADR-0060 and ADR-0061 → Accepted at the next lock-in sweep, index updated in the same change (review 4.D). Last for the usual reason: while Proposed they remain cheaply editable if any Tier 1/2 outcome above touches them (T1.1 is likely to lean on ADR-0060's catalog-command surface).

---

## Parked / phase-gated (not schedulable yet)

- **3.B — Cohort-dimension range model** (+ exclusive bounds, `physical_min`, non-numeric comparison — the one-PR cluster): trigger is the first hormone-panel range entry / first visibly-wrong flag. When it fires, this is **Tier-1 difficulty**: a new Proposed ADR plus a profile entity that does not yet exist, and the `resolve_ranges` single-winner guarantee converts to reader-supplied cohort filtering. T2.4's gates-table row is the tracking device; do not pull it forward.
- **3.C — Enriched-read snapshot transaction** (`BEGIN DEFERRED` around the four-query read path): scheduled as an early Phase 4 (4a) work item — its trigger, the second concurrent writer, arrives with imports-as-jobs. One-line discipline fix, not design.
- **2.4 / 3.E implementation halves** (MCP-side limiter + failure log; `value_text` shielding in `structuredContent`): Phase 4 (4b) MCP work item, inheriting the requirements staged by T3.1.
- **Carryovers, unchanged:** 07-07 §4.B's 1.G third bullet and 1.K ADR-0008 note (soft); 3.F CGM aggregation (trigger: Phase 7 CGM importer, decided with the 07-06 T1.1 audit-granularity outcome in hand).

## Dependency summary

```text
T2.1 (INV-7/8) ──→ T1.1 (merge ADR) ──→ Phase 3.5 merge/remove WI
T2.4 (dev-plan pass: seed bullet) ──→ Phase 3.5 catalog WI
T2.4 (dev-plan pass: 4a/4b) ──→ Phase 4 planning
T3.1 (MCP paragraphs) ──→ Phase 4 (4b) MCP WI ──→ parked 2.4/3.E implementation
T3.4 (draw_utc CHECK) ──→ rides first Phase 3.5 migration
T1.1 outcome ──→ T3.5 (0060/0061 flip — keep 0060 editable until then)
Phase 4 (4a) ──→ parked 3.C (snapshot transaction)
```

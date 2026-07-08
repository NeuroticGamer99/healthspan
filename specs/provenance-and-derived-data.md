# Provenance and Derived Data

How the platform keeps three kinds of content — records of fact, interpretation of those records, and values computed from them — permanently distinguishable, no matter who or what authored each.

This is a design document in the sense of [README.md](README.md): it states the coherent idea once and synthesizes across the mechanisms that implement it. The binding decisions live in ADRs ([ADR-0043](adr/0043-ai-authored-analyses-and-annotate-scope.md), [ADR-0044](adr/0044-derived-data-points.md), and the earlier ADRs each section cites); the schema details live in [data-model.md](data-model.md).

---

## The invariant

> Nothing interpretive or derived can ever masquerade as authoritative source data — not in the schema, not in query results, not in what an MCP client sees.

This is INV-6 in [security.md](security.md). The platform's value rests on the evidentiary quality of its source data: a lab result is trustworthy because nothing synthesized can sit next to it wearing the same clothes. Contamination is silent and, once mixed, irreversible — you cannot un-blend a computed value from a table of measurements after the fact. So the distinction is enforced *structurally* (by table and by credential) wherever a machine writes, and *normatively* (by giving every content kind a sanctioned home) where only the owner's discipline can enforce it.

---

## Two axes

Every stored datum is positioned on two independent axes. The platform already used both locally — dose history's `authority_type`, clinical documents' `author_type` — before they were named as axes; this document generalizes them.

**Content class** — what the row *asserts*. Structural: determined by which table the row lives in, never by a flag that could be miswritten.

| Class | Asserts | Examples |
|---|---|---|
| **Source** | Something happened, was measured, was said, or was observed at a point in time | Lab results, CGM readings, dose changes, clinical events, visit notes (by clinician *or* patient), subjective observations |
| **Interpretation** | A conclusion *about* other stored data | The owner's analyses, AI-authored analyses |
| **Derivation** | Structured values *computed from* other stored data | ADR-0021 aggregates (internal caches), future derived series ([ADR-0044](adr/0044-derived-data-points.md)) |

**Authorship** — who produced the content. Column-level, per row where a table admits multiple authors; stamped from token identity when written through the API ([ADR-0043](adr/0043-ai-authored-analyses-and-annotate-scope.md)), never caller-supplied. Values in use: `clinician`, `patient`/`self`, device/lab (implicit in import provenance), `ai`.

The axes are orthogonal, and the combinations matter. A patient-authored visit note is *source* (a contemporaneous record of what was discussed — lower authority than the clinician's note, which is what `author_type` expresses, but still a record of fact, not a synthesis). A journal entry saying "new ache since starting X — I suspect the medicine" is *source* too: the fact that the owner suspected this on this date is itself a datum, and its narrative form cannot be confused with a measurement. An AI-authored analysis is *interpretation* regardless of how confident it sounds. A computed HOMA-IR point is *derivation* even though it is numeric and plottable like a lab value — which is exactly why it needs structural separation.

**Boundary nuance — imported third-party computations are source.** Levels Zone scores are computed by Levels, but from this platform's perspective they are source data: the platform records the fact that *Levels reported this score*, read-only, not recomputable ([data-model.md](data-model.md), Metabolic Context). The `derivation` class covers only values computed *from data already in this database* — by the platform, the owner, or an AI client. Provenance here describes this database's epistemics, not the universe's.

---

## Where each content type lives

| Content | Class | Authorship | Mechanism |
|---|---|---|---|
| Lab results, CGM, body composition, wearable aggregates | Source | Lab / device (via import provenance) | [data-model.md](data-model.md); [ADR-0027](adr/0027-audit-trail-and-corrections.md) `import_batch_id` |
| Clinical events, interventions, dose history | Source | Patient-entered; per-row `authority_type` on dose changes | [data-model.md](data-model.md) |
| Clinical documents & visit notes | Source | `author_type`: `clinician` \| `patient` | [data-model.md](data-model.md); [ADR-0034](adr/0034-clinical-document-storage.md), [ADR-0041](adr/0041-clinical-document-fts.md) |
| Subjective observations (journal) | Source | `patient` | [data-model.md](data-model.md), Subjective Observations |
| Analyses & interpretations | Interpretation | `author_type`: `self` \| `ai`, stamped | [data-model.md](data-model.md), Analyses; [ADR-0043](adr/0043-ai-authored-analyses-and-annotate-scope.md) |
| Time-series aggregates | Derivation (internal cache, not user-facing) | Platform | [ADR-0021](adr/0021-time-series-aggregation.md), [ADR-0027](adr/0027-audit-trail-and-corrections.md) |
| Derived data points / series | Derivation | Owner, AI, or plugin | [ADR-0044](adr/0044-derived-data-points.md) — principle decided, schema deferred; interim home is the structured attachment on analyses |

---

## Write paths and enforcement

- **Interpretation-class tables are gated by the `annotate` scope**, not `write` ([ADR-0043](adr/0043-ai-authored-analyses-and-annotate-scope.md) extending [ADR-0026](adr/0026-named-scoped-tokens.md)). An AI client granted authorship holds `read annotate` — it can author and manage analyses and touch nothing else. The default `mcp` token stays read-only; granting authorship remains a deliberate, named token issuance.
- **Authorship is stamped, never claimed.** Each token carries an `authorship` attribute fixed at issuance; the Core Service sets `author_type` from it and rejects payloads that try to supply it — the same rule that stamps event `source` from token identity (ADR-0026).
- **Source-data writes stay where they were**: `write`/`import` scoped, validated at the REST boundary, audited per [ADR-0027](adr/0027-audit-trail-and-corrections.md). No AI-held credential carries them by default.
- **The owner's own discipline is the residual.** Nothing technically stops the owner typing a computed value in as a manual lab result. The control is normative: every content kind has a sanctioned home, so the disguise has no incentive ([ADR-0044](adr/0044-derived-data-points.md) states this honestly, in the same spirit as [ADR-0033](adr/0033-plaintext-artifact-disposal.md)'s best-effort framing).

---

## Presentation rule

Every read surface — REST responses and MCP tool output — carries content class and authorship alongside the content, so a consumer can always tell measurement from interpretation from derivation.

For MCP this closes a loop specific to AI clients: without the rule, a model can retrieve its *own prior analysis* and weight it as if it were data — a self-reinforcement loop where yesterday's hypothesis hardens into today's premise. With attribution surfaced (and free text delimited and instruction-shielded per the MCP output contract, [api-reference.md](api-reference.md)), an AI client can — and tool descriptions instruct it to — treat prior interpretation as interpretation, whoever authored it.

---

## What is deliberately not decided here

- **Subjective-observation vocabulary** — the journal starts freeform; structured tags/scales are an open question ([open-questions.md](open-questions.md), Schema).
- **Derived-series schema** — [ADR-0044](adr/0044-derived-data-points.md) fixes the principle and defers the table design, with an explicit trigger and recorded sub-questions ([open-questions.md](open-questions.md), Schema).
- **Embedding/semantic search over interpretive content** — same future plugin layer as for clinical documents ([ADR-0041](adr/0041-clinical-document-fts.md)), inside the encryption boundary.

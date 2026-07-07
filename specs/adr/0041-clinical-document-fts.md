# ADR-0041: Clinical Document Full-Text Search

## Status
Proposed

## Context and Problem Statement
Clinical documents and visit notes ([data-model.md](../data-model.md)) are among the highest-value data types for AI clients, and the `body` free-text column is their primary queryable surface — an AI client answers "what did my cardiologist say about my LDL trajectory?" by searching clinician narrative, not structured values. [open-questions.md](../open-questions.md) left the search strategy undecided among three options: SQLite FTS5, application-level scan in the Core Service, or embedding-based semantic search via a plugin. The [architecture review](../architecture-review-2026-07-06.md) (item 3.D) recommended resolving it now — and deciding it *with the documents table*, so the index and its triggers ship in migration 0001 rather than being retrofitted onto a populated table later.

[ADR-0034](0034-clinical-document-storage.md) decided where the *original binary* lives (content-addressed BLOBs inside the encrypted database) and explicitly disclaimed the search question: FTS indexes the extracted `body` text, not the stored binary. This ADR decides that separate concern.

## Decision Drivers
- The queryable surface is clinician *narrative*; the consumer is an AI client asking natural-language questions, where recall matters more than exact-string precision
- Every at-rest guarantee is scoped to the SQLCipher boundary ([ADR-0013](0013-encryption-at-rest.md)); a search index that stores note content outside that boundary would silently void the trust model, exactly like the plaintext-document-directory defect [ADR-0034](0034-clinical-document-storage.md) closed
- Clinical documents are audited, correctable data ([ADR-0027](0027-audit-trail-and-corrections.md)): rows carry `superseded_by`, so the index must stay coherent as bodies are corrected without teaching the correction machinery about search
- App-level scan becomes a dead end the moment a query spans years of notes ("summarize all provider guidance on my insulin resistance")
- The index should ship in migration 0001 with the documents table — retrofitting FTS onto a populated table is avoidable work

## Considered Options
1. **SQLite FTS5 external-content virtual table over `body`** (chosen)
2. **Application-level search** in the Core Service (scan `body` per query)
3. **Embedding-based semantic search** via a plugin, as the primary mechanism

## Decision Outcome
Chosen: **option 1 — an FTS5 external-content virtual table indexing the `body` column.**

```sql
CREATE VIRTUAL TABLE clinical_documents_fts USING fts5(
    body,
    content='clinical_documents',
    content_rowid='id',
    tokenize='porter unicode61 remove_diacritics 2'
);
```

- **External-content mode** (`content=`/`content_rowid=`) means FTS5 stores only the inverted index, not a second copy of the text — the `body` already lives in `clinical_documents`. This halves the storage cost relative to a standalone FTS table and keeps a single source of truth for the note text.

- **Encryption is inherited, not re-engineered.** FTS5's backing shadow tables (`clinical_documents_fts_data`, `_idx`, `_docsize`, `_config`) are ordinary SQLite tables, so SQLCipher encrypts their pages exactly like every other page in the database. The search index sits inside the same boundary as the data it indexes, with zero additional key management, backup, or rotation machinery — the property that makes FTS5 correct here where an external search engine would be a second plaintext surface to secure.

- **Sync via triggers (external-content requires them).** FTS5 does not observe the content table automatically, so migration 0001 ships three triggers alongside the table:

  ```sql
  CREATE TRIGGER clinical_documents_fts_ai AFTER INSERT ON clinical_documents BEGIN
      INSERT INTO clinical_documents_fts(rowid, body) VALUES (new.id, new.body);
  END;
  CREATE TRIGGER clinical_documents_fts_ad AFTER DELETE ON clinical_documents BEGIN
      INSERT INTO clinical_documents_fts(clinical_documents_fts, rowid, body)
          VALUES ('delete', old.id, old.body);
  END;
  CREATE TRIGGER clinical_documents_fts_au AFTER UPDATE ON clinical_documents BEGIN
      INSERT INTO clinical_documents_fts(clinical_documents_fts, rowid, body)
          VALUES ('delete', old.id, old.body);
      INSERT INTO clinical_documents_fts(rowid, body) VALUES (new.id, new.body);
  END;
  ```

  The `'delete'` command must be given the *old* column values — this is a fixed FTS5 external-content idiom, not a design choice. Parser re-extraction (which [ADR-0034](0034-clinical-document-storage.md) anticipates when extractors improve) and any index corruption are handled by `INSERT INTO clinical_documents_fts(clinical_documents_fts) VALUES('rebuild');`, which regenerates the index from the content table — so the index is never a retrofit.

- **Corrections: index every row, filter to current at query time.** Because `clinical_documents` carries `superseded_by` ([ADR-0027](0027-audit-trail-and-corrections.md)), a value correction inserts a new row (new `id`) and points the original at it — both bodies now exist in the table, and therefore both are in the index. Rather than teach the triggers supersession logic, the index deliberately covers **all** rows and current-state is applied at query time:

  ```sql
  SELECT c.id, snippet(clinical_documents_fts, 0, '[', ']', '…', 12)
  FROM clinical_documents_fts f
  JOIN clinical_documents c ON c.id = f.rowid
  WHERE clinical_documents_fts MATCH :query
    AND c.superseded_by IS NULL
  ORDER BY rank;
  ```

  This keeps the triggers purely mechanical (index by rowid, no join to interpret), leaves superseded bodies searchable if a history view ever wants them, and composes with the `clinical_documents_current` view the correction model already defines. The designated metadata-repair carve-out ([ADR-0027](0027-audit-trail-and-corrections.md)) never touches `body`, so it causes no index churn.

- **Tokenizer: `porter unicode61 remove_diacritics 2`.** `unicode61` gives Unicode-aware tokenization with accent folding; the `porter` wrapper adds English stemming so morphological variants collapse (`trajectory` ≡ `trajectories`, `diagnosed` ≡ `diagnosis` ≡ `diagnosing`). This favors **recall** — the right bias for an AI client that asks conceptual questions and cannot know which surface form a note used. Porter targets common English suffixes and passes non-dictionary tokens through largely intact, so clinical terms and drug names (`HbA1c`, `atorvastatin`, `LDL`) are essentially unaffected; the cost is that truly exact literal matching is unavailable (query terms are stemmed too), which almost never matters for narrative search. This default is **implementation-tunable, not load-bearing**: changing it is a small migration that redefines the virtual table and runs `'rebuild'` — sub-second at personal archive scale — not a redesign.

- **MCP surface.** FTS results feed the MCP search tools; the returned `body` text passes through the MCP tool output contract's delimited, instruction-shielded free-text rule ([api-reference.md](../api-reference.md), review 3.G), and row/pagination caps apply as to every tool. Binary originals remain unexposed by default ([ADR-0034](0034-clinical-document-storage.md)).

### Embeddings are a future plugin layer inside the boundary — not a replacement
Embedding-based semantic search is genuinely valuable and explicitly *not* rejected — it is a layer on top of FTS, not an alternative to it. It belongs to the plugin surface ([ADR-0010](0010-cli-plugin-model.md) `analysis`/`query` types), and its design is deferred to a future ADR. One constraint is fixed now: if such a plugin stores note content or PHI-derived embeddings, its vector store must live inside the SQLCipher boundary, for the same reason the FTS index does. FTS5 covers exact and stemmed lexical search today; semantic retrieval extends it later without displacing it.

### Positive Consequences
- Full-text search over clinician narrative with no dependency beyond standard SQLCipher (FTS5 is compiled into standard builds)
- The search index inherits encryption, backup, rotation, and disposal from the database — no second security surface
- The index ships in migration 0001 with the documents table; no retrofit onto a populated table
- The correction model and the search index stay decoupled — triggers are mechanical, current-filtering is a query-time join
- Semantic/embedding search remains an open, additive path rather than a foreclosed one

### Negative Consequences / Tradeoffs
- External-content mode requires maintenance triggers; they are mechanical but must be shipped and tested as part of the schema
- Porter stemming precludes exact literal matching on `body` (accepted — the consumer is natural-language AI search; the MCP layer can offer phrase queries where needed)
- The index covers superseded rows too, a small storage cost that buys mechanical triggers and optional history search

## Pros and Cons of the Options

### FTS5 external-content virtual table (chosen)
- Pro: encrypted-by-inheritance, zero new dependency, index not duplicated, ships with the table, scales to years of notes
- Con: maintenance triggers required; stemming trades exact-match for recall

### Application-level search
- Pro: no schema complexity; nothing to keep in sync
- Con: a full scan of every `body` per query — a dead end as narrative accumulates over years; re-implements in Python what SQLite already does in C

### Embedding-based semantic search as the primary mechanism
- Pro: most powerful for natural-language intent matching
- Con: adds a vector-store dependency that must itself be brought inside the encryption boundary; heavier to build and operate; best as an additive plugin layer over FTS, not the base lexical index

## Links
- Related: [ADR-0034](0034-clinical-document-storage.md) — original binary storage; deliberately disclaims the search question this ADR answers (FTS indexes extracted `body`, not the binary)
- Related: [ADR-0027](0027-audit-trail-and-corrections.md) — `superseded_by` correction model; the index covers all rows and filters to current at query time
- Related: [ADR-0013](0013-encryption-at-rest.md) — the SQLCipher boundary the FTS shadow tables inherit
- Related: [ADR-0035](0035-migration-execution-semantics.md) — migration 0001 ships the virtual table and triggers with the documents table
- Related: [ADR-0010](0010-cli-plugin-model.md) — embedding/semantic search as a future `analysis`/`query` plugin layer over FTS
- Related: [api-reference.md](../api-reference.md) — MCP tool output contract (review 3.G) governs returned `body` free text
- Related: [data-model.md](../data-model.md) — Clinical Documents & Visit Notes schema
- Resolves: [architecture review 2026-07-06](../architecture-review-2026-07-06.md), item 3.D — FTS5 external-content over `body`, encryption inheritance, trigger mechanics, embeddings as a future in-boundary plugin layer
- Resolves: [open-questions.md](../open-questions.md) — "Clinical documents — full-text search strategy"

# ADR-0043: AI-Authored Analyses and the `annotate` Scope

## Status
Proposed

## Context and Problem Statement
The platform stores interpretation alongside source data: the owner's own analyses and conclusions, reviewable longitudinally, and analysis written back by an AI client over MCP — tagged as such ([data-model.md](../data-model.md), Analyses & Interpretations; [provenance-and-derived-data.md](../provenance-and-derived-data.md)). Two requirements follow: AI-authored content must be storable *without* granting the AI any capability against source data (INV-6, [ADR-0044](0044-derived-data-points.md)), and attribution must be trustworthy — an analysis row must say whose voice it is in a way the writer cannot forge.

The existing credential model almost reaches this. [ADR-0026](0026-named-scoped-tokens.md) made the MCP token read-only by default and anticipated write capability as a deliberate second token ("e.g. `mcp-write`"). But its scope model is deliberately flat — scopes gate *capability classes*, with no resource-level granularity — and `write` is one class: create, correct, and delete on **all** individual records. A second MCP token carrying `write` would hand a prompt-injected AI client the ability to create lab results, alter dose history, and supersede clinical documents — precisely the contamination the provenance model exists to prevent, and a reopening of the blast-radius hole ADR-0026 closed, one legitimate-looking token issuance later.

Attribution has the same forgery problem in miniature: if `author_type` were caller-supplied, an AI client (or whatever is injected into it) could label its output as the owner's own conclusion, and a future AI session reading it back would weight it accordingly.

## Decision Drivers
- Storing AI analysis must grant zero capability against source-data tables — the containment must be structural, not behavioral (INV-6)
- Attribution must be stamped, never claimed — the same principle that stamps event `source` from token identity (ADR-0026)
- Granting the AI client authorship must remain a visible, deliberate token issuance; the read-only default stays intact
- Self- and AI-authored analyses belong in one table, distinguishable per row — longitudinal review ("what did I conclude last quarter, and what did the model conclude?") is one query, not a join across parallel tables
- The flat scope model is preserved: a new capability class earns a new scope ([ADR-0040](0040-health-endpoint-authentication.md) precedent with `monitor`); granularity inside a scope stays forbidden

## Considered Options
1. **Reuse `write`** — mint the anticipated `mcp-write` token and stamp authorship
2. **New `annotate` scope gating interpretation-class tables** (chosen)
3. **AI stays read-only permanently** — the owner pastes AI output in manually

## Decision Outcome
Chosen: **option 2 — a ninth flat scope, `annotate`.**

- **Grant.** `annotate` permits create, supersede, and delete on interpretation-class tables — currently exactly `analyses` — and nothing else. Routes for those tables declare `annotate` as their required scope; `write` does not grant them. Interpretive authorship is a different capability class from source-data mutation, exactly as `monitor` is a different class from `read` (ADR-0040): the separation exists so one can be granted without the other.

- **Authorship is a token attribute, stamped onto rows.** Every token carries an `authorship` attribute fixed at issuance — `self` by default (owner-held clients), `ai` for tokens minted for AI clients: `healthspan token create mcp-analyst --scopes read,annotate --authorship ai`. On any interpretive-table write, the Core Service sets the row's `author_type` from the token's attribute; a payload that attempts to supply `author_type` is rejected. This is the event-`source` stamping rule (ADR-0026) applied to data rows. The stamped token name is recorded as `author_token`; the client may additionally supply `tool_info` free text (model name/version), which is stored as a *claim*, clearly distinct from the stamped identity.

- **Author guard on mutation.** A token may supersede or delete only interpretive rows whose stamped `author_token` is its own — an AI client manages its own prior outputs and cannot rewrite the owner's conclusions (or another AI token's). Tokens whose authorship is `self` (owner-held: `gui`, `cli-admin`) may manage **all** interpretive rows — AI-authored rows are still the owner's data to prune. Supersession and deletion otherwise follow [ADR-0027](0027-audit-trail-and-corrections.md) unchanged (supersede for corrections, hard delete with full audit row).

- **Defaults.** `cli-admin` and `gui` gain `annotate` (they are the owner's own authoring surfaces). The `mcp` token is unchanged — read-only. Granting an AI client authorship is the deliberate act ADR-0026 already prescribed, now with the right shape: the recommended second token carries `read annotate`, never `write`.

- **Audit interplay.** ADR-0027's machinery is unchanged: every interpretive-table mutation writes its audit row in the same transaction, actor = token name. Attribution is thereby recorded twice with different jobs — the audit row says *who performed the API call* (operational record); the row's `author_type` says *whose voice the content is* (data, queryable forever). They normally agree.

- **MCP presentation.** Analyses returned by MCP tools carry `author_type` and `tool_info`, and tool descriptions instruct the client to treat interpretation as interpretation — closing the self-reinforcement loop where a model retrieves its own prior conclusion and weights it as data ([provenance-and-derived-data.md](../provenance-and-derived-data.md), Presentation rule). Analysis `body` text returned through MCP passes through the same delimited, instruction-shielded free-text rule as clinical-document text ([api-reference.md](../api-reference.md)) — an AI-writable surface that later feeds AI reads is a stored-prompt-injection vector, and the output contract is the existing control for exactly that shape.

### Positive Consequences
- AI-authored analysis is storable with zero capability against source data. The blast radius of a prompt-injected `read annotate` token is: spurious interpretive rows — clearly attributed, superseded or pruned in bulk by the owner, recoverable, and never confusable with measurements
- Attribution is trustworthy by construction; forging `author_type = self` is structurally impossible through the API
- ADR-0026's read-only MCP default and deliberate-issuance principle are preserved and sharpened
- One table for self and AI analyses keeps longitudinal comparison a single query

### Negative Consequences / Tradeoffs
- A ninth scope and an `authorship` attribute on token records — small, mechanical extensions to ADR-0026's tables
- The author guard adds a per-row ownership check on interpretive supersede/delete
- The manual-paste path (owner pastes AI text via the GUI) stamps `self` and loses machine attribution — accepted; the sanctioned path exists, and no design can stop retyping
- Stored AI text becomes a persistent surface that future AI sessions read — mitigated by attribution plus the MCP output contract's instruction shielding, and bounded by the annotate token's inability to touch anything else

## Pros and Cons of the Options

### Reuse `write` with provenance stamping
- Pro: no new scope; uses the token ADR-0026 already anticipated
- Con: hands a prompt-injectable client create/correct/delete over every record type — the exact hole ADR-0026 closed; distinguishing "AI writing analysis" from "AI writing lab results" inside one scope would require the resource-level granularity the flat model deliberately forbids

### New `annotate` scope (chosen)
- Pro: containment is structural; capability-class boundary matches the content-class boundary; follows the established ADR-0040 pattern for growing the scope set
- Con: ninth scope, authorship attribute, author guard — all mechanical

### AI stays read-only; owner pastes manually
- Pro: zero new attack surface
- Con: the AI text still ends up in the database — but stamped `self`, which is *worse* provenance than the thing avoided; loses the tagged, queryable AI-analysis record entirely

## Links
- Extends: [ADR-0026](0026-named-scoped-tokens.md) — adds the `annotate` scope, the token `authorship` attribute, and `annotate` in the `cli-admin`/`gui` defaults
- Related: [ADR-0044](0044-derived-data-points.md) — companion decision; together they establish INV-6
- Related: [ADR-0027](0027-audit-trail-and-corrections.md) — audit actor vs. row authorship; supersede/delete semantics under the author guard
- Related: [ADR-0040](0040-health-endpoint-authentication.md) — precedent for extending the scope set by ADR
- Related: [ADR-0029](0029-mcp-streamable-http.md) — the MCP transport the write path rides
- Related: [api-reference.md](../api-reference.md) — MCP output contract; instruction shielding of stored analysis text
- Related: [provenance-and-derived-data.md](../provenance-and-derived-data.md) — the content-class/authorship model this implements
- Related: [security.md](../security.md) — INV-6
- Related: [data-model.md](../data-model.md) — Analyses & Interpretations schema sketch

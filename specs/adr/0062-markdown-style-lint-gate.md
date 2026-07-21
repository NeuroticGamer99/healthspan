# ADR-0062: Markdown Style-Lint Gate (PyMarkdown), Tuned to the Corpus

## Status
Proposed

## Context and Problem Statement
[ADR-0061](0061-markdown-link-check-gate.md) added link-integrity checking to the docs-consistency CI job ([ADR-0045](0045-repository-workflow-and-ci-enforcement.md) §6), mechanizing one class of doc-review vigilance. It deliberately validates *links*, not *style*. The gap it left is style consistency: fenced code blocks with no language, missing blank lines around fences, stray double-blank lines, headings and lists formatted inconsistently across a corpus that is now ~100 markdown files and growing faster than the code.

The concrete pain is that AI reviewers — CodeRabbit on every PR — surface these `MDxxx` style findings by the handful, PR after PR. Each is individually trivial and collectively noise: it costs a review round to triage nits a linter would catch mechanically, and it trains the reviewer's signal down. This is exactly the "requirements that would otherwise depend on code-review vigilance" that [testing-strategy.md](../testing-strategy.md) (CI Gates) says a gate should absorb. A style linter is the natural second doc-linter on the docs-consistency job, the same shape [ADR-0061](0061-markdown-link-check-gate.md) established for link integrity.

The subtlety — and the reason this needs a decision rather than a default config — is that the corpus has strong, deliberate house conventions that a stock linter fights. A scouting run (PyMarkdown 0.9.39, front-matter extension on, scoped to tracked `specs/` + `README.md` + `CLAUDE.md`, excluding gitignored `specs/personal/`) produced **5,402 raw findings**, but they are not 5,402 defects:

| Rule | Hits | Auto-fixable | What it flags | In this corpus |
|------|-----:|:---:|------|------|
| MD013 line-length | 4,291 | no | Lines over 80 chars | Deliberate long-line, em-dash prose |
| MD022 blanks-around-headings | 620 | no | No blank line above/below a heading | House ADR style: a value sits tight under its `##` |
| MD032 blanks-around-lists | 441 | no | List not surrounded by blank lines | Same convention: a list starts directly under its heading |
| MD040 fenced-code-language | 28 | no | ` ``` ` fence with no language | Genuine — worth tagging |
| MD031 blanks-around-fences | 16 | yes | Fence not surrounded by blank lines | Genuine — mechanical |
| MD036 emphasis-as-heading | 3 | no | Bold line used as a pseudo-heading | Genuine — all 3 fixed in this PR (2 files) |
| MD012 no-multiple-blanks | 2 | yes | Consecutive blank lines | Genuine — mechanical |
| MD024 duplicate-heading | 1 | no | Two headings with identical text | `### Architecture` under two different `##` parents |

The first three rows — 5,352 of 5,402, **99.1% of all findings** — are house conventions, not defects. The remaining ~50 are genuine, small, and mostly mechanical.

## Decision Drivers
- The recurring cost is real and review-dependent: AI-reviewer style nits recur every PR, which is the precise thing a gate exists to remove ([testing-strategy.md](../testing-strategy.md), CI Gates)
- Any new tool must fit the established toolchain: gate tools are `uvx`-installed from PyPI with versions pinned in [`ci.yml`](../../.github/workflows/ci.yml)'s `env` block (ruff, pyright, pip-audit); [ADR-0061](0061-markdown-link-check-gate.md) set the "no runtime dependency" bar for doc gates
- **No Node dependency** — a hard constraint from the project owner; the Python/uv toolchain must not acquire a Node toolchain for a linter
- The config must bend to the corpus, not the reverse: a gate that flags 4,900 house-style lines is noise that gets disabled wholesale, defeating its purpose
- The point is to quiet the *AI reviewer*, which uses its own linter and reads its own config file — a gate that goes green while CodeRabbit keeps flagging the same rules solves nothing
- [ADR-0045](0045-repository-workflow-and-ci-enforcement.md) §4: a new gate joins the `ci-ok` fan-in and never touches branch protection, so the marginal cost is one tool plus one CI step

## Considered Options

### A. Linter: PyMarkdown (Python) vs. markdownlint-cli2 (Node)
The de-facto standard is **markdownlint-cli2**, a Node tool — and it is precisely what CodeRabbit runs internally, which is why review findings arrive as `MDxxx` codes. **PyMarkdown** (`pymarkdownlnt` on PyPI) is a pure-Python linter that implements the same `MDxxx` rule numbers and names.

The owner's constraint is no Node dependency, and this ADR records *why that constraint costs nothing here* rather than treating it as axiomatic:

- **Toolchain fit** (favors PyMarkdown, decisive): PyMarkdown installs via `uvx pymarkdownlnt@<pin>`, identical to how every other gate tool is provisioned. A Node linter would add a Node runtime, a second package ecosystem, and a second lockfile discipline to a repo that has none — a large standing cost for a style check.
- **Rule vocabulary** (neutral): both speak the same `MDxxx` numbers, so a rule tuned for one is understood by the other. This is what makes the dual-config approach below tractable.
- **Auto-fix capability** (the one genuine Node advantage — and it is moot here): markdownlint-cli2's `--fix` can repair MD022 and MD032; PyMarkdown's `fix` cannot (verified: PyMarkdown fixes MD012 and MD031 but not MD013/MD022/MD032/MD040). That advantage would matter *only if we intended to enforce MD022/MD032* — and the decision below is to **disable** exactly those rules as house style. Node's edge applies to the rules we are turning off, so it buys nothing.

**Chosen: PyMarkdown.** The owner's no-Node constraint aligns with toolchain fit, and the only capability Node adds is auto-fixing rules this ADR disables anyway.

### B. Which ADR does this extend — 0061 or 0045 §6?
Both are defensible. [ADR-0061](0061-markdown-link-check-gate.md) itself extends [ADR-0045](0045-repository-workflow-and-ci-enforcement.md) §6, so 0045 §6 is the ultimate root of the docs-consistency gate. This gate could be framed as a third, independent extension of 0045 §6.

**Chosen: extends [ADR-0061](0061-markdown-link-check-gate.md)** (and, through it, [ADR-0045](0045-repository-workflow-and-ci-enforcement.md) §6). The two are the same move — "add a doc-linter to the docs-consistency job that mechanizes review vigilance" — applied to two facets (links, then style). Grouping style under the link-check ADR keeps that pair legible as one build-out of the docs gate, rather than scattering three parallel extensions across 0045 §6. [ADR-0061](0061-markdown-link-check-gate.md) is still Proposed, so this extension link is added to it without governance ceremony; if 0061 is later Accepted, the extension stands.

### C. The house-style rules: disable, or enforce with a one-time normalization?
MD013 (4,291), MD022 (614), and MD032 (441) are the three high-volume rules, and all three encode intentional conventions: long-line prose, and content (a value or a list) placed tight under its `##` heading — the latter used uniformly across ~50 ADRs.

- **Enforce** would mean a one-time normalization of ~1,055 MD022/MD032 sites plus every long line. Crucially, PyMarkdown *cannot auto-fix* MD022/MD032, so this is ~1,055 hand-edits, the bulk of them **inside Accepted ADRs** — a governance cost under [CLAUDE.md](../../CLAUDE.md) rule 5 for zero rendering change (GitHub renders tight and spaced headings identically).
- **Disable** preserves the conventions, costs nothing, and leaves a residual gate of ~50 genuine findings.

**Chosen: disable MD013, MD022, MD032.** They are consistent house conventions, not defects; enforcing them would churn the corpus (and the immutable ADRs) to satisfy a linter's default aesthetic.

### D. Keeping CodeRabbit in agreement
CodeRabbit reads a **markdownlint** config (`.markdownlint.{json,yaml}` / `.markdownlint-cli2.*`), not PyMarkdown's config. If the disable decisions live only in PyMarkdown's config, our gate goes green while CodeRabbit keeps flagging MD013/MD022/MD032 — the exact noise this ADR exists to remove.

**Chosen: maintain both configs, one intent.** The PyMarkdown config (in `pyproject.toml`) drives our CI gate; a mirrored [`.markdownlint.yaml`](../../.markdownlint.yaml) at the repo root drives CodeRabbit. Both express the same rule decisions in the shared `MDxxx` vocabulary. They must be changed together; the discipline is recorded in-file (a prominent header comment in each pointing at the other) and here. How that "must" is enforced is option E.

### E. Enforcing the two-config sync: comment discipline vs. a mechanized drift-check
Option D leaves two files that must agree; this is how the agreement is kept. A **CI drift-check** — a small script that parses both configs and fails if their rule decisions diverge — would mechanize it in the spirit of [testing-strategy.md](../testing-strategy.md) (don't trust to vigilance what a gate can enforce). Against it: the two formats are not mechanically 1:1 (PyMarkdown's `plugins.mdNNN.enabled = false` vs. markdownlint's `MDNNN: false`, plus the deliberate front-matter asymmetry), so the checker must normalize both to a canonical rule→decision map before diffing. The cost is modest, not large: `tomllib` is stdlib and the docs-consistency job's existing [`check_adr_index.py`](../../scripts/check_adr_index.py) / [`check_spec_links.py`](../../scripts/check_spec_links.py) already do comparable parsing; the one genuine friction is that `.markdownlint.yaml` needs parsing without a stdlib YAML module (the mirror is a handful of rules, so a narrow hand-parse suffices).

**Chosen for now: comment discipline** (the header comment in each file), with the **drift-check deferred**, not rejected — it lands most naturally in the same follow-up PR that wires the gate (§5). The trigger is observable *before* any divergence ships: the two config files must move together, so the first PR that touches one without the other — visible in its diff at review time — is the signal to build the checker; until then reviewers watch for that pairing. (A trigger of "wait until the configs actually diverge in practice" would be self-defeating — a shipped divergence is the very CodeRabbit noise this ADR exists to remove.)

## Decision Outcome

### 1. Tool and provisioning
PyMarkdown (`pymarkdownlnt`), run via `uvx pymarkdownlnt@<version>` with the version pinned in [`ci.yml`](../../.github/workflows/ci.yml)'s `env` block alongside the other gate tools, per [testing-strategy.md](../testing-strategy.md) "Workflow provenance". The front-matter extension is enabled so YAML front matter is parsed as front matter, not misread as Setext headings.

### 2. Rule configuration (the tuned corpus profile)
Authoritative in `pyproject.toml` `[tool.pymarkdown]`, mirrored in [`.markdownlint.yaml`](../../.markdownlint.yaml):

- **Disabled** (house style): `MD013` line-length, `MD022` blanks-around-headings, `MD032` blanks-around-lists.
- **`MD024` duplicate-heading**: configured `siblings_only = true`. Repeated subsection names (`### Architecture`) under distinct `##` parents are legitimate structure, not duplication; this scopes the rule to true siblings and resolves the single corpus hit without a content edit.
- **All other rules: default-enabled**, including `MD040` (fenced-code-language), `MD031` (blanks-around-fences), `MD012` (no-multiple-blanks), and `MD036` (emphasis-as-heading).

### 3. Config-sync discipline
The two config files are a manual mirror. Each carries a header comment stating that any rule enable/disable/parameter change in one **must** be mirrored in the other, because our CI gate reads `pyproject.toml` and CodeRabbit reads `.markdownlint.yaml`. (The lone asymmetry: the front-matter extension is a PyMarkdown concept; markdownlint handles front matter natively, so it has no mirror line — noted in-file so the absence does not read as drift.)

### 4. Scope boundary
Same as [ADR-0061](0061-markdown-link-check-gate.md): tracked `*.md` under `specs/` plus root `README.md` and `CLAUDE.md`; `specs/personal/` is neither scanned (gitignored, absent in CI) nor otherwise touched. Tooling docs under `.claude/` are out of scope for now; widening is a config change if a need appears.

### 5. Phasing — what this PR does, and what it defers
This decision is landed in two steps to separate the low-risk configuration from the corpus cleanup:

- **This PR**: this ADR (Proposed); both config files with the sync discipline; the three MD036 hits (across two files) fixed ([ADR-0044](0044-derived-data-points.md) option labels 1–2; [data-model.md](../data-model.md) HealthKit subsection); the MD024 hit resolved via `siblings_only`. The CI gate itself is **not yet wired**, so nothing new blocks merges.
- **Deferred to a follow-up PR**: wiring `uvx pymarkdownlnt` as a step of the docs-consistency job (feeding `ci-ok`), and the small corpus cleanup the enabled rules then require — the 28 `MD040` fenced-code languages (each named by hand), and the auto-fixable `MD031` (16) and `MD012` (2). Until that PR lands, CodeRabbit already honors `.markdownlint.yaml` and stops flagging the disabled house-style rules; it will still surface the ~46 genuine `MD040`/`MD031`/`MD012` sites, which is correct — they are real and get cleaned in that PR.

## Consequences

### Positive
- The dominant, recurring AI-reviewer noise — MD013/MD022/MD032 on nearly every PR — is silenced at the source the moment `.markdownlint.yaml` lands, with no corpus churn.
- The residual gate is small and mostly mechanical (~50 findings, 18 auto-fixable), so the follow-up cleanup is bounded and the standing gate stays quiet.
- No Node dependency; the tool provisions exactly like every other gate (`uvx`, pinned), and joins `ci-ok` with no branch-protection change.
- House conventions (long-line prose, tight-heading ADR style) are preserved by decision, not eroded by a linter default.

### Negative / Tradeoffs
- Two config files encode one intent and can drift; the guard is a header comment in each, not yet a mechanized check (option E, deferred).
- Disabling MD022/MD032 means the tight-heading convention is never enforced, only tolerated — a new doc that spaces its headings is equally accepted; consistency of *that* axis is no longer gated.
- MD040 is enabled but not auto-fixable, so the 28 untagged fences are hand-work in the follow-up, and future untagged fences are a hard failure a contributor must fix (the intended behavior).
- The style gate lands a step behind its own config: between this PR and the follow-up, the rules are advisory (via CodeRabbit) but not CI-blocking.

## Links
- Extends: [ADR-0061](0061-markdown-link-check-gate.md) — the link-check gate on the same docs-consistency job; this adds the style facet. ADR-0061 gains an `Extended by: ADR-0062` navigation link (Proposed, edited without ceremony)
- Builds on: [ADR-0045](0045-repository-workflow-and-ci-enforcement.md) §6 (the docs-consistency gate) and §4 (the `ci-ok` aggregate that absorbs new gates without branch-protection change)
- Related: [testing-strategy.md](../testing-strategy.md) — CI Gates; the "mechanize review vigilance" mandate this gate satisfies, and "Workflow provenance" (tool-version pinning)
- Related: [CLAUDE.md](../../CLAUDE.md) — ADR governance (rule 5, on why enforcing MD022/MD032 across Accepted ADRs is a cost) and decision-capture routing (config knobs recorded in the owning ADR)

# ADR-0061: Markdown Link-Check Gate for the Docs-Consistency Job

## Status
Proposed

## Context and Problem Statement
The docs-consistency CI gate ([ADR-0045](0045-repository-workflow-and-ci-enforcement.md) §6) runs one script, [`check_adr_index.py`](../../scripts/check_adr_index.py), which validates the ADR *index table* against the files on disk. It does **not** validate the cross-file markdown *links* that thread the spec corpus together. The Phase-3.5 review-doc relocation into [reviews/](../reviews/) (PR #38) rewrote ~200 relative links by hand and verified them with a one-off script that was then discarded. Nothing now prevents a future PR from reintroducing a dead link — a reference to an old `specs/architecture-review-*.md` path, a moved script, a renamed ADR — and passing CI green. A 404 that lands inside an immutable Accepted ADR is doubly costly: [CLAUDE.md](../../CLAUDE.md) rule 5 permits the corrective link edit without a superseding ADR, but it is governance friction that a gate would prevent outright.

[testing-strategy.md](../testing-strategy.md) (CI Gates) frames every gate as mechanizing "requirements that would otherwise depend on code-review vigilance," and [ADR-0045](0045-repository-workflow-and-ci-enforcement.md) §6 casts the docs gate as the "generate-or-test" item. Link integrity sits squarely in that mandate. This ADR **extends [ADR-0045](0045-repository-workflow-and-ci-enforcement.md) §6** — it adds a second script to the existing docs-consistency job — and reverses none of ADR-0045's decisions.

## Decision Drivers
- A dead link inside an Accepted ADR is easy to introduce (any file move) and carries governance ceremony to fix; the asymmetry argues for prevention over cure.
- The corpus is link-dense and cross-referential; manual vigilance held on PR #38 *only* because someone wrote and then threw away a checker — that is exactly the review-dependence a gate exists to remove.
- [ADR-0045](0045-repository-workflow-and-ci-enforcement.md) §4: a new gate joins the `ci-ok` fan-in and never touches branch protection, so the marginal cost is one script plus one CI step.
- Precedent: [`check_adr_index.py`](../../scripts/check_adr_index.py) is stdlib-only with no runtime dependency; a link checker should meet the same bar.

## Considered Options
1. **A new blocking gate authorized by this extension ADR** (chosen) vs. **wiring the gate under [ADR-0045](0045-repository-workflow-and-ci-enforcement.md) §4's "adding a gate is routine" mechanism with only a PR `Decisions:` note.** §8's no-ADR precedent ("report-only coverage may be added later without an ADR") is explicitly scoped to a *report-only* gate. This gate is *blocking* — it fails the docs-consistency job, which fans into `ci-ok` and stops the merge — and it changes what §6 documents the docs gate as running. A new blocking enforcement surface is a stronger step than report-only coverage, so [CLAUDE.md](../../CLAUDE.md) rule 1 fires and the extension ADR is required; the `Decisions:` note supplements but does not substitute for it.
2. **File-existence validation only** (chosen) vs. **also validating `#fragment` anchors.** A `#L123` line anchor cannot be meaningfully validated against a file whose lines shift; heading anchors could be, but the dead-*file* link is the failure mode observed and worth the low false-positive risk. Anchor validation is deferred, not rejected.
3. **Scope to the specs/ tree** (chosen) vs. **crawl all tracked docs (root `CLAUDE.md`, `README.md`).** The observed failure mode — a 404 in an Accepted ADR — lives under specs/. Link *targets* are already validated wherever they resolve (a specs/ file linking `../../scripts/foo.py` is checked), so the coverage gap is only *source* files at the repo root; extending `rglob` is a one-line change if a need appears.

## Decision Outcome

### 1. A new stdlib script, `scripts/check_spec_links.py`
It crawls every `*.md` under `specs/` except `specs/personal/` (gitignored, absent in CI), extracts each `[text](target)` link, and resolves every **relative** target against the linking file's directory, reporting any that does not exist. External (`http(s)://`, `mailto:`) and pure `#anchor` targets are skipped; a `#fragment` is stripped before resolving (file existence is checked, anchors are not); targets that resolve under `specs/personal/` are skipped as unvalidatable. Fenced code blocks and inline code spans are removed before scanning, so an example link quoted in code — the arc42-cell reference `[adr/](adr/)` in [architecture-review-2026-07-06.md](../reviews/architecture-review-2026-07-06.md) is the live case — is not mistaken for a navigable link. Exit 0 when every link resolves, 1 with one line per dead link.

### 2. Wired into the existing docs-consistency job
The script runs as a second step of the `docs-consistency` job in [`ci.yml`](../../.github/workflows/ci.yml), alongside the ADR-index check. Both feed the `ci-ok` aggregate ([ADR-0045](0045-repository-workflow-and-ci-enforcement.md) §4), so a dead link fails the PR without any change to the branch-protection ruleset or the set of required checks.

### 3. Deliberate scope boundaries
File existence only, not anchor validity (option 2); the specs/ tree as the source set, with root-level docs deferred (option 3); `specs/personal/` neither crawled nor validated as a target (containment plus CI absence). Inline `[text](target)` links are matched, including an image's `![alt](target)` target (a dead local image is a real defect worth catching); reference-style links, an escaped `]` in link text, and the *outer* target of an image-badge nesting `[![alt](img.png)](target.md)` are silently unmatched, and root-absolute (`/x`) / protocol-relative (`//host/x`) targets are skipped as out-of-scope (GitHub resolves them against the repo root, which this gate does not model). The corpus uses none of these. Existence is checked against the working-tree filesystem, which equals the git checkout in CI (the authoritative run); a *local* run may diverge on an untracked linked file or a case-only filesystem mismatch. Validating against `git ls-files` (git-truth rather than filesystem-truth) is a deliberate non-goal — it would add a subprocess dependency and complicate the stdlib-only test story for a divergence CI already closes — recorded here as a candidate enhancement if a local/CI mismatch ever bites. The full list of parser approximations, including those that fail *loudly* (a target containing `)`, a space, `<...>`, or a `%20` escape; a link split across a line wrap; a link inside an HTML comment or a multi-line inline code span), lives in the `check_spec_links.py` docstring. These are recorded so a future widening is a conscious extension, not a silent rediscovery.

## Consequences

### Positive
- A dead cross-file link fails CI at PR time instead of landing inside an immutable ADR; the manual verification PR #38 needed becomes mechanical.
- The gate is self-justifying on its first run: any dead link already in the corpus surfaces immediately (the same way `check_adr_index.py` earned its place by catching two drifts on first run).
- No new dependency, no new required status check, no branch-protection change.

### Negative / Tradeoffs
- Anchor drift (a `#L123` link surviving a target's line shift) is not caught — accepted; line anchors are not validatable against a moving file.
- Root-level docs (`CLAUDE.md`, `README.md`) are not crawled as sources until a need appears.
- A legitimate file rename still requires updating every referencing link for CI to pass — which is the point, but it is real work the gate now enforces rather than trusts to review.

## Links
- Extends: [ADR-0045](0045-repository-workflow-and-ci-enforcement.md) §6 — the docs-consistency gate this joins; ADR-0045 gains an `Extended by: ADR-0061` navigation link, content otherwise untouched (Accepted, per governance)
- Related: [testing-strategy.md](../testing-strategy.md) — CI Gates; the "mechanize review vigilance" mandate this gate satisfies
- Related: [CLAUDE.md](../../CLAUDE.md) — ADR governance; rule 5 permits link fixes on Accepted ADRs, which this gate makes rarely necessary by catching dead links before merge
- Resolves: the "Markdown link checker in CI" entry in [open-questions.md](../open-questions.md)

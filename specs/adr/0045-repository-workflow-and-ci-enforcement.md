# ADR-0045: Repository Workflow and CI Enforcement

## Status
Accepted

## Context and Problem Statement
[testing-strategy.md](../testing-strategy.md) (CI Gates) specifies the *content* of the platform's CI gates — log canary, secret scanning, strict typing, lint/format, dependency audit — and the sequencing rule that the code gates ship in the same PR as the first code. What no document decides is the **enforcement mechanics around them**: how the main branch is protected, which merge strategies are allowed, where CI runs, how pinned tools and actions stay current, and *when* protection begins. With coding imminent (fixtures are the last precursor), these need deciding now, before the first agent-authored PR exists.

Two constraints shape the answer. The maintainer is solo, so any required-approval count above zero deadlocks the repository (an author cannot approve their own PR). And the repository has deliberately run **direct-to-main** through the specs/ADR phase — an efficient workflow (in-session propose → review → commit) that PR ceremony would only slow down while there is no code to gate.

## Decision Drivers
- Agent-authored code PRs are the imminent population; the CI gates exist precisely to mechanize review vigilance for them ([testing-strategy.md](../testing-strategy.md))
- Solo maintainer: no human second reviewer exists; enforcement must come from status checks, not approvals
- The repository is public, so GitHub-hosted runners are free with no minute caps — including Windows and macOS
- Local development is Windows-only today; CI is the *only* routine coverage for Linux and macOS, and cross-platform seams (path handling, `fcntl`/`msvcrt` advisory locks per [ADR-0042](0042-process-supervision-and-single-instance-locking.md)) are a realistic bug class
- Supply-chain discipline precedent: actions SHA-pinned (`publish.yml`), install-time hash verification ([ADR-0036](0036-plugin-package-installation-integrity.md)); CI's own tooling should meet the same bar
- The spec-phase direct-to-main workflow already provides in-session review for spec edits; protection should not tax it before it protects anything

## Considered Options
1. Activate branch protection now vs. **defer activation to the first code PR** (chosen)
2. Classic branch protection vs. **repository ruleset** (chosen)
3. Merge strategies: merge commits / **squash + rebase only, linear history** (chosen)
4. Runners: **GitHub-hosted full 3-OS matrix** (chosen) vs. Linux-only-on-PR vs. self-hosted
5. Secret scanning: `gitleaks/gitleaks-action` vs. **pinned, hash-verified gitleaks binary** (chosen)
6. Update automation: **Dependabot** (chosen) vs. Renovate vs. manual

## Decision Outcome

### 1. Protection is defined now, activated at the first code PR
The ruleset is checked in at [`.github/rulesets/main-protection.json`](../../.github/rulesets/main-protection.json) but **not applied**. Spec/ADR work continues direct-to-main until the first PR that introduces code or a runtime dependency; that PR both adds the code gates to `ci.yml` (per testing-strategy's ship-with-first-code rule) and applies the ruleset:

```
gh api repos/{owner}/{repo}/rulesets -X POST --input .github/rulesets/main-protection.json
```

Until then, [`ci.yml`](../../.github/workflows/ci.yml) runs on every push to main as a **non-blocking alarm** — a failure emails the maintainer rather than blocking the push. In-session review already does a PR's work for spec edits; first-code is also exactly when agent-authored changes begin, the population protection is for.

### 2. A ruleset, with no bypass
A repository **ruleset** rather than classic branch protection — same enforcement, but the configuration is exportable/importable JSON, so the protection policy itself lives in the repository and is reviewable like any other change. Rules on the default branch:

- direct pushes blocked (changes arrive only via PR), branch deletion and force-pushes blocked
- required status check: **`ci-ok` only** (see §4), with the up-to-date-branch policy strict
- required approvals: **0** — the solo-maintainer constraint; the green-CI and PR requirements are what protection actually enforces here
- linear history required
- **`bypass_actors` empty**: even an admin cannot push directly or merge red. The emergency escape hatch is deliberately editing the ruleset — the right amount of friction.

### 3. Squash + rebase, no merge commits
Merge commits are disabled (braided history, noisy `git log`/`bisect`, agent scratch commits landing verbatim). Both remaining methods are allowed: **rebase** for branches whose commits are already deliberately crafted (the established one-commit-per-logical-change discipline lands byte-for-byte, trailers included), **squash** for multi-commit agent scratch branches (the PR description becomes the commit message). Repo-level merge-method settings are aligned with the ruleset's `allowed_merge_methods` at activation.

### 4. `ci-ok`: a single aggregate required check
Required status checks and path-filtered jobs interact badly on GitHub: a path-skipped required check leaves a PR permanently "pending". The standard fix is adopted structurally: a fan-in job **`ci-ok`** `needs:` every gate job, fails if any of them failed or was cancelled, treats `skipped` as satisfied, and is the **only** context the ruleset requires. Consequences: docs-only PRs can path-skip the test matrix without deadlock, and adding/removing gates never touches branch protection — new jobs just join the `needs` list.

### 5. GitHub-hosted runners, full matrix on every code PR
All CI runs on GitHub-hosted runners (`ubuntu-latest`, `windows-latest`, `macos-latest`); no self-hosted hardware is needed or wanted — CI is what covers the platforms the maintainer has no local machine for, not the other way around. When the test suite exists it runs the **full 3-OS × Python 3.14 matrix on every code PR** (public repo: free; the suite will be small for a long time; cross-platform breakage is the realistic bug class). Economizing to Linux-on-PR/full-on-merge is the fallback if runtime ever warrants it. GUI (PySide6) tests run headless on all three OSes via Qt's `offscreen` platform plugin (pytest-qt; Linux runners need an apt step for Qt's system libraries); visual and interactive verification remains local and manual — CI proves the GUI code executes cross-platform, not that it looks right.

### 6. Two gates run pre-code
Two of testing-strategy's gates are meaningful against a specs-only tree and are live in `ci.yml` now, ahead of the first-code boundary:

- **Secret scanning**: gitleaks **v8.30.1 as a pinned, sha256-verified binary** scanning the full git history. The official `gitleaks-action` was evaluated and rejected: as of v3 it is distributed under a commercial EULA, and running the MIT-licensed binary with install-time hash verification matches the [ADR-0036](0036-plugin-package-installation-integrity.md) integrity discipline anyway. The version bump path is: new version + new checksum, one edit, Dependabot-independent (deliberate).
- **Docs consistency**: [`scripts/check_adr_index.py`](../../scripts/check_adr_index.py) verifies the [ADR index](README.md) against the files on disk — every row's file exists, numbering matches, the Status cell equals the file's `## Status` field, and every ADR file has exactly one row. This mechanizes the CLAUDE.md ADR-governance rule ("the index must always match the actual files") and delivers the docs-half of the 2026-07-06 review's deferred 4.B (generate-or-test) item; the code-facing half (host-matrix tables vs. enforcement code) still waits for the code to exist. The check earned its place on its first run by catching two real drifts: ADR-0019's index row still said "Proposed — stub" after T3.2 corrected the file, and ADR-0004's status field carried a non-vocabulary suffix.

The remaining gates — ruff lint+format, pyright strict, the test matrix, the log canary scan, pip-audit — **must join `ci.yml` in the first code PR**, tool versions pinned in that same PR (testing-strategy's "Workflow provenance" rule, restated in the workflow's header comment).

### 7. Dependabot, weekly, grouped
[`.github/dependabot.yml`](../../.github/dependabot.yml) covers `github-actions` (it understands and preserves the SHA-pin-with-version-comment form) and `pip` (a no-op until dependencies exist), weekly, grouped into single PRs. Renovate offers more configurability than this repository needs; manual bumping is what lets pins rot.

### 8. Accepted as-is, and explicitly not done
- The **pip-audit daily schedule** (when it exists) surfaces failures via GitHub's failed-workflow notification email — accepted as sufficient for a solo repository. `publish.yml` gains the release-blocking pip-audit step when the first dependency exists.
- **No coverage-threshold gate.** testing-strategy doesn't call for one; the canary, property, and security suites are the real assurance, and percentage thresholds invite gaming. Report-only coverage may be added later without an ADR.
- **No self-hosted runners, no merge commits, no admin bypass.**

### Positive Consequences
- Branch protection policy is itself version-controlled and reviewable, and activating it is one command at a boundary that is already defined
- The two live gates protect the asset the repository actually has today (specs: no leaked credentials, no governance drift) without taxing the spec-phase workflow
- `ci-ok` decouples branch protection from workflow shape permanently
- The gates-ship-with-first-code trap now has a workflow file with the requirement written at the top, not just a spec paragraph

### Negative Consequences / Tradeoffs
- Until activation, nothing *blocks* a bad push — the pre-code gates are alarms, not gates; accepted deliberately for the remaining (short) spec phase
- Deferred activation is a human-memory dependency; mitigated by the requirement being stated in the workflow header, testing-strategy, and this ADR
- Full-matrix-on-every-PR spends runner time on platforms most PRs don't affect; accepted while free and fast, with the economization fallback named
- gitleaks version bumps are manual (binary pin, not action) — the cost of avoiding the EULA'd action and keeping hash verification

## Pros and Cons of the Options

### Activate protection now
- Pro: mechanically enforces ADR immutability immediately
- Con: every spec edit becomes branch → PR → merge with only two cheap gates actually gating; taxes the propose→review→commit session workflow that already provides review

### Classic branch protection
- Con: configured only through UI/API state with no natural home in the repository; rulesets are the current, exportable mechanism

### Merge commits allowed
- Con: braided history, noisy bisect, agent scratch commits land verbatim

### Self-hosted runners
- Con: hardware and maintenance for something GitHub provides free at this repository's visibility; the maintainer's unspooled Linux hardware becomes a *local* dev environment when ready, not CI

### gitleaks-action
- Pro: PR-annotation conveniences
- Con: commercial EULA as of v3; the pinned, hash-verified binary is license-clean and matches ADR-0036 discipline

## Links
- Related: [testing-strategy.md](../testing-strategy.md) — CI Gates (gate content and the ship-with-first-code rule; this ADR decides the enforcement mechanics around them)
- Related: [CLAUDE.md](../../CLAUDE.md) — ADR governance rule mechanized by the docs-consistency gate
- Related: [ADR-0036](0036-plugin-package-installation-integrity.md) — the hash-verification discipline the gitleaks install step follows
- Related: [ADR-0023](0023-distribution-mechanism.md) — `publish.yml`, which gains the release-blocking pip-audit step when dependencies exist
- Related: [ADR-0042](0042-process-supervision-and-single-instance-locking.md) — example of the cross-platform seam the 3-OS matrix exists to test
- Resolves (docs half): [architecture review 2026-07-06](../reviews/architecture-review-2026-07-06.md), item 4.B — index/docs generate-or-test; the code-facing half waits for enforcement code to exist

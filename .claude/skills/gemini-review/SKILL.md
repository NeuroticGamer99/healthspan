---
name: gemini-review
description: Dispatch the Antigravity SDK (Gemini) review workflow on the current PR, wait for its review, verify each finding against the code, and reply. Use after /ship, or any time a PR is open and wants the Gemini lens.
---

# /gemini-review — dispatch, await, and triage a Gemini review

> **Best-effort reviewer (demoted 2026-07-24).** This is *not* a routine chain member like
> `/coderabbit-review` or `/copilot-review`. The free AI Studio tier realistically supports only
> **≈1 successful review per day** — one agentic review consumes roughly a full day's free-tier
> request budget (observed as ~20 requests/day for this project+model in 2026-07; the free-tier
> daily cap is project/model/account-dependent and shifts, so the AI Studio rate-limit dashboard is
> authoritative) — and Google also throws frequent
> transient 503 "high demand" errors, so **most runs fail without producing a report**. Run this
> only when the user explicitly asks for the Gemini lens, and **never let its failure or absence
> block a merge** — CodeRabbit and Copilot are the reliable two. A failed run here is an external
> capacity/quota fact, not a code defect. (Reliable Gemini review would need a paid API key or a
> local interactive tool on a Gemini subscription — a deferred decision.)

The Gemini lens is not a GitHub App but a repo-owned workflow —
`.github/workflows/gemini-review.yml` runs the Antigravity SDK (`google-antigravity`, Gemini 3 Pro
class) against the PR's diff and posts a real PR review authored by `github-actions[bot]`, with
`.gemini/styleguide.md` as its review lens. It is opt-in and, per the note above, best-effort.
`/ship gemini` runs this chain after shipping; invoke it directly on any PR that is already open.

Two facts specific to this reviewer:

- **A dispatch workflow resolves on `main`**, so this reviewer cannot review the PR that
  introduces or modifies its own workflow — such a PR is reviewed by the merged version (or, for
  the introducing PR, not at all; the request step fails loud in that case).
- **Quota is the free AI Studio tier** and Google may cut it without notice. A run that fails on
  quota surfaces as a failed workflow run, which the wait step reports — it never silently passes.

## 1. Find the PR

```bash
gh pr view --json number,url,headRefName,state
```

If there is no open PR for the current branch, stop and say so — run `/ship` first.

## 2. Dispatch the review

```bash
uv run --locked python scripts/bot_review.py request --bot gemini --pr <N>
```

The script dispatches the workflow on `main` with the PR number as input, then **verifies a new
run actually started** — the dispatch endpoint answers 204 even for asks that never produce a run
(disabled workflow, workflow file not on `main` yet), and waiting on one of those buys a
30-minute timeout for a review nobody managed to ask for.

The confirmed run is **title-matched to this PR** (the workflow's `run-name` carries the PR
number), so a concurrent dispatch for another PR cannot cross-confirm.

**It prints the floor and the run id to use next**, the floor stamped before the dispatch:

```text
dispatched gemini-review.yml run 16234567890 for PR 56
since: 2026-07-23T14:00:00Z
  pass that to: wait/fetch --bot gemini --pr 56 --since 2026-07-23T14:00:00Z --run 16234567890
```

Use those exact values in steps 3 and 4 — do not mint your own.

## 3. Wait for the review

```bash
uv run --locked python scripts/bot_review.py wait --bot gemini --pr <N> --since <floor> --run <run id>
```

The `--run` id is what lets a failed workflow run end the wait immediately — that run was the
only thing that could have posted the review. (On a recovered floor with no run id, wait falls
back to scanning this PR's runs by title.)

Run with `run_in_background: true`. Exit codes:

- **0** — the review is ready; continue to step 4. A **clean** run is still a review here (unlike
  CodeRabbit): the body states `posted 0 inline finding(s)` and fetch prints no comments — report
  the clean verdict and stop.
- **1** — either the dispatched run **failed** (the message names the run — check its logs;
  common cause: exhausted free-tier Gemini quota) or the wait timed out. **Silence is not a clean
  review**; report and stop.
- **3** — **empty range**: there was nothing to review — the PR head introduced no changes against
  `main` (already merged, or an empty PR), or every changed path was excluded by the sensitive-path
  filters. Distinct from a clean run: the diff was never looked at, not looked at and found clean.
  Report which cause (the body says) and stop; there is nothing to triage.

## 4. Triage and reply

```bash
uv run --locked python scripts/bot_review.py fetch --bot gemini --pr <N> --since <the floor from step 2>
```

Prints the review and only that review's comments, with the `id` to reply to, plus a `NOTE:` on a
count mismatch. Findings the agent could not anchor to a diff line appear **in the review body**
rather than as inline comments — triage those too; the body says how many there are.

Then follow **`.claude/bot-review-triage.md`** through its closing section: verify each finding
against the real code, reply per finding (body-only findings get one summary reply on the review
thread), report the verdict table, **stop for the user's go before changing any code**, and close
out per its §4 — a push never re-triggers this workflow, so a re-review of the fixed commit is a
fresh `/gemini-review` run — and this is the one chain the user still names explicitly, on quota
grounds rather than cost (ADR-0074).

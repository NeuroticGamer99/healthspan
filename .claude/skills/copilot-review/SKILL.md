---
name: copilot-review
description: Request a GitHub Copilot cloud review on the current PR, wait for it to complete, verify each finding against the code, and reply. Use after /ship, or any time a PR is open and wants a second opinion.
---

# /copilot-review — request, await, and triage a Copilot review

A second, independent opinion on an open PR. Copilot's findings overlap CodeRabbit's only partly —
on PR #26 it raised two the other missed — so it is worth running even on a PR CodeRabbit has
already passed.

## 1. Find the PR

```bash
gh pr view --json number,url,headRefName,state
```

If there is no open PR for the current branch, stop and say so — run `/ship` first.

## 2. Request the review

```bash
uv run python scripts/bot_review.py request --bot copilot --pr <N>
```

The script requests the review and then **verifies a reviewer was actually added**, failing loud if
not. That check is the point: requesting a login GitHub does not accept returns **HTTP 200 with an
empty `requested_reviewers`** — a silent no-op with no error to catch, which otherwise buys a
30-minute wait for a review nobody asked for.

If it errors (unavailable on the plan, insufficient permission), report the error verbatim and stop.
Do not retry blindly. There is no auto-review ruleset on this repo — Copilot reviews only when asked.

**It prints the floor to use next**, stamped before the request:

```
requested copilot; requested_reviewers now: Copilot
since: 2026-07-16T22:20:00Z
  pass that to: wait/fetch --bot copilot --pr 27 --since 2026-07-16T22:20:00Z
```

Use that exact value in steps 3 and 4 — do not mint your own. A floor stamped after the request can
exclude the very review it triggered, and improvising one is how that bug arrives.

The bot's identity is a minefield (it is requested under one login and displayed under another); the
map and its rationale live in `scripts/bot_review.py`, with `tests/test_bot_review.py` holding the
rules in place. Do not re-derive them by hand.

## 3. Wait for the review

```bash
uv run python scripts/bot_review.py wait --bot copilot --pr <N> --since <the floor from step 2>
```

Run with `run_in_background: true`. Exit 0 means a findings review is ready; exit 1 is a timeout —
**silence is not a clean review**, so report and stop.

## 4. Triage and reply

```bash
uv run python scripts/bot_review.py fetch --bot copilot --pr <N> --since <the floor from step 2>
```

Prints the review and only that review's comments, with the `id` to reply to, plus a `NOTE:` when
the body's stated count disagrees with what was fetched.

Then follow **`.claude/bot-review-triage.md`** through its closing section: verify each finding
against the real code, reply per finding, report the verdict table, **stop for the user's go
before changing any code**, and close out per its §4 — re-requesting Copilot after the fix
commit is a fresh `/copilot-review` run.

Copilot's findings skew toward performance and internal-consistency observations. Both of the ones
it raised on PR #27's predecessor were instructive rather than simply right or wrong — a true
complexity observation whose suggested fix would have defeated a fail-loud safety guard, and an
inverted diagnosis where the code was right and the comment was the bug. The lesson is not that
Copilot is unreliable; it is that the suggested remedy needs its own review, separately from the
observation.

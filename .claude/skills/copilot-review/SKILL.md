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

The script requests the review and then **confirms the ask reached GitHub**, failing loud if it
cannot. That check is the point: requesting a login GitHub does not accept returns **HTTP 200 with
an empty `requested_reviewers`** — a silent no-op with no error to catch, which otherwise buys a
30-minute wait for a review nobody asked for.

**What it confirms against is the issue timeline's `review_requested` event, not the
`requested_reviewers` array the request populates.** Reading that array back is what the check used
to do, and it false-negatived on **four consecutive accepted requests** (PRs #81, #82, #83, #85):
Copilot clears itself from the array as it picks the request up, so the read-back races the bot and
loses — on #85 it refused at `00:05:18Z` and the review landed at `00:09:14Z`. The timeline entry is
a record of the ask rather than a description of the current state, so nothing the bot does
afterwards retracts it. Do not re-derive this at the terminal: `scripts/bot_review.py` holds it and
`tests/test_bot_review.py` pins it, including that **either** of Copilot's two request-side logins
confirms the ask — the timeline records `Copilot`, the request POSTs
`copilot-pull-request-reviewer[bot]`, and accepting both is what keeps the check off a bet about
which of them GitHub chooses to show. The old check compared the right login, for the record; it
read the wrong *field*.

If the **ask itself** fails — unavailable on the plan, insufficient permission, a login GitHub
rejects outright — report the error verbatim and stop. Do not retry blindly. That is a different
outcome from an ask that was made and could not be *confirmed* — the paragraph beginning "**A
refusal prints that floor too**" governs that one, and it is explicitly not a reason to stop. Read
this instruction as scoped to the request call, never as a blanket "any error ends the chain": the
unconfirmed-ask refusal arrives as an error too, and obeying a blanket stop on it abandons a review
that is already being written. **A floor is printed here as well, and seeing one is not a signal
that the ask survived** — the command prints it on every exit after the request is attempted,
because a failed POST cannot prove the server did not act on it. What tells the two cases apart is
the error text, not the presence of the floor. There is no auto-review ruleset on this repo —
Copilot reviews only when asked.

**It prints the floor to use next**, stamped before the request:

```text
requested copilot; timeline review_requested event 29195776393 names Copilot
since: 2026-07-16T22:20:00Z
  pass that to: wait/fetch --bot copilot --pr 27 --since 2026-07-16T22:20:00Z
```

**A refusal prints that floor too, above the error, and it is a usable one** — it was stamped before
the ask, so no review of this ask can predate it. An unconfirmed ask is not a refused one: on every
occasion this check has failed here, the review was already on its way. So read the refusal as
*check the PR*, not as *stop* — go on to step 3 with the printed floor, and only report Copilot
unavailable if nothing arrives. The error no longer says "Do not wait", which was the previous
wording and was wrong all four times.

Use that exact value in steps 3 and 4 — do not mint your own. A floor stamped after the request can
exclude the very review it triggered, and improvising one is how that bug arrives.

The bot's identity is a minefield (it is requested under one login and displayed under another); the
map and its rationale live in `scripts/bot_review.py`, with `tests/test_bot_review.py` holding the
rules in place. Do not re-derive them by hand.

**The review's depth is not yours to choose, and this is where to say so rather than discover it.**
Copilot's review effort level is a **repository setting** — Settings → Copilot → Code review →
"Review effort level" — overriding an organization default. It is not a per-PR selector and not a
per-request argument, so neither this skill nor `scripts/bot_review.py` can set it, and a session
that goes looking for a flag will not find one. Docs research on 2026-08-09 found no REST, GraphQL
or `gh` surface for it at all; that is a finding with a date on it, not a permanent property, and
`specs/open-questions.md` carries what would reopen it.

**The expected setting for this repository is `Lite`** — which the GA docs label "Standard review
(default)", so the expectation is that nothing has been changed rather than that something was
configured. The other level is `Balanced`, which reviews more deeply and costs more Copilot AI
credits *and* more Actions minutes; the two were renamed from the preview's Low/Medium and went GA
on 2026-08-07. A third, `Max`, has been seen in the PR sidebar marked "coming soon" while the GA
docs use that word as a *plan* name — if the sidebar and the docs disagree, the sidebar is the one
looking at this account.

Confirming the setting is a **human, one-time, in-the-UI** action; an agent cannot read it back. If
a run comes back conspicuously shallower or more expensive than usual, that setting is the first
thing to check — and it changes without leaving a trace in any diff, which is the failure class this
whole toolchain keeps paying for.

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

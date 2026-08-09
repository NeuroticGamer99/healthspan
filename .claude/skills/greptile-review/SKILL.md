---
name: greptile-review
description: Trigger a Greptile review on the current PR, wait for it to complete, verify each finding against the code, and reply. Use after /ship, or any time a PR is open and wants the Greptile lens.
---

# /greptile-review — trigger, await, and triage a Greptile review

Greptile no longer reviews on PR creation — `skipReview: "AUTOMATIC"` in `.greptile/config.json`
made it opt-in, and `triggerOnUpdates: false` keeps a push from re-triggering it, so **every
Greptile review on this repository is one somebody asked for**. This skill is how that choice is
spent. `/ship greptile` runs this chain automatically after shipping; invoke it directly on any PR
that is already open.

`.greptile/README.md` explains why the trigger is manual — including the PR whose automatic review
was spent on a commit that stopped existing within the hour — and why the config values are what
they are.

## 1. Find the PR

```bash
gh pr view --json number,url,headRefName,state
```

If there is no open PR for the current branch, stop and say so — run `/ship` first.

## 2. Trigger the review

```bash
uv run python scripts/bot_review.py request --bot greptile --pr <N>
```

That posts `@greptileai review` and verifies the created comment reads back exactly as written —
the body starts with `@`, which `gh api` field flags can treat as a read-from-file directive, and a
mangled trigger summons nothing while still buying a full 30-minute poll. **The handle is
`@greptileai`, not `@greptile`.**

**It prints the floor to use next**, stamped before the trigger. Use that exact value in steps 3
and 4 — do not mint your own. A floor stamped after the trigger can exclude the very review it
caused, and improvising one is how that bug arrives.

The "Re-trigger Greptile" link in the summary comment's footer is **not** usable here: it
authenticates to the repository owner's Greptile account, not to the GitHub App, and redirects an
unauthenticated caller to a login page. The comment is the only agent-accessible trigger.

**This step is right whether or not a review already exists.** `skipReview` is documented but not
yet confirmed by measurement here (`.greptile/README.md` says so and names the observation that
settles it), and if the App did review on creation after all, this is simply a re-review: the floor
is stamped before the trigger, the summary is edited in place, and step 3 waits for that edit.
Report an unasked review if you see one — it is the measurement.

## 3. Wait for the review

```bash
uv run python scripts/bot_review.py wait --bot greptile --pr <N> --since <the floor from step 2>
```

Run with `run_in_background: true`. Runs have taken 2–8 minutes. Exit codes:

- **0** — there is something to triage; continue to step 4. It reports how many findings are open.
- **2** — the run was **clean**: no files needing attention, no stated count, and nothing left
  unanswered. Report and stop.
- **1** — failure or timeout. Read the message: it distinguishes two very different states.
  "no findings review after Ns" means Greptile has not answered — **silence is not a clean
  review**, so report and stop. **"STALE, not missing"** means it *has* answered, but about a
  superseded commit: its summary names the commit it reviewed and that is not the PR head. Nothing
  has looked at the current code. Do not read the existing summary as this commit's verdict — go
  back to step 2 and trigger again.

The wait deliberately keeps polling on a stale summary, because that is precisely what the trigger
in step 2 is expected to clear.

It also keeps polling when the summary states a finding count that the inline comments have not
reached yet: Greptile posts its summary about four seconds before the comments it counts, and
returning inside that window hands step 4 a review whose findings have not landed.

## 4. Triage and reply

```bash
uv run python scripts/bot_review.py fetch --bot greptile --pr <N> --since <the floor from step 2>
```

This prints the summary comment and **the findings that have no reply yet**, with the `id` to reply
to — answered findings are counted, not reprinted, so a re-run after triage does not re-litigate
what you already answered. Greptile's artifacts do not match the other bots' and the script
accounts for it. A findings run's review object has an **empty body** with everything in inline
comments; a clean run posts no review object at all; a re-review edits the summary comment in place
without creating anything new; and — PR #72 — a findings run can post **no review object and no
inline comment either**, leaving the finding as prose in the summary. The summary comment is the
only artifact present in every one of those, which is why it, and not the reviews endpoint, is what
the script polls. Do not re-derive any of this at the terminal; `scripts/bot_review.py` and its
tests are the record.

Watch for the `NOTE:` lines. A count mismatch means the summary's own finding count disagrees with
what was fetched — **investigate**, never assume which side is wrong. A freshness note means the
summary named no commit, so staleness could not be checked at all; a staleness note means the
review looked at an older commit, so its findings are real but the current code is unreviewed.

Exit **1** covers several distinct states, so read the message rather than the code. The two
commands word the no-report case differently — `wait` times out with *"no greptile findings review
after Ns"*, `fetch` refuses with *"no greptile summary after \<floor\>"* — and both mean the same
thing: it has not reported, which is not a clean review. Beyond that:

- *"The two signals disagree"* — the summary's prose verdict and its stated finding count
  contradict each other. The prose is model-written and the count comes from a configurable block,
  so either can drift; read the summary on the PR rather than guess which to believe.
- *"states N finding(s) but only M were fetched"* — **`fetch` only**, and it means one of two
  things, which the message names. If it adds *"usually a review still landing — re-run"*, the
  comments are still in flight: Greptile posts its summary about four seconds ahead of them, and a
  re-run resolves it. If it adds *"the comments are no longer in flight … exist only in the summary
  text"*, re-running will never help. Those findings were written as prose in the summary with no
  comment object behind them, so no threaded reply will ever clear them — **read the summary on the
  PR and answer them there**, as a PR-level comment carrying the
  `Acknowledges greptile summary <id>` reference (`.claude/bot-review-triage.md` §2, ADR-0067).
  That reference is what lets the `outstanding` merge gate credit the answer; without it the gate
  stays blocked however thoroughly the comment answers the findings.

  That second shape is not hypothetical: Greptile did exactly this on PR #72, stating one finding
  with no review object and no inline comment anywhere. Treating the gap as always-transient is
  what made an earlier version of `wait` poll for comments that were never coming.

**`wait` never emits either of those two messages** — do not look for them in its output. Inside
the grace window it simply keeps polling; past it, it *succeeds* with exit **0** and says so on the
ready line: `… is ready — N open finding(s), and M that exist only in the summary text`. That is
the same condition, routed as work to do rather than as a refusal, because there is something to
read. Step 4's `fetch` is where it becomes an exit 1.

Then follow **`.claude/bot-review-triage.md`** through its closing section: verify each finding
against the real code, reply per finding, report the verdict table, **stop for the user's go before
changing any code**, and close out per its §4 — a re-review of the fixed commit is a fresh
`/greptile-review` run, spent deliberately.

Two Greptile-specific notes for triage:

- **Its P1/P2/P3 badge is a claim like any other**, and its calibration here cuts both ways: a
  silent-lockout defect on PR #69 that two handed-off `/code-review` passes and both other bots
  missed, and three findings on PR #81 that measurement refuted one by one. Neither record earns
  assent; both earn attention.
- **Replies and 👍/👎 reactions are claimed to tune future reviews.** Unlike the other bots, triage
  here may have a lasting effect, so a declined finding is worth a reason rather than a bare "no".

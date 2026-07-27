---
name: greptile-review
description: Collect the Greptile review of the current PR — it reviews automatically at PR creation — then verify each finding against the code and reply. Use after /ship, or to re-trigger a review on a fixed commit.
---

# /greptile-review — collect, verify, and triage a Greptile review

**Greptile is the one reviewer here that is not asked.** The GitHub App reviews every non-draft PR
on creation, on its own, with no repo configuration involved; `.greptile/config.json` deliberately
leaves `triggerOnUpdates: false` so it does *not* re-review later pushes. So this skill's usual job
is to **collect** a review that is already running or already done — not to trigger one.

That is also the gap it exists to close: a review nobody collects is worse than no review, because
the PR looks reviewed. `/squash-merge` will not merge with an uncollected Greptile review
outstanding.

`.greptile/README.md` explains why the automatic trigger is kept and why the config values are what
they are.

## 1. Find the PR and the floor

```bash
gh pr view --json number,url,state,createdAt,headRefOid
```

If there is no open PR for the current branch, stop and say so — run `/ship` first.

**The floor is the PR's `createdAt`** when collecting the automatic review: Greptile's summary
comment is posted a few minutes after the PR opens, so PR creation is the one floor guaranteed to
precede it and to exclude nothing. Do not improvise a later one.

## 2. Only if you need a *re*-review

Skip this step when collecting the automatic run — it is already in flight.

After fixes land, the existing review is stale (it reviewed the old commit) and a push does not
re-trigger anything. Ask for a fresh one:

```bash
uv run python scripts/bot_review.py request --bot greptile --pr <N>
```

That posts `@greptileai review` and verifies the created comment reads back exactly as written —
the body starts with `@`, which `gh api` field flags can treat as a read-from-file directive, and a
mangled trigger summons nothing while still buying a full 30-minute poll. **The handle is
`@greptileai`, not `@greptile`.** It prints a floor stamped *before* the trigger; use that value in
steps 3 and 4 instead of `createdAt`.

The "Re-trigger Greptile" link in the summary comment's footer is **not** usable here: it
authenticates to the repository owner's Greptile account, not to the GitHub App, and redirects an
unauthenticated caller to a login page. The comment is the only agent-accessible trigger.

## 3. Wait for the review

```bash
uv run python scripts/bot_review.py wait --bot greptile --pr <N> --since <the floor>
```

Run with `run_in_background: true`. Creation runs have taken 2–8 minutes, a re-trigger ~5.5.
Exit codes:

- **0** — there is something to triage; continue to step 4. It reports how many findings are open.
- **2** — the run was **clean**: no files needing attention, no stated count, and nothing left
  unanswered. Report and stop.
- **1** — failure or timeout. Read the message: it distinguishes two very different states.
  "no findings review after Ns" means Greptile has not answered. **"STALE, not missing"** means it
  *has* answered, but about a superseded commit — its summary names the commit it reviewed and that
  is not the PR head. Nothing has looked at the current code; go to step 2 and re-trigger. Do not
  read the existing summary as this commit's verdict.

The wait deliberately keeps polling on a stale summary, because that is precisely what the
re-trigger in step 2 is expected to clear. **When you are merely collecting** — no trigger posted,
`triggerOnUpdates` false — a summary that is already stale can never become fresh on its own, and
the poll will run the full 30 minutes to tell you what it knew on the first pass. Pass a short
`--timeout` (say `--timeout 120`) when collecting, and keep the default only after a re-trigger.

It also keeps polling when the summary states a finding count that the inline comments have not
reached yet: Greptile posts its summary about four seconds before the comments it counts, and
returning inside that window hands step 4 a review whose findings have not landed.

## 4. Triage and reply

```bash
uv run python scripts/bot_review.py fetch --bot greptile --pr <N> --since <the floor>
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
changing any code**, and close out per its §4.

Two Greptile-specific notes for triage:

- **Its P1/P2/P3 badge is a claim like any other.** Calibration so far is 2 findings across 4 runs,
  both legitimate — including one silent-lockout defect that two handed-off `/code-review` passes
  and both other bots missed — but a clean record earns attention, not assent.
- **Replies and 👍/👎 reactions are claimed to tune future reviews.** Unlike the other bots, triage
  here may have a lasting effect, so a declined finding is worth a reason rather than a bare "no".

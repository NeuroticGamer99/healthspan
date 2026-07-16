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

## 2. Request the review — then verify it took

Capture the floor timestamp first, then request:

```bash
SINCE=$(date -u +%Y-%m-%dT%H:%M:%SZ)
gh api repos/OWNER/REPO/pulls/N/requested_reviewers \
  -f "reviewers[]=copilot-pull-request-reviewer[bot]"
```

**Verify the reviewer was actually added before waiting. Never trust the 200:**

```bash
gh api repos/OWNER/REPO/pulls/N --jq '.requested_reviewers[].login'   # expect: Copilot
```

If that is empty, **stop** — do not start the poll. Requesting a login GitHub does not accept
returns **HTTP 200 with `"requested_reviewers": []`**: a silent no-op, no error to catch. Skipping
this check buys a 30-minute poll that times out on a review nobody ever asked for, and a
"no review arrived" report that sends the reader hunting for an outage that does not exist.

### Copilot's four identities

This bot presents under different logins depending on where you look. Getting one wrong fails
silently, never loudly:

| Where | Login |
|---|---|
| **Request** it as (REST `reviewers[]`) | `copilot-pull-request-reviewer[bot]` |
| Appears in `requested_reviewers` as | `Copilot` |
| The **review** is authored by | `copilot-pull-request-reviewer[bot]` |
| The **inline comments** are authored by | `Copilot` |

They are one entity (`id: 175728472`, `node_id: BOT_kgDOCnlnWA`, `type: Bot`) wearing two names.
Do not infer the request login from the timeline's `requested_reviewer.login` — that is the display
identity *after* the fact, not the value you pass. Match with a case-insensitive
`test("copilot";"i")` filter rather than an exact login, in both directions.

It is a **Bot**, which is why REST's user-only `reviewers[]` silently drops a bad value. GraphQL's
`RequestReviewsInput` has a separate `botIds` field — `userIds` rejects a bot node id outright
(`Could not resolve to User node`) — if the REST path ever stops working.

There is no auto-review ruleset on this repo — Copilot reviews only when asked.

If the request errors outright (unavailable on the plan, insufficient permission), report the error
verbatim and stop. Do not retry blindly.

## 3. Wait for the review

Same background-poll shape as `/ship` step 4, keyed on the Copilot bot:

```bash
DEADLINE=$(( $(date +%s) + 1800 ))
while :; do
  n=$(gh api repos/OWNER/REPO/pulls/N/reviews \
        --jq "[.[] | select(.user.login==\"copilot-pull-request-reviewer[bot]\")
               | select(.submitted_at > \"$SINCE\")] | length" 2>/dev/null || echo 0)
  [ "${n:-0}" -gt 0 ] && { echo "COPILOT_REVIEW_READY"; exit 0; }
  [ "$(date +%s)" -ge "$DEADLINE" ] && { echo "TIMEOUT waiting for Copilot"; exit 1; }
  sleep 30
done
```

Run with `run_in_background: true`. On timeout, report and stop — **silence is not a clean
review**.

## 4. Triage and reply

Copilot puts an overview in the review body and its findings in inline comments — **authored under
different logins** (§2), so match case-insensitively.

**Scope to the review this run produced.** Identify it by id, then read the body and comments
through that id — never through the PR-level endpoints:

```bash
RID=$(gh api repos/OWNER/REPO/pulls/N/reviews \
        --jq "[.[] | select(.user.login|test(\"copilot\";\"i\"))
               | select(.submitted_at > \"$SINCE\")] | sort_by(.submitted_at) | last | .id")

gh api repos/OWNER/REPO/pulls/N/reviews/$RID --jq '"[\(.state)] \(.submitted_at)\n\(.body)"'

gh api repos/OWNER/REPO/pulls/N/reviews/$RID/comments \
  --jq '.[] | "=== \(.path):\(.line // .original_line) [\(.id)] ===\n\(.body)"'
```

The PR-level `/comments` endpoint returns **every** comment from every run: findings you already
fixed, and the bot's own conversational replies ("agreed — this is correctly fixed"), which are not
findings at all. Measured on PR #27, it returned 5 comments where the current review had 1. Triaging
that set re-litigates closed findings and treats thank-you notes as defects. The bot's replies are
themselves modelled as *reviews*, so the review list needs the same scoping — pick the review by id
and stay inside it.

**Cross-check the count.** The scoped body states `generated N comments`; the scoped comment fetch
must return exactly N. A mismatch means the scoping or the filter is wrong — go find the difference
before triaging. An empty result is only a clean review when the body says zero.

This check is only meaningful against a correctly-scoped fetch: run it over the PR-level endpoint
and it compares this review's N against every run's comments, then confidently reports a filter bug
that does not exist.

Then follow **`.claude/bot-review-triage.md`**: verify each finding against the real code, reply
per finding, report the verdict table, and **stop for the user's go before changing any code**.

Copilot's findings skew toward performance and internal-consistency observations. Both of the
findings it raised on PR #26 were instructive rather than simply right or wrong, and both are worked
through in the triage doc: a true complexity observation whose suggested fix would have defeated a
fail-loud safety guard, and an inverted diagnosis where the code was correct and the comment was the
bug. The lesson is not that Copilot is unreliable — both findings were worth the read — but that the
suggested remedy needs its own review, separately from the observation.

## 5. After the fixes land

If the user approves fixes: apply, re-run the gates, commit, push, then post the "fixed in `<sha>`"
replies. Re-requesting a Copilot review after a push is a fresh `/copilot-review` run.

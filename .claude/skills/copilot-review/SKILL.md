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

Capture the floor timestamp first, then request:

```bash
SINCE=$(date -u +%Y-%m-%dT%H:%M:%SZ)
gh api repos/OWNER/REPO/pulls/N/requested_reviewers -f "reviewers[]=Copilot"
```

**The logins are asymmetric, and it matters:** you *request* the reviewer `Copilot`, but the review
arrives from `copilot-pull-request-reviewer[bot]`. Requesting the bot login fails; waiting on the
`Copilot` login never fires. (Verified from PR #26's timeline:
`requested_by=NeuroticGamer99 -> reviewer=Copilot`.)

There is no auto-review ruleset on this repo — Copilot reviews only when asked.

If the request errors (unavailable on the plan, already requested, insufficient permission), report
the error verbatim and stop. Do not retry blindly.

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

Copilot puts an overview in the review body and its findings in inline comments:

```bash
gh api repos/OWNER/REPO/pulls/N/reviews \
  --jq '.[] | select(.user.login=="copilot-pull-request-reviewer[bot]") | .body'

gh api repos/OWNER/REPO/pulls/N/comments \
  --jq '.[] | select(.user.login=="copilot-pull-request-reviewer[bot]")
        | "=== \(.path):\(.line // .original_line) [\(.id)] ===\n\(.body)"'
```

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

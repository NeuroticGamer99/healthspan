---
name: coderabbit-review
description: Trigger a CodeRabbit review on the current PR, wait for it to complete, verify each finding against the code, and reply. Use after /ship, or any time a PR is open and wants the CodeRabbit lens.
---

# /coderabbit-review — trigger, await, and triage a CodeRabbit review

CodeRabbit no longer reviews on push — `auto_review.enabled: false` in `.coderabbit.yaml` made it
opt-in, one deliberately chosen reviewer chain per PR instead of every bot dogpiling every PR. This
skill is how that choice is spent on CodeRabbit. `/ship coderabbit` runs this chain automatically
after shipping; invoke it directly on any PR that is already open.

## 1. Find the PR

```bash
gh pr view --json number,url,headRefName,state
```

If there is no open PR for the current branch, stop and say so — run `/ship` first.

## 2. Trigger the review

```bash
uv run python scripts/bot_review.py request --bot coderabbit --pr <N>
```

CodeRabbit is not requestable through `requested_reviewers` — its ask channel is the
`@coderabbitai review` command comment, which the script posts and then **verifies read back
exactly as written**, failing loud if the created comment's body was mangled (the body starts with
`@`, which `gh api` field flags can treat as a read-from-file directive). A trigger that did not
land as written never summons the bot, and waiting on it buys a 30-minute timeout for a review
nobody asked for.

**It prints the floor to use next**, stamped before the trigger:

```
triggered coderabbit via comment 5058928383
since: 2026-07-23T14:00:00Z
  pass that to: wait/fetch --bot coderabbit --pr 54 --since 2026-07-23T14:00:00Z
```

Use that exact value in steps 3 and 4 — do not mint your own. A floor stamped after the trigger
can exclude the very review it caused, and improvising one is how that bug arrives.

## 3. Wait for the review

```bash
uv run python scripts/bot_review.py wait --bot coderabbit --pr <N> --since <the floor from step 2>
```

Run with `run_in_background: true`. Exit codes:

- **0** — a findings review is ready; continue to step 4.
- **2** — the run was **clean**: CodeRabbit posted its "No actionable comments were generated"
  summary and no review object exists (a clean run posts none, PR #29). Nothing to fetch or
  triage — report the clean verdict and stop.
- **1** — timeout. **Silence is not a clean review**; report and stop. If the trigger comment is
  visible on the PR but the bot never answered, say that too — it distinguishes an ignored ask
  from a slow one.

## 4. Triage and reply

```bash
uv run python scripts/bot_review.py fetch --bot coderabbit --pr <N> --since <the floor from step 2>
```

Prints the review and only that review's comments, with the `id` to reply to, plus a `NOTE:` when
the body's stated count disagrees with what was fetched — which means *investigate*, not that
either side is definitively wrong (the bot has been seen claiming 2 while posting 1, having counted
before deduplicating).

Then follow **`.claude/bot-review-triage.md`** through its closing section: verify each finding
against the real code (its §1a explains why the fetch trusts only `scripts/bot_review.py`), reply
per finding, report the verdict table, **stop for the user's go before changing any code**, and
close out per its §4 — a push no longer re-triggers CodeRabbit, so a re-review of the fixed
commit is a fresh `/coderabbit-review` run, spent deliberately.

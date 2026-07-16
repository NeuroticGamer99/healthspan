---
name: ship
description: Commit the change /land proposed, push, open or update the PR, wait for CodeRabbit's review, verify each finding against the code, and reply. Use in place of typing "commit" after /land.
---

# /ship — commit, PR, and CodeRabbit triage

Runs after `/land` has surveyed the change, run the gates, and proposed a commit message.
**Invoking `/ship` is the user's approval of that message** — do not re-litigate or rewrite it.

`/land` proposes; `/ship` disposes. Stop and report at any step that fails; never push past a red
gate.

## 1. Preconditions

- `/land` has run in this session and proposed a commit message the user has seen. If it hasn't,
  run `/land` first and stop — never invent a commit message here.
- Re-run the gates if anything changed since `/land`, or if you are unsure. Read the pinned
  versions out of the `env:` block of `.github/workflows/ci.yml` (`RUFF_VERSION`,
  `PYRIGHT_VERSION`, `PYTEST_VERSION`) — match CI, don't guess:

  ```bash
  uvx "ruff@$RUFF_VERSION" check .
  uvx "ruff@$RUFF_VERSION" format --check .
  uv run --with "pyright==$PYRIGHT_VERSION" --with "pytest==$PYTEST_VERSION" pyright
  uv run --with "pytest==$PYTEST_VERSION" pytest -q -n auto
  uv run python scripts/check_adr_index.py   # when specs/adr/ is touched
  ```

  The `--with "pytest==…"` on the **pyright** line is not redundant: it is how CI resolves the test
  files' imports, so dropping it can typecheck differently than CI does. The `-n auto` on pytest is
  a deliberate divergence — CI runs the suite serially so the log-canary gate sees one stream
  (testing-strategy.md) — but the version pin still matches.

  A gate that has gone red since `/land` stops the ship.
- Confirm the branch is not `main`. If it is, stop — branch first.

## 2. Commit and push

**Capture the review floor before the push**, in the same command — this timestamp is what step 4
waits on, and it is only correct if it is taken before the push that triggers the review:

```bash
SINCE=$(date -u +%Y-%m-%dT%H:%M:%SZ)   # BEFORE the push below; step 4 waits on this
```

Capturing it after the push is a real bug, not a style point: a review submitted in the gap would
fail step 4's `submitted_at > $SINCE` filter, and the poll would spin to a false timeout on a review
that had already landed. CodeRabbit has answered in under four minutes, so the gap is not
theoretical.

Then:

- Commit with the message `/land` proposed, unchanged, including its `Decisions:` section.
- The co-author trailer must name the model running **this** session — read it from the system
  prompt; never carry a trailer forward from an earlier commit.
- Push, setting upstream on a new branch: `git push -u origin <branch>`.

## 3. Open or update the PR

- If a PR already exists for the branch, the push updates it — say so and reuse it.
- Otherwise `gh pr create --base main`, with a body carrying: what landed and why, the `Decisions:`
  section, and a test plan (the gates, plus what the new tests actually cover). End with the Claude
  Code attribution line.
- Report the PR URL.

## 4. Wait for CodeRabbit

Use `$SINCE` from step 2 — the floor captured before the push, which distinguishes this review from
the bot's review of an earlier commit. If you reached this step without it, you pushed first: take
the floor from the push's own timestamp (`git log -1 --format=%cI`) rather than `date` now, which
would be after the review may already have landed.

Poll in the background so the wait costs nothing:

```bash
DEADLINE=$(( $(date +%s) + 1800 ))
while :; do
  n=$(gh api repos/OWNER/REPO/pulls/N/reviews \
        --jq "[.[] | select(.user.login==\"coderabbitai[bot]\")
               | select(.submitted_at > \"$SINCE\")] | length" 2>/dev/null || echo 0)
  [ "${n:-0}" -gt 0 ] && { echo "CODERABBIT_REVIEW_READY"; exit 0; }
  [ "$(date +%s)" -ge "$DEADLINE" ] && { echo "TIMEOUT waiting for CodeRabbit"; exit 1; }
  sleep 30
done
```

Run it with `run_in_background: true` — one notification arrives when the review lands, and you can
keep working meanwhile. Do not poll in the foreground.

Two details this depends on:

- **Key on the submitted review, not on comments.** CodeRabbit posts progress chatter and a
  walkthrough comment before the actual review; a comment-based wait fires early on noise.
- **Key on `submitted_at > $SINCE`.** CodeRabbit re-reviews on every push, so an unfiltered check
  matches a stale review instantly and you triage the wrong one.

On timeout: report that no review arrived, give the PR URL, and stop. **Silence is not a clean
review** — never report "no findings" from a timeout.

## 5. Triage and reply

**Scope to the review this push produced.** Identify it by id, then read the body and its comments
through that id — never through the PR-level endpoints (the inner `id` is what you reply to):

```bash
RID=$(gh api repos/OWNER/REPO/pulls/N/reviews \
        --jq "[.[] | select(.user.login==\"coderabbitai[bot]\")
               | select(.submitted_at > \"$SINCE\")] | sort_by(.submitted_at) | last | .id")

gh api repos/OWNER/REPO/pulls/N/reviews/$RID --jq '.body'

gh api repos/OWNER/REPO/pulls/N/reviews/$RID/comments \
  --jq '.[] | "=== \(.path):\(.line // .original_line) [\(.id)] ===\n\(.body)"'
```

This matters more here than anywhere: CodeRabbit re-reviews on **every push**, so by the second push
the PR-level `/comments` endpoint is returning findings you already fixed alongside the new ones,
plus CodeRabbit's own conversational replies ("agreed — this is correctly fixed"), which are replies,
not findings. Measured on PR #27 after three pushes: 5 comments returned, 1 of them the current
review's actual finding. Its replies are modelled as *reviews* too, so the review list needs the
same scoping — pick by id and stay inside it.

**Cross-check the count.** The scoped body opens with `Actionable comments posted: N`; the scoped
fetch must return exactly N. A mismatch means the scoping or filter is wrong — resolve it before
triaging, and never report an unexplained empty result as a clean review.

CodeRabbit's inline bodies embed a long "🧩 Analysis chain" section that often truncates the actual
finding when piped through `head`. If a finding's text looks cut off, fetch that one comment's full
body by `id` rather than triaging a fragment.

Then follow **`.claude/bot-review-triage.md`**: verify every finding against the real code, reply
per finding, report the verdict table, and **stop for the user's go before changing any code**.

## 6. After the fixes land

If the user approves fixes: apply them, re-run the gates, commit, push (the PR updates itself), and
only then post the "fixed in `<sha>`" replies so the SHA is real. CodeRabbit will re-review the new
commit — triage that pass the same way if it raises anything new.

Then `/copilot-review` for the second opinion.

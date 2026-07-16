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
- Re-run the gates if anything changed since `/land` (or if you are unsure): `uvx "ruff@<pinned>"
  check .`, `uvx "ruff@<pinned>" format --check .`, `uv run --with "pyright==<pinned>" pyright`,
  `uv run pytest -q -n auto`, and `uv run python scripts/check_adr_index.py` when `specs/adr/` is
  touched. Pinned versions live in the `env:` block of `.github/workflows/ci.yml` — match CI, don't
  guess. A gate that has gone red since `/land` stops the ship.
- Confirm the branch is not `main`. If it is, stop — branch first.

## 2. Commit and push

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

Capture the timestamp **before** pushing — it is the floor that distinguishes this review from the
bot's review of an earlier commit:

```bash
SINCE=$(date -u +%Y-%m-%dT%H:%M:%SZ)   # capture BEFORE the push in step 2
```

Then poll in the background so the wait costs nothing:

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

Fetch the review body and its inline comments (the `id` is what you reply to):

```bash
gh api repos/OWNER/REPO/pulls/N/reviews \
  --jq '.[] | select(.user.login=="coderabbitai[bot]") | .body'

gh api repos/OWNER/REPO/pulls/N/comments \
  --jq '.[] | select(.user.login=="coderabbitai[bot]")
        | "=== \(.path):\(.line // .original_line) [\(.id)] ===\n\(.body)"'
```

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

---
name: squash-merge
description: Squash-merge the current branch's PR with a clean composed commit message — gates verified green, body passed via --body-file -, result read back from origin/main. Use when the user asks to merge after reviews are clean.
---

# /squash-merge — merge the PR with a clean squash message

The last step of the chain: `/land` → `/ship` → the review chains the user chose to spend
(`/coderabbit-review`, `/copilot-review` — reviews are opt-in per PR, except Greptile, which
reviews every PR unasked and is collected with `/greptile-review`) → `/squash-merge`. Invoking
it is the user's approval to merge — but never past a red or pending gate. Stop and report at any
step that fails.

## 1. Preconditions

- An open PR exists for the current branch: `gh pr view --json number,title,state,headRefName`.
  If not, stop and say so.
- **Local state matches the remote PR**: `git fetch origin`, then confirm the working tree is
  clean and `git rev-parse HEAD` equals the PR's `headRefOid` (`gh pr view <N> --json
  headRefOid`). A mismatch means the message would be composed from commits the PR doesn't have
  (or miss ones it does); stop and reconcile. The fetch also refreshes `origin/main`, which
  step 2's `rev-list` depends on — a stale tracking ref mis-picks the first commit.
- **All checks green**: `gh pr checks <N>` must exit 0. Exit code 8 means checks are still
  *pending* — that is not green; watch in the background (`gh pr checks <N> --watch`,
  `run_in_background: true`) and merge only when everything has passed. A red check stops the
  merge outright.
- **Spent bot reviews complete and triaged**: every finding from a review chain that *was* run
  has a threaded reply per `.claude/bot-review-triage.md`, and each spent chain's latest run is
  clean or fully triaged against the latest pushed commit. An unanswered finding — or a review
  that was requested/triggered and has not answered yet — stops the merge. A PR whose review
  chains were deliberately not spent (reviews are opt-in per PR) has nothing to wait for: state
  plainly that no bot reviewed it and merge on the user's explicit say-so.
- **No Greptile finding is unanswered**: Greptile is *not* opt-in — its App reviews every PR on
  creation — so unlike the others it is never legitimately unspent, and a PR can reach this step
  carrying findings nobody asked for and nobody read. Check with the PR's `createdAt` as the floor,
  so the whole PR's history is in scope:

  ```bash
  uv run python scripts/bot_review.py fetch --bot greptile --pr <N> --since <PR createdAt>
  ```

  **Exit 2 clears this gate** — either the run was clean, or every finding it posted already has a
  threaded reply. **Exit 0 stops the merge**: it lists findings with no reply, which is the exact
  thing this gate exists to catch. Exit 1 means the signals disagreed and no verdict is available;
  read the PR before deciding.

  A `NOTE:` saying the review looked at an older commit is **not** a blocker. Fix commits land
  after a review by definition, so a triaged PR reaches this step stale as a matter of course; it
  only means Greptile has not seen the final state. Re-trigger via `/greptile-review` step 2 if you
  want a verdict on the merged code, but do not treat it as an unmet precondition. Do not treat the
  mere presence of a Greptile summary comment as evidence anyone read it.
- The user has asked for the merge in this session. `/ship` and `/copilot-review` never merge;
  neither does this skill uninvited.

## 2. Compose the squash message

GitHub's default squash message concatenates every branch commit — the `/land`-approved message
jumbled together with "address review" fixups. Always replace it:

- **Subject**: the PR title plus ` (#<N>)`.
- **Body**: the body of the branch's *first* commit — the message `/land` proposed and the user
  approved:

  ```bash
  first=$(git rev-list origin/main..HEAD | tail -1)
  git log -1 --format=%b "$first"
  ```

  Keep its `Decisions:` section and co-author trailer intact. If fix commits followed
  (`git rev-list --count origin/main..HEAD` greater than 1), insert one line above the
  `Decisions:` section noting what rode along — "Includes bot-review fixes (…)" — never the fixup
  messages themselves. If the first commit's body is somehow empty, compose from the PR
  description's "What landed" and `Decisions:` sections instead.

  **The body must describe the merged state, not the first attempt.** The first commit's body is
  the *base*, not a quotation: where a later commit invalidated a claim it makes, correct that
  claim rather than reproducing it. The "Includes …" line records what rode along; it does not
  neutralize a sentence the branch went on to contradict, and a reader of `main` has only this
  message. Read the branch diff (`git diff origin/main...HEAD`), not just the first commit, and
  reconcile every claim against it before composing.

  This is not hypothetical: on PR #65 the review findings were about the entry's *own* accuracy,
  so the fixes falsified three of the first commit's sentences — an unqualified quota rate, a
  posture the branch had since softened, and a citation it had removed. Verbatim reuse would have
  put all three permanently on append-only `main`. Where a correction is judgment rather than
  fact — the claim is arguable, or rewriting it would change what the user approved at `/land` —
  surface the fork and let the user pick; everything else, fix and say so in the report.

## 3. Merge

```bash
gh pr merge <N> --squash --delete-branch --subject "<subject>" --body-file - <<'EOF'
<composed body>
EOF
```

**`--body-file -` is the only stdin form, and the heredoc delimiter must be quoted** (`<<'EOF'`
— unquoted, the shell runs command substitution on backticks and expands `$` inside the
message). `--body -` is accepted without error and sets the literal one-character string `-` as
the commit body — `gh` does not follow `git commit -F -` conventions. This exact mistake shipped PR #43's squash commit (`97e43ce`, 2026-07-20) with a
body of "`-`", and the same flag pair exists on `gh pr create` (see `/ship`, which carries the
same rule for PR bodies).

## 4. Verify — mandatory

A zero exit is not a clean merge; refresh the tracking ref (do not trust `gh pr merge` to have
fast-forwarded it — that behavior is incidental, not guaranteed) and read the state back:

```bash
git fetch origin main
git log --format=%B -1 origin/main
```

must show the composed subject and body — compare ignoring trailing whitespace (`%B` appends a
trailing newline, and GitHub may normalize trailing blank lines), but every content line must
match; this read-back is what caught `97e43ce`. `origin/main` is the authoritative check. Then
sync local `main` explicitly rather than trusting gh's incidental fast-forward:

```bash
git checkout main && git merge --ff-only origin/main
```

(a no-op when gh already fast-forwarded it), and confirm the feature branch is gone. If the
message is wrong, stop and report what the body actually says — never force-push `main` to
repair it; `main`'s history is append-only and the fix is the user's call.

## 5. Report

The merged SHA, confirmation the message verified, and the next queued step (worklist item or
phase work item) if one is on deck.

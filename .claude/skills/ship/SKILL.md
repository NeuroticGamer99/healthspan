---
name: ship
description: Commit the change /land proposed, push, and open or update the PR. With a reviewer argument (/ship coderabbit) also trigger that bot's review chain; bare /ship ships only. Use in place of typing "commit" after /land.
---

# /ship — commit, PR, and (optionally) a chosen reviewer

Runs after `/land` has surveyed the change, run the gates, and proposed a commit message.
**Invoking `/ship` is the user's approval of that message** — do not re-litigate or rewrite it.

Takes an optional reviewer argument choosing which bot chain to spend on this PR — reviews are
opt-in per PR, one deliberately chosen lens instead of every bot dogpiling every PR:

- **`/ship`** — ship only. **Greptile is the exception to "nothing reviews unasked": its GitHub
  App reviews every new PR on its own**, so opening a PR always starts one. After reporting the PR
  URL, say that Greptile is already reviewing and offer `/greptile-review` to collect it, then
  remind the user of the chains they can additionally spend: the two reliable lenses
  `/coderabbit-review` and `/copilot-review`, a local `/code-review`, and — only if explicitly
  asked — the best-effort `/gemini-review` (see below). Do not wait for anything here.
- **`/ship greptile`** — ship, then run the **`/greptile-review`** chain (step 4) to collect the
  review the PR creation already started. No trigger is posted; the chain begins at the wait.
- **`/ship coderabbit`** — ship, then run the **`/coderabbit-review`** chain (step 4).
- **`/ship gemini`** — ship, then run the **`/gemini-review`** chain (step 4). **Best-effort, not a
  routine chain member** (demoted 2026-07-24): on the free tier it usually fails without producing a
  report — the `/gemini-review` skill has the quota/capacity math. Run it only when the user names
  it; never let its failure or absence block a merge. Not the sunset Gemini Code Assist app — this
  is the repo-owned Antigravity SDK workflow.
- **`/ship copilot`** — ship, then tell the user Copilot is by preference not chained from
  `/ship`: it runs as its own explicit `/copilot-review` step, which you should offer to run now.
- Any other argument: stop and say it is not a known reviewer.

`/land` proposes; `/ship` disposes. Stop and report at any step that fails; never push past a red
gate.

**The git recipes in this file are Bash — run them with the Bash tool, not PowerShell.** The
guards below (`[ -n "$mb" ]`, `{ …; exit 1; }`) are Bash syntax, and git's `^{commit}` revision
suffix is mangled by PowerShell's parser unless the whole revision is quoted — which here it is,
deliberately. This matters most at the collapse: it is the only history rewrite in the skill
chain, and a guard that fails to parse is a guard that does not run. Step 3's note on writing the
PR body is the deliberate exception and stays as it is: its subject is file *encoding*, not git,
and the PowerShell form it names writes UTF-8 without a BOM correctly.

## 1. Preconditions

- `/land` has run and proposed a commit message the user has seen, and wrote it to
  `<scratchpad>/commit-msg/<branch>.txt`, with the exact branch name beside it in
  `<scratchpad>/commit-msg/<branch>.branch` (its step 7). **Read the message from that file** — it
  is the approved copy — and **check the sidecar before using it**: its single line, trailing
  newline and any carriage return or BOM ignored, must equal `git rev-parse --abbrev-ref HEAD`.
  Compare the trimmed line rather than raw bytes — the file is written on Windows, where a tool can
  emit CRLF against `.gitattributes eol=lf`, and a correct ship blocked by a line ending is how an
  operator learns to skip the check. Anything else is a mismatch. In detached HEAD `rev-parse`
  answers the literal `HEAD`, which no sidecar holds, so that state fails closed — correctly, since
  there is no branch to push. The path is a hint, not a proof of ownership: on Windows, *which*
  file a branch-derived path names depends on which API resolves it, and `/land` step 7 carries the
  full measured breakdown for the legal branches `a./b` and `a/b`. What it means here is that a file
  sitting at exactly the expected path can still hold another work item's message — a .NET or
  PowerShell read of `a./b` returns `a/b`'s contents silently (measured) — and committing that
  would ship the wrong `Decisions:` links onto this branch. **The sidecar closes this without
  needing to know which layer misbehaved**, which is the point of resting on a checked value: any
  wrong read yields some *other* branch's name, and that mismatches `git rev-parse --abbrev-ref
  HEAD`. Do not reason about which layer is the safe one; `/land` step 7 records why that ranking
  does not hold. A missing or mismatched sidecar is "no file": stop and
  re-run `/land`, never reconcile it by hand. If `/land` ran
  in an earlier session, the file is under *that* session's scratchpad — ask the user for the
  path rather than re-deriving the prose from memory or transcript. **Then verify the trailer
  before using the file**: its `Co-Authored-By` must name the model running *this* session
  (read the model from the system prompt). A file written by an earlier session can carry that
  session's model, and nothing else will ever notice — on mismatch, stop and re-run `/land`;
  never edit the trailer here. If no matching file and no `/land` run exists, run `/land`
  first and stop — never invent a commit message here.
- Re-run the gates if anything changed since `/land`, or if you are unsure. Read the pinned
  versions out of the `env:` block of `.github/workflows/ci.yml` (`RUFF_VERSION`,
  `PYRIGHT_VERSION`, `PYTEST_VERSION`) — match CI, don't guess:

  ```bash
  uvx "ruff@$RUFF_VERSION" check .
  uvx "ruff@$RUFF_VERSION" format --check .
  uv run --with "pyright==$PYRIGHT_VERSION" --with "pytest==$PYTEST_VERSION" pyright
  uv run --with "pytest==$PYTEST_VERSION" pytest -q -n auto
  uv run python scripts/check_adr_index.py   # when specs/adr/ is touched
  uv run python scripts/check_spec_links.py  # always — validates targets anywhere in the repo
  ```

  The `--with "pytest==…"` on the **pyright** line is not redundant: it is how CI resolves the test
  files' imports, so dropping it can typecheck differently than CI does. The `-n auto` on pytest is
  a deliberate divergence — CI runs the suite serially so the log-canary gate sees one stream
  (testing-strategy.md) — but the version pin still matches.

  A gate that has gone red since `/land` stops the ship.
- Confirm the branch is not `main`. If it is, stop — branch first.

## 2. Collapse savepoints, commit, and push

- **Decide "has it ever been pushed" by asking the remote, never the cache**:
  `git ls-remote --exit-code --heads origin "refs/heads/<branch>"`. A local tracking ref is stale
  evidence in both directions — `git fetch --prune` drops it while the remote branch may have been
  recreated, and a push from another clone or worktree never created it here — and this guard
  protects ADR-0069's only history rewrite, so it gets the authoritative probe (`/squash-merge`
  step 1 fetches first for the same class of hazard). Two details of the probe are load-bearing:
  - **Give it the full ref, not the bare branch name.** A bare name matches on the ref *tail*, so
    `git ls-remote --exit-code --heads origin python-5caa5761e1` exits 0 against this repo's own
    origin, printing `refs/heads/dependabot/pip/python-5caa5761e1` (measured). Any local branch
    whose name equals the last segment of some remote branch — `savepoint-skill` against a remote
    `chore/savepoint-skill` — then reads as already-pushed, skips the collapse, and is first-pushed
    with a savepoint as its first commit: the state this skill says breaks `/squash-merge`'s
    first-commit extraction. `refs/heads/<branch>` is anchored and exits 2 on the same probe.
  - **Classify all three outcomes, and fail closed on the third.** Exit 0 the remote has it; exit 2
    it does not; **any other exit is no evidence either way** — 128 covers an unreachable host, an
    unknown remote name, and an expired credential (measured). Do not read "not 2" as pushed, nor
    "not 0" as never-pushed: the first silently skips the collapse, the second aims `reset --soft`
    at history this session knows nothing about. Stop and report instead.
- **If the branch carries savepoint commits and the remote does not have it**, collapse them
  first (ADR-0069). Make a final `/savepoint` if **`git status --porcelain` prints any line at
  all — `??` untracked lines included** (that skill's containment scan and explicit path list are
  the only way work enters a commit here — never stage leftovers with `git add -A`). Do not reach
  here for `/land` step 7's **tracked-modified, not dirty** discriminator: that one exists because
  `git stash create` cannot hash untracked state, and it is the wrong test for this question.
  Applied here it reads an untracked-only tree as nothing-to-do, skips the checkpoint, and
  collapses a branch whose new files were never staged — so they are never pushed. ADR-0069's
  problem statement records that exact tree: the ADR-0068 branch carried seven untracked files,
  including its largest. Then resolve and check the base **before any reset**:

  ```bash
  git fetch origin --quiet
  mb=$(git merge-base origin/main HEAD)
  [ -n "$mb" ] && git cat-file -e "$mb^{commit}" || { echo "no merge base — stop"; exit 1; }
  git reset --soft "$mb"
  git commit -F <scratchpad>/commit-msg/<branch>.txt
  ```

  The **fetch** comes first because `git merge-base origin/main HEAD` reads the local tracking
  ref: a stale `origin/main` resolves the base further back than it truly is, and `reset --soft`
  then folds commits that are already upstream into this PR's single composed commit
  (`/squash-merge` step 1 fetches ahead of the same class of hazard, and says so). The **guard**
  exists because an empty `mb` fails two different ways and only one of them is loud. Quoted, as
  written above, `git reset --soft ""` exits 128 on `fatal: ambiguous argument ''` (measured) —
  caught by this skill's stop-on-failure rule. Unquoted, the same emptiness leaves a bare
  `git reset --soft`, a silent no-op to HEAD: the collapse skipped with no error and a savepoint
  pushed as the branch's first commit. The guard makes the outcome independent of that quoting,
  and it additionally catches a merge base that resolves to no object at all, which no amount of
  quoting would. Resolve `mb` in the same Bash call that uses it; shell variables do not
  survive between Bash tool invocations, and an empty `mb` is precisely what the guard is for.
  The checkpoints were local scaffolding; public history gets the single composed commit, same as
  today. The reflog keeps the chunks.

  **If `git commit -F` fails after the reset, put the tip back before reporting:**

  ```bash
  git reset --soft ORIG_HEAD
  ```

  Everything above guards the state *before* the rewrite and nothing guards the window inside it.
  The two steps are not atomic: `reset --soft` succeeds, then `commit -F` exits 1 on an empty
  index — reachable whenever the savepoints net to no diff against the merge base (a file added
  by one checkpoint and removed by a later one), and equally on a mistyped or space-containing
  `<scratchpad>` path — leaving the branch pointing at the merge base with every savepoint off
  the tip. The stop-on-failure rule then halts exactly there. `reset --soft ORIG_HEAD` restores
  the tip and the index (measured: `ORIG_HEAD` is set by the collapse's own reset), and "the
  reflog keeps the chunks" is the archaeology, not the recovery — name the command rather than
  leaving an operator to derive it from a reflog at the one moment the branch looks destroyed.
- **If the remote has the branch, never reset** — the collapse window has passed and the PR
  history is what it is. Rewriting pushed history is not this skill's call. What to do instead
  turns on where the new work sits, and "commit on top with the same `-F` file" is right for
  only one of the three cases:
  - **Tree dirty** — the new work is uncommitted. Stage it exactly as the next bullet does
    (`/savepoint`'s scan and explicit path list, never `git add -A`) and commit on top with the
    same `-F` file.
  - **Tree clean, new work already in savepoint commits** — the ordinary shape of a *second*
    ship, since `/apply-review` checkpoints every finding batch and every reviewer round. There
    is nothing to commit: `git commit -F` against an empty index exits 1, and running it here
    is how the second ship of any branch dies. Push the savepoints as they stand and say so.
    Their subjects become PR history, which is harmless — `/squash-merge` composes the merge
    from the **first** commit's body, and that is still `/land`'s message from the first ship.
    **Say the other half out loud too: the message the user just approved was not used.**
    Invoking `/ship` is an approval (this skill's opening line), and on this path the freshly
    composed message is verified by step 1 and then goes nowhere — the merge body still comes
    from the first ship's. Silence here reads as "landed", and the divergence surfaces at merge
    time or never. Name the file, say it is unconsumed, and tell the user that if *this* text is
    what they want on the merge, `/squash-merge` is where it goes.
  - **Tree clean, nothing ahead of the remote** — nothing to commit and nothing to push; say so
    and go to step 3, where an existing PR is reused.

  The collapse guarantee therefore rests on a stated precondition: **this
  skill is the only route by which a branch is first pushed** (already the standing rule — pushes
  go through skills, never hand-rolled commands). A branch pushed around `/ship` with savepoints
  uncollapsed leaves a savepoint as its first commit, which also breaks `/squash-merge`'s
  first-commit-is-the-composed-message extraction — if you inherit that state, say so and let
  the user decide rather than resetting a pushed branch.
- On a branch with no savepoints (nothing between the merge base and `HEAD`), **stage the work
  the same way a savepoint would** — nothing has staged it yet, and `git commit -F` against an
  empty index exits 1 (or, worse, commits only a stray pre-staged file): run `/savepoint`'s scan
  and explicit `git add` path list over the change, then `git commit -F
  <scratchpad>/commit-msg/<branch>.txt`.
- **On each path that commits from the `-F` file** — the collapse, the tree-dirty commit-on-top,
  and the no-savepoints branch — the message is `/land`'s file, unchanged, including its
  `Decisions:` section and the co-author trailer step 1 verified. **Then read the commit back**:

  ```bash
  git log -1 --format=%B > <scratchpad>/commit-msg-landed.txt

  # Put the SOURCE through git's own cleanup first, then normalize both sides
  # identically. `git stripspace` is the same implementation `git commit -F`
  # runs, so the comparison cannot drift from it; `--format=%B` then appends a
  # trailing newline the file does not carry, which the printf pair removes.
  git stripspace < <scratchpad>/commit-msg/<branch>.txt > <scratchpad>/expected-raw.txt
  printf '%s\n' "$(cat <scratchpad>/expected-raw.txt)"      > <scratchpad>/expected-msg.txt
  printf '%s\n' "$(cat <scratchpad>/commit-msg-landed.txt)" > <scratchpad>/landed-msg.txt
  diff <scratchpad>/expected-msg.txt <scratchpad>/landed-msg.txt
  ```

  `git commit -F` applies whitespace cleanup, so the one artifact this mechanism exists to keep
  faithful is otherwise the one never checked. Measured, `whitespace` mode strips leading blank
  lines, strips trailing whitespace from every line, collapses runs of blank lines, and trims
  trailing blanks. **`git stripspace` is that same cleanup**, which is why the source goes through
  it rather than the operator being asked to judge the difference: with it, **any** delta stops
  the ship. The earlier form said "whitespace-only deltas are that cleanup — fine" and left the
  classification to whoever ran it, so a correct ship carrying one trailing space produced a red
  diff that had to be talked past — a check whose verdict depends on operator judgement is the
  same species as one whose verdict depends on its own quoting. `--cleanup=verbatim` would also
  make the comparison exact and is the wrong way to get there: it buys exactness by landing the
  trailing whitespace and blank-line runs in public history. The normalization is not decoration: raw, the two
  files differ by that one appended newline and `diff` exits 1 on a correct landing (measured), so
  **every** ship reports a red gate — and under this skill's own "never push past a red gate" rule
  that either stops good ships or teaches the operator to ignore the one check standing between
  the approved text and public history. The second outcome is the worse one.

  **Do not run this on the paths that make no commit from the file** — "tree clean, new work
  already in savepoint commits" and "tree clean, nothing ahead of the remote". There `git log -1`
  answers the last *savepoint* subject, which mismatches the composed message at word level by
  construction, so the rule above would stop a legitimate push over a commit deliberately not
  made. Both sit under the **already-pushed** bullet, which carries its own reporting
  instructions — that is where their coverage comes from, not from this check.

  The normalization is narrower than it looks, which is why it is safe to apply unconditionally
  here: command substitution strips *trailing newlines* and `printf` restores exactly one, so the
  only difference it can hide is how many newlines a file ends with. Measured against the rest —
  trailing whitespace on a line, extra interior blank lines, a changed word, a dropped trailer —
  every one still differs after normalizing. Interior blank lines in particular survive it, so
  git's collapse of them remains visible and remains a judgement call under the rule above.
- Push, setting upstream on a new branch: `git push -u origin <branch>`.

## 3. Open or update the PR

- If a PR already exists for the branch, the push updates it — say so and reuse it.
- Otherwise `gh pr create --base main`, with a body carrying: what landed and why, the `Decisions:`
  section, and a test plan (the gates, plus what the new tests actually cover). End with the Claude
  Code attribution line.
- **Write the composed body to `<scratchpad>/pr-body.md` and pass that path** to `--body-file`
  — the read-back in the next bullet diffs against that same file, so it has to survive the call.
  Write it as **UTF-8 without a BOM** (the Write tool, or PowerShell's
  `[System.IO.File]::WriteAllText($path, $content, [System.Text.UTF8Encoding]::new($false))`):
  PR bodies here are full of em dashes and `Set-Content` without `-Encoding UTF8` writes cp1252
  and corrupts them silently (`CLAUDE.md`, PowerShell file encoding). Keeping
  it on disk is what lets the read-back below be a real `diff`; a body that exists only inside a
  heredoc can only be spot-checked, which tests the lines you thought to check rather than the
  one that broke. If you do pipe it, `--body-file -` is the only stdin form and the heredoc
  delimiter must be *quoted* (`<<'EOF'` — unquoted, the shell runs command substitution on every
  backtick and expands `$` inside the body before `gh` sees it, and markdown bodies are full of
  backticks). `--body -` is accepted without error and sets the literal string `-` as the
  description (it silently discarded PR #43's body, 2026-07-20).
- Then read it back and **diff it**:

  ```bash
  gh pr view --json body --jq .body > <scratchpad>/pr-body-landed.md

  # Normalize both sides identically before comparing — `--jq` always appends a
  # trailing newline and a composed file may end with none (or several), so an
  # unnormalized diff fails on a body that landed perfectly. Same rule, and the
  # same reason, as /squash-merge step 4.
  printf '%s\n' "$(cat <scratchpad>/pr-body.md)"        > <scratchpad>/expected.txt
  printf '%s\n' "$(cat <scratchpad>/pr-body-landed.md)" > <scratchpad>/landed.txt
  diff <scratchpad>/expected.txt <scratchpad>/landed.txt
  ```

  The `--jq` unwraps the JSON envelope to raw markdown — without it the escaped `{"body":"..."}`
  form can never match. A full comparison, not merely "isn't `-`", so shell-expansion damage is
  caught too.
- Report the PR URL.

## 4. The chosen reviewer chain

Bare `/ship` ends at step 3: report the PR URL, note that Greptile is already reviewing, and list
the chains available. **Do not wait for anything** — `/ship greptile` is how the user asks for the
Greptile wait, and no *other* review is coming unasked (`auto_review.enabled: false`), so waiting
for one polls a silent PR to a 30-minute timeout.

`/ship greptile`: continue with the **`/greptile-review`** skill, **skipping its step 2** — the
review was triggered by opening the PR, and posting `@greptileai review` on top of it would ask
twice for the same thing. Use the PR's `createdAt` as the floor, wait, then fetch and triage.

`/ship coderabbit`: continue with the **`/coderabbit-review`** skill from its step 2 — it posts
the `@coderabbitai review` trigger through `scripts/bot_review.py request` (which stamps and
prints the floor), waits in the background, fetches exactly that review, and triages per
`.claude/bot-review-triage.md`, stopping for the user's go before changing any code. Everything
the wait/fetch protects against — reply-reviews with empty bodies, per-page `jq` aggregation,
string-compared timestamps, the clean run that posts no review object at all — is documented and
tested in `scripts/bot_review.py`; do not re-derive it here.

`/ship gemini` (best-effort only): continue with the **`/gemini-review`** skill from its step 2 —
the same request/wait/fetch/triage shape, with the ask being a workflow dispatch of
`.github/workflows/gemini-review.yml` (verified to have actually started a run). Expect it to fail
most of the time on the free tier (the `/gemini-review` skill explains why) — that is not a defect
and is never grounds to hold the merge; report the failure and move on. Note its resolves-on-`main`
caveat: a PR that introduces or modifies that workflow cannot be reviewed by its own version of it.

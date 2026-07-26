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
  uv run python scripts/check_spec_links.py  # always — validates targets anywhere in the repo
  ```

  The `--with "pytest==…"` on the **pyright** line is not redundant: it is how CI resolves the test
  files' imports, so dropping it can typecheck differently than CI does. The `-n auto` on pytest is
  a deliberate divergence — CI runs the suite serially so the log-canary gate sees one stream
  (testing-strategy.md) — but the version pin still matches.

  A gate that has gone red since `/land` stops the ship.
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

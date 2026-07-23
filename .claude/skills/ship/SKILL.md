---
name: ship
description: Commit the change /land proposed, push, and open or update the PR. With a reviewer argument (/ship coderabbit) also trigger that bot's review chain; bare /ship ships only. Use in place of typing "commit" after /land.
---

# /ship — commit, PR, and (optionally) a chosen reviewer

Runs after `/land` has surveyed the change, run the gates, and proposed a commit message.
**Invoking `/ship` is the user's approval of that message** — do not re-litigate or rewrite it.

Takes an optional reviewer argument choosing which bot chain to spend on this PR — reviews are
opt-in per PR, one deliberately chosen lens instead of every bot dogpiling every PR:

- **`/ship`** — ship only. Nothing reviews automatically (`auto_review.enabled: false`); after
  reporting the PR URL, remind the user of the reviewer chains they can spend: `/coderabbit-review`,
  `/copilot-review`, or a local `/code-review`.
- **`/ship coderabbit`** — ship, then run the **`/coderabbit-review`** chain (step 4).
- **`/ship copilot`** — ship, then tell the user Copilot is by preference not chained from
  `/ship`: it runs as its own explicit `/copilot-review` step, which you should offer to run now.
- Any other argument (e.g. `gemini` — Gemini Code Assist's consumer GitHub app was sunset
  2026-07-17 and is not a reviewer here): stop and say it is not a known reviewer.

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
- Pass the body via **`--body-file -`** with a *quoted* heredoc (`<<'EOF'` — unquoted, the shell
  runs command substitution on every backtick and expands `$` inside the body before `gh` sees
  it, and markdown bodies are full of backticks). `--body -` is accepted without error and sets
  the literal string `-` as the description (it silently discarded PR #43's body, 2026-07-20).
  Then read it back: `gh pr view --json body --jq .body` (the `--jq` unwraps the JSON envelope
  to raw markdown — without it the escaped `{"body":"..."}` form can never match) must match the
  composed description — a full comparison, not merely "isn't `-`", so expansion damage is
  caught too.
- Report the PR URL.

## 4. The chosen reviewer chain

Bare `/ship` ends at step 3: report the PR URL and the reviewer chains available. **Do not wait
for a review** — since `auto_review.enabled: false`, no review is coming unasked, and waiting for
one polls a silent PR to a 30-minute timeout.

`/ship coderabbit`: continue with the **`/coderabbit-review`** skill from its step 2 — it posts
the `@coderabbitai review` trigger through `scripts/bot_review.py request` (which stamps and
prints the floor), waits in the background, fetches exactly that review, and triages per
`.claude/bot-review-triage.md`, stopping for the user's go before changing any code. Everything
the wait/fetch protects against — reply-reviews with empty bodies, per-page `jq` aggregation,
string-compared timestamps, the clean run that posts no review object at all — is documented and
tested in `scripts/bot_review.py`; do not re-derive it here.

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
- Report the PR URL.

## 4. Wait for CodeRabbit

`scripts/bot_review.py` owns the waiting. Run it in the background so the wait costs nothing —
one notification arrives when the review lands:

```bash
uv run python scripts/bot_review.py wait --bot coderabbit --pr <N> --since-commit HEAD
```

Use `run_in_background: true`; do not poll in the foreground. Exit 0 means a findings review is
ready; exit 1 means it timed out (default 30 min, `--timeout`); exit 2 means the run was **clean**
— CodeRabbit posted its "No actionable comments were generated" summary and no review object
exists (a clean run posts none, PR #29), so skip steps 5–6: there is nothing to fetch or triage.
Report the clean verdict and go straight to `/copilot-review`. **A timeout is not a clean review**
— report it and stop rather than concluding "no findings".

`--since-commit HEAD` derives the floor from the commit you just pushed, in UTC. Prefer it to a
hand-written `--since`: the script converts correctly, whereas a hand-rolled `git log --format=%cI`
yields a *local* offset that string-compares as newer than stale reviews and admits all of them.

## 5. Triage and reply

```bash
uv run python scripts/bot_review.py fetch --bot coderabbit --pr <N> --since-commit HEAD
```

That prints the review body and only *that review's* comments, each with the `id` you reply to. It
also cross-checks the body's `Actionable comments posted: N` against what it fetched and prints a
`NOTE:` on a mismatch — which means *investigate*, not that either side is definitively wrong (the
bot has been seen claiming 2 while posting 1).

Everything the fetch protects against is documented, and tested, in `scripts/bot_review.py` and
`tests/test_bot_review.py`: it reads one review by id (the pull-level endpoints return every past
run's findings plus the bots' own replies), skips reply-reviews (a bot's "agreed, this is fixed" is
itself a review, with an empty body), pages explicitly, and compares instants rather than strings.

Then follow **`.claude/bot-review-triage.md`**: verify every finding against the real code, reply
per finding, report the verdict table, and **stop for the user's go before changing any code**.

## 6. After the fixes land

If the user approves fixes: apply them, re-run the gates, commit, push (the PR updates itself), and
only then post the "fixed in `<sha>`" replies so the SHA is real. CodeRabbit will re-review the new
commit — triage that pass the same way if it raises anything new.

Then `/copilot-review` for the second opinion.

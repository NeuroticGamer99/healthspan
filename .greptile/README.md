# Why this Greptile config looks the way it does

`config.json` is JSON and cannot carry comments, so the rationale that
`.coderabbit.yaml` keeps inline lives here instead. This file is not part of
Greptile's configuration surface — it reads `config.json`, `rules.md` and
`files.json` — it is for whoever edits those next.

## The posture: automatic at creation, manual afterwards

Greptile reviews this repository **automatically when a PR is opened**. That is
the GitHub App's own behaviour and needs no configuration; the repo ran four
reviews on stock defaults before this config existed.

That is deliberate and differs from every other reviewer here — CodeRabbit is
`auto_review.enabled: false`, Copilot and the Antigravity workflow are asked
explicitly. Greptile is kept automatic because it is free, it answers in two to
eight minutes while the rest of the ship flow is still running, and across its
first four runs it produced two findings, both legitimate, one of which
(PR #69's silent ACL lockout) two handed-off `/code-review` passes and both other
bots had missed. The cost that made the others opt-in was triage attention, and
Greptile has not spent any.

What automatic review does create is the risk of a finding nobody collects.
That is closed on the tooling side, not by suppressing the review:
`/greptile-review` collects it, and `/squash-merge` refuses to merge while any
Greptile finding lacks a threaded reply. The gate is keyed on unanswered
findings rather than on the review's freshness, because fix commits land *after*
a review by definition — a triaged PR reaches merge time with the review one or
more commits behind, and a freshness gate would fire on that ordinary end state
while staying silent on the finding nobody read.

- **`triggerOnUpdates: false`** — set explicitly although it is also the
  default, because it is a *choice*, not a limitation someone should later
  "fix". A re-review of a fixed commit is a deliberate spend
  (`.claude/bot-review-triage.md` §4), asked for with an `@greptileai review`
  comment.
- **`statusCheck: false`** — for two independent reasons. It creates a GitHub
  status check *instead of* the summary comment, and that summary comment is the
  completion signal `scripts/bot_review.py` polls for both outcomes; enabling
  this would make every wait time out. It would also add a check that interacts
  with the ADR-0045 branch ruleset, which is an architectural decision (CLAUDE.md
  rule 1) rather than a config tweak.

## The review settings

- **`strictness: 1`** (verbose end of 1–3, default 2) — the analogue of
  CodeRabbit's `profile: assertive`. This is a solo, spec-heavy project where
  design nits are wanted; four stock runs at strictness 2 produced no noise, so
  there is headroom to ask for more.
- **`commentTypes`** drops `"style"` from the default
  `["logic", "syntax", "style", "info"]`. Formatting, naming and structural
  consistency are already gated by `ruff`, `pyright` and PyMarkdown (ADR-0062) in
  CI, and a bot comment restating a blocking gate is pure noise. This is the
  mechanical form of a rule the other two reviewers only receive as prose.
- **`ignorePatterns`** mirrors `.coderabbit.yaml`'s `path_filters` — the
  gitignored data, database, log and recovery-kit artifacts, plus
  `specs/personal/`. Greptile clones the repository, so it sees tracked files
  only and `specs/personal/` has no tracked files; the exclusion is defence in
  depth against a force-added artifact reaching a public review comment, exactly
  as it is for CodeRabbit. Written in `.gitignore` syntax as a single
  newline-separated string, which is the schema Greptile defines for this key.

  This is the **third** copy of that containment list — the other two are
  `.coderabbit.yaml` and `scripts/gemini_review_logic.py`'s `EXCLUDED_GLOBS` —
  so it is drift-tested rather than comment-enforced, in
  `tests/test_gemini_review_logic.py`. A containment boundary has to fail the
  build, not depend on a reviewer noticing one line.

## Where the conventions live

`rules[]` in `config.json` carries the conventions that have a natural file
scope (ADR governance → `specs/adr/**`, the `except` convention → `**/*.py`,
PowerShell UTF-8 → `**/*.ps1`, and so on). Greptile's scoped rules are more
expressive than anything the other two reviewer configs can express, so the
mechanizable half of `CLAUDE.md` lives there. `rules.md` carries the rest: how
to weigh a finding, and what not to flag at all.

`.coderabbit.yaml` and `.gemini/styleguide.md` are the sibling configs. A
convention added or changed in one must be mirrored in the others.

## Observed review shapes

The artifacts Greptile posts are not the shape the other bots use, and
`scripts/bot_review.py` encodes what was observed live rather than what the docs
imply. A findings run posts a review object with an **empty body**, its inline
comments, and a summary issue comment; a clean run posts the summary issue
comment **only**, with no review object at all. A re-review **edits the existing
summary comment in place** — it creates no new comment and does not move the
review object. The summary comment is therefore the completion signal for both
outcomes, and its footer names the commit actually reviewed, which is how a
stale review is told from a fresh one. The full set of failure modes each rule
prevents is documented at the `greptile` entry in `scripts/bot_review.py`.

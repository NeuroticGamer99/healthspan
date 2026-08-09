# Why this Greptile config looks the way it does

`config.json` is JSON and cannot carry comments, so the rationale that
`.coderabbit.yaml` keeps inline lives here instead. This file is not part of
Greptile's configuration surface — it reads `config.json`, `rules.md` and
`files.json` — it is for whoever edits those next.

## The posture: nothing reviews unasked

Greptile's GitHub App reviews every new PR on its own. That is its default and
needs no configuration — the repo ran four reviews on stock defaults before this
file existed. **`skipReview: "AUTOMATIC"` turns that off**, leaving the
`@greptileai review` comment as the only trigger, which is what
`scripts/bot_review.py request --bot greptile` posts. The value is a
case-sensitive enum and the uppercase spelling is the documented one
([controlling nitpickiness](https://www.greptile.com/docs/code-review/controlling-nitpickiness)).

Until now it was the other way round, and deliberately so: the automatic review
was free, it answered in two to eight minutes while the rest of the ship flow
was still running, and it spent little triage attention — which was the cost
that had made every other reviewer opt-in. Its record over that period is mixed
rather than uniform, and both halves are worth keeping: PR #69's silent ACL
lockout was a real defect that two handed-off `/code-review` passes and both
other bots missed, while PR #81's three findings were every one refuted by
measurement. **Neither half is what changed here.** What changed is which commit
the free review lands on.

**Measured on PR #83.** Greptile reviewed the commit the PR was opened at and
raised two legitimate P1s. The branch then moved fifteen commits, including a
redesign that replaced the mechanism both findings were about. Because
`triggerOnUpdates` is `false` — correctly, see below — nothing re-reviewed it,
so the automatic run had been spent on a head that stopped existing within the
hour and the review that mattered had to be asked for anyway. A creation trigger
fires at the one moment in a PR's life when its diff is least likely to be the
diff that merges.

So the spend is now made where the other three are made: deliberately, on a
commit someone chose. `/greptile-review` triggers, waits and triages as one
chain, and `/ship greptile` runs that chain after the push. This also ends a
carve-out nothing else in the repository had — `/ship`, `/squash-merge` and
`.claude/bot-review-triage.md` each had to name Greptile as the exception to
"nothing reviews unasked", and each now does not.

What the old posture created was the risk of a finding nobody collects, and
that risk is what the merge gate was built around; the gate is unchanged and
still earns its keep. `/squash-merge` refuses to merge while any bot's finding
lacks a threaded reply, keyed on unanswered findings rather than on the review's
freshness — fix commits land *after* a review by definition, so a triaged PR
reaches merge time with its review a commit or two behind, and a freshness gate
would fire on that ordinary end state while staying silent on the finding nobody
read.

One piece of that gate did change with the trigger. `scripts/bot_review.py`
carried a `silent_always_reviewers` detector whose only subject was Greptile: a
bot that reviews unasked has no legitimate silence, so silence had to block the
merge. Under a manual trigger Greptile's silence means what CodeRabbit's means —
the chain was not spent — so its `always_reviews` flag is now `False`. The flag
and the detector stay: they are what a future unasked reviewer would set and
need, ADR-0067 §2 records the detector as part of the gate's contract, and its
tests now drive it through a synthetic always-reviewing spec so it stays proven
rather than merely present.

**This is verified in Greptile's docs but not yet by measurement here**, and the
measurement is cheap: the first PR opened after this lands should attract no
Greptile review until one is asked for. If one arrives unasked, the key was not
honored — record that here rather than re-deriving it later.

- **`triggerOnUpdates: false`** — set explicitly although it is also the
  default, because it is a *choice*, not a limitation someone should later
  "fix". Every review is a deliberate spend (`.claude/bot-review-triage.md` §4),
  asked for with an `@greptileai review` comment; a push is not a decision to
  review, and with `skipReview` set neither is opening the PR.
- **`statusCheck: false`** — because Greptile's docs describe this as creating a
  status check *instead of* the summary comment, and that summary comment is the
  completion signal `scripts/bot_review.py` polls for both outcomes. Enabling it
  would make every wait time out.

  Note what this key does **not** control: a `Greptile Review` check is posted
  by the App itself — observed passing on PR #71 with this set to `false`, on a
  PR the App had reviewed automatically. Whether it still appears on a PR the
  App never reviews is unmeasured, and the same first-PR observation that
  confirms `skipReview` will answer it. So "it would add a new check" is *not* a
  reason to leave it off, and the check it posts is not in the ADR-0045 required set
  (`ci-ok` is), so nothing here interacts with the branch ruleset today.
  Enabling `statusCheck` later would still want checking against that ruleset,
  but the blocking objection is the summary comment, not the check.

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
imply. Four shapes so far:

- **Findings run** — a review object with an **empty body**, its inline
  comments, and a summary issue comment (PRs #67, #69).
- **Clean run** — the summary issue comment **only**, with no review object at
  all (PRs #68, #70, #71).
- **Re-review** — the existing summary comment **edited in place**; no new
  comment, and the review object does not move (PRs #68, #69).
- **Findings with no comments at all** — a summary stating a finding count, with
  no review object *and* no inline comment; the findings exist only as prose in
  the summary, where nothing can reply to them (PR #72).

The summary comment is the only artifact present in every one of those, which is
why it — and not the reviews endpoint — is what the tooling polls. Its footer
names the commit actually reviewed, which is how a stale review is told from a
fresh one. The last shape is also why a stated count exceeding the comments
found is only treated as "still arriving" within a two-minute grace window: past
it, the missing findings are not late, they were never comments. The full set of
failure modes each rule prevents is documented at the `greptile` entry in
`scripts/bot_review.py`.

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
so the automatic run had gone against a head that stopped existing within the
hour and the review that mattered had to be asked for anyway. A creation trigger
fires at the one moment in a PR's life when its diff is least likely to be the
diff that merges.

So the choice is now made where the other three are made: deliberately, on a
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
it is consistent with a chain that never ran, and never proves one (ADR-0074 §2)
— so its `always_reviews` flag is now `False`. The flag
and the detector stay: they are what a future unasked reviewer would set and
need, ADR-0067 §2 records the detector as part of the gate's contract, and its
tests now drive it through a synthetic always-reviewing spec so it stays proven
rather than merely present.

**Measured on PR #84, the PR that introduced the key — it is honored.** That PR
opened at 19:14:52Z and attracted no Greptile artifact; the summary comment was
*created* at 19:18:08Z, after the `@greptileai review` trigger posted at
19:15:33Z, and its footer read `Reviews (1)`. One review, and the trigger caused
it. Greptile was demonstrably up, answering in 2m35s, and `main` did not carry
the key at the time — so the App read `config.json` from the **PR head**, which
is worth knowing on its own: a config change to this file takes effect on the PR
that makes it, not one PR later.

**No ADR records this posture, deliberately.** Greptile's own review of PR #84
raised the opposite — that CLAUDE.md's routing rule 4 sends a config default to
its owning ADR — and the owner's decision was that this predates the convention
of writing ADRs for tooling and is too small a change to start now. Rule 4's
wording presumes an owning ADR exists and is silent on what to do when none ever
did; the sibling reviewers are in the same position (CodeRabbit's
`auto_review.enabled: false` and Gemini's opt-in default have no ADR either), so
this file is the owning record for all three. Recorded here rather than left in a
PR thread, because the question will be asked again.

- **`triggerOnUpdates: false`** — set explicitly although it is also the
  default, because it is a *choice*, not a limitation someone should later
  "fix". Every review is a deliberate act (`.claude/bot-review-triage.md` §4),
  asked for with an `@greptileai review` comment; a push is not a decision to
  review, and with `skipReview` set neither is opening the PR.
- **`statusCheck: false`** — because Greptile's docs describe this as creating a
  status check *instead of* the summary comment, and that summary comment is the
  completion signal `scripts/bot_review.py` polls for both outcomes. Enabling it
  would make every wait time out.

  Note what this key does **not** control: a `Greptile Review` check is posted
  by the App itself — observed passing on PR #71 with this set to `false`, on a
  PR the App had reviewed **automatically**. It is tied to that path, not to
  reviewing as such: PR #84's checks carried no Greptile entry at all, listed
  after its triggered review had completed. So "it would add a new check" is
  *not* a reason to leave it off, and no check it posts is in the ADR-0045 required set
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
imply. Six shapes so far:

- **Findings run** — a review object with an **empty body**, its inline
  comments, and a summary issue comment (PRs #67, #69).
- **Clean run** — the summary issue comment **only**, with no review object at
  all (PRs #68, #70, #71).
- **Re-review** — the existing summary comment **edited in place**; no new
  comment, and the review object does not move (PRs #68, #69).
- **Findings with no comments at all** — a summary stating a finding count, with
  no review object *and* no inline comment; the findings exist only as prose in
  the summary, where nothing can reply to them (PR #72).
- **Findings with no comments and no count either** — the shape above with its
  one detector disabled (PR #84). The summary said `Files Needing Attention:`
  and encoded its issue in the "Fix All in Claude Code" badge href, but carried
  no `Fix the following N code review issue` line, which is the only pattern the
  `greptile` spec's `count` regex matches — so `stated_count` returned `None`,
  the cross-check was skipped, and `outstanding` cleared the merge gate on a PR
  holding a real unanswered finding. **The cause is a Greptile dashboard toggle,
  outside this repository**, disabled while the owner was looking for the
  skip-review option. That is the finding, not the toggle: a merge gate must not
  depend on a setting that leaves no trace in a diff, and the same file had
  already chosen an HTML marker for `summary_marker` on exactly that reasoning
  while leaving `count` on a visible sentence in an optional block. The repair
  needed no new regex — `clean_marker` is not in that block and already told the
  poller this run was not clean — and it landed separately, as
  `unprovable_summaries` (ADR-0067 §2): a summary-marker bot whose summary does
  not read clean, and which posted nothing *newer than that summary*, cannot
  clear the gate. It reads a marker's absence and two timestamps, so no toggle
  can take any of them away. Freshness rather than mere existence, and that was
  a correction: keyed on "has this bot any findings at all", a re-review editing
  the summary in place still cleared the gate whenever an earlier run's finding
  had been answered. **The durable reproduction is that repair's
  regression test**, which encodes this artifact shape directly rather than
  depending on a dashboard setting outside the repository — a reproduction
  resting on the toggle would be the same defect one level up. The toggle
  stays off, but nothing rests on that any more.
- **A summary with comments outside the diff** — a summary carrying a
  "Comments Outside Diff" section emits the marker `greptile_failed_comments`
  where every shape above emits `greptile_other_comments_section` (PR #81).
  Which sections a body carries is the configurable part, so the marker
  *suffix* is variable and only the `greptile_` namespace is stable. Keyed on
  the one literal, `wait` burned its full 1800s on a review that had landed in
  3.5 minutes, `fetch` refused with "no greptile summary" while the summary was
  plainly on the PR, and — quietest and worst — `outstanding` read the comments
  endpoint, cleared the gate normally, and never listed a P1 that existed only
  as summary prose. This shape is why `summary_marker` matches the namespace
  rather than a section, and it is recorded here because that code comment
  cites *this list* as what a future maintainer should re-derive the pattern
  from: without this entry, following the instruction would re-derive the
  narrow marker it replaced.

The summary comment is the only artifact present in every one of those, which is
why it — and not the reviews endpoint — is what the tooling polls. Its footer
names the commit actually reviewed, which is how a stale review is told from a
fresh one. The **findings-with-no-comments-at-all** shape (PR #72) is also why a stated count exceeding the comments
found is only treated as "still arriving" within a two-minute grace window: past
it, the missing findings are not late, they were never comments. The full set of
failure modes each rule prevents is documented at the `greptile` entry in
`scripts/bot_review.py`.

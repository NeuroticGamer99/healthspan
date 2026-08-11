# Bot-review triage

The shared procedure for handling an automated PR review. Used by `/coderabbit-review`
(CodeRabbit — directly or via `/ship coderabbit`), `/greptile-review` (Greptile — directly or via
`/ship greptile`), `/gemini-review` (the Antigravity SDK Gemini workflow — directly or via
`/ship gemini`), and `/copilot-review` (GitHub Copilot). The bots are useful; none is trusted.

**One section reaches past the bots.** §1's *Under-reporting* rule is an owning statement for
`/apply-review` step 3 as well — registered in `scripts/check_doc_citations.py` and cited there
rather than restated — because a finding that names one site is a work item for the rule whoever
raised it. Restructuring or renumbering §1 during a bot-skill change breaks that caller, and the
citation gate checks the pointer, not the section number.

**None of them reviews unasked.** Greptile was the last that did — its GitHub App reviews every
new PR by default — until `skipReview: "AUTOMATIC"` in `.greptile/config.json` put it on the same
manual trigger as the rest (`.greptile/README.md` has the reasoning). So every finding on a PR is
one somebody decided to go looking for.

**But silence does not run the other way.** A bot that posted nothing is *consistent with* a
chain that never ran and is never proof of one: a chain that ran clean leaves no artifact too. Gemini usually fails
without producing a report on the free tier (`/ship` says so outright), Copilot's request
confirmation false-negatived on **four** consecutive PRs while the review was already being
written, and a Greptile review has landed on a PR while the tooling reported no summary at all.
That Copilot count is repaired — the confirmation now reads the issue timeline instead of reading
`requested_reviewers` back — so read the four as what the failure class cost, not as a live defect.
Track what you triggered and read its `wait` result; never infer from the silence whether a review ran. `/squash-merge`'s "every review
that was triggered has answered" is the same rule stated where it blocks a merge.
§2's "never leave a finding unanswered" is mechanically enforced for **every** bot — the reply *is*
the record, so an unanswered finding reads to the tooling exactly like one nobody ever collected —
and `/squash-merge` refuses to merge while any of them lacks a threaded reply. See §4 for the
command.

**Governing principle: a bot finding is a hypothesis, not a defect.** Verify every one against the
actual code before acting on it or replying to it. The record from PR #26 (seven findings across
both bots) is the calibration: five were legitimate, one was invalid — it contradicted the owning
ADR — and one paired a correct observation with a remedy that would have caused a regression.

## 1. Triage each finding

Read the cited `file:line` and the surrounding code. Never rely on the bot's paraphrase of the
code, and never accept a finding because it is confidently worded. Classify each:

- **Legitimate** — the defect is real and the suggested fix is sound.
- **Legitimate observation, wrong remedy** — the fact is right but the proposed change would break
  something. Fix it another way, or decline the remedy and say why. (#26: Copilot's exact-match
  fast-path for `resolve_biomarker_name` rested on a true O(N×M) observation, but the fix would
  have skipped the union-namespace scan that detects the >1-match ambiguity the resolver fails loud
  on — defeating a tested safety property.)
- **Invalid** — contradicted by an owning ADR, a spec, or the code itself. Decline with the
  citation. (#26: CodeRabbit wanted the `?category=` lookup made case-sensitive; ADR-0055 §1
  mandates case-insensitive resolution, and the change would have broken the tests asserting it.)

Checks a bot routinely misses — run them yourself:

- **Under-reporting — enumerate the peers before you fix, and show the search.** A reviewer flags
  one instance of a pattern that occurs several times. (#26: Copilot found one out-of-order
  category pair; there were two.) So a finding of the form "X is wrong *here*" is not a work item
  for one site; it is a work item for the rule. Before editing, search the repository for the claim
  or pattern with **`git grep -nF --untracked -- '<the exact claim>'`** — and **report the command and
  its hits alongside the finding** — the *whole* hit list, not a truncated view of it: a `head` on
  the verification command is how a fourteen-site rule gets written down as three, which happened
  here, in the evidence for this very bullet. Include peers the reviewer did not name. Every part
  of that spelling defends against a way the search *lies* rather than fails, and §1a records what
  each one costs when it is missing. A search that cannot run, or that answers "one site" because it never
  looked, is worse than none: it puts a green record behind the gap. Then decide each
  peer deliberately: sameness of *shape* is not sameness of *meaning*, and a peer that legitimately
  differs is recorded as differing, not swept in. Past two sites, prefer one named mechanism over
  N hand-written copies, and put the reason it exists where the next author will read it. Run the
  search again after the edit; you are done when the only hits are the intended ones. **A fix that
  addresses only the site the reviewer named is not finished**, and the search is what makes that
  checkable — "remember to look for peers" is not. This check is **not bot-specific**: it binds
  findings from `/code-review` reports and from the local `spec-reviewer`/`test-reviewer` rounds
  identically, and `/apply-review` step 3 cites it rather than restating it. Both known
  recurrences were expensive — one rule went unimplemented at a fresh site in four consecutive
  rounds of `scripts/bot_review.py`'s error floor before it became a shared helper, whose
  `_floor_on_error` docstring names all four sites, and a corrected claim in this repository's own
  prose survived in a nearby paragraph of the very file the finding named.
- **Inverted diagnosis.** Establish whether the *code* or the *comment/spec* is the thing that is
  wrong. (#26: the category seed order was correct — it mirrored ADR-0055 §6 — and the comment
  claiming "alphabetical" was the error, so the bot's suggested reorder would have been the wrong
  fix, silently diverging the seed from its ADR.)
- **Spec conflict.** Before accepting, check the owning ADR. A "fix" that contradicts an Accepted
  ADR needs a superseding ADR, not a code edit (CLAUDE.md, ADR governance).
- **Scale and threat model.** A performance or concurrency finding is only real at the scale and
  under the access model this system actually has. Say which, with numbers, rather than accepting
  or dismissing on instinct.

## 1a. Verify your own tooling, not just the findings

Fetching a review is not the neutral step it looks like. Identity strings, timestamps, scoping and
pagination all fail *silently* here — six separate bugs on PR #27, three of them introduced by the
previous round's fix — so that logic now lives in `scripts/bot_review.py`, under
`tests/test_bot_review.py`, instead of in prose that cannot be tested. **Use the script; do not
re-derive its rules by hand at the terminal.** Each rule it encodes is a fact about a live API,
recorded there with the failure it prevents.

What generalizes beyond that script — the first four from its fetching bugs, the last from the
peer search §1 prescribes, because "your own tooling" is not only the code that talks to the API:

- **A success code is not proof.** Verify the *state* a call was supposed to produce, not its exit
  status. Requesting a reviewer GitHub does not accept returns `200` and adds no one — a no-op no
  `|| report` guard can catch.
- **An empty result is a claim, and claims get checked.** Prove it is nothing before reporting it,
  against something independent — a review body that says `generated N comments` while your fetch
  found none means one of you is wrong, and it may not be the bot.
- **A correct check over wrongly-scoped input is its own failure mode.** The count cross-check run
  against a pull-level fetch compares this review's `N` with every run's comments and confidently
  reports a filter bug that does not exist.
- **When a cross-check trips, investigate — do not assume whose fault it is.** CodeRabbit has
  claimed 2 while posting 1, having counted before deduplicating. The check's job is to make you
  look, not to name the culprit.
- **A search recipe is tooling too, and it lies the same way.** §1's peer search is spelled
  `git grep -nF --untracked -- '<claim>'`; each part was paid for here. **`git grep`** because `grep -rn` does
  not exist in this environment's primary shell, descends `.venv/` from the repo root, and needs an
  `--include` list that goes stale — a `*.md`/`*.py` pair silently drops `ci.yml` and
  `pyproject.toml`, so a rule living in CI config reads as "no peers". **`--untracked`** because
  plain `git grep` searches the *index*, so a brand-new unstaged file holding a peer is invisible —
  measured, 0 hits against 1 for the same probe. With it the domain is tracked plus
  untracked-but-not-ignored, which is exactly the set `scripts/check_spec_links.py` calls git-truth
  and gates on for the same reason; `.gitignore` still applies, so `.venv/` stays out. Its one cost
  is the flip side of that domain — an untracked scratch file that happens not to be ignored can
  surface as a "peer", and deciding it differs is the judgement the paragraph below already asks
  for. **`-F`** because a claim is text, not a pattern: `git grep -n "check() == []"` exits fatal on `Unmatched [ or [^` where the `-F` form
  searches it literally. **`--`** because a claim may begin with `-`. **Single quotes** because a
  claim's backticks otherwise reach the shell — measured, bash tried to execute a markdown file's
  contents as commands and returned five unrelated matches **at exit 0**, this section's silent
  failure wearing a green record. Neither obvious escape is one: the harness Grep tool keeps the
  shell out and is still a regex engine with **no literal mode**, rejecting that same claim. For an
  apostrophe-bearing claim, heredoc it into a variable and keep the command whole —
  `git grep -nF --untracked -- "$claim"`; dropping the `-F --` on the way reinstates both failures.

Silence is the failure mode to distrust most: a wrong answer argues with you, a silent one doesn't.

## 2. Reply on the PR

Post one threaded reply per finding:

```bash
gh api repos/{owner}/{repo}/pulls/{n}/comments \
  -f body="..." -F in_reply_to={comment_id}
```

- **Declined** — give the reason and cite the ADR/spec section. When the observation is valid but
  the remedy isn't, say both: concede the fact, then explain what the fix would have broken.
- **Accepted** — reply after the fix has landed so the commit SHA in the reply is real.

Never leave a finding unanswered. An unaddressed comment is indistinguishable from an overlooked
one, to both a reviewer and to the next person reading the thread.

**A finding with no comment object cannot take that threaded reply.** Two real shapes: Greptile can
leave a finding only as prose in its summary comment (PRs #72, #73) — with or without its
empty-bodied review object alongside, which is why the summary acknowledgement below also clears a
summary-comment bot's zero-matched review alarm (ADR-0067 §2) — and Gemini renders findings it
cannot anchor to a diff line as bullets in its review *body*. Answer those in a **PR-level issue
comment** that states your verdicts — same verification bar as any threaded reply — and carries the
machine-checkable reference (ADR-0067):

```text
Acknowledges <bot> (summary|review) <id>
```

`<bot>` is the bot key as passed to `--bot` (`greptile`, `gemini`); the kind is `summary` for a
summary comment, `review` for a review body; `<id>` is the artifact id — §4's gate prints the exact
string to post, so never compose it from memory. The reference must **own its whole line** inside
the prose that does the answering — nothing else on the line beyond an optional final period; one
comment may acknowledge several artifacts, one line each. A reference quoted mid-line, in a
blockquote (`>`), or with the banner's surrounding text still attached to the line is deliberately
not credited: the gate's own refusal contains a valid reference, and pasting that banner into a
comment to ask about the blocker must not clear the alarm it quotes, however the paste wraps. The gate credits an acknowledgement only
when the comment is authored by a non-bot login and **created** after the artifact it answers —
editing a comment later neither re-dates nor revives it. A Greptile re-review edits its summary in
place, which moves the summary's timestamp past every earlier acknowledgement — deliberately: new
prose is a new decision, so the gate re-blocks until the new summary is answered.

## 3. Report, then stop

Report a verdict table to the user — finding, severity, verdict, action — with the reasoning for
anything declined, and flag where the bot was wrong in an interesting way (it calibrates how much
to trust the next one).

**§1's peer search needs a home here too, for the same reason the reviewer rounds do below.** The
table is one row per finding and the search is per finding, but it is two values rather than one —
the command with its hit count *before* the edit, and the re-run count *after* — so record it as
its own line under the table: for each finding that named a site, the search that was run, how many
sites it returned, and which of them were fixed versus judged legitimately different. Without it a
round that fixed one of four peers reports exactly like a round that fixed the only one, which is
the state §1 was rewritten to make visible.

**Do not change code without the user's explicit go.** Verification and replies are automatic;
fixes are not. When the go comes, the sequence is **fix → gates → reviewer rounds until they
settle → gates again if those rounds edited anything → push → replies**. "Gates" is the full
`/land`/`/ship` set, including `uv run python scripts/check_spec_links.py`, since Markdown edits
outside `specs/` can break spec-link targets. The replies come last so each "fixed in `<sha>`"
reply names the commit that survived every round.

**The reviewer rounds in that sequence are not optional, and their position in it is the point.**
§4's "stale, not clean" cuts both ways: a bot fix diverges the reviewed state from the landed
state exactly as an `/apply-review` round does, and the pass that ran before `/land` never saw
it. If you run them after the push instead, the replies name a commit the next round supersedes —
§2's "reply after the fix has landed so the commit SHA in the reply is real", broken by its own
tooling. Let them default to the whole branch rather than scoping them to the fix; the
interactions between a fix and the code already on the branch are what the re-run is for.
All of `/apply-review` step 5's operational rules apply here unchanged, **and so does step 6's
reporting obligation**: record how many rounds ran, each round's verdict, and **which mode each
ran in** — isolated, or the sequential live-tree fallback. Step 5 owns how to run them and step
6 owns how to report them, so inheriting only step 5 leaves a bot-fix round whose isolation
setup failed recording a bare "pass"; `/squash-merge` never asks about the local reviewers at
all, and `/land` step 6 — the one consumer that does ask — is not in this file's sequence. A
fallback-only pass would then be counted as equivalent to an isolated one, which
`.claude/reviewer-isolation.md` explicitly forbids. §3's table is one row per **bot finding**
and has nowhere to put this, so record the rounds as their own line beneath it — how many ran,
each one's mode, each one's verdict — rather than trying to fit a round into a finding's row.

Read `/apply-review`'s bare step numbers as **its** steps, never as the sections of this file:
"re-runs step 4's gates" there means the ruff/pyright/pytest gate run, not §4 below — which is
the bot re-review chain, whose invocation policy is [ADR-0074](../specs/adr/0074-bot-review-invocation-policy.md).

## 4. After the fixes land

Nothing re-reviews a push on its own — Greptile included, whose `triggerOnUpdates` is deliberately
`false`. Re-reviewing the new commit is a fresh run of the bot's own skill (`/coderabbit-review`,
`/greptile-review`, or `/copilot-review`). **Run one when the work calls for it
and say so; do not ask permission first** — [ADR-0074](../specs/adr/0074-bot-review-invocation-policy.md)
bounds these by repetition rather than authorization, so what is forbidden is a re-trigger inside a
loop, or a third run on one PR without a stated reason, not the invocation itself. **`/gemini-review`
is the exception and is named separately for that reason**: it stays user-named, on the exhaustible
free-tier quota grounds ADR-0074 carves out, which were never about cost. **A review that
ran before the fixes is stale, not clean** — it reports on code that no longer exists. Greptile
names the commit it reviewed and `bot_review.py` checks it against the PR head, so a stale one is
reported as such rather than read as this commit's verdict; for the others, staleness is yours to
track. When every review that ran is clean or triaged and the user asks for the merge, finish the
chain with **`/squash-merge`** — it composes a clean squash message and verifies the result on
`origin/main`.

You can check §2's "never leave a finding unanswered" mechanically at any point, for every bot at
once:

```bash
uv run python scripts/bot_review.py outstanding --pr <N> --since <PR createdAt>
```

Exit 2 means nothing is unanswered; exit 0 lists what is. Exit 1 means either the sweep found a
zero it could not prove — printed under a `CANNOT CLEAR THE GATE` banner — or it failed outright
(`gh` auth, a timeout, a usage error), in which case there is no banner and the gate never ran.
Both stop a merge, but only the first is a statement about the PR. `/squash-merge` runs this as a
precondition and stops on either non-2 code. It reads the reply threads rather than your
recollection of them, and it counts a reply from the bot itself as no reply at all — CodeRabbit
acks threads routinely, and a bot agreeing with itself is not triage.

When the banner names findings that live where no reply can reach — a summary stating more
findings than its comments, a summary that does not read clean while nothing at all was posted to
match, a review rendering findings in its body, or (for a summary-comment bot only) a review whose
inline comments never landed — the exit is §2's acknowledgement, not an
override: the refusal line prints the exact `Acknowledges <bot> (summary|review) <id>` string, and
a PR-level comment answering those findings and carrying it clears the gate on the next run
(ADR-0067). The same authorship rule applies as for threaded replies — an acknowledgement from any
bot login counts as no acknowledgement at all.

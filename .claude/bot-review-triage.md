# Bot-review triage

The shared procedure for handling an automated PR review. Used by `/ship` (CodeRabbit) and
`/copilot-review` (GitHub Copilot). Both bots are useful; neither is trusted.

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

- **Under-reporting.** The bot may flag one instance of a pattern that occurs several times. (#26:
  Copilot found one out-of-order category pair; there were two.)
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

The same skepticism applies to the commands this procedure runs. Two failure modes, both learned
the hard way on PR #27:

- **A success code is not proof.** Verify the *state* a call was supposed to produce, not its exit
  status. Requesting a reviewer GitHub does not accept returns `200` with an empty
  `requested_reviewers` — a silent no-op that a `2>&1 || report` guard never catches, and that then
  costs a 30-minute poll and a false "no review arrived".
- **An empty result is a claim, and claims get checked.** If a query for findings returns nothing,
  prove it is nothing before reporting it. Cross-check against something independent: the review
  body states `generated N comments`; a mismatch means the filter is wrong, not that the review was
  clean. `/copilot-review` filtered comments on the review's author login and silently returned
  empty on a review that had findings — the count cross-check is what caught it.

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

## 3. Report, then stop

Report a verdict table to the user — finding, severity, verdict, action — with the reasoning for
anything declined, and flag where the bot was wrong in an interesting way (it calibrates how much
to trust the next one).

**Do not change code without the user's explicit go.** Verification and replies are automatic;
fixes are not. When the go comes, fix, re-run the gates, push, and only then post the "fixed in
`<sha>`" replies.

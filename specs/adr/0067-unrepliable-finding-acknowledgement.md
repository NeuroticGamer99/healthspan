# ADR-0067: PR-Level Acknowledgement Clears Unrepliable Bot Findings

## Status
Proposed

## Context and Problem Statement
The merge gate (`scripts/bot_review.py outstanding`, run as a `/squash-merge` precondition) refuses to merge while any bot finding lacks a threaded reply from a non-bot login. The reply is credited through `in_reply_to_id` — a field that exists only on **pull-request review comments**. Two real finding shapes have no comment object at all, so no reply is physically possible:

- **Greptile summary-prose findings.** A findings run can post no review object and no inline comment, leaving the finding only as text in its summary issue comment (PR #72, and again on #73 — it recurs). Note the evidentiary caveat for #73: a re-review edits the summary in place, so the earlier state survives in the gate's own refusals at the time, not in the comment as it reads later — which is itself a property this ADR's re-arm rule leans on. The gate's `undercounted_summaries` detector sees a stated count exceeding the matched comments and blocks under `CANNOT CLEAR THE GATE`.
- **Gemini body-rendered findings.** Anything the Antigravity workflow cannot anchor to a diff line becomes a bullet in the review *body* (and its HTTP-422 fallback re-posts every finding that way). The `body_only_findings` detector blocks on the same grounds.

Both detectors are correct to block — a zero the sweep cannot prove must not clear a merge — but nothing the triager does can ever unblock them. The triage procedure (`/greptile-review` step 4) already prescribes the only possible answer: read the summary on the PR and answer the findings there, as a PR-level comment. The gate counts *comment objects*, not answers, so it cannot see that answer, and the only exits are overriding the gate (free, unrecorded) or re-triggering a metered bot (spent, and it may reproduce the same shape). This ADR closes that loop: it defines the machine-checkable form of the answer the procedure already requires.

This is the first change that **loosens** the gate. Every prior extension of `bot_review.py` (#60, #62, #63, #71, #72) tightened it, and tightening a safety mechanism is self-justifying; permitting something previously refused is not. No ADR records the gate's contract — the one piece of automation that can block or permit a merge — so the loosening decision is recorded here rather than made silently in code.

## Decision Drivers
- The gate must stay closed over findings nobody has read; the fix must not create a cheaper way to wave a real gap through
- The answer the triage procedure prescribes must be one the gate can credit — a procedure whose correct execution still blocks the merge trains people to override the gate
- Re-triggering a bot is metered and requires explicit authorization; a permanently blocked gate makes that spend, or an unrecorded override, the routine exit
- A bot must never clear the gate on its own say-so, under any of its logins — the invariant `answered_ids` already enforces for threaded replies
- Every future bot added to `BOTS` must inherit a defined answer to "can its unrepliable findings be cleared, and how"

## Considered Options
1. Status quo — override the gate manually when the banner names a prose finding
2. Re-trigger the bot until the findings land as comment objects
3. **A machine-checkable PR-level acknowledgement, credited by the gate** (chosen)

## Decision Outcome
Chosen: **option 3**. Option 1 leaves the override unrecorded and the gate worthless for exactly these findings — an override that becomes routine is a gate that no longer exists. Option 2 spends metered reviews to change an artifact shape the bot may simply reproduce, and it re-reviews code that has not changed. Option 3 makes the already-prescribed answer visible to the gate, under conditions that keep it as strong as the threaded reply it substitutes for.

### 1. The acknowledgement contract
An **acknowledgement** is a PR-level issue comment that clears one unrepliable artifact when all three conditions hold, each closing a distinct hole:

1. **Authored by a non-bot login** — excluded via `bot_logins()`, the same exclusion `answered_ids` applies through `not_by`. A bot must never ack itself (CodeRabbit acks threads routinely), and the exclusion spans every configured bot's every login, so neither a second identity nor a *different* bot can stand in for a person.
2. **Posted after the artifact it answers** — the acknowledgement's `created_at` must be strictly later than the artifact's timestamp. *Posted* means creation time deliberately: an acknowledgement is a decision made when it is written, and a later edit — a typo fix, an added link — neither re-dates nor revives it. On `updated_at`, editing a stale ack after a re-review had invalidated it would resurrect the old decision against prose it never read. The artifact side **is** `updated_at`: a Greptile re-review edits the summary **in place**, which moves that timestamp past every earlier acknowledgement and deliberately invalidates them — new prose is a new decision.
3. **Carrying an explicit reference naming the artifact**, in the form `Acknowledges <bot> (summary|review) <id>` — `<bot>` is the bot's spec key (`greptile`, `gemini`), the kind is `summary` for a summary issue comment and `review` for a review body, and `<id>` is the artifact's id. Matched case-insensitively but only when the reference **owns its whole line** — anchored at both ends, with separators that do not span lines, and at most a single trailing period (prose ends that way; anything else on the line disqualifies it). Line ownership is load-bearing: the gate's refusal messages print the exact reference to post — so the triager never composes it from memory — which means a comment merely *quoting* a refusal (pasting the banner to ask about the blocker) contains a valid reference. A start-of-line anchor alone is not enough, because a hard-wrapped paste can land the reference at a line start with the banner's trailing text still attached; requiring the whole line closes that shape too. The reference sits on its own line inside the prose that does the answering, and one comment may acknowledge several artifacts, one line each. The id requirement is what stops a passing "LGTM" from clearing a real gap.

The acknowledgement clears the **artifact**, not individual findings within it: the prose findings have no individual ids to name, so the comment that carries the reference is expected to answer all of them, exactly as `/greptile-review` step 4 already requires. The gate's refusal messages print the exact reference string to post, so the triager never composes it from memory.

### 2. Scope — the merge gate's detectors, not the triage commands
Three detector sites honor acknowledgements, and the boundary is part of the decision:

- `undercounted_summaries` (the Greptile shape) — cleared by a `summary` acknowledgement. The alarm cannot distinguish prose-only findings from batching or a partial filter miss, and the acknowledgement does not pretend to: what it asserts is that a person read the summary and answered everything it states, which resolves the ambiguity at the trust level a threaded reply already gets.
- `body_only_findings` (the Gemini shape) — cleared by a `review` acknowledgement naming the review whose body carries the findings.
- `unmatched_reviews`, **for a `summary_marker` bot only**, cleared by the same `summary` acknowledgement rather than one of its own. That bot's findings review is empty-bodied by design, so a findings run whose inline comments never land trips this detector on the identical prose-only shape (PR #72 one step over — a review object present, zero comments), and the artifact a person reads and answers is the summary either way; a second acknowledgement naming a body-less review would be ceremony without content. For every other bot this detector honors nothing: zero-matched there means the author filter missed, and no PR-level comment makes unread comment objects read.

`silent_always_reviewers` honors nothing for any bot — silence leaves no artifact to read, so there is nothing an acknowledgement could honestly assert. The per-bot `wait`/`fetch` commands do **not** credit acknowledgements either: their job is collecting and triaging a run, where an unread prose finding is work to surface, not a merge question to settle. `fetch`'s refusal to classify an undercounted summary remains a prompt to go read it.

### 3. Trust model
Any non-bot login can post an acknowledgement, sight unseen by the gate. This is accepted deliberately: it is exactly the trust already extended to threaded replies, where any non-bot reply — whatever its content — marks a finding answered. The acknowledgement is not weaker than the mechanism it substitutes for, and at this repository's single-maintainer scale the author set is the owner. A multi-maintainer deployment that needs authorship restrictions is a revisit trigger, not a knob shipped ahead of the need (the ADR-0049 §4 posture).

### Positive Consequences
- The gate becomes clearable by doing what the triage procedure already prescribes, so the correct workflow and the mechanized one stop diverging
- The previously terminal `CANNOT CLEAR THE GATE` shapes now have a recorded, auditable exit — a PR-level comment on the PR itself — instead of an unrecorded override or a metered re-trigger
- A Greptile re-review automatically re-arms the gate for its edited summary, because the in-place edit moves the timestamp the ack must postdate
- Future bots inherit a defined contract for unrepliable findings rather than each forcing this decision again

### Negative Consequences / Tradeoffs
- The gate now accepts an artifact-level answer where threaded triage is per-finding: one acknowledgement can cover several prose findings, and nothing verifies each was addressed. Bounded by the same trust model as replies, and the reference's id requirement keeps the ack deliberate
- The acknowledgement clears the undercount and unmatched alarms **whatever their cause**. An identity drift that leaves real, threaded comment objects unmatched is therefore no longer a hard block for a summary-marker bot once its summary is acknowledged — accepted because the person acknowledging has, by the contract, read the summary those findings are counted in, and because keying the ack on a cause the detector itself cannot establish would re-create the permanent block for the observed real shapes. Identity drift for every other bot remains unclearable, and the drift alarm's zero-matched trigger is unchanged
- A stale acknowledgement silently stops counting after a re-review edits the summary — correct, but the triager must notice the gate re-blocking rather than being told the ack expired
- The exact reference string becomes a small parsing contract; a wording drift in a hand-typed ack (wrong kind, wrong id) fails closed, blocking the merge until corrected

## Consequences for Other Documents
- **`.claude/bot-review-triage.md`**: §2 documents the acknowledgement form beside the threaded-reply command; §4 explains how it clears the `CANNOT CLEAR THE GATE` banner
- **`.claude/skills/greptile-review/SKILL.md`**: step 4's "answer them there, as a PR-level comment" names the reference that makes the answer creditable
- **`.claude/skills/squash-merge/SKILL.md`**: the banner description notes that acknowledged artifacts no longer block

## Links
- Related: [ADR-0064](0064-reviewer-workflow-trust-boundary.md) — the only prior ADR touching `bot_review.py` (Gemini run attribution); this ADR records the gate contract that had no owner
- Related: [ADR-0045](0045-repository-workflow-and-ci-enforcement.md) — the workflow-enforcement posture the merge gate extends

# ADR-0073: Operator-Handoff Presentation, and the Peer-Site Search as a Required Output

## Status
Proposed

## Context and Problem Statement
Two rules in the harness prose were failing in the same way, which is why one decision records both.

**The first is presentation.** Five skills end by handing the user something to act on — a path to open, a command to run — and each states *that* it should be surfaced without stating *how*: `/land` step 7 said "print both paths", `/review-handoff` step 3 said "resolved and absolute", `/review-prep` step 3 said "the path of the carrier file", `/apply-review` step 1 quoted `/review-handoff`'s behaviour secondhand, and `/ship` step 2 said "name the file". Three of the five — `/land`, `/review-prep`, `/ship` — named neither absoluteness nor form, and `/apply-review` named absoluteness only in a clause quoting `/review-handoff` while its own fallback instruction named neither. The spread is the evidence: the presentation was never decided anywhere, only written five times in five vocabularies.

That gap is not cosmetic, because of one property of the surface. The session scratchpad root carries a **per-session UUID** (`…/claude/<project-slug>/<session-uuid>/scratchpad`) that exists nowhere the reader can look up. A scratchpad path abbreviated with an ellipsis is therefore not inconvenient, it is unusable — no care at the receiving end reconstructs the missing segment. A prose-embedded absolute path is the milder version: correct, present, and still requiring re-typing, because the VS Code chat webview renders assistant prose as unselectable text while a fenced block gets a hover *copy* button. Measured 2026-08-10 on the branch that became PR #86: `/land` step 7 reported the composed commit message as `…/scratchpad/commit-msg/fix/copilot-request-confirmation.txt` mid-sentence, and the user had to ask for it again.

**The second is peer-site enumeration.** `.claude/bot-review-triage.md` §1 already carried the observation *"the bot may flag one instance of a pattern that occurs several times"*, and `/apply-review` step 3 already said to implement the smallest correct change. Neither demanded anything checkable. Two measured recurrences followed. One rule went unimplemented at a fresh site in **four consecutive review rounds** of `scripts/bot_review.py`'s error floor — each fix correct, each written to the finding rather than to the rule — and the loop ended only when the guard became the shared `_floor_on_error` context manager, after which "which calls are covered" is greppable rather than established by reading three functions. Separately, a corrected claim in this repository's own prose survived in a nearby paragraph of the very file the reviewer's finding named — a file that had been read in the same session — and both local reviewers caught it.

The two share a shape: **an instruction that states an awareness where it should demand an output.** "Print the path" and "the bot may under-report" are both satisfiable by a session that does nothing observable. Prominence is not the missing ingredient — the under-reporting bullet was already in the right file and was read — so the fix in both cases is to name the artifact the instruction must produce.

## Decision Drivers
- A rule stated in five vocabularies is a rule nobody decided; the repository already has the answer to that in `.claude/reviewer-isolation.md`, whose callers cite it rather than restate it.
- Adding a sixth prose copy would be the restater pattern this repository has paid for before — a drifted restatement in `.claude/bot-review-triage.md` survived a clean grep during a review round and had to be found by reading.
- An instruction whose satisfaction is unobservable cannot be reviewed. The two edits below both convert one into an instruction whose output a reviewer can look for.
- Neither rule is mechanizable **as a whole**, and both have an enumerable half that is. Judging whether a path in conversational output was one the operator needed to act on, or whether two code sites are the same case, is exactly the content-half judgment [ADR-0070](0070-personal-data-containment-gate.md) declined to mechanize. Judging whether five named files still contain a given string is not, and ADR-0070's actual shape is the model: it mechanized the **enumeration** half and left the content half to authors.
- The presentation rule governs a surface (the chat webview) that no repository artifact reads, so its home has to be harness prose rather than `specs/`.

## Considered Options
1. **Fix the five sites in place, in their own words** — the status quo continued.
2. **Put the presentation contract in `CLAUDE.md`** so it binds every session, not only skill-invoked ones.
3. **Make `/review-handoff` the authority** and have the other four cite it.
4. **A new `.claude/operator-handoff.md`, cited by all five** — chosen.
5. **For the second rule, state it in both `.claude/bot-review-triage.md` §1 and `/apply-review` step 3 independently** — rejected as a second copy of a rule already failing at full strength.

## Decision Outcome

### 1. `.claude/operator-handoff.md` is the single authoritative statement
It defines a **handoff target** — a path or command the user has to open, copy, or run — and requires four things of one: **absolute** (for anything outside the workspace, which every scratchpad artifact is), **resolved** (no placeholder of any kind survives into what the user sees — stated as a shape rather than a list, because a closed enumeration let `/code-review <effort>` through as a literal template that satisfied every named rule), **alone in its own fenced `text` block** (one target per block, so a triple-click or the copy button selects exactly the thing), and **never elided** into prose or behind an ellipsis. It states the two reasons — the copy button and the unreconstructable session UUID — and bounds itself: paths named in explanation, paths written into artifacts read by machines, and multi-line recipes already fenced are all outside it.

`/land`, `/review-prep`, `/review-handoff`, `/apply-review` and `/ship` cite it — named without step numbers, the same way `.claude/operator-handoff.md` names them and for the same reason: nothing gates an ordinal, and this change renumbered two skill sections while it was being written. Each keeps only what is genuinely local — `/review-handoff` step 3 still specifies that its hand-off block contains `/apply-review` plus the quoted path and nothing else, because that is content, not presentation.

**The contract changed one downstream justification, and that change is part of the decision.** `/review-handoff` step 3 previously fenced only its hand-off command, so its bare path was decorative and a user wanting to *open* the report was expected to copy the command and strip the `/apply-review "…"` wrapper. Under the contract both are copyable blocks and the stripping step is gone; the paragraph explaining why the repetition is deliberate now says so.

### 2. Under-reporting is promoted from observation to a required search
`.claude/bot-review-triage.md` §1 owns the statement. A finding of the form "X is wrong *here*" is a work item for the rule, not the site: before editing, search for the claim or pattern across the file types that could carry it, and **report the command and its hits alongside the finding**, including peers the reviewer did not name. Each peer is then judged deliberately — sameness of shape is not sameness of meaning, and a peer that legitimately differs is recorded as differing rather than swept in. Past two sites, prefer one named mechanism over N hand-written copies. Re-run the search after editing; the work is done when the only hits are the intended ones.

The check is **not bot-specific**. It binds findings from `/code-review` reports and from local `spec-reviewer`/`test-reviewer` rounds identically — which matters, because the four-round instance above came from `/code-review` and not from a bot. `/apply-review` step 3 cites it as a step of its own rather than restating it, and step 6's per-finding table gains the search and its site count as a reported column, so the obligation has somewhere to land.

Two adjacent clarifications were forced by the promotion. `/apply-review` step 3's **"smallest correct change"** now says explicitly that "smallest" constrains the remedy and not its scope — the smallest correct change to a rule broken at four sites still touches four sites — because read the other way that line licenses exactly the failure being fixed. And a `fixed` row with no search behind it is named as the shape of a rule patched at one site.

### 3. The enumerable half of both rules is gated
`scripts/check_doc_citations.py` asserts, per owning document, that the document exists and that every file registered as citing it still cites it — six citations across two documents today (`.claude/operator-handoff.md`'s five callers, and `/apply-review`'s citation of `.claude/bot-review-triage.md` §1). It runs in the `docs-consistency` job beside `scripts/check_reviewer_agents.py`, whose citation assertion is the same shape for `.claude/reviewer-isolation.md`.

**"Still cites it" is the document's path plus, for some rows, a registered *needle* — an extra literal the citation must carry.** The path alone proved insufficient the first time it was relied on: `/apply-review` already contained `.claude/bot-review-triage.md` before this change, in an unrelated §4 reference, so the row added to gate §1's peer-search rule was satisfied by a mention that had nothing to do with it. Measured, not reasoned: deleting the entire cited step left the gate exiting 0 and reporting six healthy citations. The needle for that row is the bold lead-in of the bullet being cited — not §1's heading, which names the wider procedure and would be no more distinguishing than the path.

**The needle buys detection with a false positive, and the direction that costs is not the obvious one.** Retitling the section without touching the caller produces no reaction — disclosed where it matters, since the gate checks the pointer and never the section number. The costly case is the *coherent* rename: `check()` reads only the caller, never the owning document, so an author who retitles the bullet **and** correctly updates the citation to track it — one edit, nothing broken — drops the needle string and turns the gate red. That is accepted deliberately. A row that fails loudly on a cosmetic edit is recoverable in one commit; a row that passes while the rule it names has been deleted is the defect this whole ADR is about, and it had already happened once.

**This is the half that trading restatement for citation newly puts at risk**, and it is the reason the trade is not free. Five restatements drift in content; one statement plus five pointers cannot, because there is only one copy of the rule — but a skill rewritten in different words can silently drop the pointer, leaving prose that reads correctly and is governed by nothing. That failure is textual and closed-set, so it is gated. The existence assertion matters separately: the citations are code spans rather than markdown links, so renaming or deleting an owning document leaves `scripts/check_spec_links.py` silent while every pointer to it dangles.

What stays ungated is the judgment: whether a given path in conversational output was a handoff target, and whether two sites a finding might govern are the same case. The gate also cannot catch a *new* citing site that forgets both the pointer and its own registry row — it covers drift in a known set, not discovery, the same blind spot `check_reviewer_agents.py` has for a hypothetical third reviewer agent. **The registry is therefore itself a list of where the copies are**, which `open-questions.md` warns drifts by the same mechanism it is meant to stop; that entry records why the warning binds the registry's completeness and not its contents, and this ADR claims no more than that.

An **orphan-citation check** — sweep the tree for each owning document's path and flag any file containing it that is not a registered caller — was considered for the discovery half and declined on measurement. It would work only for a document nothing mentions except its callers, and neither document is one. Both already accumulate mentions that are structural rather than incidental — the ADR that decides the rule, the gate that enforces it, the test that pins the gate — and the busier of the two is referenced across most of the bot-review skill family besides. So the check would need a hand-maintained exemption list per document, on day one: a second list guarding the first, against a failure the CI-verified rows already make loud. Those exemptions would grow for exactly the reason the rule exists — writing *about* a rule is not deferring to it. (Deliberately argued without the counts that motivated it. They were measured, and they expire: every new document that discusses this rule changes them, while acceptance freezes this text against correction in place.)

### Positive Consequences
- Five vocabularies collapse to one contract plus five citations; a sixth caller inherits it by citing rather than by being remembered.
- Both rules now produce something a reviewer can look for: a fenced block, and a search with its hits.
- The `.claude/` tree gained markdown-lint and link-check coverage in PR #82, so the new doc is gated for form even though its content is not.

### Negative Consequences / Tradeoffs
- A sixth `.claude/` doc to keep current, and one more file a session must read to know the rules.
- The peer search costs a grep per site-specific finding, including on findings where the answer is obviously one site.
- **Neither rule is enforced where it actually operates.** §3 gates that the citations survive, never that a session obeyed what they point at. Both rules remain author-discipline at the moment of use, and the second has already been broken by an author who had read it hours earlier — the promotion makes the failure *visible in the record*, which is the whole claim, not that it makes the failure impossible. Do not read the green gate as evidence the rules were followed.
- The gate's registry is hand-maintained, so a sixth caller that forgets both the citation and its registry row passes. Adding a caller is now a two-file edit.
- Judging "same case" stays subjective. Two refusals in the `_floor_on_error` sweep correctly stayed floor-free, and sweeping them in would have reversed a considered decision.

## Pros and Cons of the Options

### Option 1 — five in-place fixes
- Pro: smallest diff; no new file.
- Con: leaves five independent statements to drift, which is the condition being fixed.
- Con: the memory of *why* lives in whichever site was edited last.

### Option 2 — `CLAUDE.md`
- Pro: binds every session, including ones that invoke no skill; the session-UUID trap is arguably an unguessable gotcha, which is that file's bar.
- Con: the same file's bar was applied to the under-reporting rule and rejected it as ordinary engineering practice, so the pair would land in two different homes.
- Con: `CLAUDE.md` is loaded into every session's context; a presentation contract with four rules and two rationales is a poor use of that budget when a citation costs one line.

### Option 3 — `/review-handoff` as the authority
- Pro: cheapest; it already carries the rule and the reasoning.
- Pro: no new file.
- Con: inverts the dependency — `/land` and `/ship` would cite a review skill for a rule that has nothing to do with reviews.
- Con: a skill file is loaded when its skill is invoked, so the authority would be invisible to the four callers that are not it.

### Option 4 — a new `.claude/` doc (chosen)
- Pro: mirrors `.claude/reviewer-isolation.md`, an in-repo pattern whose callers cite it rather than restating it. **Two facts there, not one, and fusing them overstates the precedent**: the *skills* that cite that document — three by its own opening's reckoning, four by a plain search, the gap being `/review-prep` citing it for a different rule — are gated by nothing, while `scripts/check_reviewer_agents.py` gates the citation in the two *agent* files. Disjoint sets either way, which is the point; the ambiguity in the count is itself the judgement `open-questions.md` records rather than resolves. So the pattern is proven and its enforcement is not, which is the reason §3 registers this contract's callers explicitly instead of assuming a precedent covers them.
- Pro: gives the next handoff rule a home, rather than a sixth vocabulary.
- Con: a new file, and one more thing that can go stale relative to its callers — the sync risk `scripts/check_reviewer_agents.py` exists to cover for the isolation doc. §3 closes it with the same mechanism; the con was real, and the cost of answering it is a second gate script and its suite.

### Option 5 — state the peer rule in both files
- Pro: visible at both points of use with no indirection.
- Con: two copies of a rule is the restater pattern; this repository has measured a restater drifting and surviving a grep.

## Links
- Related: [ADR-0072](0072-review-pipeline-and-ledgers.md) — the pipeline owning three of the five citing skills. Not an extension: the contract also binds `/land` and `/ship`, which that ADR does not govern, and it changes presentation rather than pipeline structure.
- Related: [ADR-0070](0070-personal-data-containment-gate.md) — the precedent for declining to mechanize a judgment-bearing rule.
- Related: [ADR-0068](0068-reviewer-isolation-worktrees.md) — the cite-don't-restate pattern this follows, and the first to gate its own citations. §3 makes this the second. Whether the two gates should become one is deferred, not rejected, and is recorded with its trigger in [open-questions.md](../open-questions.md) ("Whether the two citation gates should be one") rather than argued here.
- Related: [open-questions.md](../open-questions.md)

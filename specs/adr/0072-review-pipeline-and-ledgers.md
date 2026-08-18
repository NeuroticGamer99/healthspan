# ADR-0072: The Review Pipeline — brief → prep → handoff → apply — and Its Ledger

## Status
Proposed

## Context and Problem Statement
The repository runs two review loops. The **external** loop is `/land` → `/review-prep` → `/code-review` → `/review-handoff` → `/apply-review`, repeated until findings converge, then handed to the GitHub bot lenses. The **local** loop spawns `spec-reviewer` and `test-reviewer` two to four times per apply ([ADR-0068](0068-reviewer-isolation-worktrees.md)). Both were built incrementally around a `/code-review` that predates multi-agent runs, and neither skill mentions a briefing step — yet every high-yield pass of the last three months was briefed, by hand, in chat.

Nine limitations were diagnosed across two multi-agent passes, and the failures share one shape: **a consumer keys on something no producer is obliged to write.** Measured instances, each of which cost real work:

- `/apply-review`'s drift check compares "the report's recorded tree hash" — nothing obligates recording one. The single report that carried it did so because a briefing asked ad hoc.
- `/apply-review` tags its checkpoint `[x<N>]` and is the only step that knows `N`, but nothing records the round number. The fallback — count `code-review-<branch>-*.md` files across sibling scratchpads — returns the wrong answer on a branch that adopted the tag convention mid-flight (it did, on its own branch), on a re-run of `/review-handoff`, and on a review abandoned before apply.
- A review that **skipped** an angle is indistinguishable from one that ran it and found nothing. That one ambiguity is the whole mechanism behind the portability angle going unexecuted in **four consecutive passes**, caught each time only because someone kept a written roster by hand.
- A brief handed over **by path** was edited twice while the receiving session held it. Nothing could tell which revision had been read; the wrong version was inferred, two records were rewritten to something false, and the receiving session was told it had been briefed on a roster it never saw.
- A brief written from recollection rather than from a record misstated a non-execution count and then buried the very angle whose remedy said it needed a dedicated round.

Separately, three defects were found in the current skills by reading the `/code-review` documentation against them, all cheap to fix and all already costing something: `/review-prep` step 1 asserts that `/code-review` "cannot be handed an arbitrary `git diff` range" and rests a whole paragraph on it — **the command accepts a ref range**, so the pinned scope can be enforced rather than recommended; the emitted command inherits a **sticky effort level** across sessions, which is the measured mechanism behind a `high`-vs-`xhigh` discrepancy that survived only because a human transcribed it; and the report's missing per-finding verdicts were misdiagnosed as a reviewer failure when the actual cause is that structured findings are produced only when a **host application** requests them — a terminal or `-p` run reports text.

Finally, the owner needs the loop to be a **budget instrument**. The decision "keep sharpening locally or switch to the GitHub bot lenses" is made per round on quantity and severity, and it is currently made from prose reconstructed on request. Severity self-reported by a reviewer does not carry it: on one branch every finding was labelled "correctness" while actual consequence ranged from a loud `exit 1` to a containment gate silently passing.

## Decision Drivers
- Every field a downstream step reads must be a field an upstream step is **obliged** to write; detection-after-the-fact is the pattern this pipeline keeps paying for.
- The brief's value is concentrated in what a diff cannot produce — the orchestrator's own unchecked assumptions — so the design must make "silently emit a generic brief" impossible rather than merely discouraged.
- The angle history must be readable by a **fresh session and by a skill**, which rules out memory as its home.
- Review artifacts must not tax the review machinery: [ADR-0068](0068-reviewer-isolation-worktrees.md) materializes every tracked file into a worktree per reviewer per round.
- A rule written only in an ADR is invisible to the editor who opens the skill file; the carry-and-cite convention (reasoning here, rule visible where the edit happens) applies to everything below.

## Considered Options
1. **A flag on `/review-prep` instead of a separate `/review-brief`** — rejected: the two run in **different sessions**. The orchestrator holds the prior round's findings and knows what was checked; the reviewer holds the diff. A flag implies one session can do both, which is exactly the assumption that forced one pass's briefing to arrive as pasted chat text.
2. **A brief that auto-composes from `git log` plus the last report** — rejected as the primary mode. It produces something plausible and much weaker, with the weakness invisible because the output looks like a brief. The generate-vs-structure line below is the mitigation.
3. **Two artifacts (brief + carrier) carried through to handoff** — rejected: either can go stale alone and `/review-handoff` must know both. The brief is absorbed instead.
4. **One monotone ledger file, appended per round** — rejected. Nothing in `specs/` is append-per-event; the closest analogue, `open-questions.md`, grew `+465/−86` over 49 commits and things *leave* it. A single file is also the worst case for concurrent branches.
5. **A machine-first `.review/angle-ledger.jsonl`** — rejected. Its stated advantage is false: git merges line by line, so two branches appending different final lines conflict exactly as two branches appending table rows do. It also forfeits the markdown lint and link-check coverage the docs gates just gained, and the PR-diff reviewability that is the ledger's purpose.
6. **`specs/review-angle-ledger.md` at the specs root** — rejected: it puts a review artifact back into the directory that commit `059a435` ("Relocate historical review docs to `specs/reviews/`", PR #38) cleared of review artifacts, against `specs/reviews/README.md`'s explicit "new review reports belong here" rule.
7. **Moving the two branch-keyed carriers to `refs/`-namespaced blobs** — considered and declined; see §9.

## Decision Outcome

### 1. The skill chain
| Skill | Runs in | Produces |
|---|---|---|
| `/review-brief` | **orchestrator** session | the briefing file, **and the round's fragment**, created when it allocates `N` (§5) |
| `/review-prep` | **reviewer** session | the carrier (absorbing the brief) and the exact command to run |
| `/review-handoff` | **reviewer** session | the report, and the round's fragment **filled in** — it does not create it |
| `/apply-review` | **orchestrator** session | the fixes, and the fragment's severity and `Disposition` columns |

Four skills in the table, **five that must change**. The **pipeline** is the first three — brief → prep → handoff — and `/apply-review` is its consumer; but it writes back to the round's fragment, so it is a producer of the ledger even though it produces no review artifact. The fifth is `/squash-merge`, which appears in no row above because it produces nothing for a review, yet it gains the fragment-collapse step (§8) and is the only one of the five gaining **new** machinery rather than a rewrite. Any count of "how many skills must change" is **five**, and dropping the fifth drops the highest-risk item in the plan.

`/review-brief` serves **both** loops. The external `/code-review` pass is primary, but the local `spec-reviewer`/`test-reviewer` smokes are where the measured savings landed (one briefed round went 172k → 85k tokens and produced the first clean report in 24 rounds), and a brief skill that serves only the external pass leaves the more frequent case improvised.

### 2. One carrier per review
`/review-prep` **reads the brief and merges it into the carrier**. From prep onward there is exactly one artifact per review. The brief lives in the **orchestrator session's scratchpad** and is handed over **by path**, by the human, the way every other cross-session value in this chain moves. That is settled here because it is the one artifact crossing a session boundary whose location §8 and §9 do not otherwise fix, and BRIEF-3 would have had to choose silently. The alternative — the brief as tracked markdown in the repository — is rejected on the same argument §8 spends a whole section making about fragments: it would pay every O(tracked-files) gate on every push, and it would put brief prose, which quotes findings and the orchestrator's own unchecked assumptions, permanently on `main`. The accepted cost is the one §9 already catalogues for the other carriers: a reviewer session cannot find a path under the orchestrator's scratchpad without being told it, so a human relays it. The two run in different sessions (§1), so there is no other carrier, which is exactly why the revision stamp in §4 is mandatory. What is prohibited is what happens *after* absorption: from prep onward nothing may reference the brief file, and no downstream step may read it. Referencing it past that point is what leaves a second artifact able to go stale alone, and re-reading it is what exposes the mutable-underneath-its-reader failure a second time.

### 3. The generate-vs-structure line
Two claims are easy to merge by accident — *where* `/review-brief` runs and *how much of the brief it may generate*. The line:

- **Encode** — the section structure (angles / already-verified / settled-with-reasons / reporting bar), the standing heuristics (brief each round at a different angle; the previous round's fixes are the next round's highest-yield surface; check the cheap things yourself first), and a different-angle-each-round nudge.
- **Mechanically fill** — round number, gate results, what changed since the last pass, prior findings applied, diff size, the angle roster read from the ledger.
- **Prompt for, and never substitute** — the priority angle and its rationale, what the orchestrator has already checked itself, and the settled list **with reasons**.

The discriminator: the *heuristic* is recordable and stable, so encode it; the *instance* is not. Nothing derivable from a diff produces the sentence that found two Tier-1 interaction defects — "about twenty guards changed in one pass and I have NOT systematically looked for interactions between them" — because it requires knowing which changes the orchestrator had already scrutinized. A brief that leaves a section empty and states "no priority angle supplied this round" is honest; one that invents three plausible angles wastes ten agents.

### 4. Expiry, stamp, and echo
- **Revision stamp — the hash is in the filename, so the path is the stamp.** `/review-brief` writes the brief as `brief-<round>-<h8>.md`, where `<h8>` is the first eight hex characters of `sha256` over its bytes, and the report **echoes it back**. A stamp stored only *inside* the brief proves the wrong thing — it shows the report echoed what prep read, not that prep read what the orchestrator wrote, because editing the brief updates its self-described stamp too and every downstream check still agrees. Carrying the expected hash out of band would fix that, and would cost a **second carrier** for the one hop §2 says has only one; a value crossing a session boundary with no obliged producer is this ADR's own thesis reproduced. Putting it in the name avoids both: there is still exactly one artifact and one path, and `/review-prep` recomputes `sha256` over the bytes and compares against the name it was given.

  **The substrate is part of the rule, because on this platform the default tooling rewrites it.** The hash is over the file's exact bytes, read and written as **UTF-8 without a BOM, LF line endings**, and both ends must read in binary — `CLAUDE.md` § PowerShell file encoding records that a `Get-Content`/`Set-Content` without `-Encoding UTF8` silently transcodes to Windows-1252, and these briefs are em-dash and arrow prose throughout. A prep that reads with a default-encoding call computes a different digest than the brief was written with, and the mismatch fires on a brief nobody touched. That failure is worse than no check: this comparison is the loud signal that a brief changed under its reader, and a permanently-false alarm trains the operator to ignore precisely the signal the design buys. The same hazard runs the other way if a brief is ever stored in the repository, where `.gitattributes eol=lf` renormalizes line endings between write and read — which is one input to the open question of where the brief lives. **An edited brief no longer matches its own filename**, so the edit is loud instead of invisible, and a revised brief is a *new file with a new name* that the orchestrator hands over deliberately. The incident this closes is the measured one: a brief edited twice under its reader, where the receiving session could not tell which revision it held.
- **Expiry — a byte-level digest, not a status listing.** The brief records what tree state it was written against, and `/review-prep` checks it. **`git status --porcelain` is not sufficient on its own**: it reports each path's *status*, not its content, so a second edit to an already-modified file leaves the listing byte-identical — and an edit to a file the brief already knew was dirty is the most likely post-brief change there is. The digest therefore covers content: `HEAD^{tree}` for the tracked-and-committed half, `git stash create`'s tree for tracked modifications, and `git hash-object` over the untracked-but-not-ignored paths. That triple is not invented here — `.claude/reviewer-isolation.md` invariant 1 already documents it, including the trap that a tree hash silently omits untracked files. A `git status --porcelain` listing is still recorded, as the human-readable companion to the digest, never as the check. **None of this is built yet** — like everything else in this ADR it is specified here and implemented in BRIEF-3/4/5.

  **The untracked leg excludes `specs/reviews/angle-ledger/`, and the formula is unsound without that carve-out** (added 2026-08-17, when BRIEF-3 implemented this and the cycle surfaced). The round's own fragment is untracked-but-not-ignored — exactly the class the third leg covers — and §5 has `/review-brief` create it, then write the brief's `<h8>` into it. So the digest would cover a file containing a hash of the bytes that contain the digest: **no fixed point exists**, and the check would fire on every brief against a tree nobody had touched — the permanently-false-alarm outcome §4 elsewhere calls worse than no check. Reordering does not fix it; only the exclusion does. Recorded in the formula rather than left to the skill because a session implementing prep's half of this check from this ADR alone would otherwise rebuild the cycle. The accepted residual: an edit to the round's own still-untracked fragment between brief and prep is invisible to the digest. The other two legs are unaffected, so edits to already-tracked ledger content are still caught.
- **Anchors — three, not two.** The pinned scope records `HEAD`, `HEAD^{tree}`, **and the base resolved to a commit SHA**. [ADR-0069](0069-local-checkpoint-commits.md)'s `/ship` collapse dangles every pre-collapse commit SHA while the tree hash survives it, which is why the first two are both kept. The third is why a range can be trusted at all: the scope is `<base>...<head>`, and `<base>` is a *ref* — `origin/main` by default ([ADR-0068](0068-reviewer-isolation-worktrees.md)) — which advances while the head anchors stay fixed. A record holding only the head anchors describes a range whose meaning changes underneath it, so prep resolves the base once and the carrier, the report and the fragment all carry that SHA.

### 5. Report obligations
`/review-handoff` must produce all of the following; each exists because its absence was measured.

| Obligation | The failure it closes |
|---|---|
| `Round: N` header field, and the resolved base SHA | Retires the file-counting heuristic, wrong in three measured ways; a ref-only base moves underneath the record |
| Brief revision stamp echo | A brief edited under its reader |
| **Executed-angle roster**, reconciled against the briefed roster | Four consecutive passes silently skipping one angle |
| **"Not covered"** section, for scope | Three rounds of silence read as three rounds of coverage |
| Uncertainty mapping — the brief's numbered uncertainties, answered | The highest-yield brief section, previously ad hoc |
| Per-source **verbatim** capture, with synthesis in a labelled layer, **subject to the containment rule below** | Convergence and conflict are findings, not noise to average away |
| Per-finding **`Assessment`**, from the closed set in §7 | Two findings that were *wrong* had to be written as prose; one was wrong in the dangerous direction |
| Negative results and settled-item challenges | A challenge to a settled item has nowhere to go |
| **Surface** recorded (terminal / `-p` / host app) | Missing structured verdicts misdiagnosed as reviewer failure |
| **Scope echo** vs the range prep asked for | Nothing checked that a review honoured its scope |
| **Fragment path** — the ledger fragment this round wrote | A cross-check, not a necessity: under §8's naming the path is computable from branch and `Round: N`, so a recorded value that disagrees is a signal something went wrong |

The executed-angle roster and the "not covered" section are **distinct**: one is about angles run, the other about scope reached.

**Verbatim capture stops at personal data, and the fragment is where this bites.** `CLAUDE.md` confines personal health values, results, diagnoses, medications and identifying information to `specs/personal/`. Neither half of the risk is new — verbatim capture is an old obligation and containment is an old rule — but **this ADR creates the combination**: before it, a report lived in the session scratchpad, untracked and never pushed, while a fragment and a digest are committed and travel to a public remote. So: a review source quoting personal data is recorded by **data category alone** — never the value, and **never the path**. `CLAUDE.md` is explicit that "the provenance or sequence of the owner's actual records — which lab, which panel, in what order — is personal even with no values attached", and a path under the containment directory *is* that provenance: it names the lab and the panel in the filename. An earlier draft of this rule said "by path and data category", which would have published exactly what the rule exists to withhold — a containment defect inside the containment remedy, recorded here rather than quietly corrected because it is the second time on this branch that a fix has been aimed one step past where the property still holds. Where a reference must be re-findable, use an **opaque source id** the orchestrator can resolve locally, not a path. Quote the finding, not the value, and not where the value lives. The enumeration gate ([ADR-0070](0070-personal-data-containment-gate.md)) will not catch a violation here, because the path is `specs/reviews/`, not `specs/personal/` — this is the content half, which stays human judgement, and the ledger is now one of the places it must be exercised.

**`/review-brief` allocates `N`, and it does so by creating the round's fragment.** A round begins when a brief is issued for the external
loop, so `N` is assigned there and flows brief → prep → handoff → apply unchanged; every later step reads it and none derives it. It
increments by one per **round begun** — not per report written, per apply performed, per brief file issued, or per file found on disk, which
is what the retired heuristic did.

**Allocation writes something, or the number is not durable.** `/review-brief` creates `specs/reviews/angle-ledger/branches/<b6>/round-<N>.md` at
allocation, holding the round's scope and angle roster and nothing else; `/review-handoff` fills it, `/apply-review` amends it. Without that
write, a round briefed and then abandoned before handoff leaves no trace on the branch that `N` was consumed — fragments were previously
written by handoff — and the next brief, especially from a fresh orchestrator session, re-issues the same number to a different round.
Creating the fragment at allocation is also what makes `max(existing)+1` a sound derivation rather than the counting heuristic in disguise:
the files being counted are written *by the allocator at allocation*, not discovered afterwards.

Four consequences follow, each a case the earlier design got wrong:

- A **re-run of `/review-handoff`** for the same round reuses `N` and rewrites `round-<N>.md` in place — reachable because the name is a
  function of `(branch, round)`, with no glob and no month-boundary hazard.
- A **round abandoned before apply** keeps its number and leaves its fragment, with the analysis recorded as not performed — a different fact
  from a round that found nothing, and a different fact again from a number nobody can account for.
- A **revised brief does not consume a number.** §4 requires a corrected brief to be a new file with a new name, for the same round; since
  `N` is consumed by fragment creation rather than by brief issuance, the revision is the same round and the earlier "increments per brief
  issued" reading — under which every brief correction would silently advance the ledger — does not arise.
- A **local smoke round** consumes no number at all (§7).

The last row is a **cross-check rather than a carrier**, and it is worth saying why the distinction changed. Under the retired write-time grammar the path was genuinely uncomputable by the far side — the fragment's name came from the reviewer session's clock, so `/apply-review`, in the orchestrator session, would have had to glob a shard and guess. §8's identity-derived naming removes that: the path is a function of the branch and `Round: N`, both of which the report already carries. So the field stays, but as a redundancy — a recorded value that disagrees with the computed one is a signal that something upstream went wrong, which is worth more than the value itself.

**The effort level is named by the operator at `/review-brief`, and for an external round the argument is required** — a bare invocation is refused rather than defaulted (settled 2026-08-17, with the skill). Defaulting is what the stickiness makes dangerous: a skill supplying its own level hides the value most likely to be wrong, and the operator gets no signal that the round ran shallower than the last. This is a deliberate divergence from `/review-prep`'s current `Default high`, which predates the brief and is reconciled when prep is rewritten; until then the two agree in practice, since the level the brief prints is the one typed into `/code-review`. A local round has no effort control to set, so the argument is **accepted and ignored** rather than refused — `/review-brief local high` is a harmless spelling of `/review-brief local`, not an error. Stated to the letter because the skill's own grammar says the same and the two are read against each other.

`/review-prep` is to emit the command in full — `/code-review <effort> <base>...<head>` — with the effort level printed explicitly and the reason stated, because the level is sticky across sessions. Its "cannot be handed a range" paragraph is deleted in the same change, and the pin becomes enforced rather than recommended. Present tense throughout this ADR describes the **specified** design; that paragraph still stands in the skill today and is removed by BRIEF-4.

### 6. Severity is consequence-based and applied at apply time
Severity is assigned by the **fixer**, who knows the consequence, not by the reviewer, who reports a category. The scale, highest first:

| Level | Criterion |
|---|---|
| `silent-wrong` | Produces a wrong answer with no signal — including a gate that passes when it should fail |
| `loud-wrong` | Fails, but visibly: a raised error, a non-zero exit, a red test |
| `latent` | Correct today, but a stated contract rests on something nothing enforces |
| `hygiene` | Readability, duplication, naming; no behavioural consequence |

Reviewer-supplied categories are recorded verbatim and never overwritten; the scale above is a **separate column**. Deliberately not adopted: the bot vocabulary (`Critical`/`Major`/`Minor`), because it is the axis that already failed to separate a loud `exit 1` from a silently passing gate.

### 7. What the ledger records
One **fragment per external round**, created by `/review-brief` at allocation, then filled by `/review-handoff` and amended by `/apply-review`. Its layout:

1. **Angle record** — what was reviewed and how. One block, heading `Angle record`.
2. **Round record** — what it cost and what it meant, in two blocks under one section:
   1. the **counts**, heading `Round record`;
   2. the **analysis** that reads them, heading `Round record, second half — the analysis`, immediately following the counts.

Two sections, three blocks, and the list above names all three — including their headings and their order. It is authoritative: where it and the bold-lead paragraphs below disagree, the list wins. An earlier draft made the list authoritative while omitting the third block from it entirely, so a generator author following the stated priority found nothing specifying the analysis block at all — a tie-break rule that could not resolve the ambiguity it was written for.

**Angle record** — the durable half, and the input to every future brief: round number and date, loop, surface, pinned scope (`<base>...<head>` with `<base>` **resolved to a SHA**, `HEAD`, `HEAD^{tree}` — the three anchors of §4), effort level, brief revision stamp, **angles briefed**, **angles executed**, **angles briefed but not executed**, **`Examined`** (the surface the round actually reached — see the coverage paragraph below, which is where the field and its reasoning are stated), the do-not-re-run list carried with its evidence and with the paths and commit each entry is scoped to, and the diff size stated against the ~1,000-line detection cliff with the split recommendation if it exceeds it.

**Coverage is a second axis, and it is the one that failed.** Added 2026-08-17, from a measurement taken after this ADR was first written and folded in here rather than left in the skill, because it changes what the fragment must hold. On RUNG-3, five local rounds over one four-clause paragraph: rounds 2 and 3 were briefed on the remedy with a do-not-re-run list, and round 4 — told to assume nothing was settled — found **two clauses that had been false since the first version and had survived three rewrites**. The mechanism, stated exactly: *the union of "what this round's angle covers" and "what the do-not-re-run list excludes" was smaller than the artifact, and nothing tracked the remainder.* An angle-only ledger would have recorded rounds 2 and 3 as two distinct angles and **looked healthy while the blind spot grew** — which is why angle and scope are recorded separately rather than one standing in for the other. Three consequences, all of them schema:

- The angle record carries **`Examined`** — the surface a round actually reached, distinct from the angles it was briefed at — so the never-examined remainder is *derivable* rather than assumed empty. A fragment predating the `/review-handoff` rewrite carries no such field, and an absent field means **unknown**, which resolves to *not examined*: the direction that costs tokens rather than coverage.
- **A do-not-re-run exclusion is scoped to the text that earned it and lapses when that text is edited.** Each entry therefore carries the **paths it covers** and the **commit the clearing round ran against**, and a brief lapses any entry whose paths have changed since. Exclusions otherwise accumulate monotonically, so the reviewed surface shrinks every round while the artifact keeps changing. They are a real token win — one briefed round went 172k → 85k — so the remedy is expiry, not removal. Granularity is **per file**: it over-expires and never under-expires, and under-expiry is the measured failure. Finer addressing needs a way to name a region that survives edits above it, which line numbers do not. **An entry whose recorded commit is unreachable lapses**, since `/ship`'s collapse dangles every pre-collapse SHA ([ADR-0069](0069-local-checkpoint-commits.md)) and an entry that cannot be evaluated is not an entry that holds.
- **Reviewer silence is not a verdict.** A clause no round asked about was never cleared, and promoting absence to "settled" is what carried the original defects forward — the same fail-open shape as reading the absence of a denial as approval.

**The ~1,000-line cliff is a *code* number, and for ungated prose the split recommendation belongs well below it.** Recorded here 2026-08-17 rather than left in the skill, because the carry-and-cite convention this ADR sets in Decision Driver 5 puts the reasoning in the ADR and only the rule in the file that obeys it. The cliff comes from an analysis of 1.5M PRs, gathered on code. Measured against this repository's own merged prose PRs — ADRs and skill files, the least-gated artifacts here — review load is already heavy at less than half of it: PR #83 (+331 lines) drew ~51 findings across six lenses; PR #91 (+477) took ~12 rounds, each finding a defect in the previous remedy; PR #94 (+295) took four local rounds to converge. **Three merged PRs, all from this repository** — the enumeration above is the sample, stated without a separate count beside it so the two cannot drift — so **~350–450 lines** is where a prose round starts costing, not a second cliff — the angle record states the size against the ~1,000-line figure as before, and a generator says which of the two applies to the artifact in hand. The distinction is a refinement rather than a contradiction under §7's own tiebreaker: the two are scoped to different artifact classes and neither denies the other.

**The same five rounds produced a second finding, about economy rather than coverage, and it is recorded here because a brief generator leans on it.** Distinct question, distinct conclusion, same branch: *does this artifact carry more claims than the job it exists to do needs?* — judged against the artifact's stated jobs rather than against correctness. It was the highest-value question asked in those five rounds. The round that came back **fidelity-clean** still recommended cutting **two of the four clauses**, and that cut is what ended a streak of rounds each finding a defect in the last remedy. Kept separate from the coverage paragraph above deliberately: both come from RUNG-3 and they are easy to merge into one narrative, but one is about surface never examined and the other about text that should not exist. The same ungated-prose caveat below governs it.

The caveat belongs with the finding: **n=1 artifact, and it was ungated prose.** The two briefings find **disjoint classes** — remedy-briefed rounds found every remedy defect, whole-artifact rounds found every original-text defect, and neither ever found the other's — but the value scales with how ungated the artifact is (`CLAUDE.md`, ADRs and skills highest; tested code lowest). A generator must state this as a recommendation for the artifact in hand, never as an unconditional cadence.

**Round record** — the budget half: finding count, the severity tally from §6, per-finding `Assessment` and `Disposition` from the closed sets below, the **fraction of the round's findings that sat inside the previous round's fixes** (the number that distinguishes real convergence from writing less new material — one measured round was 8 of 8), any scope mismatch, and the convergence call.

The fragment is **created by `/review-brief`** at allocation (§5), filled by `/review-handoff`, and **amended in place** by `/apply-review`, which owns the severity and `Disposition` columns because they are apply-time facts.

**Round record, second half — the analysis.** Not a third section: this is the prose half of the block above, sitting under its counts. It is the reading rather than the numbers, written by `/apply-review` once its reviewer loop has settled. The counts above say what a round cost; this says what it *means*, and it exists because that reading was previously produced only when someone thought to ask for it. `/apply-review` is the only step that can write it: it alone holds the report, the re-verification verdicts, the churn of its own remedies, and the smoke rounds, and the last of those is unknown until the loop terminates. The generate-vs-structure line of §3 applies unchanged:

- **Mechanically filled** — *locus* (which files and which sections took the findings, and which were reviewed clean); *type mix*, from the closed set below; *precision* (how many findings survived re-verification); *remedy churn* (how many fixes needed a second attempt); *cross-lens delta* (what this round caught that the local smokes had not, and the reverse).
- **Prompted, never substituted** — the causal read, and the convergence call's reasoning.

The type set is closed, for the reason §6's severity set is: an open list of examples is a self-label, and a self-label is what produced a round where every finding read `correctness`.

| Type | The finding says |
|---|---|
| `false-claim` | A statement about existing machinery is not true of that machinery |
| `contradiction` | The document disagrees with itself |
| `gap` | A rule is specified with a hole in it |
| `overreach` | A claim may well be true, but nothing measured it |
| `cosmetic` | Wording, ordering, or a count, with nothing resting on it |

Like severity, the type is applied by the fixer from this set, never taken from the reviewer's own label.

**`Assessment` and `Disposition` are two different questions and get two different names.** They were one word, `Verdict`, used in the report for whether a finding is real and in the fragment for what the fixer did about it — one column name, two vocabularies, two sessions, and neither set closed. That is the defect this section refuses to tolerate for a severity or a type, so it is not tolerated here either.

| `Assessment` — the report's, by the reviewer or re-verifier | Meaning |
|---|---|
| `real` | The defect is present and reachable |
| `wrong` | Verified, and the finding is incorrect |
| `already-resolved` | Real when written, since fixed or never applicable |
| `unverified` | Could not be established either way, and why |

| `Disposition` — the fragment's, by `/apply-review` | Meaning |
|---|---|
| `fixed` | An edit landed |
| `declined` | Real, and deliberately not fixed — with the reason |
| `deferred` | Routed onward rather than settled now — a later work item, or an `open-questions.md` entry. Reachable from `real` **and** from `unverified`, since a question worth tracking is a legitimate outcome for a finding nobody could establish |
| `no-action` | `Assessment` was `wrong` or `already-resolved`; or `unverified` and deliberately dropped rather than tracked |

`unverified` and `declined` exist because the earlier open sets had no home for them: a round whose surface emitted no per-finding judgement at all had to omit the column, and a finding accepted-but-not-fixed had nowhere to sit. A tally that silently drops those is not reproducible across rounds, which is the whole reason the ledger exists. The type is `cosmetic`, not `hygiene`, deliberately: `hygiene` is a **severity** value in §6, and one string meaning two things across two columns of one record is how a tally stops being reproducible.

**`false-claim` and `overreach` are the pair that will be confused, so the tiebreaker is stated rather than left to taste: ask whether the statement was shown *false* or merely *unsupported*.** A statement contradicted by the mechanism is `false-claim` however cautiously it was worded; a statement nothing contradicts, and nothing establishes either, is `overreach`. Both live on this branch and the pair is worth carrying as the worked example. "Remedy churn is readable from the `[x<N>s<M>]` tags" is `false-claim` — `/ship`'s collapse demonstrably removes them. "The local half dominates the CI half" was `overreach` — plausibly true, never measured, and it was corrected by hedging rather than by reversing. Reaching for `overreach` because it sounds gentler is the error the tiebreaker exists to stop.

**`contradiction` and `gap` are the other confusable pair, and they need their own tiebreaker because the one above runs on the wrong axis** — truth-versus-support cannot separate consistency from completeness. Ask instead whether there is a **second statement** the first cannot coexist with. If two statements in the document cannot both hold, it is `contradiction`, however incomplete either looks alone; if the document simply does not answer a case, with nothing to disagree with, it is `gap`. The worked example is this ADR's own digest shard rule: it said fragments and digests share a shard *and* that a digest's timestamp is its write time, and across a month boundary those two cannot both hold — `contradiction`, not `gap`, even though the symptom presents as an unhandled edge case. That is the general trap: a contradiction is usually **discovered** as a gap, so classify by what the document contains, never by how the defect first appeared.

**Remedy churn is the field with no other home**, and on the round that motivated this section it was the highest-yield number: two of seven remedies were wrong on the first attempt and together generated four of the six findings the subsequent smokes returned. Nothing else in this pipeline sees a fix's own error rate — every other artifact records the finding and stops at the edit.

**Its source is the apply's own smoke rounds, and it must be written while they are still reachable.** `/apply-review` tags each smoke `[x<N>s<M>]`, so churn is readable from the branch log *during* the apply that produced it — and only then. `/ship` collapses `merge-base..HEAD` into one commit ([ADR-0069](0069-local-checkpoint-commits.md)), taking every one of those tags with it; the branch carrying this ADR lost five that way. So this is not a field a later step can recompute, which is the argument for the fragment holding it rather than a reader deriving it: **write it at the end of the apply, or it is gone.** An apply resumed after a collapse, or one whose earlier rounds are on the far side of a `/ship`, has no tags to read — it records what it can and says the rest is unavailable, which is a different statement from zero churn.

Two rules keep it honest. **A section with no pattern to report says so** rather than composing one; an invented reading is the same defect as an invented brief angle, and harder to spot because it is a narrative. And **smoke rounds are cited here as evidence but never counted as rounds** — the headline metric stays external-only per the paragraph below, while *precision*, *remedy churn* and *cross-lens delta* are unmeasurable without referring to them. The line between citing and counting is operational, not rhetorical: a smoke never increments the round number, never appears in the finding count or the severity tally, and never contributes to the repeat-fraction — the four values the keep-sharpening-or-ship decision is read from. It may be named in the prose that explains those values. A later reader should not reconcile the tension by deleting the smoke references; the exclusion protects the numbers, not the narrative.

**Local rounds produce no fragment.** The owner classes the `spec-reviewer`/`test-reviewer` smokes as tests he would run by hand; counting them inflates the metric the decision is bought with. They still get a brief (§1).

**Reconciliation, recorded because it amends an earlier framing.** These were scoped as two ledgers with two lifetimes. Once the round record's collapse moved to `/squash-merge` (§8) they share one location and one lifecycle, so they are one artifact with two sections. The split survives as a difference in **use**, not in storage: the angle record is read by `/review-brief` on every subsequent round and every subsequent branch; the round record is history, consulted by a human deciding where to spend the next tokens.

### 8. Ledger location, naming, and collapse
**Path.** `specs/reviews/angle-ledger/`, a subfolder of the existing review-records directory — not the `specs/` root, which a landed PR deliberately cleared of review artifacts. The directory keeps the name `angle-ledger/` after the reconciliation in §7 because the angle record is its durable half — the part read on every future round and every future branch.

**Naming — reproducible, never derived from write time.** Two paths, both relative to the `specs/reviews/angle-ledger/` root above, and both computable from values that are known before the write and
identical on a re-run:

| Artifact | Path | Derived from |
|---|---|---|
| Fragment | `angle-ledger/branches/<b6>/round-<N>.md` | `<b6>` = first six hex of `sha256` over the branch name; `<N>` = the round |
| Digest | `angle-ledger/digests/<N/100>/pr<N>.md` | the PR number alone |

**An earlier draft named both from the writing clock**, and that one choice made four separate things unimplementable: a `/review-handoff`
re-run could not reproduce its own fragment's path, a re-entered `/squash-merge` created a second digest instead of overwriting the first, an
abandoned round left no record that its number had been consumed, and a revised brief could not be told apart from a new round. All four
dissolve once a name is a *function of identity* rather than of when the write happened. This is the general rule worth carrying: **anything
that may be re-entered must be addressable by what it is, not by when it was made.**

**The shard falls out of the same change.** `<YYYY-MM>` was the old shard and it derived from the timestamp, so removing the timestamp
removes it. What replaces it is better, because each new shard is bounded by something real rather than by the calendar: a branch's fragments
live under that branch's `<b6>/`, bounded by rounds-per-branch and **deleted wholesale at collapse**; a digest lives in a bucket of a hundred
PRs, so `digests/0/` holds PRs 0–99 and no digest directory ever exceeds a hundred entries, permanently, at any merge rate.

**Why a hash of the branch and not the branch.** §9 measured the hazard of a branch name used as a **path component** — a trailing dot
normalized by one API and not another, two correct-looking files on disk, git refusing outright at exit 128 — and a branch name also contains
`/`, so it would nest unpredictably. `<b6>` is six characters from `[0-9a-f]` and carries none of that.

**And the fragility it trades into, said plainly.** Deriving identity from the branch *name* assumes the name is stable, and a branch name is a label rather than a fact. Rename a branch mid-flight and `<b6>` changes: the next `/review-brief` computes `max(existing)+1` against a new, empty directory and restarts at round 1, while the old fragments sit under the stale hash where collapse — which keys on the current name — will never look, and are orphaned permanently. That is the one re-entry case this scheme does not cover, and it is the same shape as the defect it fixed: the old grammar was addressable by *when*, this one by a *what* that can be edited. No stable branch identity is available at brief time — the PR number is not known until `/ship`, and the branch's first commit SHA is rewritten by the collapse — so the limitation is accepted rather than designed away. The remedy is procedural and belongs with the rule: **rename before the first round, or move `branches/<b6>/` to the new hash as part of the rename.** Nothing enforces it; §10 carries it.

**The collision bound is honest rather than absolute.** 24 bits, so two *concurrently open* branches colliding is negligible at any plausible number of them, and a collision would
surface as an add/add merge conflict rather than a silent overwrite, since fragments are committed.

**Why one file per round rather than one growing file.** Fragments exist so that two simultaneously-open branches never touch the same path — now true by construction rather than by probability, since each branch's fragments live under its own `<b6>/` directory. That need ends at merge — so the two requirements separate cleanly: conflict-freedom while branches are open (one file per round), and bounded growth on `main` (collapse at merge).

**Collapse, and how "the branch's fragments" is determined.** `/squash-merge` collapses the branch's fragments into a single **per-PR
digest**. The set is `angle-ledger/branches/<b6>/` — the whole directory, since a branch's fragments are the only things in it — with git
provenance (`origin/main...HEAD`) as the cross-check that the directory holds nothing the branch did not add. A directory listing was
previously forbidden here for a good reason, now dissolved: the hazard was listing a *shared* shard where another branch's uncollapsed
fragments could be swept up, and per-branch directories mean there is nothing of another branch's to sweep.

**The collapse is idempotent, and this is the property the write-time grammar could not offer.** A re-run writes the same
`digests/<N/100>/pr<N>.md` and overwrites it. A re-run that finds `branches/<b6>/` already gone finds a digest already there and stops —
it does not compose a second, empty digest over an emptied directory, which is exactly what the earlier grammar produced when a collapse
failed part-way and was retried forty seconds later. A crash *between* those two — digest written, directory only partly deleted — resolves by the same principle but not the same action: **an existing digest is authoritative and is never recomposed.** The re-run finishes the deletion and stops. Recomposing from the fragments that happen to remain would overwrite a complete digest with one built from a subset of its own inputs, which is data loss rather than recovery — and nothing about the inlining rule prevents it, since inlining is about not linking to files that are gone, not about completeness. The general rule: **the digest is written once and only overwritten by a run that recomposed it from a complete set.** That is safe because of an invariant worth stating rather than leaving implicit: **once a digest exists for a PR, no new fragment is added to that branch's directory** — the branch has merged, and a round briefed against it afterwards would be a round on a different branch with a different `<b6>`. If that ever stopped holding, "finish the deletion and stop" would discard fragments no digest ever captured, which is the same data loss by a different route.

This is **new machinery in `/squash-merge`**, not an extension of something already there: the `/savepoint` collapse belongs to `/ship`, and
`/squash-merge` performs no collapse of any kind today. The philosophy is borrowed — branch-local scaffolding folded away at a single seam —
but the implementation is not, and BRIEF-5 must size it as new work. The digest **inlines** fragment content and must not link to it:
`scripts/check_spec_links.py` resolves every relative markdown link, so a digest linking to deleted fragments fails CI.

**The PR number is always knowable**, which is what lets a digest be addressed by it alone — and the reason belongs here rather than only in the skill: `/squash-merge` refuses to run unless an open PR exists for the branch, so no path in this pipeline produces a digest without one, and the number is known before the digest is composed. If that precondition is ever relaxed, this naming rule needs a fallback and does not have one. Leaving digests addressable only by write time is what made a re-entered collapse produce a second file, which is the defect this naming rule exists to remove.

**Why collapse is required rather than optional.** At roughly ten ledger-worthy rounds per PR, and a merge rate **measured** on this repository — 44 PR-numbered squash commits on `main` in the month to 2026-08-09, against 83 over the 4.6 months since 2026-03-21, so ~18/month lifetime and ~44/month recently — uncollapsed fragments reach somewhere between ~2,000 and ~5,000 files per year. The round figure remains an assumption rather than a measurement, and a conservative one, since one branch recorded 28 local plus 11 external rounds. **An earlier draft of this paragraph assumed ten merged PRs a month and presented it as grounded** — roughly 4× low, and by §7's own taxonomy that is `overreach`: a claim nothing contradicted and nothing established. It is recorded rather than quietly corrected because the same error would have made the shard-sizing insurance and the one-digest-per-PR granularity comparison wrong by the same factor. Against 266 tracked files and 121 tracked markdown files today, that is the ledger becoming several times the rest of the repository by file count within a year. The cost is not the filesystem; it is that five O(tracked-files) paths pay for every fragment, and two of them carry a multiplier:

| Path | Where it runs | Per-fragment multiplier |
|---|---|---|
| `scripts/check_spec_links.py` — resolves every relative link in every markdown file | docs-consistency job | ×1 (`ubuntu-latest`, no matrix) |
| PyMarkdown — lints every markdown file git considers part of the repo (tracked **plus** untracked-but-not-ignored) | docs-consistency job | ×1 (`ubuntu-latest`, no matrix) |
| `scripts/check_personal_containment.py --scope history` — whole tree plus history | `gitleaks` job ("Secret scan (gitleaks + personal-data containment)") | ×1 (`ubuntu-latest`, no matrix) |
| The containment gate's **live-repository test** (`tests/test_check_personal_containment.py`, `worktree` scope against the real tree) | `test` job | **×3 — it is inside the three-OS matrix** |
| `scripts/review_worktree.py` — materializes **every tracked file to disk** | local, never CI | ×(reviewers × rounds) |
| **The review scope itself** — committed fragments enter every later round's `<base>...<head>` diff | the review, never CI | ×(rounds after the fragment lands) |

The last row is the one an enumeration of *machinery* misses, and it is the sharpest instance of the ledger taxing its own process: fragments are committed (§5 depends on it) and the pinned scope is `<base>...<head>` (§4), so round 4's diff contains rounds 1–3's fragments. §7 obliges the angle record to state the diff size against the ~1,000-line detection cliff — a number the ledger now inflates with its own bookkeeping, pushing later rounds toward a split recommendation caused by the instrument rather than the work, and spending reviewer attention re-reading prior rounds' records. A brief should say so and exclude the ledger from the size it reports.

The two single-leg docs jobs are the ones easy to see, and reasoning from them alone to "the CI half is one leg" is wrong: the containment gate reaches the real repository from inside the test matrix, so a fragment is walked three times there. The local path's multiplier is larger still — two reviewers per round, two to four rounds per apply — but its product is unmeasured, so it is stated as the likely dominant cost rather than a demonstrated one. What the collapse decision rests on is the growth premise itself, which holds at any of these multipliers. That last one is a feedback loop worth naming: **the ledger taxes the exact process that generates it**, and it lands hardest on the Windows leg, already the ~3× wall-clock outlier ([ADR-0063](0063-parallel-ci-test-execution.md)). Collapse at merge caps `main` at one digest per merged PR — the same *granularity* as `specs/reviews/`'s one file per review event, though at the measured merge rate several times its volume. Granularity is the claim; parity of scale is not.

**Generator obligation.** Fragments and digests are machine-written and must be PyMarkdown-clean and link-check-clean **by construction**, permanently. The repository has already paid once to clean 46 MD040/MD031/MD012 sites; a generator that emits lint-dirty markdown converts that one-time cost into a recurring one.

### 9. The branch-keyed carriers stay in the session scratchpad
This resolves the `open-questions.md` entry "Where the two branch-keyed carriers live", whose recorded trigger was this rewrite.

`/land`'s composed commit message (`commit-msg/<branch>.txt` plus its `.branch` sidecar) and `/review-prep`'s metadata carrier (`review-prep-<branch>.md`) **stay in the session scratchpad**, keyed by branch, each guarded by its existing check-on-read: `/ship` compares the sidecar, `/review-handoff` compares the recorded `Branch` field before trusting any value in the carrier.

The alternative was measured and is real: a branch-derived path does not name one file on Windows but a different file depending on which API resolves it — for the legal branches `a./b` and `a/b`, a .NET write normalizes the trailing dot and silently overwrites, a Node write keeps both files after which a .NET or PowerShell read of `a./b` returns `a/b`'s contents with two correct-looking files on disk and no fault reported, and git refuses the component outright with exit 128. A `refs/`-namespaced blob fails closed against all three. It is declined anyway, on size: detection already closed the failure, and the message carrier is load-bearing only between `/land` and the **first** `/ship`, because the collapse puts the message into the branch's first commit and `/squash-merge` reads it from there afterwards. A carrier lost outside that window costs nothing; one lost inside it costs a `/land` re-run.

**One assumption behind that sizing has changed and is recorded so a later reader does not mistake it for a measurement taken under current conditions.** "Costs one `/land` re-run" was sized under sequential, single-session work. Parallel PRs across more sessions raise the *rate* at which a `/ship` runs in a different session than its `/land`. This does not reverse the decision — detection still holds and the prize is still one re-run — but if that rate rises, the `refs/` spelling is the one to take, and both carriers move together or neither does, since they are one design question.

**This does not reopen the checkpoint collapse.** The collapse's rejection of a repo-scoped message rests on durability *and* visibility; the option above addresses only durability, leaving intact the argument that decided it — the composed message as the branch's first commit is PR-visible, reviewable by every lens, and verifiably what `/squash-merge` extracts.

### 10. Where the rules are written
Reasoning lives here; the rule is **carried into the skill file that must obey it, citing this ADR**. An ADR-only rule reproduces a failure found three times on one branch: a prohibition "stated in the ADR, enforced by nothing, and invisible to an editor who opens the file directly". Mechanization where it is cheap, per the `tests/test_except_convention.py` precedent: the agent-output-pointer rule is already gated by `tests/test_no_task_output_citations.py`, and a sync check in the shape of `scripts/check_markdownlint_config_sync.py` should pin the self-contained-capture rule into `review-handoff`. **Five** of this ADR's rules are enforced by **nothing**, and are named here rather than left to read as settled — the third is the layout priority rule two paragraphs down, the fourth is §8's rename remedy — move a branch's ledger directory when the branch is renamed, or its fragments orphan silently — and the fifth is the invariant §8 leans on to make "never recompose a digest" safe: that no new fragment is added to a branch's directory once its digest exists. Nothing checks that; allocation computes `max(existing)+1` against whatever is on disk and would restart at round 1 against a directory that a collapse has just emptied. All five count because "an editor remembering it" is not enforcement. **The containment rule on verbatim capture** (§5) cannot be gated: ADR-0070's enumeration gate keys on the path, and a fragment's path is `specs/reviews/`, so a personal value quoted into a fragment passes every mechanical check the repo has. It is human judgement at capture time, exactly as ADR-0070 concluded for the content half generally — and the ledger is a *new* place that judgement must be exercised, which is the whole reason it is listed. **Round-number allocation** (§5) is the second: nothing verifies that `N` came from `/review-brief` rather than from a session counting files, which is the heuristic it replaces; the cheap mechanization, if one is ever wanted, is that a fragment whose `N` is not `max(existing)+1` for the branch is a detectable error.

A third candidate, named because its absence is what let one representation drift from another three times while this ADR was being written: §7 gives the fragment layout as a numbered list *and* as bold-lead paragraphs, and states that the list wins. That priority rule is enforced by an editor remembering it. When the fragment template becomes a real artifact, a sync check in the same shape should pin the two representations to each other. The generate-vs-structure line (§3) is **not** mechanizable and is stated at the top of `/review-brief` with the twenty-unchecked-guards example attached, so an editor meets the reasoning rather than a bare rule.

## Consequences

### Positive
- Every field a downstream step reads becomes a field an upstream step is obliged to write; the round number, tree anchor, angle roster, brief revision, and surface all stop depending on someone remembering to ask.
- The angle history becomes readable by a skill and by a fresh session, which is the precondition for `/review-brief` generating from a record rather than from recollection.
- The keep-sharpening-or-ship decision gains a per-round instrument with a consequence-based severity axis and the repeat-fraction number that distinguishes convergence from exhaustion.
- Ref ranges make the pinned scope enforceable, which turns scope verification from unbuildable into a string comparison.

### Negative / Tradeoffs
- The ledger adds tracked markdown to a repository whose gates are O(tracked files) and whose reviewer worktrees materialize all of them. Collapse at merge is what keeps this bounded, so **a skipped collapse is a real cost, not a cosmetic one** — the shard rule is the insurance and the growth figures above are the argument.
- `specs/reviews/` acquires a *living* subfolder inside a directory its README describes as immutable historical artifacts. The README is amended to draw that line explicitly; the ambiguity is real and is paid for with one sentence rather than a new directory.
- The obligations in §5 make `/review-handoff` heavier. A report that satisfies all eleven is longer than what the current skill produces, and on a large round that cost is paid every round.
- Severity is assigned by the fixer, so it is unavailable until apply. A round abandoned before apply leaves a fragment whose analysis, and possibly whose counts, are recorded as not performed — a known-empty column rather than a zero, and never backfilled by guesswork. Since §5 has `/review-brief` create the fragment at allocation, an abandoned round leaves one even if no review ever ran.
- The digest inlines rather than links, so per-round detail on `main` is reachable only through git history. This is deliberate: a linking digest fails the link check the moment fragments are removed.

## Consequences for Other Documents
- [`specs/reviews/README.md`](../reviews/README.md) — one sentence distinguishing the living `angle-ledger/` subfolder from the frozen point-in-time snapshots, plus **one index row pointing at the directory**, not a row per round. A directory-level row also keeps the ledger out of the shared-index merge-conflict problem entirely.
- [`specs/open-questions.md`](../open-questions.md) — "Where the two branch-keyed carriers live" moves to **Resolved** (§9).
- [ADR-0068](0068-reviewer-isolation-worktrees.md) — a `Related` navigation link only, no decision content; and [ADR-0069](0069-local-checkpoint-commits.md) — its forward pointer into the carriers question is corrected, since that entry is now resolved rather than deferred.
- `.claude/skills/review-brief/`, `review-prep/`, `review-handoff/`, `apply-review/` — implemented against this ADR in the following work items, each carrying its rules in-file with a citation back here (§10).
- `.claude/skills/squash-merge/` — gains the fragment-collapse step §8 specifies. Listed separately because it is the one skill here that gains **new** machinery rather than a rewrite: it performs no collapse of any kind today, so BRIEF-5 sizes it as new work.

## Links
- Related: [ADR-0068](0068-reviewer-isolation-worktrees.md) — reviewer isolation; this pipeline *consumes* that machinery rather than extending its decision (no field or policy of ADR-0068 changes here), and its per-reviewer, per-round worktree materialization is the cost model behind §8
- Related: [ADR-0069](0069-local-checkpoint-commits.md) — checkpoint collapse; two of §4's three anchors exist because the collapse dangles pre-collapse SHAs while the tree hash survives, and §8 reuses its `/squash-merge` seam
- Related: [ADR-0063](0063-parallel-ci-test-execution.md) — the Windows leg's ~3× wall-clock outlier status, which §8's growth argument lands on
- Related: [ADR-0061](0061-markdown-link-check-gate.md), [ADR-0062](0062-markdown-style-lint-gate.md) — the docs gates the generator obligation in §8 must satisfy by construction
- Related: [ADR-0067](0067-unrepliable-finding-acknowledgement.md) — precedent for recording a finding's disposition when the normal reply channel cannot carry it
- Related: [`specs/reviews/README.md`](../reviews/README.md) — the "new review reports belong here" rule that decides §8's path
- Related: [CLAUDE.md](../../CLAUDE.md) § Subagent output pointers — the agent-output-pointer rule §10 cites as its mechanization precedent
- Related: [open-questions.md](../open-questions.md) — "Harness agent-output files are empty", the expiring observation the capture-as-it-arrives obligation rests on; and "Where the two branch-keyed carriers live", resolved by §9

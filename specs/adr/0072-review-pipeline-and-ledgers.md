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
| `/review-brief` | **orchestrator** session | the briefing file |
| `/review-prep` | **reviewer** session | the carrier (absorbing the brief) and the exact command to run |
| `/review-handoff` | **reviewer** session | the report and the round's ledger fragment |
| `/apply-review` | **orchestrator** session | the fixes, and the fragment's severity and verdict columns |

Four skills, not three. The **pipeline** is the first three — brief → prep → handoff, one artifact out of each — and `/apply-review` is its consumer; but it writes back to the round's fragment, so it is a producer of the ledger even though it produces no review artifact. Any count of "how many skills must change" is four.

`/review-brief` serves **both** loops. The external `/code-review` pass is primary, but the local `spec-reviewer`/`test-reviewer` smokes are where the measured savings landed (one briefed round went 172k → 85k tokens and produced the first clean report in 24 rounds), and a brief skill that serves only the external pass leaves the more frequent case improvised.

### 2. One carrier per review
`/review-prep` **reads the brief and merges it into the carrier**. From prep onward there is exactly one artifact per review. Brief → prep handover is **by path** — the two run in different sessions (§1), so there is no other carrier, which is exactly why the revision stamp in §4 is mandatory. What is prohibited is what happens *after* absorption: from prep onward nothing may reference the brief file, and no downstream step may read it. Referencing it past that point is what leaves a second artifact able to go stale alone, and re-reading it is what exposes the mutable-underneath-its-reader failure a second time.

### 3. The generate-vs-structure line
Two claims are easy to merge by accident — *where* `/review-brief` runs and *how much of the brief it may generate*. The line:

- **Encode** — the section structure (angles / already-verified / settled-with-reasons / reporting bar), the standing heuristics (brief each round at a different angle; the previous round's fixes are the next round's highest-yield surface; check the cheap things yourself first), and a different-angle-each-round nudge.
- **Mechanically fill** — round number, gate results, what changed since the last pass, prior findings applied, diff size, the angle roster read from the ledger.
- **Prompt for, and never substitute** — the priority angle and its rationale, what the orchestrator has already checked itself, and the settled list **with reasons**.

The discriminator: the *heuristic* is recordable and stable, so encode it; the *instance* is not. Nothing derivable from a diff produces the sentence that found two Tier-1 interaction defects — "about twenty guards changed in one pass and I have NOT systematically looked for interactions between them" — because it requires knowing which changes the orchestrator had already scrutinized. A brief that leaves a section empty and states "no priority angle supplied this round" is honest; one that invents three plausible angles wastes ten agents.

### 4. Expiry, stamp, and echo
- **Revision stamp — the hash is in the filename, so the path is the stamp.** `/review-brief` writes the brief as `brief-<round>-<h8>.md`, where `<h8>` is the first eight hex characters of `sha256` over its bytes, and the report **echoes it back**. A stamp stored only *inside* the brief proves the wrong thing — it shows the report echoed what prep read, not that prep read what the orchestrator wrote, because editing the brief updates its self-described stamp too and every downstream check still agrees. Carrying the expected hash out of band would fix that, and would cost a **second carrier** for the one hop §2 says has only one; a value crossing a session boundary with no obliged producer is this ADR's own thesis reproduced. Putting it in the name avoids both: there is still exactly one artifact and one path, and `/review-prep` recomputes `sha256` over the bytes and compares against the name it was given. **An edited brief no longer matches its own filename**, so the edit is loud instead of invisible, and a revised brief is a *new file with a new name* that the orchestrator hands over deliberately. The incident this closes is the measured one: a brief edited twice under its reader, where the receiving session could not tell which revision it held.
- **Expiry — a byte-level digest, not a status listing.** The brief records what tree state it was written against, and `/review-prep` checks it. **`git status --porcelain` is not sufficient on its own**: it reports each path's *status*, not its content, so a second edit to an already-modified file leaves the listing byte-identical — and an edit to a file the brief already knew was dirty is the most likely post-brief change there is. The digest therefore covers content: `HEAD^{tree}` for the tracked-and-committed half, `git stash create`'s tree for tracked modifications, and `git hash-object` over the untracked-but-not-ignored paths. That triple is not invented here — `.claude/reviewer-isolation.md` invariant 1 already documents it, including the trap that a tree hash silently omits untracked files. A `git status --porcelain` listing is still recorded, as the human-readable companion to the digest, never as the check. **None of this is built yet** — like everything else in this ADR it is specified here and implemented in BRIEF-3/4/5.
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
| Per-finding **verdict slot** including "verified, and the finding is wrong" | Two such findings had to be written as prose; one was wrong in the dangerous direction |
| Negative results and settled-item challenges | A challenge to a settled item has nowhere to go |
| **Surface** recorded (terminal / `-p` / host app) | Missing structured verdicts misdiagnosed as reviewer failure |
| **Scope echo** vs the range prep asked for | Nothing checked that a review honoured its scope |
| **Fragment path** — the ledger fragment this round wrote | `/apply-review` runs in the *other* session and cannot otherwise identify which fragment to amend |

The executed-angle roster and the "not covered" section are **distinct**: one is about angles run, the other about scope reached.

**Verbatim capture stops at personal data, and the fragment is where this bites.** `CLAUDE.md` confines personal health values, results, diagnoses, medications and identifying information to `specs/personal/`. Neither half of the risk is new — verbatim capture is an old obligation and containment is an old rule — but **this ADR creates the combination**: before it, a report lived in the session scratchpad, untracked and never pushed, while a fragment and a digest are committed and travel to a public remote. So: a review source quoting personal data is recorded by **path and data category**, never reproduced, in the report, the fragment and the digest alike. Quote the finding, not the value. The enumeration gate ([ADR-0070](0070-personal-data-containment-gate.md)) will not catch a violation here, because the path is `specs/reviews/`, not `specs/personal/` — this is the content half, which stays human judgement, and the ledger is now one of the places it must be exercised.

**`/review-brief` allocates `N`, and nothing else does.** A round begins when a brief is issued for the external loop, so `N` is assigned there and flows brief → prep → handoff → apply unchanged; every later step reads it and none derives it. It increments by one per brief issued on the branch — **not** per report written, per apply performed, or per file found on disk, which is what the retired heuristic did. Three consequences follow and are stated because each is a case the heuristic got wrong: a **re-run of `/review-handoff`** for the same brief reuses `N` and rewrites that round's fragment in place rather than adding a second one; a **round abandoned before apply** keeps its number and leaves a fragment whose analysis is recorded as not performed, which is a different fact from a round that found nothing; and a **local smoke round never consumes a number at all** (§7). Allocation at the brief is what makes the number exist before anything can be counted, which is the property the counting heuristic could never have.

The last row is there because its absence would reproduce this ADR's own thesis. The fragment's name is a timestamp from the **reviewer** session's clock (§8); `/apply-review` runs in the orchestrator session and amends that fragment in place. Without a recorded path it would have to glob a shard and guess, which is a consumer keying on something no producer is obliged to write — the exact failure the rest of this document exists to remove.

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
One **fragment per external round**, and its layout is the template `/review-handoff` writes and `/apply-review` amends:

1. **Angle record** — what was reviewed and how.
2. **Round record** — what it cost and what it meant, in two halves: the **counts**, then the **analysis** that reads them.

Two sections, three blocks of prose. The analysis is the round record's second half, never a third section — a fragment format read off this list rather than off the paragraph headings below is the one that will match.

**Angle record** — the durable half, and the input to every future brief: round number and date, loop, surface, pinned scope (`<base>...<head>` with `<base>` **resolved to a SHA**, `HEAD`, `HEAD^{tree}` — the three anchors of §4), effort level, brief revision stamp, **angles briefed**, **angles executed**, **angles briefed but not executed**, the do-not-re-run list carried with its evidence, and the diff size stated against the ~1,000-line detection cliff with the split recommendation if it exceeds it.

**Round record** — the budget half: finding count, the severity tally from §6, per-finding verdicts (applied / rejected-as-wrong / deferred), the **fraction of the round's findings that sat inside the previous round's fixes** (the number that distinguishes real convergence from writing less new material — one measured round was 8 of 8), any scope mismatch, and the convergence call.

The fragment is written by `/review-handoff` and **amended in place** by `/apply-review`, which owns the severity and verdict columns because they are apply-time facts.

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

Like severity, the type is applied by the fixer from this set, never taken from the reviewer's own label. The type is `cosmetic`, not `hygiene`, deliberately: `hygiene` is a **severity** value in §6, and one string meaning two things across two columns of one record is how a tally stops being reproducible.

**`false-claim` and `overreach` are the pair that will be confused, so the tiebreaker is stated rather than left to taste: ask whether the statement was shown *false* or merely *unsupported*.** A statement contradicted by the mechanism is `false-claim` however cautiously it was worded; a statement nothing contradicts, and nothing establishes either, is `overreach`. Both live on this branch and the pair is worth carrying as the worked example. "Remedy churn is readable from the `[x<N>s<M>]` tags" is `false-claim` — `/ship`'s collapse demonstrably removes them. "The local half dominates the CI half" was `overreach` — plausibly true, never measured, and it was corrected by hedging rather than by reversing. Reaching for `overreach` because it sounds gentler is the error the tiebreaker exists to stop.

**`contradiction` and `gap` are the other confusable pair, and they need their own tiebreaker because the one above runs on the wrong axis** — truth-versus-support cannot separate consistency from completeness. Ask instead whether there is a **second statement** the first cannot coexist with. If two statements in the document cannot both hold, it is `contradiction`, however incomplete either looks alone; if the document simply does not answer a case, with nothing to disagree with, it is `gap`. The worked example is this ADR's own digest shard rule: it said fragments and digests share a shard *and* that a digest's timestamp is its write time, and across a month boundary those two cannot both hold — `contradiction`, not `gap`, even though the symptom presents as an unhandled edge case. That is the general trap: a contradiction is usually **discovered** as a gap, so classify by what the document contains, never by how the defect first appeared.

**Remedy churn is the field with no other home**, and on the round that motivated this section it was the highest-yield number: two of seven remedies were wrong on the first attempt and together generated four of the six findings the subsequent smokes returned. Nothing else in this pipeline sees a fix's own error rate — every other artifact records the finding and stops at the edit.

**Its source is the apply's own smoke rounds, and it must be written while they are still reachable.** `/apply-review` tags each smoke `[x<N>s<M>]`, so churn is readable from the branch log *during* the apply that produced it — and only then. `/ship` collapses `merge-base..HEAD` into one commit ([ADR-0069](0069-local-checkpoint-commits.md)), taking every one of those tags with it; the branch carrying this ADR lost five that way. So this is not a field a later step can recompute, which is the argument for the fragment holding it rather than a reader deriving it: **write it at the end of the apply, or it is gone.** An apply resumed after a collapse, or one whose earlier rounds are on the far side of a `/ship`, has no tags to read — it records what it can and says the rest is unavailable, which is a different statement from zero churn.

Two rules keep it honest. **A section with no pattern to report says so** rather than composing one; an invented reading is the same defect as an invented brief angle, and harder to spot because it is a narrative. And **smoke rounds are cited here as evidence but never counted as rounds** — the headline metric stays external-only per the paragraph below, while *precision*, *remedy churn* and *cross-lens delta* are unmeasurable without referring to them. The line between citing and counting is operational, not rhetorical: a smoke never increments the round number, never appears in the finding count or the severity tally, and never contributes to the repeat-fraction — the four values the keep-sharpening-or-ship decision is read from. It may be named in the prose that explains those values. A later reader should not reconcile the tension by deleting the smoke references; the exclusion protects the numbers, not the narrative.

**Local rounds produce no fragment.** The owner classes the `spec-reviewer`/`test-reviewer` smokes as tests he would run by hand; counting them inflates the metric the decision is bought with. They still get a brief (§1).

**Reconciliation, recorded because it amends an earlier framing.** These were scoped as two ledgers with two lifetimes. Once the round record's collapse moved to `/squash-merge` (§8) they share one location and one lifecycle, so they are one artifact with two sections. The split survives as a difference in **use**, not in storage: the angle record is read by `/review-brief` on every subsequent round and every subsequent branch; the round record is history, consulted by a human deciding where to spend the next tokens.

### 8. Ledger location, naming, and collapse
**Path.** `specs/reviews/angle-ledger/`, a subfolder of the existing review-records directory — not the `specs/` root, which a landed PR deliberately cleared of review artifacts. The directory keeps the name `angle-ledger/` after the reconciliation in §7 because the angle record is its durable half — the part read on every future round and every future branch.

**Naming.** `<YYYY-MM-DD>T<HHMMSS>Z-<b6>.md`, e.g. `2026-08-09T143211Z-a3f9c1.md`, where `<b6>` is the first six hex characters of `sha256` over the branch name. This is a **hybrid — extended date, basic time — and is deliberately not conformant ISO 8601**, because colons are illegal in Windows filenames and the primary development machine is Windows. Do not describe it as ISO 8601 anywhere. UTC with an explicit `Z` is mandatory: local time sorts wrong across a DST boundary and is genuinely ambiguous for one hour every autumn, and the ledger's ordering *is* its index.

**Which instant.** The timestamp records the moment `/review-handoff` **writes the fragment**. It is locally derivable, needs no state carried across skills, and is the only instant the writing skill reliably knows. Round-start would be more faithful to ordering but must be threaded brief → prep → handoff — one more field a consumer keys on and a producer must be obliged to write, which is the failure shape this ADR exists to close.

**No branch name in the filename — but a hash of it, deliberately.** The timestamp alone is a bounded charset with no user-controlled text, which is what makes it immune to the Windows branch-name hazard measured in §9. A hex digest of the branch name keeps that property exactly: the hazard is the branch name used **as a path component** — trailing dots normalized by one API and not another — and a digest is not the name, it is six characters from `[0-9a-f]`. What it buys is the thing a one-second timestamp cannot: **two branches handing off in the same second no longer produce the same path.** Without it, §8's claim that simultaneously-open branches never touch the same path is false — rare, and loud when it fires (an add/add merge conflict rather than a silent overwrite, since fragments are committed), but a stated invariant that is merely improbable is not one. Digests inherit the same protection from their `-pr<N>` suffix, PR numbers being unique by construction, so a collapse that runs twice overwrites its own digest rather than colliding with another branch's. A human label for a round goes in the file's first heading, never in its name.

**Why one file per round rather than one growing file.** Fragments exist so that two simultaneously-open branches never touch the same path. That need ends at merge — so the two requirements separate cleanly: conflict-freedom while branches are open (one file per round), and bounded growth on `main` (collapse at merge).

**Collapse, and how "the branch's fragments" is determined.** `/squash-merge` collapses the branch's fragments into a single **per-PR digest** under `specs/reviews/angle-ledger/`. The set is selected by **git provenance — the fragments this branch added over `origin/main...HEAD` — never by listing the shard directory.** A directory listing is correct only while every prior branch collapsed successfully: one skipped collapse leaves an earlier branch's fragments on `main`, the next branch inherits them at its next merge, and a listing-based collapse then sweeps another branch's rounds into this PR's digest. Concurrently open branches cannot collide — each commits its fragments on its own branch, so neither tree holds the other's — but that is a property of the *commit*, not of the path, and the selection rule must not rest on the path. This is **new machinery in that skill**, not an extension of something already there: the `/savepoint` collapse belongs to `/ship`, and `/squash-merge` today performs no collapse of any kind. The philosophy is borrowed — branch-local scaffolding folded away at a single seam — but the implementation is not, and BRIEF-5 must size it as new work. This applies to **both** sections; an earlier framing put the round record's collapse at `/ship`, which cannot work, because `/ship`'s collapse fires only before the **first** push while external rounds continue on the open PR. The digest **inlines** fragment content and must not link to it: `scripts/check_spec_links.py` resolves every relative markdown link, so a digest linking to deleted fragments fails CI.

**Shard rule, and it covers digests too.** Fragments and digests both live under `angle-ledger/<YYYY-MM>/`, **each filed by its own timestamp's month** — which for a digest is the month it was written, not the month its fragments were. A branch whose rounds ran in one month and merged in the next therefore empties one shard and adds its digest to another. That is correct rather than a defect: the shard exists to bound a directory's size, not to group a branch's history, and an empty shard costs nothing. Do not add a rule making the digest inherit a fragment's month — it would put a file's shard out of step with its own name, which is the ambiguity the single grammar avoids. Sharding fragments alone would close the unbounded-growth hazard on the transient half and leave it open on the permanent one, where ~120 digests a year would otherwise land flat in a single directory. This is insurance, cheap to specify now and expensive to retrofit once paths are baked into four skills: if a collapse is ever skipped or deferred, no single directory grows unbounded.

**A digest's name follows the same grammar, plus the PR number** — `<YYYY-MM-DD>T<HHMMSS>Z-pr<N>.md`, the timestamp recording when `/squash-merge` writes it. The suffix is digits only, so the filename stays the bounded, no-user-text charset the fragment rule requires; the branch name is no safer in a digest's name than in a fragment's. It also makes the two **distinguishable by path alone** — a name whose suffix is `pr<N>` is a digest, one whose suffix is six hex characters is a fragment — so a skill or a reader can tell a collapsed round from a live one without opening a file. Collapse status is a property of the **file**, never of the shard: because each file is filed by its own month, one shard can hold live fragments from branches still in-round beside digests from PRs merged that same month.

**The PR number is always knowable**, so the grammar's `-pr<N>` component is mandatory rather than best-effort — and the reason belongs here rather than only in the skill: `/squash-merge` refuses to run unless an open PR exists for the branch, so no path in this pipeline produces a digest without one, and the number is known before the digest is composed. If that precondition is ever relaxed, this naming rule needs a fallback and does not have one. Leaving digests unnamed would reopen exactly the branch-derived-filename hazard §9 measures, in the one file that persists.

**Why collapse is required rather than optional.** At roughly ten ledger-worthy rounds per PR and ten merged PRs per month — the round figure is an assumption, not a measurement, and a conservative one, since one branch recorded 28 local plus 11 external rounds — uncollapsed fragments reach ~1,200 files per year. Against 266 tracked files and 121 tracked markdown files today, that is the ledger becoming several times the rest of the repository by file count within a year. The cost is not the filesystem; it is that five O(tracked-files) paths pay for every fragment, and two of them carry a multiplier:

| Path | Where it runs | Per-fragment multiplier |
|---|---|---|
| `scripts/check_spec_links.py` — resolves every relative link in every markdown file | docs-consistency job | ×1 (`ubuntu-latest`, no matrix) |
| PyMarkdown — lints every markdown file git considers part of the repo (tracked **plus** untracked-but-not-ignored) | docs-consistency job | ×1 (`ubuntu-latest`, no matrix) |
| `scripts/check_personal_containment.py --scope history` — whole tree plus history | secrets-scan job | ×1 (`ubuntu-latest`, no matrix) |
| The containment gate's **live-repository test** (`tests/test_check_personal_containment.py`, `worktree` scope against the real tree) | `test` job | **×3 — it is inside the three-OS matrix** |
| `scripts/review_worktree.py` — materializes **every tracked file to disk** | local, never CI | ×(reviewers × rounds) |

The two single-leg docs jobs are the ones easy to see, and reasoning from them alone to "the CI half is one leg" is wrong: the containment gate reaches the real repository from inside the test matrix, so a fragment is walked three times there. The local path's multiplier is larger still — two reviewers per round, two to four rounds per apply — but its product is unmeasured, so it is stated as the likely dominant cost rather than a demonstrated one. What the collapse decision rests on is the growth premise itself, which holds at any of these multipliers. That last one is a feedback loop worth naming: **the ledger taxes the exact process that generates it**, and it lands hardest on the Windows leg, already the ~3× wall-clock outlier ([ADR-0063](0063-parallel-ci-test-execution.md)). Collapse at merge caps `main` at roughly one digest per merged PR, which matches `specs/reviews/`'s existing granularity of one file per review event.

**Generator obligation.** Fragments and digests are machine-written and must be PyMarkdown-clean and link-check-clean **by construction**, permanently. The repository has already paid once to clean 46 MD040/MD031/MD012 sites; a generator that emits lint-dirty markdown converts that one-time cost into a recurring one.

### 9. The branch-keyed carriers stay in the session scratchpad
This resolves the `open-questions.md` entry "Where the two branch-keyed carriers live", whose recorded trigger was this rewrite.

`/land`'s composed commit message (`commit-msg/<branch>.txt` plus its `.branch` sidecar) and `/review-prep`'s metadata carrier (`review-prep-<branch>.md`) **stay in the session scratchpad**, keyed by branch, each guarded by its existing check-on-read: `/ship` compares the sidecar, `/review-handoff` compares the recorded `Branch` field before trusting any value in the carrier.

The alternative was measured and is real: a branch-derived path does not name one file on Windows but a different file depending on which API resolves it — for the legal branches `a./b` and `a/b`, a .NET write normalizes the trailing dot and silently overwrites, a Node write keeps both files after which a .NET or PowerShell read of `a./b` returns `a/b`'s contents with two correct-looking files on disk and no fault reported, and git refuses the component outright with exit 128. A `refs/`-namespaced blob fails closed against all three. It is declined anyway, on size: detection already closed the failure, and the message carrier is load-bearing only between `/land` and the **first** `/ship`, because the collapse puts the message into the branch's first commit and `/squash-merge` reads it from there afterwards. A carrier lost outside that window costs nothing; one lost inside it costs a `/land` re-run.

**One assumption behind that sizing has changed and is recorded so a later reader does not mistake it for a measurement taken under current conditions.** "Costs one `/land` re-run" was sized under sequential, single-session work. Parallel PRs across more sessions raise the *rate* at which a `/ship` runs in a different session than its `/land`. This does not reverse the decision — detection still holds and the prize is still one re-run — but if that rate rises, the `refs/` spelling is the one to take, and both carriers move together or neither does, since they are one design question.

**This does not reopen the checkpoint collapse.** The collapse's rejection of a repo-scoped message rests on durability *and* visibility; the option above addresses only durability, leaving intact the argument that decided it — the composed message as the branch's first commit is PR-visible, reviewable by every lens, and verifiably what `/squash-merge` extracts.

### 10. Where the rules are written
Reasoning lives here; the rule is **carried into the skill file that must obey it, citing this ADR**. An ADR-only rule reproduces a failure found three times on one branch: a prohibition "stated in the ADR, enforced by nothing, and invisible to an editor who opens the file directly". Mechanization where it is cheap, per the `tests/test_except_convention.py` precedent: the agent-output-pointer rule is already gated by `tests/test_no_task_output_citations.py`, and a sync check in the shape of `scripts/check_markdownlint_config_sync.py` should pin the self-contained-capture rule into `review-handoff`. Two of this ADR's rules are enforced by **nothing**, and are named here rather than left to read as settled. **The containment rule on verbatim capture** (§5) cannot be gated: ADR-0070's enumeration gate keys on the path, and a fragment's path is `specs/reviews/`, so a personal value quoted into a fragment passes every mechanical check the repo has. It is human judgement at capture time, exactly as ADR-0070 concluded for the content half generally — and the ledger is a *new* place that judgement must be exercised, which is the whole reason it is listed. **Round-number allocation** (§5) is the second: nothing verifies that `N` came from `/review-brief` rather than from a session counting files, which is the heuristic it replaces; the cheap mechanization, if one is ever wanted, is that a fragment whose `N` is not `max(existing)+1` for the branch is a detectable error.

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
- Severity is assigned by the fixer, so it is unavailable until apply. A round abandoned before apply leaves a fragment with a finding count and no severity tally — recorded as a known-empty column, not backfilled by guesswork.
- The digest inlines rather than links, so per-round detail on `main` is reachable only through git history. This is deliberate: a linking digest fails the link check the moment fragments are removed.

## Consequences for Other Documents
- [`specs/reviews/README.md`](../reviews/README.md) — one sentence distinguishing the living `angle-ledger/` subfolder from the frozen point-in-time snapshots, plus **one index row pointing at the directory**, not a row per round. A directory-level row also keeps the ledger out of the shared-index merge-conflict problem entirely.
- [`specs/open-questions.md`](../open-questions.md) — "Where the two branch-keyed carriers live" moves to **Resolved** (§9).
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

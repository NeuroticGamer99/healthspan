---
name: review-brief
description: Brief a review round from the angle ledger rather than from recollection — read what earlier rounds examined, lapse the exclusions their evidence no longer covers, allocate the round number by creating its ledger fragment, and hand the brief over. Use before /review-prep for an external round, and before launching a spec-reviewer or test-reviewer smoke.
---

# /review-brief — brief the round from the record, not from memory

The opening step of the review pipeline specified by
[ADR-0072](../../../specs/adr/0072-review-pipeline-and-ledgers.md): brief → prep → handoff →
apply. Every high-yield review pass this repository has run was briefed, by hand, in chat — and
each of those briefs died with its session, which is why the same angle went unexecuted in four
consecutive passes and why one clause survived three rewrites while three rounds were briefed on
its neighbours.

This skill exists so a briefing is **generated from a record**. It reads the angle ledger, states
what earlier rounds actually examined, drops the exclusions whose evidence has expired, and writes
a brief the reviewer session can be handed by path.

It serves **both loops**. The external `/code-review` pass is primary; the
`spec-reviewer`/`test-reviewer` smokes are the more frequent case and the one where the measured
saving landed — a briefed local round went 172k → 85k tokens and produced the first clean report
in 24 rounds. The two differ in what they produce, not in how they are briefed; see
[The local-round variant](#the-local-round-variant).

## The line this skill is built on: generate, fill, or ask

ADR-0072 §3. Two questions are easy to merge by accident — *where* a brief comes from, and *how
much of it this skill may write*. The line, and it governs every section below:

- **Encode** — the section structure, and the standing heuristics: brief each round at a different
  angle; the previous round's fixes are the next round's highest-yield surface; check the cheap
  things yourself first.
- **Mechanically fill** — round number, gate results, what changed since the last pass, prior
  findings applied, diff size, and the angle roster read from the ledger.
- **Prompt for, and never substitute** — the priority angle and its rationale, what the
  orchestrator has already checked itself, and the settled list **with reasons**.

The discriminator is that a *heuristic* is recordable and stable while an *instance* is not.
Nothing derivable from a diff produces the sentence that found two Tier-1 interaction defects —
"about twenty guards changed in one pass and I have NOT systematically looked for interactions
between them" — because writing it requires knowing which changes the orchestrator had already
scrutinized. That is knowledge no artifact holds.

**A brief that leaves a section empty and says so is honest; one that invents three plausible
angles wastes ten agents.** Where this skill has nothing to fill a section with, it writes that
the section is empty and why. It never composes a priority angle, and it never promotes its own
guess to a finding-shaped claim.

## Argument — the effort level

The grammar, stated in full because a partial one leaves `/review-brief local high` undefined:

```text
/review-brief <effort>            an external round at that effort
/review-brief local [<effort>]    a local smoke; any effort given is accepted and ignored
/review-brief                     ambiguous — resolve it at step 1 before refusing anything
```

The level is embedded in the exact command the brief emits
(`/code-review <effort> <base>...<head>`).

**Determine the loop before applying any refusal.** A bare invocation is *ambiguous*, not invalid:
it is exactly what an operator types for a local smoke, which needs no effort at all. So ask step
1's question first, and only then apply the rule — **for an external round the effort argument is
required, and a bare invocation is refused**: say the level is missing, name the usual values, and
stop. Refusing before the loop is known turns the most ordinary local invocation into an error;
deciding the loop first and then refusing costs one question.

That refusal is not a defaulting decision left undecided. `/code-review`'s effort is **sticky
across sessions**, so a skill that quietly supplies its own default hides the one value most likely
to be wrong, and the operator would have no signal that the round ran shallower than the last.
Naming it per round is the whole point of the argument.

Note the deliberate divergence: `/review-prep` today documents `Default high`. That default
predates this skill and is reconciled when `/review-prep` is rewritten; until then, a brief that
carries an explicit level and a prep that would have defaulted agree in practice as long as the
brief's level is the one typed into `/code-review`.

A local round takes no effort argument — `spec-reviewer` and `test-reviewer` have no such control —
so an effort given after `local` is accepted and ignored rather than silently applied somewhere.

## 1. Determine the loop

| Loop | Reviewers | Round number | Ledger fragment | Brief form |
|---|---|---|---|---|
| **External** | `/code-review`, run by the operator in a second session | allocated here | created here | a file, handed over by path |
| **Local** | `spec-reviewer` / `test-reviewer`, launched in this session | none consumed | none | emitted inline in this session |

Ask which loop this round is, or take it from the invocation if the operator said. The rest of
this skill is written for the external loop; the local variant's differences are collected in one
section at the end rather than qualified inline at every step.

## 2. Read the ledger

The ledger lives at `specs/reviews/angle-ledger/`. A branch's fragments live under a directory
named for a hash of the branch, so two open branches never contend for a path:

```bash
branch=$(git symbolic-ref --quiet --short HEAD) || { echo "detached HEAD — no branch to key the ledger on"; exit 1; }
b6=$(printf '%s' "$branch" | shasum -a 256 | cut -c1-6)
dir="specs/reviews/angle-ledger/branches/$b6"
```

**`symbolic-ref` and not `rev-parse --abbrev-ref`, and this one fails closed on purpose.**
`--abbrev-ref` prints the literal string `HEAD` on a detached checkout — a `git checkout <sha>`, a
bisect, or a linked worktree pinned to a commit, all reachable in ordinary use here given ADR-0068's
reviewer worktrees. Every such session would then hash the same string and share **one** ledger
directory: rounds from unrelated states pool together, `max(existing)+1` allocates against another
state's fragments, and a per-branch collapse would sweep them up as if they were one branch's.
`symbolic-ref --quiet` exits 1 there instead (measured). This is the same trap and the same remedy
`/savepoint` already documents and ADR-0069 records; it is restated here only as the reason for the
spelling, not as a new rule.

**`shasum -a 256` and not `sha256sum`**, which is a portability choice and not a style one:
`sha256sum` is GNU coreutils and is absent from a stock macOS, while `shasum` ships with Perl and is
present on macOS, Git Bash and WSL alike (the last two measured here). Both print the same digest in
the same format, so `cut -c1-6` is unchanged. Do not "simplify" it back — `ci.yml`'s gitleaks step
does use `sha256sum`, but that job is pinned to `ubuntu-latest`, where the question does not arise.

`printf '%s'` and not `echo`: the hash is over the branch name with **no trailing newline**, and
an implementation that hashes the newline computes a different directory for the same branch. The
hash is used rather than the name because a branch name contains `/`, and because a name used as a
path component was measured normalizing differently across APIs on this platform (ADR-0072 §8).

**Round number.** `N` is `max(existing round numbers in "$dir") + 1`, and `1` when the directory
does not exist. That is a sound derivation because the files being counted are written *by this
skill at allocation* (step 5) rather than discovered afterwards — it is not the retired
file-counting heuristic in disguise.

**It is sound only while the directory has not been emptied under it**, which is a real case and
not a hypothetical: a collapse deletes the branch's fragment directory wholesale, so a
`/review-brief` run afterwards sees no fragments and allocates round 1 again — over numbers already
used, and against the invariant ADR-0072 §8 leans on to make "never recompose a digest" safe (no new
fragment joins a branch's directory once its digest exists). Nothing enforces that, here or
anywhere; it is listed with the other unenforced rules at the end of this file rather than left as
a soundness claim with a hole in it.

**Read every existing fragment's angle record.** From them, assemble three things:

1. **Angles executed**, per round — the roster the next brief must differ from.
2. **Angles briefed but not executed.** An angle with a non-execution history gets a
   **single-angle round**: brief it alone, with nothing else competing for the reviewer's
   attention. Four consecutive passes skipped one angle while every one of them was briefed on
   three or four at once. **A single-angle round still takes every standing entry of step 6's
   roster** — "alone" means no other *unexecuted* angle rides along with it. The standing entries
   cost a sentence each and are not what crowded those four passes out; step 6 defines them, and
   this rule does not re-list them, because a carve-out that names a subset of a roster defined
   elsewhere is how one of them silently stops being asked for.
3. **What each round examined** — the surface it actually reached, not the angle it was briefed
   at. The remainder — artifact minus the union of everything examined — is what the brief must
   put back on the table when step 6 composes it.

**Angle and scope are orthogonal, and scope is the axis that failed** — so record both, and never
let one stand in for the other. **Reviewer silence is not a verdict:** a clause no round asked
about was never cleared, so treat an unexamined region as unexamined rather than as settled.
ADR-0072 §7 holds the measurement these two rules come from and the failure mode they close; do not
restate it here.

**Absence-tolerance, because this skill lands before the steps that fill its inputs.** Fragments
written before `/review-handoff` is rewritten carry no examined-surface field. An absent field
means **unknown**, which resolves to *not examined* — the direction that costs tokens rather than
coverage. Until that rewrite lands, expect the remainder to be the whole artifact, and say so in
the brief rather than reporting a coverage figure the ledger cannot support.

## 3. Lapse the exclusions whose evidence has expired

The do-not-re-run list is a real token win and is kept. What is added is expiry.

**An exclusion is scoped to the text that earned it, and lapses when that text is edited.**
Exclusions otherwise accumulate monotonically, so the reviewed surface shrinks every round while
the artifact keeps changing — which is the mechanism above, arriving by a second route.

Each entry in the ledger's do-not-re-run list carries the paths it covers and the commit the round
that cleared it ran against. At brief time, for each entry, **in this order**:

```bash
# 1. reachability first — an entry whose anchor is gone cannot be tested at all
git merge-base --is-ancestor <cleared-at> HEAD    # exit 0 = reachable; 1 = not; 128 = object gone

# 2. only if that exits 0, ask whether the covered text has changed
git diff --name-only <cleared-at> -- <the entry's paths>
git ls-files --others --exclude-standard -- <the entry's paths>
```

**The order is load-bearing, not presentational, and the reason is the quieter of the two failures.**
An anchor orphaned by `/ship`'s collapse passes through two distinct states, and `git diff` treats
them differently (both measured):

| State of `<cleared-at>` | `git diff --name-only <cleared-at> -- <paths>` | `--is-ancestor` |
|---|---|---|
| Dangling but still present — **immediately after a collapse** | **exit 0, with output** | exit 1 |
| Actually collected, after a `gc --prune` | exit 128, `fatal: bad object` | exit 128 |

The first row is the dangerous one and it is the state the collapse actually leaves behind. Skip
the reachability test there and `git diff` does not complain: it silently compares the current tree
against an **orphaned** one and answers confidently, which is the `silent-wrong` class ADR-0072 §6
ranks worst. The entry then lapses or holds on the strength of a comparison against a state that is
no longer on the branch. The loud 128 arrives only later, whenever `gc` happens to run — so the
same entry behaves one way this week and another way next, with nothing in between to explain it.

Testing reachability first means the content commands only ever run against an anchor that is
reachable, which is the one state in which their output means what the paragraph below says it
means. Any non-zero exit from the reachability test — 1 or 128 — lapses the entry outright, and the
content commands are then not run.

**Two commands at step 2, and neither is optional.** The first compares the clearing commit against the
**index and working tree**, not against `HEAD` — the `<cleared-at>..HEAD` form is a commit-to-commit
diff and returns *empty* for a file edited but not yet committed (measured), which is the ordinary
working state and precisely the state step 4's digest triple exists to cover. An exclusion that
survives an uncommitted edit to the text that earned it is under-expiry, in the one case most
likely to occur. The second catches a path that has appeared since as an untracked file, which no
diff form reports.

Non-empty output from **either** means the entry **lapses**: drop it from the exclusion list and
return those paths to the reviewable surface, saying which round's clearance expired and why. Both
empty means it stands, and it is carried into the brief **with its evidence** — which round cleared
it, at what angle, and what that round concluded. An exclusion with no evidence attached is an
assertion, and the reviewer has no way to weigh it.

**File granularity is deliberate.** It over-expires — an edit anywhere in a file lapses an
exclusion covering one clause of it — and, *given both commands above*, never under-expires.
Under-expiry is the measured failure; over-expiry costs tokens. That claim is a property of the
pair, not of file granularity on its own: run only the commit-to-commit form and the scheme
under-expires on every uncommitted edit. A finer scheme needs a way to address a region that
survives edits above it, which line numbers do not.

**Why the reachability test above comes first, and why it is `--is-ancestor`.** `/ship`'s collapse
(ADR-0069) rewrites a savepoint branch into one commit and leaves every pre-collapse SHA dangling —
present in the object database, absent from the branch's history. An entry anchored there cannot be
evaluated, and an entry that cannot be evaluated is not an entry that holds. Where the round also
recorded a tree hash, an anchor that fails the test may still be identified through it; where it
cannot, the entry lapses. Both non-zero exits lapse it; the table above is the authoritative
account of which state produces which, and this paragraph does not restate them.

**Reachability, not existence — `git cat-file -e` is the wrong instrument here and passes on
exactly the case this rule exists for.** A dangling commit is still an object: `cat-file -e` exits 0
on it (measured), so the entry would be judged evaluable and the step-3 diff computed against a
commit no longer in the branch's history — comparing the current tree to an orphaned state, and
flipping behaviour the first time `gc` runs. `--is-ancestor` asks the question the rule actually
poses. The sibling skills already draw this line the same way: `/apply-review` and `/land` use
`cat-file -e` for *existence* of an anchor and `--is-ancestor` for *reachability*, which is the
split ADR-0069 records.

## 4. Pin the scope, and state the size honestly

Record **three anchors**, per ADR-0072 §4 — the base **resolved to a commit SHA**, `HEAD`, and
`HEAD^{tree}`. The base is a ref (`origin/main` by default) and advances underneath a record that
holds only the head anchors; the tree is what survives `/ship`'s collapse when the SHA does not.

**Expiry digest.** The brief records the tree state it was written against so `/review-prep` can
tell whether the tree moved underneath it. `git status --porcelain` is **not** sufficient alone: it
reports each path's *status*, not its content, so a second edit to an already-modified file leaves
the listing byte-identical — and an edit to a file the brief already knew was dirty is the most
likely post-brief change there is. Use the content-covering triple that
`.claude/reviewer-isolation.md` § The two invariants, invariant 1 already documents — `HEAD^{tree}`,
`git stash create`'s tree, and `git hash-object` over untracked-but-not-ignored paths — and do not
restate its reasoning here. Record the porcelain listing beside the digest as the human-readable
companion, never as the check.

**Exclude `specs/reviews/angle-ledger/` from the digest's untracked enumeration.** Without that
exclusion the check cannot ever pass, and it fails on a tree nobody touched — the
permanently-false-alarm outcome the paragraph below and ADR-0072 §4 both call worse than no check.
The fragment this brief is about to allocate (step 5) is untracked-but-not-ignored, which is exactly
the class the third leg of the triple covers, and step 6 then edits that same fragment again to
write the stamp in. **Ordering alone does not rescue it**, which is why the exclusion rather than a
reordering is the fix: the brief's bytes contain the digest, the digest would cover the fragment,
and the fragment contains a hash *of the brief's bytes*. There is no fixed point. Excluding the
ledger removes the cycle at its only breakable link, and it is the same exclusion the diff-size rule
below already applies for its own reasons.

**Diff size.** State it against the ~1,000-line cliff at which defect detection drops ~70%, and
recommend a split above it (ADR-0072 §7).

**Exclude the ledger from that number.** Fragments are committed and the pinned scope is
`<base>...<head>`, so round 4's diff contains rounds 1–3's fragments. Counting them pushes a later
round toward a split recommendation caused by the instrument rather than the work.

**And for ungated prose, recommend a split well below the cliff.** The 1,000-line figure was
measured on code; against this repository's own merged prose PRs, review load is already heavy at
less than half of it, so treat **~350–450 lines** as where a prose round starts costing. It is a
refinement scoped to a different artifact class, not a second cliff — ADR-0072 §7 holds the
measurement and the PRs it rests on. Say which number applies to the artifact in hand.

## 5. Allocate the round number by creating its fragment

**Allocation writes something, or the number is not durable** (ADR-0072 §5). Create

```text
specs/reviews/angle-ledger/branches/<b6>/round-<N>.md
```

holding the round's scope and angle roster and nothing else. Without that write, a round briefed
and then abandoned leaves no trace that `N` was consumed, and the next brief — especially from a
fresh session — re-issues the same number to a different round.

**Creating it is not enough — it has to reach the branch, and this skill does not commit.**
An untracked fragment leaves no trace *on the branch* at all, which is the very durability the
paragraph above claims for it, and it dies to a `git clean` or a fresh worktree. Three parts of the
design assume the fragment is committed: this step's durability argument, ADR-0072 §8's collision
argument (a clash surfaces as an add/add conflict "since fragments are committed"), and
`/squash-merge`'s future collapse, which reads a branch's fragments through `origin/main...HEAD`.
Commits here belong to `/savepoint`, which requires an explicit enumerated path list and forbids
`git add -A`, so **name the fragment's path in the handoff (step 7) as owed to the next
`/savepoint`**. Until it is committed, treat every durability claim above as pending rather than
satisfied.

**`N` is consumed by fragment creation, not by brief issuance** — so a re-run of
`/review-handoff` reuses it, an abandoned round keeps it, a revised brief does not take a new one,
and a local round takes none at all. ADR-0072 §5 works through why each of those four cases needs
it that way; the rule here is the whole of what this step must obey.

The fragment this skill writes:

```markdown
# Round <N> — <branch>

## Angle record

- **Date:** <YYYY-MM-DD>
- **Loop:** external
- **Effort:** <the argument>
- **Surface:** _not yet filled — /review-handoff_
- **Base (resolved):** <sha>
- **HEAD:** <sha>
- **HEAD tree:** <tree hash>
- **Diff size:** <n> lines (<m> excluding the ledger)
- **Brief revision stamp:** _not yet filled — step 6, once the brief has been named_
- **Angles briefed:** <the roster, assembled per step 6's rules and known by now>
- **Angles executed:** _not yet filled — /review-handoff_
- **Briefed but not executed:** _not yet filled — /review-handoff_
- **Examined:** _not yet filled — /review-handoff_

### Do-not-re-run, carried into this round

| Excluded | Paths | Cleared at | Cleared by | Evidence |
|---|---|---|---|---|
```

**Two of those fields are owed by this skill, not by a later one, and the order is why.** The
stamp is a hash over the composed brief's bytes, which do not exist until step 6 — so allocation
writes the placeholder and step 6 comes back and fills it. The roster is the reverse: assemble it
first (step 6's roster rules) and write it here, because ADR-0072 §5 has allocation record the
scope *and the roster*. Neither is a licence to guess: **write the placeholder rather than a value
you do not have yet.** Everything marked `/review-handoff` is owed by that skill and is never
filled here.

`/review-handoff` fills its own placeholders and appends the round record; `/apply-review` amends
it with severity and disposition. **This skill never invents a value for a field a later step
owns**, and an unfilled field says which step owes it.

**The fragment is machine-written and must be lint-clean by construction**, permanently — and
from the moment it is written, not from the moment it is committed. Both markdown gates enumerate
`git ls-files --cached --others --exclude-standard`, so PyMarkdown and
`scripts/check_spec_links.py` walk an untracked-but-not-ignored fragment exactly as they walk a
tracked one; a dirty fragment reddens the gates while it is still uncommitted. Tag every
fence with a language, leave one blank line around fences and headings, and never emit two
consecutive blank lines. The repository has already paid once to clean 46 lint sites; a generator
that emits dirty markdown converts that into a recurring cost.

**Personal data never reaches a fragment.** A fragment is committed and travels to a public
remote, which a session-scratchpad report never did. Record a review source that quotes personal
data by **data category alone** — never the value, and **never the path**, because a path under
the containment directory names the lab and the panel and is therefore provenance, which
`CLAUDE.md` classes as personal on its own. Where a reference must be re-findable, use an opaque
id the orchestrator can resolve locally. The containment gate will not catch a violation here: it
keys on the path, and a fragment's path is under `specs/reviews/`.

## 6. Compose the brief, and stamp it by name

Sections, in this order: **the round and its scope** (the three anchors, the effort, the diff size
and what it excludes); **angles for this round**; **already verified, with evidence**;
**do-not-re-run, with evidence** (step 3's survivors); **settled, with reasons**; **the
orchestrator's own uncertainties, numbered**; **the reporting bar**; and **the exact command to
run**.

**Gate results are mechanically filled.** Run the gates through `python3 scripts/run_gates.py` —
never assemble their commands by hand — and state which were green at brief time. The interpreter
is part of the invocation, twice over: the script carries no execute bit, so a bare
`scripts/run_gates.py` is "Permission denied" on every POSIX leg and appears to work only on
Windows — and the spelling is `python3`, per ADR-0077 §8, which registered it as the repository's
interpreter and is the spelling `ci.yml` uses throughout. Measured on this machine: `python` does
not exist in WSL at all, while `python3` resolves on both legs. A reviewer told the
gates are green spends its attention elsewhere; one told nothing re-derives it.

**The angle roster.** Different angle each round, drawn from what step 2 showed unexecuted, plus
these standing entries:

- **The economy angle** — *does this artifact carry more claims than the job it exists to do
  needs?*, judged against the artifact's stated jobs rather than against correctness. Stand it up
  for any ungated-prose round; ADR-0072 §7 holds the measurement that earned it a standing slot.
- **The remedy angle** — the previous round's fixes, which are reliably the highest-yield surface.
  ADR-0072 §7 records what that cost on the round that motivated it; do not generalize the figure
  past what it says.
- **The whole-artifact angle** — no exclusions, the entire changed artifact. It is not redundant
  with the remedy angle: the two find **disjoint** classes, which is why both stand. ADR-0072 §7
  holds the measurement.

**Precedence, because two of these instructions conflict and a brief that states both without
ranking them is worse than a brief that states one.** The do-not-re-run list from step 3 and the
whole-artifact angle contradict each other by construction: one narrows the surface, the other
mandates all of it. Resolve it by **dispatch, not by wording** — the exclusion list governs the
differentiated angles, and the whole-artifact angle is dispatched as **its own round or its own
agent, carrying no exclusion list at all**. Leaving it unranked in one brief means either the
exclusions are dead text that round, or the whole-artifact angle is silently narrowed to the
un-excluded remainder — which is the blind spot the angle exists to catch, reintroduced by the
brief that commissioned it. A brief that cannot dispatch them separately says which one it is
running this round.

The value of the last two scales with how ungated the artifact is — highest for `CLAUDE.md`, ADRs
and skills, lowest for tested code — so state the roster as a recommendation for the artifact in
hand, not as an unconditional cadence.

**Number the uncertainties.** The orchestrator's own open questions go in as a numbered list,
because `/review-handoff` is obliged to answer them by number. This is prompted and never
substituted: an invented uncertainty is worse than none, since it directs real attention at a
question nobody had.

**Write it, hash it, then name it.** The brief's revision stamp **is its filename** — a stamp
stored inside the file proves the wrong thing, because editing the brief updates its self-described
stamp too and every downstream check still agrees. So: write the composed brief to the session
scratchpad, compute `sha256` over the file's **bytes**, take the first eight hex characters as
`<h8>`, and rename to

```text
brief-<N>-<h8>.md
```

Read and write it as **UTF-8 without a BOM, with LF line endings, in binary**. On this platform the
default tooling rewrites both: `CLAUDE.md` § PowerShell file encoding records that a
`Get-Content`/`Set-Content` without `-Encoding UTF8` silently transcodes to Windows-1252, and these
briefs are em-dash and arrow prose throughout. A prep that reads with a default-encoding call
computes a different digest and the mismatch fires on a brief nobody touched — which is worse than
no check, because a permanently-false alarm trains the operator to ignore the one signal that says
a brief changed under its reader.

**Then write `<h8>` into the fragment's `Brief revision stamp` field**, replacing the placeholder
step 5 left there. This is the one place the skill returns to an artifact it has already created,
and it is the last thing it does before handing over — a fragment still carrying the placeholder is
a round whose brief was never named, which is a different fact from a round in progress.

**A revised brief is a new file with a new name**, handed over deliberately. An edited brief no
longer matches its own filename, so the edit is loud instead of invisible. It is the same round,
consumes no new `N`, and its new `<h8>` overwrites the stamp in the same fragment.

## 7. Hand it over

Present the brief's path per `.claude/operator-handoff.md` — absolute, resolved, alone in its own
fenced block. The scratchpad root contains a per-session UUID that exists nowhere the reader can
look it up, so an elided or templated path is not merely untidy but unusable.

Then state, in prose:

1. That the brief is handed to the **reviewer session** — a different session from this one — and
   that nothing downstream may read it after `/review-prep` absorbs it. From prep onward there is
   exactly one carrier; a second artifact able to go stale alone is what the revision stamp exists
   to catch.
2. The round number allocated, and the fragment path it was allocated by — named as **owed to the
   next `/savepoint`**, since this skill does not commit and an uncommitted fragment leaves no
   trace on the branch (step 5).
3. That `/review-prep` runs next, in the reviewer session. **Do not tell the operator it will
   verify the stamp or the expiry digest: today it does neither.** ADR-0072 §4 specifies both
   checks and `/review-prep` gains them in BRIEF-4; until then the brief carries the two values and
   nothing compares them, so a brief edited or superseded under its reader goes undetected. Say
   that plainly — the same way the effort-default divergence is flagged above. A promised check
   that does not run is worse than an absent one, because the operator stops looking.

Do **not** run `/code-review`, and do not simulate a review from your own reading of the diff.

## The local-round variant

For a `spec-reviewer` or `test-reviewer` smoke, everything above holds except what the table in
step 1 lists. Concretely:

- **No file, no stamp, no round number, no fragment.** The brief is emitted inline in this session
  and pasted into the agent's prompt. The stamp exists for a cross-session hop; a smoke has none.
  Local rounds are deliberately kept out of the ledger's counts — the owner classes them as tests
  run by hand, and counting them inflates the metric the keep-sharpening-or-ship decision is bought
  with.
- **Steps 2 and 3 still run.** The angle history and the exclusion expiry are what make a smoke
  cheap; the 172k → 85k measurement came from a briefed local round. Read the ledger even though
  this round will not write to it.
- **One angle per agent.** Two reviewers briefed at one angle is a redundant round; two agents at
  two angles is a wider one. The standing entries of step 6's roster still apply to each.
- **Step 6's "exact command to run" section is empty for a local round**, and says so rather than
  being omitted: there is no command, because this session launches the agents itself. Everything
  else step 6 composes is written as normal.
- **The last smoke before `/land` is where the whole-artifact angle is dispatched** — its own
  round, no exclusion list at all. This is step 6's precedence rule applied to the local loop
  rather than a second rule: the differentiated angles run with exclusions in the earlier smokes,
  and this fixed slot is the round that carries none. It is what catches the narrowing steps 2 and
  3 describe, and skipping it because the previous rounds were clean is exactly the reasoning that
  carried two false clauses through three rewrites.
- Launch the agents per `.claude/reviewer-isolation.md`, which owns whether they may run in
  parallel.

## What this skill does not enforce

Named rather than left to read as settled, in the spirit of ADR-0072 §10:

- **Nothing verifies that `N` came from here** rather than from a session counting files. The cheap
  mechanization, if one is ever wanted, is that a fragment whose `N` is not `max(existing) + 1` for
  its branch is a detectable error.
- **Nothing moves a branch's ledger directory when the branch is renamed.** `<b6>` is derived from
  the branch *name*, so a rename orphans the existing fragments under a stale hash and restarts
  allocation at round 1 against an empty directory. Rename a branch and move
  `specs/reviews/angle-ledger/branches/<old b6>/` by hand, or do not rename it.
- **Nothing watches the round's own fragment between brief and prep.** Step 4 excludes
  `specs/reviews/angle-ledger/` from the expiry digest's untracked leg, because including it makes
  the digest unsatisfiable (ADR-0072 §4 records the cycle). The cost is that an edit to *this
  round's* fragment — its scope, its roster, its stamp — is the one change the expiry check cannot
  see. The exclusion is scoped to that one leg, so edits to already-tracked ledger content are
  still caught by the other two; it is the new, still-untracked fragment that goes unwatched.
- **Nothing stops a fragment being added to a branch's directory after that branch's digest
  exists.** ADR-0072 §8 leans on that invariant to make "never recompose a digest" safe, and
  allocation computes `max(existing) + 1` against whatever is on disk — so a collapse that has
  emptied the directory (or crashed part-way through deleting it) is followed by an allocation
  that restarts at round 1, re-issuing numbers the digest already holds. This is the case step 2's
  soundness paragraph defers to.
- **Nothing compares a brief's stamp against the fragment's.** A revised brief writes a new `<h8>`
  into the fragment, but an *unedited superseded* brief still hashes to its own filename, so it
  passes the only check ADR-0072 §4 defines and the reviewer runs against a brief the orchestrator
  has replaced. The fragment is the sole artifact that knows better, and nothing instructs prep to
  read it. Closing this is BRIEF-4's, when prep gains the stamp check at all.
- **Nothing checks the containment rule on capture.** It keys on a path, and a fragment's path is
  never under the containment directory. This is human judgement at capture time, and the ledger is
  a new place it must be exercised.

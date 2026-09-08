# ADR-0079: Personal Data Lives Outside the Repository Tree (extends ADR-0070 and ADR-0068)

## Status
Proposed

## Context and Problem Statement
[CLAUDE.md](../../CLAUDE.md)'s first rule confined personal health data to one gitignored directory inside the working tree, `specs/personal/`. The placement bought nothing: the folder was unversioned, never pushed, and absent from every clone. What it did do was make the boundary between public and personal a *path inside the tree*, and that boundary is the sole reason a defence stack exists and keeps growing.

Four measured facts drove the decision.

1. **The boundary has been defeated repeatedly, silently, by tools that were clean under review.** [ADR-0070](0070-personal-data-containment-gate.md) records six holes in the prose containment scan that independent review passes each reported clean. [ADR-0068](0068-reviewer-isolation-worktrees.md) records the snapshot tool's containment claim defeated across its fifth, sixth and seventh review passes: a directory junction replicating content from outside the tree, a plain file at the bare path `specs/personal` (which a directory-only ignore rule does not cover), a link inside the repository pointing at the directory (a junction on Windows, a symlink on POSIX), and a hard link. The containment gate's own docstring concedes that the rule's prose copies could not be enumerated: six consecutive attempts, each wrong.
2. **Every one of those guards covers git surfaces only.** Any tree-walker that ignores `.gitignore` — an archiver, a packager, a publish-this-folder mistake, a future tool nobody wrote a carve-out for — sees the files. The evaluation in [open-questions.md](../open-questions.md) of a local agent CLI found exactly this: the agent read a gitignored file on the first attempt, and a per-tool deny list was routed around through a tool it did not name.
3. **`git clean -xdf` deletes ignored files.** The only copy of the owner's notes sat one routine command from deletion. This is a data-loss footgun, not only a leak footgun.
4. **The repository is public, so a pushed personal blob is unrecoverable.** Maximal severity wants prevention by construction, with detection as the backstop — not detection as the only line.

The move converts the enumeration-leak class from *detected* to *impossible by construction*: no path inside the repository holds personal data, so there is no in-tree boundary for a tool to cross.

## Decision Drivers
- Prevention by construction over detection: a boundary that does not exist cannot be defeated.
- As little code as possible — but a guard is only surplus once an audit shows it neither replicates, names, nor reads what a recreation would put back. The first draft of this ADR skipped that audit and specified three deletions that review showed unsafe (§4).
- The content half of the rule — values or provenance paraphrased into public prose — is location-independent. Nothing here may be read as weakening it.
- [ADR-0071](0071-commit-shared-claude-settings.md)'s split: machine-specific values never enter the shared repository.

## Considered Options
1. **Keep the folder in-tree and keep growing the guards** — the status quo.
2. **Move the data out of the tree and delete every guard whose threat model was "data lives in-tree"** — this ADR's first draft.
3. **Move the data out of the tree; retarget every path guard as a backstop against recreation; delete nothing until a per-guard audit shows it has no replication, naming, or reading surface** (chosen).

## Decision Outcome

### 1. The rule
Personal health data and personally identifying information live in a **sibling directory outside the repository**, with its own private version control and its own backup. **No path in this repository may hold personal data, and `specs/personal/` must never exist.** The directory's location is never written into the repository: it is recorded in `.claude/settings.local.json` (an `additionalDirectories` grant, which is what lets a session read it without prompts) and in agent memory, per ADR-0071's rule for machine-specific values.

The content half of CLAUDE.md's rule is unchanged: analyze real reports in conversation or in the personal directory, publish only the generic structure they reveal, and treat the provenance or sequence of the owner's actual records as personal even with no values attached.

### 2. The two threats that survive the move
- **R1 — recreation.** A session writes personal data at or under `specs/personal/` again — a directory's descendants, or a plain file at the bare path — or anywhere in-tree. Path machinery can address this for exactly one path name, and only at the points where git or a tool would otherwise carry the recreated file somewhere public.
- **R2 — content.** Values or provenance paraphrased into public prose. Location-independent, unchanged by the move, and stays a prose and judgement control — ADR-0070 §4 already declined to mechanize it, and this ADR does not reopen that. A recreated file's *name* is R2 as much as its contents: a filename under the old path is provenance.

### 3. What stays, and what each protects now
Each protection was asked one question: *what does it protect once no personal data exists inside the tree?* Every path guard answers the same way — a recreation — and they differ in **where** they stop it from travelling.

- **`.gitignore`'s `specs/personal/` line.** An accidental recreation stays untracked, so nothing stages it by accident. The alternative — dropping the line so a recreation is *visible* — is worse, because `/savepoint`'s explicit-path staging is the only thing between an untracked file and a commit.
- **`scripts/check_personal_containment.py`, its tests, the CI history step, and the `/land` and `/savepoint` invocations stay whole.** The gate is the backstop against a recreation that is **force-added, tracked, staged, or committed**. It is blind to an ignored, untracked recreation *by design* — its sources are git-visibility sources, and the `.gitignore` line above is what keeps such a file out of them — so "backstop against recreation" must be read with that qualifier wherever the gate is described. Its docstring records six wrong attempts to enumerate its holes, so re-deriving that list inside a deletion change is the remedy-riskier-than-the-finding shape; its shrink is logged in [open-questions.md](../open-questions.md) and is not part of this change.
- **The reviewer bots' "flag personal data anywhere, quote nothing" rules.** Their substance is R2. Their wording changes from "the only location is `specs/personal/`" to "no location in this repository". (gitleaks is a credential scan; it is unaffected and is not a content control for health data.)
- **The reviewer bots' `specs/personal/` exclusions** — CodeRabbit's `path_filters` entry, Greptile's `ignorePatterns` entry, and `EXCLUDED_GLOBS` in `scripts/gemini_review_logic.py`, which also drives the pre-tool hook that refuses any Gemini agent call naming an excluded path. Each list carries both recreation shapes — the directory's descendants and a plain file at the bare path, which the trailing-slash `.gitignore` rule does not cover — spelled with a character class per letter so the match is case-insensitive: none of the three dialects has a case-insensitive switch, fnmatch on the POSIX CI runner is case-sensitive (measured: the lowercase glob misses `Specs/Personal/x.md` there), and the containment gate already matches with `:(icase)`, so a mixed-case force-add the gate rejects must not reach a bot either. Two parity tests, each comparing one of the other copies against CodeRabbit's, hold the three in step; the identical spelling is what lets them. A force-added recreation is exactly the commit they keep a bot from quoting into a public comment, while CI's containment gate is what fails it. Because the filter hides the path, the bots' instructions **say so**: they flag personal data anywhere in the input the bot receives, and state that files at or under the excluded path never reach it. Copilot has no path filter; its instruction tells it to report only that a file exists at or under that path, never its name or contents.
- **The snapshot tool's personal-path guards in `scripts/review_worktree.py`** — the tracked-personal abort, the untracked and link filters, the hard-link identity check, and the output capping that rests on them. They are not duplicates of the general boundary test `_escapes`: that test refuses a link resolving *outside* the repository, while a force-added `specs/personal/<file>` resolves inside it, and a hard link resolves nowhere. The tracked-personal abort fires **ahead of** the snapshot, which is the only point at which a recreation can be kept out of the two agent-readable worktrees; CI's gate runs after. The one route no guard covers is a hard link from the repository to a file in the new out-of-tree directory, recorded under Negative Consequences.
- **The link gate's two `specs/personal/` filters in `scripts/check_spec_links.py`** — the target skip and the source-set exclusion — and the diff mirror's guard that requires `PERSONAL_DIR` to exist. Removing the target skip would print a link's `specs/personal/<name>` target into the public CI log; removing the source filter would make a force-added recreation a crawl source and print its filename on any error inside it. The cost of keeping them is that a dead link into a folder that must not exist is skipped rather than failed; that link already sits in tracked prose, so the skip publishes nothing new.
- **The stats exclusion in `scripts/repo_stats.py` and the pymarkdown `:(exclude)specs/personal/**` in `scripts/run_gates.py` and CI.** A recreation is neither read into a stats report nor named in a lint log.

### 4. What may go later — audited, not assumed
The first draft of this ADR listed the snapshot guards, the link-gate skip, and the stats and lint carve-outs for deletion in a second PR, on the claim that the move left them idle or duplicated. External review of that draft found three concrete failures the deletions would have caused: the diff mirror's rename guard refuses at exit 2 once `PERSONAL_DIR` is gone and its parametrized test errors; the link gate prints a personal filename into a public CI log; and the snapshot tool checks a force-added recreation into both reviewer worktrees before CI can reject it. The claim that `_escapes` duplicated the snapshot guards was false for every guard named.

So nothing is deleted by this ADR, and no second PR is scheduled. A guard becomes a deletion candidate only when an audit of its call sites shows it neither **replicates** a recreated file (into a worktree or snapshot), **names** it (in stdout, a CI log, a manifest, a review comment), nor **reads** it. The one shrink already booked — the containment script's — is recorded in [open-questions.md](../open-questions.md) with that bar.

### 5. Sequencing
The rule change (this ADR, CLAUDE.md, the skills, the reviewer configurations, the specs prose) lands as one change. Only after it merges is the in-tree directory deleted from the working tree — it is untracked, so git is not involved, and the out-of-tree copy is verified untouched before and after. No tombstone or README is left behind: a tracked file at that path is the violation the backstops exist to catch.

### Positive Consequences
- The enumeration-leak class is closed by construction rather than by a growing list of detectors, and the data-loss footgun is gone.
- The personal directory gains version history and an off-machine backup, which the in-tree placement structurally denied.
- Every retained guard now has a named threat and a named surface. The guard stack no longer grows for the live-data case — there is no in-tree data for a new tool to need a carve-out for — but a new tool still gets §4's question asked of it, because a tool that would replicate, name, or read an ignored recreation is a surface for R1 whether or not the data is present today.

### Negative Consequences / Tradeoffs
- **A junction, symlink or hard link from the repository to the new directory is R2-class, not R1.** On Windows git walks through a junction and reports its files as ordinary untracked paths under whatever name the junction has, which the `.gitignore` line does not cover; `_escapes` stops the snapshot tool for junctions and symlinks, and nothing stops a `git add -A`. A hard link resolves nowhere, so no path predicate sees it at all. This is the same exposure as writing personal data into any other directory, and ADR-0070 already declined to build for it.
- Sessions must know where the directory is. The `additionalDirectories` grant and memory carry that; a fresh machine has to be told.
- **The guard stack does not shrink with this change.** The "as little code" driver is served only by the audited follow-ups in §4.
- **No personal path has ever been committed** — CI's history scan, a path detector, has never fired — so the move creates no rewrite. Whether a value was ever paraphrased into a file and later sanitized is the content half, which no scan measures; that residual predates this ADR and is unchanged by it.

## Pros and Cons of the Options

### Option 1 — keep the folder in-tree and keep growing the guards
- Pro: no change.
- Con: every fact in the problem statement. The guards have been defeated repeatedly across three review passes, cover git surfaces only, and defend a copy that one routine command deletes.

### Option 2 — move the data and delete the guards whose threat model was "data lives in-tree"
- Pro: less code, and a net-negative second PR that reviews cheaply.
- Con: the deletions were specified from a path-string sweep rather than a call-site audit, and review found three of them unsafe (§4). The threat model "data lives in-tree" does not vanish with the move — it becomes "data is recreated in-tree", and the same guards are what stop a recreation from being replicated, named, or read.

### Option 3 — move the data; retarget every guard; delete nothing unaudited (chosen)
- Pro: the rule change lands without weakening any surface; each guard's surviving job is written down beside it; deletion stays available per guard once its audit is done.
- Con: the guard stack keeps its current size, and the containment script stays larger than it needs to be until its shrink is done separately.

## Links
- Extends: [ADR-0070](0070-personal-data-containment-gate.md) — the enumeration gate is retargeted from "fence around the data" to "backstop against a force-added, tracked or committed recreation"; its content-half residual (§4) is unchanged
- Extends: [ADR-0068](0068-reviewer-isolation-worktrees.md) — the snapshot tool's `specs/personal/` guards (§4 and the fifth to seventh review passes) are retargeted as recreation backstops, not retired; `_escapes` remains the out-of-tree boundary test beside them
- Related: [ADR-0061](0061-markdown-link-check-gate.md) — the link gate's `specs/personal/` filters are retained as the guard against a recreated filename reaching the CI log
- Related: [ADR-0062](0062-markdown-style-lint-gate.md) — the pymarkdown exclusion is retained for the same reason
- Related: [ADR-0071](0071-commit-shared-claude-settings.md) — the rule that keeps the directory's location out of the shared repository
- Related: [ADR-0069](0069-local-checkpoint-commits.md) — the explicit-path staging that makes the `.gitignore` line the right backstop; its containment-scan clause is corrected in place to drop the old carve-out
- Related: [open-questions.md](../open-questions.md) — the local agent CLI evaluation whose posture (containment by construction, never by a path blocklist) this ADR delivers for the repository itself, and the containment-script shrink entry §4 books
- Related: [CLAUDE.md](../../CLAUDE.md) — the containment section this ADR rewrites

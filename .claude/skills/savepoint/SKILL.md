---
name: savepoint
description: Make a local checkpoint commit — containment scan, explicit git add path list, commit. Local-only scaffolding that /ship collapses into the composed commit before the first push. Use after each finding batch in /apply-review, before a smoke pass, or at end of session.
---

# /savepoint — a local checkpoint commit

Three steps, nothing else (ADR-0069). No ruff, no pyright, no pytest, no `Decisions:` section —
the absent gates are what keep this cheap enough to actually run, and the landing commit that
`/land` composes is where all of that belongs. A savepoint is scaffolding: `/ship` collapses
`merge-base..HEAD` into the single `/land`-composed commit before the first push, so nothing
here ever reaches public history as itself. The reflog keeps the chunks.

Refuse to run unless HEAD is on a branch, and unless that branch is something other than `main`.
On `main` a checkpoint has no `/ship` collapse ahead of it (that skill refuses `main` too). On a
**detached HEAD** it is worse: nothing in the three steps below is branch-aware, so all of them
succeed (measured) and leave a commit on no branch — the next `git checkout` orphans it into the
reflog, `/ship` step 1 fails closed because `git rev-parse --abbrev-ref HEAD` answers the literal
`HEAD` which no sidecar holds, and no `merge-base..HEAD` collapse can ever reach it. The scan
runs, the work is committed, and the one property the commit was made for — that `/ship` collapses
it into the landing commit — is void. Detached HEAD is reachable in ordinary use: a `git checkout
<sha>` while inspecting a savepoint anchor, a bisect, an aborted rebase. `git symbolic-ref -q HEAD`
is the direct test, exiting 1 when detached (measured). Branch first, then checkpoint. Never push
from this skill.

## 1. Containment scan

The one step that is never optional. The enumeration half is mechanized (ADR-0070):

```bash
python scripts/check_personal_containment.py --scope worktree
```

Exit 0 is the only pass; `--scope worktree` is this chunk alone. It checks the porcelain for any
containment path (matched case-insensitively, covering the bare path `specs/personal` as well as
the prefix), every tracked path under the containment directory, and — this is the half a path list
cannot do — that each staged path's index blob really is the working-tree content the scan just
read. The reasoning for each of those lives in `scripts/check_personal_containment.py`'s docstring,
next to the code it governs, and `tests/test_check_personal_containment.py` pins every one of them
as a fixture that must fail.

**A `Note:` line is not a failure.** Running here — *before* step 2 stages anything — an ordinary
"staged earlier, then kept editing" path is expected, and the gate reports it as a note rather than
failing: git shows it, `git add` resolves it, and step 2 reconciles the staged set against the
enumerated list anyway. Only a divergence git actively **hides** — a `skip-worktree` or
`assume-unchanged` entry — is a violation. An unresolved **merge conflict** is neither: the gate
reports "could not run", because a conflicted path has no single staged version and `git commit`
would refuse regardless. Finish or abort the merge first. It keeps scanning either way — the
enumeration that needs no index still runs, and anything it found is printed alongside the refusal
rather than lost to it, with an `Examined before stopping:` count saying how much of the scan ran.

**On any refusal, a missing `Note:` line is not information — do not read it as "no notes were
due".** A note exists only where a staged-content comparison got far enough to produce one, and
most refusals fire before that: the unmerged case refuses before the comparison starts, and the
not-the-top-level case below refuses before *anything* runs, carrying no findings or counts either.
Stated as a rule rather than as a list of which refusals qualify, because that list has been
enumerated wrongly every time it has been attempted. Whether a refusal carries notes depends on how
far the scan got before it fired — not on which refusal it is — so the roster changes whenever the
scan's order does.

One refusal means something else entirely and should not be retried: **the scan was not pointed at
the repository's top level.** Git spells paths inconsistently below it, so the gate declines rather
than match against a vocabulary it cannot trust. That is a wrong-directory problem — a checkout
nested inside an outer repository, or the script invoked by absolute path from another tree — not
anything about this checkpoint's chunk.

`/land` runs the same gate at `--scope branch`, which adds the whole-branch history walk. That is
`/land`'s backstop over every accumulated savepoint and is deliberately not run here, where the
scope is this chunk: a savepoint that paid for a full history walk on every checkpoint is one an
operator stops running. The two scopes are one implementation with one test suite, so the split is
now a flag rather than two prose copies that can drift apart.

Then the half no gate decides: for every added or modified file outside `specs/personal/`, confirm
it contains no personal health values, lab results, diagnoses, medications, or owner-identifying
information. Test fixtures must be synthetic.

This runs before **every** savepoint because a commit object outlives `--amend` and `reset` in
the reflog — the push gate (`/land` then `/ship`) keeps a stray on this machine, but only this
scan keeps it out of the object store in the first place.

## 2. An explicit path list

Enumerate the files this checkpoint covers, echo the list, then `git add` exactly those paths.
**Never `git add -A`, never `git add .`, never `git commit -a`.** The enumeration is the whole
distinction from the mechanism ADR-0069 rejected (an automatic `add -A` at reviewer launch):
the session names what enters the object, after the scan has seen it. A file you did not list
does not go in — if the porcelain shows something you cannot account for, stop and account for
it first. **Then prove the index matches the list before committing**: `git commit` commits the
whole index, so anything staged *before* this skill ran — an aborted earlier operation, an
editor's git integration, a manual `git add` — rides into the commit unenumerated and unscanned.
Run `git diff --cached --name-only`, compare it against the echoed list, and stop on any extra
path; the enumeration is only a containment control if the commit provably contains nothing
else.

**That path list is necessary and not sufficient — the staged *content* must match too.**
`git diff --cached --name-only` proves which paths are in the index, never that each one holds the
bytes the scan read, and the two diverge wherever `git add` silently declines to update an entry.
It does exactly that on a `skip-worktree` or `assume-unchanged` path: measured, a file staged with
personal data and then cleaned in the working tree keeps the **dirty blob** in the index, satisfies
the path check, and commits. `git add` exits 1 and names the path there, so **treat any non-zero
`git add` as a stop** — but do not rest on that alone.

Step 1's gate already proves per-path identity, so **re-run it once the index is staged**:

```bash
python scripts/check_personal_containment.py --scope worktree
```

That is the second run of the step, not a duplicate of it: the first sees what you are about to
stage, this one sees what would actually be committed, and only the second can catch an entry
`git add` declined to update. It skips deletions (which contribute no bytes) and compares a rename
against its *destination* (the source exists in neither the index nor the working tree). The
comparison is blob-to-blob, so a CRLF working file under `.gitattributes eol=lf` is not a false
alarm — a check that fires on line endings is one an operator learns to skip.

**A `Note:` surviving into this second run is worth reading, unlike in step 1.** By here you have
`git add`-ed every path you enumerated, so a staged path still differing from the working tree is
one you did not list — which the path-list reconciliation above should already have stopped you on.
Treat it as the same finding arriving by a second route, not as noise.

## 3. Commit

A bracketed phase tag, then one plain imperative subject line describing the chunk. No body
required, no `Decisions:` section, no co-author trailer. Then report the short SHA and stop.

**The tag says where in the work item this checkpoint sits**, because a branch mid-iteration is a
pile of subjects that all read like ordinary commits, with nothing distinguishing the original
build from the fourth correction of one finding:

| Tag | Phase |
|---|---|
| `[b]` | build — the work item's own work, whenever it happens; usually before any external review, but also an owner decision landing mid-apply |
| `[bs<M>]` | smoke `M` — a local `spec-reviewer`/`test-reviewer` pass during the build |
| `[x<N>]` | applying external **review** `N`'s findings |
| `[x<N>s<M>]` | smoke `M` inside that apply |
| `[g]` | a standalone gate fix — CI or a local gate came back red outside any round |
| `[l]` | land preparation |

```text
[x2s3] Make the sidecar write order load-bearing and its comparison line-ending safe
```

**`review` and `smoke` are separate words here, deliberately.** A **review** is a round of the
purchased external loop (`/review-brief` → `/review-prep` → `/code-review` → `/review-handoff` →
`/apply-review`) — metered, and the only kind counted in a convergence ledger. A **smoke** is a
local `spec-reviewer`/`test-reviewer` pass: free, run as often as it takes, and never assurance
evidence to report alongside a review. Both were called "review" until the two counters in one tag
made the sentence ambiguous every time it was spoken. The external side keeps the word because its
loop is anchored on `/code-review`, a built-in command that cannot be renamed. The *agents* keep
their names — the ambiguity was in the round noun, not in who ran it.

The tag is a *reading* aid, not a marker for finding checkpoints — `/ship` collapses
`merge-base..HEAD` by definition and never needs to identify them. It is deliberately not `wip:`:
that says only "unfinished", which every checkpoint is, so it distinguishes nothing. When you
cannot tell which phase a checkpoint belongs to, `[b]` is the honest default; do not invent a
number, because a wrong one is worse than none — the whole value is that `[x2s4]` tells the reader
this is the fourth attempt at one review's findings.

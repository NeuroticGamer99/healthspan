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

The one step that is never optional — the same working-tree scan `/land` step 3 runs (that step
also carries a whole-branch history clause; that is `/land`'s backstop over every accumulated
savepoint, deliberately not duplicated here where the scope is this chunk alone — do not "sync"
the two copies in either direction):

- `git status --porcelain` must show no `specs/personal/` path — matched **case-insensitively**
  and covering the **bare path** `specs/personal` itself, not only the `specs/personal/` prefix.
  It is gitignored, so any appearance means the ignore broke: treat as critical and stop.
  Both extra conditions are load-bearing rather than belt-and-braces, and the precedent is in this
  repo: `scripts/review_worktree.py` guards the same invariant in *tested code* and `casefold()`s
  its comparison, accepts the bare path, and uses an `:(icase)` pathspec — holes found by review
  passes rather than by design. A case-sensitive prefix test lets `specs/Personal/labs.csv` through
  on the Linux and macOS CI legs, where the ignore rule does not cover it either; and a
  directory-only ignore rule is blind to a force-added plain file or symlink at exactly
  `specs/personal`, so a prefix test that requires the trailing slash never sees it.
- For every added or modified file outside `specs/personal/`, confirm it contains no personal
  health values, lab results, diagnoses, medications, or owner-identifying information. Test
  fixtures must be synthetic.

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

**That path list is necessary and not sufficient — check the staged *content* too.**
`git diff --cached --name-only` proves which paths are in the index, never that each one holds the
bytes the scan read, and the two diverge wherever `git add` silently declines to update an entry.
It does exactly that on a `skip-worktree` or `assume-unchanged` path: measured, a file staged with
personal data and then cleaned in the working tree keeps the **dirty blob** in the index, satisfies
the path check, and commits. `git add` exits 1 and names the path there, so **treat any non-zero
`git add` as a stop** — but do not rest on that alone. Prove identity per enumerated path:

For each enumerated path `$p`, with `$st` its status letter from
`git diff --cached --name-status`:

```bash
[ "$st" = D ] || [ "$(git rev-parse ":$p")" = "$(git hash-object -- "$p")" ] || { echo "staged content is not what was scanned: $p"; exit 1; }
```

**The `D` arm is not a shortcut — without it the check breaks on an ordinary checkpoint.** A staged
deletion has no index blob and no working-tree file, so *both* halves fail with `fatal:` (measured:
`path 'f.md' does not exist` and `could not open 'f.md'`). It also needs no content check, because
a deletion contributes no bytes to the object; what still applies to it is the path list above,
unchanged.

Measured against the cases that decide whether such a check survives contact: it catches the
skip-worktree divergence, passes an ordinarily added file, and passes a CRLF file under
`.gitattributes eol=lf`. That last one is why the comparison is blob-to-blob rather than
byte-to-byte — a check that false-alarms on line endings is one an operator learns to skip, and
this repo's own `/ship` read-back carries the same lesson. It also works because **`git
hash-object` applies the path's `.gitattributes` filters by default** — `--no-filters` is the
opt-out, and `--path` is for hashing content whose real path differs, not for switching filtering
on. Measured under `eol=lf`: the plain form reproduces the staged blob exactly while `--no-filters`
does not, so no `--path` is needed here. `git diff --name-only` cannot stand in for any of it:
`skip-worktree` is precisely what that command is told to ignore, so it reports clean on the one
case this catches.

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

# Reviewer isolation — the snapshot-worktree launch procedure

The single authoritative statement of how `spec-reviewer` and `test-reviewer` are launched.
The skills that invoke them (`/apply-review` step 5, `/land` step 6, `/wi` step 5) cite this
file rather than restating it. `.claude/bot-review-triage.md` §3 cites `/apply-review`
steps 5 **and** 6 and adds a reporting shape of its own — it does not defer wholesale, and
reading it as though it did is how the per-round mode-recording obligation gets skipped and
`/land` step 6 then asks for a record nothing produced. The mechanics live
in **`scripts/review_worktree.py`**, which is tested (`tests/test_review_worktree.py`) and is
the copy that governs when this prose and the script disagree. Decision record:
[ADR-0068](../specs/adr/0068-reviewer-isolation-worktrees.md); the first, hand-run version of
this procedure accumulated five silent failure modes before its first commit, which is why the
mechanics are a script now.

## The two invariants

1. **An agent that writes to the live tree never runs beside an agent reading that tree** —
   any writer, any reader, whatever either is named. Isolation is the exemption mechanism:
   agents working in private worktrees are neither writers nor readers of the live tree, so
   they may run in parallel with each other and with anything else. This is the general rule
   the 2026-07-27 incident taught (a reviewer reading a live tree mid-mutation reported a
   specific, plausible, entirely false finding); the two reviewers are just its first clients.

   The exemption is bounded by what it assumes. **Do not edit the live tree between setup and
   the reviewers' verdicts** — the snapshot is pinned at setup, so edits after it are simply
   absent from what they read, and both agents can return **pass** against a state that no
   longer exists while `/land` step 6 counts it as a pass on the current diff. That is the
   stale-not-clean failure, reachable with no bot involved. If you did edit, re-run setup and
   the reviewers.

   The snapshot exists precisely so this is checkable — but **compare the tree, never the
   commit**. `git stash create` builds a commit carrying author and committer timestamps, so
   on a byte-identical tree it returns a different SHA every time a second has passed
   (measured; the parents differ too, the index commit being freshly timestamped as well).
   Comparing the recorded SHA directly is therefore a check that fails on an unedited tree
   and does so *more* reliably the longer you wait — the operator either re-runs forever or
   learns to ignore it, and the failure this invariant exists to catch ends up with no
   detector at all. Only the tree SHA is stable:

   ```bash
   git rev-parse <recorded snapshot>^{tree}
   git rev-parse "$(git stash create review-snapshot)"^{tree}   # must match
   ```

   If the tree is now clean, `git stash create` prints nothing at all — that is a change
   from a snapshot that recorded a SHA, so treat an empty result as a mismatch. **Unless
   setup's `snapshot:` line reads `HEAD`**, meaning no tracked file was modified and there
   was no SHA to record: then empty is the match, and reading it as a mismatch fires a
   permanent false alarm on a tree nobody touched. Match the *first field*, not the whole
   line — setup prints `snapshot: HEAD (reviewing …)`, so an exact-line comparison against
   `snapshot: HEAD` never fires, which is the permanent-false-alarm failure this paragraph
   exists to prevent, in this paragraph.

   **Setup's stdout grammar**, since sessions have been re-deriving it from transcripts and a
   prefix change would break every ad-hoc parse with nothing to diff against. One line each,
   in this order: `snapshot: <sha|HEAD> (reviewing …)`, `base: <ref>`,
   `worktree[<agent>]: <path>`, `venv[<agent>]: <path>`,
   `env[<agent>]: UV_PROJECT_ENVIRONMENT=<path> PYTHON_KEYRING_BACKEND=<backend>`,
   `untracked files copied into each worktree: <n>`, then one `  ?? <path>` line per file,
   then `state: <path>`. Parenthetical `  (…)` notes follow several of these and are prose,
   not part of the grammar. Warnings and errors go to stderr, prefixed `warning: ` / `error: `.
   Nothing outside the launcher depends on the state file's JSON shape; this list is the
   interface.

   **The tree SHA covers tracked state only, and that is not a footnote here.** `git stash
   create` builds its tree from the index and the worktree's *tracked* files; the untracked
   files reach the worktrees through the launcher's own copy step and are outside the tree
   object entirely. Edit one after setup and both `rev-parse` lines above still match. On
   this branch several of the files under review are untracked, including the launcher and
   its test file — so the check as written would have proven nothing about the two largest
   things in the change. Hash them explicitly; the two lists must match line for line:

   ```bash
   git hash-object -- <the untracked paths setup listed>                 # live tree
   git -C <worktree> hash-object -- <the same paths, same order>         # what was reviewed
   ```

2. **Launching reviews may read anything, and may write outside the repo plus two named
   places inside it** — `git worktree add` registers administrative state under
   `.git/worktrees/`, which teardown removes one recorded path at a time; and `git stash create`
   writes the snapshot's commit, tree and blob objects into `.git/objects`, which teardown
   does **not** clean, because they become unreachable when the worktrees go and a later `gc`
   collects them. No file in the working tree is written, and **nothing the index stages is
   changed** — note the content wording: read-only git commands refresh the index's stat cache
   in place, so `.git/index` is rewritten on every setup without a single staged byte
   differing. (`--no-optional-locks` does **not** suppress that refresh — measured here on git
   2.45.1 — so do not reach for it expecting the file to stop moving.) That content
   distinction is the whole point: the launcher never uses
   `git stash push -u` (removes untracked files from the live tree; a failed pop is data loss)
   or `git add -N` (changes what is staged, which a plain `git commit` would then publish).

## Launch

**If the tree is dirty, `/savepoint` first.** The launcher pins a *tree*, not a commit,
so without one every round of a loop shares a single `HEAD` — and the per-round `HEAD` and
`HEAD^{tree}` this section asks you to record are then **identical every round**, a record that
looks complete and identifies nothing. Measured: five rounds, one `HEAD`, no diffable boundary.
`/apply-review` step 5 asks for the same boundary at a round's *end* — where that savepoint is
still `HEAD` and the tree is clean, it already is this one; otherwise, whatever the caller, make
it here. A dirty tree is still reviewable (carrying uncommitted state is what the launcher is
for) and is never a reason to refuse the round.

```bash
python "$(git rev-parse --show-toplevel)/scripts/review_worktree.py" setup --scratch <session scratchpad dir>
```

Resolve the script path rather than assuming a cwd. Bash cwd persists across calls, so one
earlier `cd` is enough to leave a repo-relative path unopenable — and Python's "can't open
file" exit is **2**, which this very contract reads as "nothing to review, do not launch
reviewers". A wrong-directory launch would silently skip both reviewers on a dirty tree.
For the same reason, treat an exit 2 that does *not* print a `nothing to review:` line on
stdout as a launch failure, not a sanctioned skip.

**Record `git rev-parse HEAD` — and `git rev-parse 'HEAD^{tree}'` — beside setup's manifest in
the round's record** (ADR-0069; the record is `/apply-review` step 6 item 3). That record is
**conversational, not on disk** — step 6 item 3 says so itself, and a local round's anchors
therefore last exactly as long as the session does. Two durable carriers exist and neither is
automatic: `/review-prep`'s carrier file and the `/review-handoff` report both record the pair,
so an *external* round's anchors survive by construction. A local round's do not; if they need
to, put them somewhere that outlives the session deliberately. When `/land` runs later and finds
no record, the honest move is to re-run the reviewers, not to assert a pass on an anchor nobody
can produce — they are cheap, which is the whole reason that trade is available.
The snapshot SHA is ephemeral by design (§ Fidelity). The
`HEAD` sha anchors the round while the branch's commits stand — but `/ship`'s collapse
deliberately rewrites the savepoints, leaving every recorded pre-collapse `HEAD` dangling on a
gc timer, so a later session comparing against one alone would read a permanent false
"not an ancestor". The tree hash is what survives the collapse unchanged (`reset --soft` +
recommit rebuilds the commit, not the tree); record both, compare commit first, fall back to
tree.

- **Exit 0** — ready. The manifest on **stdout** names the snapshot SHA, the base ref, one
  worktree path per reviewer, a per-agent venv path, and the untracked files replicated into
  each. Fidelity warnings are on **stderr** — so capture both streams. A session that reads
  only stdout gets zero warnings and silently drops the skip-worktree relay this bullet
  requires. Each is printed as it is *recorded*, which for every warning decided before
  materialization means before any later abort can swallow it, and which is also why they are
  the durable copy in `state["warnings"]`. **One warning is neither**: the vanished-untracked-
  file warning cannot be known until the copy has run, so it is printed after materialization
  and is absent from `state["warnings"]`. It is the one saying the manifest is smaller than
  planned, so if you are reading a state file rather than a live transcript, note that this
  particular warning was never going to be in it.
  Launch both reviewers **in parallel — both Agent calls in a single assistant message**,
  which is the only thing that makes them concurrent; two back-to-back messages satisfy every
  other word here while serializing the round and wasting the setup that bought the
  parallelism. Tell each reviewer in its invoking prompt: its worktree path, the snapshot SHA,
  **the base ref**, the untracked-file list, and **its venv path to export as
  `UV_PROJECT_ENVIRONMENT`** for every uv run — the reviewers diff against the base they are
  told, so omitting it silently pins them to `origin/main` whatever the setup decided, and the
  venv redirect keeps `uv` clear of MAX_PATH inside an already-deep worktree path
  (`scripts/review_worktree.py` states the full reason and governs). Relay the warnings into
  your own report.
- **Exit 2** — nothing to review (clean tree, no untracked files, no branch diff). Do not
  launch reviewers — but **relay any warnings this exit printed**. The skip-worktree /
  assume-unchanged case that used to be the example here is no longer reachable on this
  path: a tree that would otherwise be "nothing to review" while the launcher holds positive
  evidence the snapshot is blind to it now **aborts (exit 1)** rather than earning exit 2,
  because exit 2 is the only code that sanctions skipping the reviewers. Neither are the two
  cases beside it: untracked symlinks and paths resolving out of the tree each promote to an
  abort on this path for the same reason. So the only warning that can still coexist with a
  genuine exit 2 is the **machine-local ignore rule** one, which is emitted earlier and
  unconditionally — relay it, because it is the one signal that "nothing to review" was
  decided over a tree this machine hides part of. (Listing the other two here as
  still-relayable was itself the error this bullet was rewritten to remove, in different
  words: `scripts/review_worktree.py` governs, and its gate aborts on all three.)
- **Exit 1** — setup aborted. Read the message first: most carry their own remedy, but a bare
  OS error (a permission denial on the scratch dir, a locked file) will not.
  **Recoverable — fix and rerun, do not fall back:** a prior run's state file still present
  (run teardown first); a staged index diverging from the worktree (reconcile with `git add`
  or `git restore --staged`); any usage error — a typo'd or unknown flag (argparse's own exit
  2 is remapped to 1 precisely so it never reads as a skip), a relative `--scratch`, an
  invalid or duplicated `--agents` (those two abort on their own, not through argparse, but
  the fix is the same: correct the command line and rerun); a scratch dir inside or containing
  the repo (same class — one wrong flag, and the abort itself says to use the scratchpad); an
  unresolvable base ref (fetch it or pass `--base` — the message says which); an **unborn
  HEAD** (`HEAD does not resolve … no commits yet`) — make a commit, the base is fine; a `git`
  call that timed out, or a genuinely transient copy-in failure (an untracked file becoming a
  symlink mid-run) — rerun once, and if it repeats it is not transient; and a held
  **`.git/index.lock`** (`could not write the index — another git process holds …`) — a
  concurrent git, an editor's git integration, fsmonitor, a stale lock from a killed process,
  or a second setup in the same repo. That last one is called out because it is the case this
  split got *wrong*: `git stash create` exits 1 for it exactly as it does for an unmergeable
  index, and the abort used to volunteer "conflicted or unmergeable index state?" for both —
  so an orchestrator matched the guess against the blocking list below and permanently
  degraded a round whose correct remedy was to wait a second and rerun (reproduced with two
  concurrent setups). The launcher now reads git's stderr and says which; trust the message,
  not the exit code.
  **Blocking — route to the fallback below:** unmergeable index; anything under
  `specs/personal/` that is tracked or no longer ignored; a base that resolves but shares no
  merge base with HEAD; a symlink-only change; an untracked nested repository in the tree; a
  **non-UTF-8 untracked filename** (deterministic — the name cannot be reproduced, so no
  rerun clears it); not being inside a git repository at all; a git failure that a rerun does
  not clear. The three base-ref/HEAD cases are distinguished by the abort message, which
  names which one occurred.
  **The question behind the split**, for classifying an abort these lists do not name — and
  there are far more `raise AbortError` sites in the launcher than the lists above have
  entries, tracked here by hand and by nothing else: *would getting past it
  require a judgement call about the work itself?* If the fix is mechanical and one the author
  would make anyway to land this work — correct the command line, fetch the ref, commit,
  reconcile the index, wait out a transient — it is **recoverable**, and the same isolated
  setup then succeeds. If it needs a decision about the content under review (untrack the
  personal file, resolve the merge, rename a file, move the nested repo, rewrite history), or
  there is simply nothing isolation can act on, it is **blocking**: a review must never press
  the author into altering its own subject in order to run, so it routes to the fallback and
  the author decides separately. Several blocking aborts still carry a one-line remedy — that
  is the author's option, not the reviewer's instruction, and it does not reclassify them.
  This is a heuristic for placing a new cause, not a proof that the lists above are complete;
  they are not, and nothing mechanically checks that a new abort site gets classified at all.
  These lists name the causes seen so far, **not a closed set**. A cause you cannot place in
  either is itself worth reporting — treat it as blocking meanwhile, and never improvise a
  third path.

**Fallback when isolation cannot be established:** run the reviewers **sequentially** against
the live tree in their read-only mode (each agent file defines it: spec-reviewer is read-only
always; test-reviewer must not mutate without a worktree). **Still hand each one the base
ref** — the fallback changes where they read, not what they review, and the agent files
default to `origin/main` when told nothing, so a session that launched with `--base <x>` and
fell back silently reports a pass over a scope it explicitly rejected. Disclose the fallback —
in each reviewer's invoking prompt and in your own report — so nobody mistakes an unisolated
pass for an isolated one.

Sequentially, even though invariant 1 does not strictly demand it here: in read-only mode both
agents are readers, and invariant 1 forbids only a *writer* beside a reader. The reason is the
obligation invariant 1 states next, which the fallback is the only mode that can violate
observably. **Do not edit the live tree between setup and the verdicts** is written as a
property of the pinned snapshot, and in the fallback there is no snapshot — the reviewers read
the live tree as it is at the moment each one looks. So the rule has to be restated in the
stronger form the fallback needs: **do not edit the live tree from the moment the first
fallback reviewer launches until the last one's verdict is in.** Running them one at a time is
what keeps that window a single sequence you control rather than two overlapping reads you
cannot reconstruct; edit in the middle and the two reviewers report on two different trees
while every consumer counts them as one pass on one diff.

**What a fallback pass is worth**, wherever it is consumed (this is a property of the
fallback, not of any one skill, so `/land`, `/apply-review` and `/wi` all read it here): it
proves less, because test-reviewer runs it without mutation and its detection power therefore
goes unproven. Before a fallback-only pass is used to justify a commit, either retry the
isolated round or surface the degradation to the user explicitly and get their go — never
count it silently as equivalent. Any record of a reviewer round states **which mode it ran
in**; a round that does not say is not evidence.

## Confining a reviewer to its worktree

Isolation is only as real as the paths the reviewer actually uses, and prose alone does not
bind it: **an agent thread resets cwd between Bash calls.** A reviewer that runs `cd
<worktree>` in one call and `uv run pytest`, `sed -i`, or `git status` in the next is
operating on the **live tree** — test-reviewer mutating it, spec-reviewer reading it
mid-mutation, which is the 2026-07-27 incident this whole mechanism exists to eliminate. The
untracked reconciliation cannot catch it either: the live tree has the same untracked
filenames, so the `??` set matches and the check passes.

So every command a reviewer runs is **self-locating**, in the same call that uses it:

- git: `git -C <worktree> …`, never a bare `git` after a `cd`.
- everything else: absolute paths under the worktree, or `cd <worktree> && <command>` as **one
  compound command** so the `cd` cannot be separated from what it scopes.

**The environment does not persist either, and two exports here carry more than tidiness.**
Bash environment is per-call exactly as cwd is, so an `export` in one call is gone by the
next — and the two things a reviewer exports are the machine-level safety, not conveniences:
`PYTHON_KEYRING_BACKEND` (the out-of-tree belt, on the machine holding a real encrypted health
database — `specs/testing-strategy.md` § Cross-Platform Testing governs) and
`UV_PROJECT_ENVIRONMENT` (the MAX_PATH redirect). A reviewer that exports in one call and runs
pytest in the next gets neither, and neither absence announces itself. Join each export to the
command it protects in one compound call, or pass it inline (`VAR=value <command>`).

The launching session is the opposite case — its cwd *persists* — which is why the launch
command above resolves the script path rather than assuming one.

## Teardown

```bash
python "$(git rev-parse --show-toplevel)/scripts/review_worktree.py" teardown --scratch <same dir>
```

Run it on every exit path — reviewer passed, failed, or died; the script tolerates worktrees
already gone, treats a missing state file as a clean no-op, and removes the reviewer venv
dirs it recorded. An abort can still leave a state file **deliberately** — setup keeps it
whenever a worktree survives the unwind, because it is the only record a teardown retry can
act on — which is exactly why teardown runs unconditionally instead of only after successful
setups. Each recorded path de-registers itself — never a repo-global `git worktree prune`,
which takes no path filter and would de-register unrelated worktrees whose directory happens
to be missing — and teardown **exits 1 if anything it created is still registered**. Treat
that exit as a real failure to chase, not clutter: a leaked worktree directory is a stale
copy of the repo that a later session could mistake for current state.

Teardown's own exit 1 has several shapes, and only the first is the stray check. As with the
launch lists above, these name the causes seen so far and are **not a closed set** — the same
hedge, for the same reason, because the version of this list that claimed to be complete was
already missing two causes stated 14 lines below it and a sixth in the script whose own
comment said this triage did not enumerate it. If a teardown failure matches none of these,
that is a finding, not a reason to improvise:

- **A stray registration or an undeletable directory.** Chase it: the message names the path
  and, for a directory it could not remove, says to remove it by hand and delete the state
  file. Rerunning teardown alone will not clear it.
- **A malformed state file** — unparseable JSON, not an object, a missing or non-string
  `root`, a `worktrees`/`venvs` value that is not a string map. Every one of these aborts
  names the file and the offending part, and tells you to inspect `git worktree list` by
  hand, remove any `wt-*` strays, then delete the state file. The file is deliberately kept
  so you can read it first.
- **A recorded repo root that is gone or is no longer a git repository** (re-cloned in place,
  `.git` removed). Same guidance: clear the leftovers under the scratch dir by hand, then
  delete the state file.
- **An interpreter older than the launcher's floor.** Self-remedying and not a defect to
  chase: the message names the running version and the command to rerun with. It is here
  because the paragraph above says a failure matching none of these shapes *is* a finding, so
  omitting a self-remedying refusal is how it gets escalated as one.
- **A recorded path this script refuses to locate or to delete** — one whose final component
  is `..` or empty (which is what a bare `.` and a filesystem root both normalize to), or one
  that resolves outside the scratch dir. Both mean the state file no longer describes what
  setup wrote. Nothing is deleted; inspect the file.
- **`worktree cleanup failed: <exc>` or `venv cleanup failed for <path>: <exc>`.** A git call
  or a filesystem call inside cleanup failed on its own account — a `.git/index.lock` held by
  a concurrent git, an fsmonitor stall, the command timeout, an unreachable volume. This is
  the one shape that is usually **transient**: wait a moment and rerun teardown. It is listed
  because it was not, and the paragraph above turns an unlisted shape into a finding — so a
  routine retry was escalated as a defect while the operator held no remedy at all, the
  message being the only refusal in the script that carried none.
- **`refusing to de-register <path>`.** The state file names a registered worktree that is
  outside the scratch dir and does not carry a launcher-generated `wt-<agent>-<short>-<hex6>`
  name. De-registering it would be a repo-global write on somebody else's worktree, so
  teardown stops instead. Inspect the state file; this is a hand-edit or a stale record, not
  a transient.

A relative `--scratch` is refused here exactly as it is on launch, and for the same reason —
teardown resolves it against the invocation cwd, so a relative value plus any earlier `cd`
would look in the wrong place and report a clean no-op over live registrations. A scratch dir
inside or containing the repo is refused here too: teardown is the half that deletes, and the
containment guard in front of every force-delete is relative to `--scratch` alone, so a
scratch dir that *contains* the repo would make every path in the repo "inside scratch".

A scratch dir **renamed** after setup is not a wedge. Run teardown with `--scratch <the new
path>`, where the state file now lives: the recorded paths are then gone but still registered,
and teardown looks for each recorded **worktree** basename under the current scratch dir,
deletes what it finds there, and de-registers the rest.

Worktrees only, and the asymmetry is deliberate. A worktree's name carries a per-round random
suffix, so finding that name under the current scratch dir finds *this* round's worktree. The
venv names are the fixed `venv-<agent>` — chosen for MAX_PATH headroom — so the same lookup
would match whatever round is running now, and a stale state file torn down against a live
scratch dir would force-delete a live reviewer's venv mid-review. A relocated venv is
therefore left where it lies: it is a rebuildable package cache under a scratch path, which
is the cheaper of the two failures by a wide margin.

That relocation step is load-bearing, and the earlier justification for omitting it — "nothing
is left on disk to protect" — was simply false: the directories moved, they did not disappear.
Measured, teardown printed "removed 2 worktree(s) and 2 venv dir(s); no strays registered",
exited 0, and unlinked the state file while both worktrees stayed on disk under the new name,
carrying the uncommitted work under review plus test-reviewer's mutation edits — the stale copy
of the repo this section warns about two paragraphs above, created by the cleanup meant to
prevent it.

## Fidelity — what the snapshot does and does not carry

The verification record lives in ADR-0068 §4; the operational summary:

- Tracked uncommitted changes are carried in full; untracked files are replicated with
  **identical bytes into every worktree** from a single read, mode bits preserved; untracked
  **symlinks are skipped with a warning**, not replicated, as is any untracked file that
  **vanishes between the manifest and the copy** (an editor swap file, a build artifact) —
  dropped from the printed manifest with a warning rather than aborting the round, since
  untracked files are the most volatile things in a tree and a rerun would race identically.
  Untracked paths that **resolve outside the repository** are skipped the same way, on a
  bare count rather than a list. A directory junction or symlinked directory anywhere on the
  path puts them there, and on Windows git walks *through* a junction and reports the files
  beneath it as ordinary untracked paths with no marker of any kind — so this is not the
  symlink case above, which is caught at the leaf. (On POSIX git reports the link itself as
  one entry, so the symlink skip already covers it and this is defense in depth.) Content on
  the far side of such a link was never in review scope; the count is redacted because those
  leaf names come from outside the repository.
  Other ways a manifested file can fail to replicate **abort the whole setup** (exit 1) rather
  than joining that company, and must not be read as if they skipped quietly: a name that is
  **not valid UTF-8** (it cannot be reproduced, and a rerun is deterministic), a path that
  **became a symlink or junction** after the manifest was taken, or whose path **resolves out
  of the tree** for the same reason (replicating either would copy bytes from outside the
  repo), and a path git reports as a
  **directory** — how it reports an untracked nested repository.
  **These two aborts redact differently, and this said they were the same.** The
  became-a-symlink abort interpolates the path; the `_escapes` abort names neither a path nor
  a count, deliberately, because its leaf components come from the far side of the link. This
  paragraph claimed "both abort on a bare count, naming no path", which was wrong about both
  — and wrong in the direction that matters, since it is what tells a relayer the message is
  safe to pass on unread. Treat the symlink abort's text as carrying a repo-relative filename
  and review it before relaying. The division that matters is not the count but the
  disposition: a skip is named in a warning *and* removed from the printed manifest, so an
  exit-0 manifest lists what the worktrees actually hold, and anything else is a hard stop.
  Neither list is closed, which is why each reviewer's own `??` reconciliation — not this
  paragraph — is what catches a file that went missing for a reason nobody enumerated.
  Content is identical **modulo
  git's configured EOL normalization** (`core.autocrlf` / `.gitattributes eol`) — a byte-level
  CRLF assertion can legitimately differ between the live tree and a worktree, while every
  `git diff` text agrees.
- The snapshot holds **worktree content, not staged index content** — a plain `git commit`
  (without `-a`) would publish index state the review never saw, so setup **aborts** when any
  path's staged content diverges from its worktree content, giving a **count** and the
  reconcile commands — not the paths. It named them until pass 9 measured why it must not:
  `git rm --cached specs/personal/<file>` is the standard remedy for the state the tracked-
  personal guard catches, and it moves the path out of `ls-files` and into exactly this
  abort, so the one guard that listed its findings was the one reachable with a
  provider-named file. `git status --short` lists them locally. Warnings are advisory
  everywhere in this procedure, and that gap is the one no disclosure can make safe. A **staged deletion of a file still on disk** counts as
  divergence and aborts too, even though it does not look like one: `git rm --cached` drops
  the path from the index entirely, so it reappears as untracked, gets replicated, and every
  reviewer sees a file the commit is about to delete.
- Setup warns about untracked files hidden by **machine-local ignore rules**
  (`.git/info/exclude`, the global excludesFile) — as a **bare count**, never the filenames and
  never their directories either, since a machine-local rule is exactly where deliberately-hidden
  (possibly personal) material would live, a hidden *directory* name is itself provenance under
  CLAUDE.md, and the warning is echoed into transcripts and the state file —
  and about **skip-worktree/assume-unchanged** entries; empty untracked directories are not
  carried at all.
- Each reviewer **reconciles the untracked list it was given** against its worktree with
  `git -C <worktree> -c core.quotePath=false status --porcelain --untracked-files=all`,
  **before it writes anything in that worktree**. The ordering is load-bearing and was
  unstated: `test-reviewer` is told its worktree is a disposable private copy to mutate
  freely and restore nothing, so any non-gitignored file it creates first — a probe test, a
  scratch fixture, a suite artifact — becomes a `??` entry the prompt did not list, and the
  reviewer then reports a fidelity failure against a perfectly replicated snapshot. That is
  the phantom-finding class this whole mechanism exists to eliminate, arriving through the
  check meant to detect it, in output built to be believed.
  Report any `??` entry the prompt did not list and any listed file that is missing. All
  three parts of the command are load-bearing, and this is the copy that governs. The `-C` first, because
  it is the one that decides whether the check means anything: a reviewer's cwd resets
  between Bash calls (§ Confining), so the command without it reads the **live** tree —
  whose `??` set matches the manifest by construction — and reports "reconciliation clean"
  in exactly the case this check exists to catch, a reviewer operating on the live tree.
  Then the two flags: without `--untracked-files=all` git
  collapses a wholly-untracked directory to a single `?? dir/` entry while the launcher's
  manifest is per-file, so a new fixture directory manufactures a phantom extra entry *and*
  reports every real file as missing — a fabricated discrepancy on a perfectly replicated
  snapshot; without the quotePath override git octal-escapes non-ASCII paths, so a correctly
  replicated file string-mismatches the list in both directions. The agent files carry the
  command because they are runtime prompts and cannot transclude this one; they point here
  for the reason.
- The snapshot SHA is **ephemeral by design**: it identifies the reviewed state during the
  session, and becomes unreachable (garbage-collectable) once the worktrees are removed. It is
  a session-scoped identity, not a durable audit ref.

## What isolation does not cover

The worktree isolates the **tree**, not the machine — a mutated suite run still shares the OS.
The out-of-bounds rules for that live in `test-reviewer.md` § Mutation testing (conftest and
autouse isolation fixtures; the keyring fail-backend habit). This procedure cannot substitute
for them.

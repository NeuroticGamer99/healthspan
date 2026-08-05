---
name: land
description: Pre-commit landing checklist — run the local gates, verify personal-data containment and ADR governance, draft the Decisions: section, and propose the commit message. Use when a work item is ready to commit.
---

# /land — pre-commit landing procedure

Walk these steps in order for the change currently in the working tree. Report each step's outcome briefly; stop and report if any step fails.

**The command recipes in this file are Bash — run them with the Bash tool, not PowerShell.** Git's
extended revision syntax does not survive PowerShell's parser: `{tree}` is taken for a script block
and serialized into `-encodedCommand <base64>`, so `<sha>^{tree}` reaches git as three arguments.
`git rev-parse` answers the *first* one and prints a **commit** SHA before erroring (exit 128,
measured) — a mislabeled hash from the very step written to prevent one.

**The exit code is the reliable tell; the printed value is not.** Which commit comes back depends
on where the quotes fall, so no single wrong number identifies the fault: bare `HEAD^{tree}` prints
HEAD's *parent*, an unquoted `$snap^{tree}` prints the stash commit's parent (which is HEAD), and
`"$snap"^{tree}` breaks the `^` off into an argument of its own and prints the stash commit itself.
All three measured, all three exit 128. `^{commit}` mangles the same way, but what it costs depends
on the command: `git rev-parse` again exits 128, while `git cat-file -e` reads the injected
`-encodedCommand` as a bundle of short switches and exits **129** on ``unknown switch `n'`` (both
measured — the 129 belongs to `cat-file`, not to `rev-parse`). Where a recipe must be portable,
quote the whole revision argument (`"$sha^{tree}"`), which both shells pass through intact.

## 1. Survey the change

`git status` and `git diff` (plus `git diff --cached` if anything is staged) — **and the branch's savepoint commits** (ADR-0069):

```bash
mb=$(git merge-base origin/main HEAD)
[ -n "$mb" ] && git cat-file -e "$mb^{commit}" || { echo "no merge base — stop"; exit 1; }
git diff "$mb"...HEAD
```

The guard is not ceremony: an unguarded `$(git merge-base …)` that resolves to nothing leaves the range `...HEAD`, which git **accepts** and answers with silence (exit 0, no output — measured), so the survey and step 3's containment scan both report clean having examined nothing. Resolve `mb` in the **same** Bash call that consumes it — shell variables do not persist between Bash tool invocations, so an `mb` set in an earlier call is empty here, which is exactly the failure the guard exists to catch.

A branch built with `/savepoint` carries most of the work item in local checkpoints that the porcelain never shows, and the change being landed is the whole branch diff plus whatever is still uncommitted — that union is what `/ship` collapses into the one commit this skill composes the message for. Confirm the set of files matches the work item — no stray edits, no leftover scratch files.

## 2. Run the gates that exist locally

Run whatever the repository currently has; skip what doesn't exist yet and say so:

- `python scripts/check_adr_index.py` — ADR index consistency (always, if `specs/adr/` or its README changed).
- `python scripts/check_spec_links.py` — spec cross-link integrity (**always** — it validates link targets anywhere in the repo, so a rename or deletion *outside* `specs/` can break a spec link; CI runs it unconditionally in the docs-consistency job, ADR-0061).
- Once the code phases land (see `specs/development-plan.md` Phase 0): `ruff check`, `ruff format --check`, `pyright`, `pytest -n auto` — run each if configured in the repo. The `-n auto` (pytest-xdist) is the intended local invocation — the suite is isolated for worker parallelism (see the dev-dependency comment in `pyproject.toml`); only CI runs serial, for its ordered `tee-sys` log capture. Don't add `-n` to `addopts` in `pyproject.toml` — that would leak into CI's invocation.

A failing gate stops the landing; fix or escalate before proceeding.

## 3. Personal-data containment check

- Verify nothing under `specs/personal/` is staged or would be committed: `git status --porcelain` must show no `specs/personal/` paths (it is gitignored; its appearance means the ignore broke — treat as critical), **and neither must any path the branch's history ever touched**:

  ```bash
  mb=$(git merge-base origin/main HEAD)
  [ -n "$mb" ] && git cat-file -e "$mb^{commit}" || { echo "no merge base — stop"; exit 1; }
  git log --diff-merges=first-parent --name-only --format= "$mb"..HEAD | sort -u
  ```

  Same guard, same reason, and the same one-Bash-call rule as step 1 — an empty `mb` turns this into `..HEAD`, which prints nothing and exits 0, and a containment gate that examined nothing is indistinguishable here from one that found nothing. The history, not the endpoint diff — a file committed by one savepoint and deleted by a later one is absent from `git diff --name-only` at both ends, yet its blob rides every push of those commits (and `/ship` does not collapse an already-pushed branch). The check exists because savepoint commits (ADR-0069) take files out of the porcelain — without it, this gate is structurally blind to everything already checkpointed, and this is the last scan before `/ship` pushes.

  **`--diff-merges=first-parent` is load-bearing, not a formatting flag.** `git log` shows *no* paths for a merge commit by default, so without it a file introduced by the merge itself — the ordinary shape of resolving a conflict in favor of "keep the added file", on the long-running branch that merges `origin/main` — is examined by nothing and its blob is pushed. Measured on a throwaway repo: a merge that adds `only_in_merge.txt` leaves `git log --name-only --format= master..HEAD` printing only the ordinary commits' paths, while `git show --stat` on the merge confirms the file. The flag makes each merge report its diff against the branch tip it was merged into, so anything the merge brought in is listed. It over-lists — paths that arrived from the merged-in side and are already upstream appear too — and that is the right direction to err for a containment scan: a file scanned needlessly costs a read, a file skipped costs the invariant.

  **Do not reach for `--first-parent` instead.** The two names read almost identically and do opposite things: `--diff-merges=first-parent` changes only how a *merge commit's diff* is rendered and leaves traversal whole, while `--first-parent` restricts the *traversal* itself and walks straight past every commit on a merged-in side branch. Measured on one repo — a side branch that added a file and deleted it again, then merged back with a real merge commit: the range holds 5 commits, `--diff-merges=first-parent` walks all 5 and reports the file, `--first-parent` walks 3 and never mentions it. That file is exactly what this scan exists to catch, so the wrong flag fails silently and in the one direction that matters.
- For every added or modified file outside `specs/personal/` — in the working tree *or* in the branch diff — confirm it contains no personal health values, lab results, diagnoses, medications, or owner-identifying information. Test fixtures must be synthetic. Each savepoint ran this scan over its own chunk at commit time; this pass is the whole-branch backstop, not a formality to skip on that account.

## 4. ADR governance check (if `specs/adr/` is touched)

- No Accepted ADR's decision content is modified (only status-field corrections, `## Links` navigation additions, typo/link fixes are permitted in place).
- New or status-changed ADRs are reflected in the `## Index` table of `specs/adr/README.md`.

## 5. Draft the `Decisions:` section

Walk the CLAUDE.md decision-capture routing rules (1–6) against the change. For every design decision the change embodies that the specs left open, confirm the owning record was created or updated *in this same change*, and list the links. If the change genuinely surfaces no such decision, the section reads `Decisions: none`. Never omit the section.

## 6. Review invocation (when warranted)

If the change includes non-trivial code or spec-conformance risk and the `spec-reviewer` / `test-reviewer` agents have not run on **the current diff**, recommend running them before the commit, launched per `.claude/reviewer-isolation.md` (parallel when its setup succeeds; its fallback is sequential — that file decides). "Have run" means on the state you are about to commit — a pass that predates an `/apply-review` round or a bot-review fix does not count, since those edit the code after the reviewers saw it (`/apply-review` step 5, `.claude/bot-review-triage.md` §3). Ask what changed since the pass rather than whether one ever happened — **and in which mode it ran**; `.claude/reviewer-isolation.md` § Launch states what a fallback-only pass is worth and what it obliges, for every caller rather than this one. Note when a phase boundary or security-critical change (encryption, key derivation, tokens, process boundaries) warrants suggesting `/code-review` or `/code-review ultra` to the user — `ultra` is user-triggered and billed separately; only the user launches it.

## 7. Propose the commit message — then stop

Compose the commit message:

- Imperative-mood title summarizing the change.
- Body explaining what and why, referencing the ADRs/specs involved.
- The `Decisions:` section from step 5.
- The co-author trailer naming the model running *this* session (read it from the system prompt; never carry one forward).

**Write the composed message to `<scratchpad>/commit-msg/<branch>.txt`, then the exact branch name
to `<scratchpad>/commit-msg/<branch>.branch`, and print both paths.** That order is load-bearing,
not stylistic — see below. The branch name goes into the path unsanitized, which removes the
collision the review-report filename convention carries:
flattening `/` to `-` puts `feat/x` and `feat-x` on one filename, and either one's stale file then
passes a name check and ships the wrong `Decisions:` links.

**The filename is a hint; the sidecar is the check.** Do not present the path form as proof that
two branches cannot share a file — on Windows it is not one, and the reason is worse than a plain
collision: *which* file a branch-derived path names depends on which API touches it. Take the legal
branches `a./b` and `a/b`. Written through .NET, the trailing dot is normalized away and the second
write silently overwrites the first. Written through this session's Write tool, both survive as
distinct files — and a subsequent .NET or PowerShell read of `a./b` then returns `a/b`'s contents,
with two correct-looking files on disk and nothing anywhere reporting a fault. Git takes a third
position, on the very operation `/ship` performs: `git commit -F` against such a path exits 128
with `could not read log file … No such file or directory` while the file sits plainly on disk.
All three measured. **Do not try to sort these into the safe ones and the dangerous one** — that was
attempted here and it does not hold. Git's `commit -F` halts itself, at its own point of failure;
the .NET write completes silently and produces a wrong-but-plausible file, and is safe only because
the sidecar catches it afterwards — which is a property of the mechanism, not of the write. Ranking
them invites exactly the reasoning this paragraph exists to prevent, that some layer can be trusted
to be loud. A component like `nul` writes as an ordinary `nul.txt` rather than failing the way the
reserved-device rule suggests. The chain from `/land` to `/ship` crosses all three layers, so there is no single
true statement about the filesystem to rest on — which is the whole reason ownership rests on a
checked value instead. A filesystem
property is the wrong thing to rest this on, which is what the `.branch` sidecar fixes: one line
holding the exact `git rev-parse --abbrev-ref HEAD`, which `/ship` compares against the branch it is
shipping. A collision then surfaces as a loud mismatch rather than as another work item's message.

**Write the message first and the sidecar second, always.** The sidecar is only an authority
because it is the *later* write: interrupted between the two, message-then-sidecar leaves a stale
sidecar still naming the previous branch, which mismatches and stops the ship. Reversed, it would
leave a freshly-correct sidecar vouching for a stale message — a guard that certifies exactly the
substitution it was added to catch. What this catches is **cross-branch** contamination, and only
that: a `/land` re-run on the same branch meets a sidecar already naming it, so the comparison
cannot tell a completed run from one abandoned after the message write. That residual is accepted
rather than closed — `/ship`'s first precondition is a message *the user has seen*, and an
interrupted `/land` never got as far as presenting one.

**Every rewrite of the message rewrites the sidecar after it — including a one-word fix.** This is
the case that actually happens, and the rule above does not cover it: revising the message after
the sidecar exists leaves the sidecar *older* than the text it vouches for, which is the reversed
order the paragraph above forbids, arrived at without anyone reversing anything. `/land` corrects
its own proposal routinely — a miscount, a rewrap, a fact caught on re-reading — so this fires more
often than the interruption case the ordering was designed for. Rewriting the sidecar costs one
call and restores the invariant.

**Do not try to verify that order from mtimes.** It is the obvious check and it does not work: two
consecutive writes land on an identical timestamp — `stat -c '%.9Y'` returned the same value for
both files, equal to the nanosecond — so a `stat` comparison passes just as readily on the forbidden
order as on the correct one, a false pass in precisely the back-to-back case this rule governs.
More precision does not rescue it. The ordering is held procedurally, by rewriting the sidecar last
every time, and the verbatim read-back below is the net for a message that drifted from what was
approved.

The **encoding** is worth checking, since both files are claimed to be LF:

```bash
S=<scratchpad>/commit-msg/<branch>
printf 'CRs: msg=%s side=%s\n' "$(tr -cd '\r' < "$S.txt" | wc -c)" "$(tr -cd '\r' < "$S.branch" | wc -c)"
```

Both counts must be `0`, and use that form rather than `grep`, which fails here for two independent
reasons — measured on three-line files of each kind. **Bare**, `grep -c $'\r'` answers **0** on a
genuine CRLF file: Git Bash reads in text mode and translates `\r\n` before grep sees it, so the
check reports clean on precisely the corruption it exists to catch. (An *unpaired* `\r` it does
find, which is what makes the blindness easy to miss when probing.) **Nested inside a `$( )`
substitution** — the natural way to interpolate the count into a report — the carriage return is
eaten before grep receives it, leaving an empty pattern that matches every line, so the same
command answers **3** on a pure-LF file. One spelling is blind, the other cries wolf, and which one
you get depends on where you wrote it rather than on anything about the file. `tr -cd '\r' | wc -c`
answered 0, 3 and 3 correctly across all three. A check whose verdict tracks its own quoting is
worse than no check at all. Write both files with the Write tool, which creates the intermediate
directories. UTF-8
without BOM, LF line endings (use the Write tool; a PowerShell or Python write can emit CRLF
against `.gitattributes eol=lf`). The conversation is not durable storage: a session that
compacts or ends between here and `/ship` loses the approved text, and it has already had to be
recovered by parsing the session `.jsonl`. `/ship` commits this file with `git commit -F`, so
the file is the message.

**Show the whole message, verbatim, read back from the file — never a summary of it.** `/ship`
treats the user's invocation as approval of what they were shown, so a paraphrase means the
approval attaches to text nobody read; and the summary is generated from the same understanding
that wrote the file, so it agrees with the file whether or not the file is right. Reading the file
back is what breaks that circle: doing it here caught a wrong finding count and an unwrapped line
in a message that had already been declared ready. This is a step, not a preference — it has been
skipped in favour of a per-section digest, and the user had to ask for the message itself.

**If you report a tree hash, measure it now — after every edit this skill has made** (the step-5
decision records, any fix from a gate), and label it as the hash of the tree being proposed.
The discriminator is **tracked-modified, not dirty** (`.claude/reviewer-isolation.md` documents
both traps): `git stash create` builds its tree from tracked state only, so on a tree whose only
changes are untracked additions it prints *nothing* — and `git rev-parse ""^{tree}` then fails —
while "dirty" reads as taking that branch. When `git status --porcelain` shows lines other than
`??`:

```bash
snap=$(git stash create land-proposal)
[ -n "$snap" ] || { echo "nothing tracked-modified — report HEAD^{tree} instead"; exit 1; }
git rev-parse "$snap^{tree}"
```

Otherwise `git rev-parse 'HEAD^{tree}'`. Capture the stash SHA into a variable and quote the
**whole** revision: the inline `git rev-parse "$(git stash create land-proposal)"^{tree}` form
leaves `^{tree}` outside the quotes, where PowerShell splits it off and `git rev-parse` answers
the bare SHA — printing the stash **commit** hash under a "tree hash" label before exiting 128
(measured). Either way **say the hash covers tracked state only**, and name
any untracked files it therefore omits — hash them separately with `git hash-object -- <paths>`
(the same split `.claude/reviewer-isolation.md` states under § The two invariants, invariant 1,
where the two `hash-object` commands live — not under its § Fidelity heading) rather than
presenting a tracked-only number as the whole tree. Never quote a hash measured earlier in the session — a
reviewed-round hash predates this skill's own edits, and presenting it here has already read as
drift and cost a full investigation. Byte-identity to a reviewed snapshot is a *separate* claim:
if you assert it, name the delta between that snapshot and this tree.

Present the message and **stop**. `/land` proposes; `/ship` disposes.

The user lands it by invoking **`/ship`**, which commits with this message, pushes, and opens the PR (`/ship coderabbit` or `/ship gemini` additionally triggers and triages that reviewer's chain). If the user instead replies "commit" (the pre-`/ship` habit), treat that as the go and run `/ship`.

**Never commit or push from this skill**, and never without that explicit go.

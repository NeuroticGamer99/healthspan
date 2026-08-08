#!/usr/bin/env python3
"""Mechanize the enumeration half of CLAUDE.md's personal-data containment rule.

The rule confines personal health data to ``specs/personal/``. It has two
halves with opposite mechanizability:

* **enumeration** — which paths does this change touch;
* **content** — does this file hold health values, provenance, or identifying
  information.

Every measured failure of this control landed in the enumeration half and none
in the content half — all of them on the ``chore/savepoint-skill`` branch, each
found by a *different* review pass, and each of those passes reporting the scan
clean apart from the one hole it found. **How many passes that was is
deliberately not stated here.** It was restated incompatibly across four
documents, caught by a bot rather than by any of them; a count that drifts is
worth less than the mechanism it summarises, and the enumerated list below is
the thing that actually needs to be right. The ledger is in
``specs/open-questions.md`` under the repo-invariants register entry.
Enumeration is ordinary code; content
is judgement and stays prose in ``/land`` and ``/savepoint``. This script owns
the first half only, and deliberately decides nothing about whether a value it
never reads is synthetic.

The six holes it closes, each of which shipped as a *prose* scan that read
clean:

1. the scan read the endpoint diff rather than the branch history, so a file
   added by one savepoint and deleted by a later one was invisible at both ends
   while its blob rode every push;
2. the merge base was substituted unguarded, so an empty result left ``..HEAD``
   — which git accepts and answers with silence, exit 0;
3. ``git log --name-only`` reports **no paths at all** for a merge commit, so a
   file force-added while resolving a merge passed unexamined;
4. the index reconciliation proved *paths*, never *content* — a
   ``skip-worktree`` entry keeps a dirty blob staged that the path check waves
   through;
5. the prose guard was case-sensitive and missed the bare path, while the
   repo's own tested launcher guard already casefolded and covered both;
6. the history scan listed paths while the content instruction inspected
   *current* files, so a value committed then sanitized rode the push invisibly.

Holes 1-3 and 6 are the history scan below, 4 is ``staged_content_mismatches``,
5 is ``is_personal_path``. Hole 6's judgement half stays with the caller: this
script names the patch stream to read (``patch_stream_command``) rather than
reading it.

**Four** further gaps were found while building the gate rather than by a
review lens, and are closed here as well. This list is the authoritative one.

**Several other documents restate parts of this list, and no reliable way to
enumerate them is known.** That sentence is the finding, not a preamble to one.
Six consecutive attempts to state where the copies live have each been wrong,
and the last three were each the *correction* of the one before:

* an inventory naming a different pair of extra holes;
* "the two skills each restate it", while ``/savepoint`` never had;
* "three further places" for the root constraint, when it was six;
* ``git grep "collapses an untracked directory"``, which misses ``/land``
  (*collapsing*);
* ``git grep "untracked directory"``, which misses ``specs/open-questions.md``
  (markdown italics put an asterisk mid-phrase);
* every count in between.

The pattern is not carelessness. **A list of where the copies are is itself a
copy**, so it drifts by the same mechanism; and a search phrase is a copy too,
broken by paraphrase and by markup rather than by arithmetic. Widening the
phrase trades a miss for noise without ever reaching completeness -- ``git grep
"untracked"`` matches seventeen files, most of them about something else.

So: start from ``git grep -n "untracked"`` and ``git grep -n "top level"``,
read the hits, and **expect to find a copy neither one surfaced**. The remedy
that has not been tried is mechanical -- a sync gate of the kind
``check_markdownlint_config_sync.py`` already is for two config files. It is
recorded under "A sync gate for a rule restated across several documents" in
``specs/open-questions.md``, with the trigger that would resolve it, and after
this many failures prose has run out of standing to be offered as a substitute.

1. ``git status --porcelain`` **collapses an untracked directory** to a single
   record naming the parent, so a personal file whose parent is itself
   untracked is reported as ``?? specs/`` and no prefix match sees it —
   ``--untracked-files=all`` is the fix, and whether the collapse happens
   depends on whether some *sibling* directory is tracked, which is not a
   property a containment scan may rest on;
2. a **shallow clone** silently truncates every history-walking scope: measured
   at ``--depth 1``, ``--scope history`` exited **0** announcing "620 paths
   examined" over 4 commits of roughly 80 — hole 2 reproduced inside the
   mechanism built to end it, now refused by ``require_full_history``;
3. a **tracked but unmodified** personal file appears in neither the porcelain
   nor a ``merge-base..HEAD`` range once its commit predates the base, so every
   scope runs ``tracked_personal`` as well;
4. git **quotes** non-ASCII path names in its line-oriented output
   (``specs/personal/caf\\303\\251.md`` arrives wrapped in literal quotes), and a
   leading ``"`` defeats a prefix test anchored at the start of the path. Every
   path list here is read ``-z``, which is why.

A fifth was found by the first external review of the gate itself and is
recorded with the others because it is the same class: ``log.showSignature``,
an ordinary user config, interleaves gpg lines into ``git log``'s stdout with
no ``-z`` delimiter of their own, **gluing them to the front of the next path**
so that a containment path stops matching. See ``_git``.

A sixth was found by the second external review and defeats not one flag but
the path vocabulary every match here is written in: **the scan must run at the
repository's top level, and nothing checked that it did.** ``REPO_ROOT`` comes
from ``__file__``, so a checkout vendored inside an outer repository puts
``root`` below a top level it never chose, and git then answers inconsistently
in two directions at once -- the porcelain and every ``log`` walk top-level
relative, ``ls-files`` cwd-relative and cwd-restricted. The first spelling
makes ``is_personal_path`` answer False for a path that is under the
containment directory, so ``--scope history`` -- which runs no index comparison
and would raise nothing -- **exits 0 over a real containment path**. See
``require_repository_root``.

That constraint is restated in several other documents. **Find them with
``git grep -n "top level"`` rather than from a count here** -- the count this
paragraph used to carry said "three further places" and there were six, which
is the argument made at length above.

Three scopes, one per caller:

``worktree``
    ``/savepoint`` step 1 — this checkpoint's chunk. Porcelain plus the staged
    blob identity check, no history.
``branch``
    ``/land`` step 3 — the whole branch. The above plus every path any commit
    in ``<merge-base>..HEAD`` touched.
``history``
    CI's secret-scan job — every path in every reachable commit, the backstop
    for anything the local gates never saw.

**The history scope is diff-based, not object-based, and that is load-bearing.**
``git rev-list --objects --all`` looks like the natural instrument and is
unsound for this question: it lists each *object* once, so files sharing content
collapse to a single line under one arbitrary path. Measured on a repo holding
three identical-content files, one of them at ``specs/personal/sub dir/a b.md``:
``rev-list --objects`` reported one blob under a different path and never named
the personal one, while the diff walk reproduced the per-commit ``ls-tree``
ground truth exactly.

Exit 0 when the scan ran and found nothing; 1 when it found something **or when
it could not run**. A precondition it cannot satisfy is a failure, never a pass
— hole 2 is precisely a gate that reported clean having examined nothing, so the
success line states how many paths were examined.

Stdlib only. Git output is read as UTF-8 with replacement, so an undecodable
path surfaces as a readable violation rather than a traceback nothing owns.
"""

from __future__ import annotations

import argparse
import io
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

# Resolved once, matching `scripts/review_worktree.py`. The `or "git"` fallback
# keeps the failure inside `_git`'s own error handling, where it reads as a
# precondition failure, rather than at import time.
_GIT = shutil.which("git") or "git"

# The containment directory, in git's spelling: forward slashes, no trailing
# slash, lowercase. `is_personal_path` handles the casing.
PERSONAL_DIR = "specs/personal"

DEFAULT_BASE_REF = "origin/main"

SCOPES = ("worktree", "branch", "history")

# Matches the launcher's envelope (`scripts/review_worktree.py`): a hung git
# child must fail the gate rather than hang the session that invoked it.
COMMAND_TIMEOUT = 120


class ContainmentError(Exception):
    """A precondition failed, so the scan did not run.

    Raised rather than returned because it is categorically different from a
    violation: a violation means the scan looked and found something, while
    this means it never looked. Both exit 1; only this one is a reason to stop
    trusting the result rather than to fix the tree.

    `found` carries any violations already collected when the precondition
    failed, and exists because the two categories are not ordered the way the
    raise site implies. `tracked_personal` needs no history at all, so a
    force-added file under the containment directory is knowable on a shallow
    clone and on a repository whose `origin/main` cannot be resolved -- exactly
    the states that raise. Discarding it would mean the operator hears "fix your
    setup" while a personal file sits in the index unnamed, which inverts which
    of the two is urgent.

    `notes` and `examined` ride along for the same reason and were added
    because they did not: the first external review of this gate found `found`
    carefully preserved while its two siblings were dropped on the identical
    path. A note is the content half's one instruction for a path whose staged
    bytes are not the working-tree bytes, and losing it exactly when the tree is
    already in trouble is the asymmetry `found` exists to prevent, reproduced
    one field over. `examined` is the evidence that the scan looked at all, and
    a partial scan has more use for it than a clean one, not less.

    `also_failed` carries the *other* precondition failures, because only one
    exception can propagate and the one that does is decided by ordering rather
    than by usefulness. Measured: an unresolved merge plus an unresolvable base
    reported only the base, so the operator was pointed at a `--base` fix while
    a live merge conflict -- the more actionable of the two, and the one
    blocking `git commit` outright -- went unmentioned. Same argument as
    `found`: the failure that stopped the scan does not retract what the scan
    had already established.

    `base` completes the set, and was the last field to. A deferred staged
    refusal fires *after* `resolve_merge_base` has already succeeded, so the
    scan knows the resolved base -- and the patch-stream command the content
    half must read is derived from it. Carrying every other field out of a
    refusal while dropping this one left the operator with the enumeration and
    no instruction for `/land` step 3b, on the one exit where the tree is
    already known to be in trouble. `None` means the scan never got that far,
    which is a different thing from a refusal that did.
    """

    def __init__(
        self,
        message: str,
        found: list[str] | None = None,
        notes: list[str] | None = None,
        examined: dict[str, int] | None = None,
        also_failed: list[str] | None = None,
        base: str | None = None,
    ) -> None:
        super().__init__(message)
        self.found: list[str] = found if found is not None else []
        self.notes: list[str] = notes if notes is not None else []
        self.examined: dict[str, int] = examined if examined is not None else {}
        self.also_failed: list[str] = also_failed if also_failed is not None else []
        self.base: str | None = base


def is_personal_path(rel: str) -> bool:
    """Whether a git-reported path is the personal directory or inside it.

    Byte-identical in behaviour to ``review_worktree._is_personal``, and
    ``tests/test_check_personal_containment.py`` pins the two together rather
    than letting a third copy of the rule drift. Not imported from there: the
    containment gate should not stop working because the reviewer launcher is
    mid-edit, and that module is 2,000+ lines whose module scope this script
    has no reason to execute.

    Case-folded because git preserves on-disk casing while the filesystems this
    project runs on (Windows, macOS) do not, so ``Specs/Personal/`` names the
    very directory the rule protects — and the POSIX CI legs are where a
    case-sensitive test lets it through.

    The bare path is matched as well as the prefix, and that is not defensive.
    ``.gitignore``'s rule is ``specs/personal/`` — trailing slash, so git
    matches it against *directories only* — which means a plain file at exactly
    ``specs/personal`` is ignored by nothing and matched by no prefix test.
    """
    folded = rel.casefold()
    return folded == PERSONAL_DIR or folded.startswith(PERSONAL_DIR + "/")


def split_nul(data: str) -> list[str]:
    """NUL-delimited git output as fields, dropping the empty trailing one.

    Empty fields are dropped rather than preserved: ``git log --format=`` emits
    an empty record per commit alongside the paths, and every caller here wants
    the paths.
    """
    return [record for record in data.split("\0") if record]


def _distinct(paths: list[str]) -> list[str]:
    """`paths` with repeats removed, first occurrence order preserved.

    Every path list here can repeat a path, and each source repeats it for its
    own reason: a history walk names a path once per *commit* that touched it,
    and `git ls-files` lists a conflicted path once per stage. Undeduplicated,
    a single leaked file across thirty checkpoints prints thirty byte-identical
    violation lines, which buries any second, different offender in the middle
    of them — and the evidence line `/land` step 3a tells the operator to read
    reports commits-touching-paths under a name that says paths.

    `dict.fromkeys` rather than `set` because the order is the reading order:
    the violations are printed in the order git walked them, and a set would
    reshuffle them differently on every run.
    """
    return list(dict.fromkeys(paths))


def parse_status_z(data: str) -> list[str]:
    """Every path named by ``git status --porcelain -z``, renames included.

    The record is ``XY<space><path>``, and when either status letter is ``R``
    or ``C`` the **next** NUL-delimited field is the source path, carrying no
    status prefix of its own. Both paths are returned: a rename out of the
    personal directory is as much a containment event as a rename into it.

    Beware when reading this next to `parse_name_status_z`: the two commands
    order a rename pair **oppositely**. ``status --porcelain -z`` gives
    ``R  <new>\\0<old>``, while ``diff --cached --name-status -z`` gives
    ``R100\\0<old>\\0<new>`` (both measured). A parser that assumes one ordering
    binds the wrong path in the other.
    """
    fields = split_nul(data)
    paths: list[str] = []
    index = 0
    while index < len(fields):
        record = fields[index]
        index += 1
        # "XY path" — two status characters, a space, then at least one more.
        if len(record) < 4 or record[2] != " ":
            raise ContainmentError(
                f"unparseable `git status --porcelain -z` record: {record!r}"
            )
        status, path = record[:2], record[3:]
        paths.append(path)
        if "R" in status or "C" in status:
            if index >= len(fields):
                raise ContainmentError(
                    f"rename/copy record {record!r} has no source path after it"
                )
            paths.append(fields[index])
            index += 1
    return paths


def parse_name_status_z(data: str) -> list[tuple[str, str]]:
    """``(status, path-holding-the-staged-bytes)`` from ``--name-status -z``.

    Deletions are kept in the result with their status so the caller can decide
    — they contribute no bytes to the commit, and both halves of a content
    comparison fail on them (``fatal: path 'f.md' does not exist`` from the
    index side and ``could not open 'f.md'`` from the worktree side, measured).

    For a rename or copy the **destination** is returned, because that is where
    the staged content lives; the source is governed the way a deletion is. The
    status letters arrive with a similarity score (``R100``), so every test
    here is a prefix match rather than an equality test.
    """
    fields = split_nul(data)
    records: list[tuple[str, str]] = []
    index = 0
    while index < len(fields):
        status = fields[index]
        index += 1
        wanted = 2 if status.startswith(("R", "C")) else 1
        if index + wanted > len(fields):
            raise ContainmentError(
                f"`git diff --cached --name-status -z` record {status!r} is "
                f"missing {wanted - (len(fields) - index)} of its path fields"
            )
        # A rename/copy carries <src>\0<dst>; the destination holds the bytes.
        path = fields[index + wanted - 1]
        index += wanted
        records.append((status, path))
    return records


def _git(root: Path, *args: str) -> str:
    """Run git in `root`, returning stdout; a non-zero exit is a hard stop.

    `-c log.showSignature=false` is a correctness fix, not hygiene. With that
    setting on -- it is a plain user config, and signing repositories set it --
    git interleaves gpg's verification lines into `git log`'s **stdout**, and
    they carry no NUL of their own. Measured on a repository with a signed
    commit: `branch_paths` returned a single "path" reading
    ``"gpg: no signature found\\n...\\nb.md"``. The noise does not merely inflate
    the count; it is *glued to the front of the next path*, so
    `is_personal_path` answers False for a path that is under the containment
    directory. A false negative, from a setting the scanned repository chose.

    Only that one setting is overridden. Neutralizing global config wholesale
    -- which `tests/test_check_personal_containment.py` does to its fixtures --
    is wrong here for the opposite reason: `safe.directory`, credential helpers
    and `core.longpaths` live there too, and a gate that discards them fails on
    the CI runners and Windows checkouts it has to work on. The test suite can
    neutralize because it owns the repositories it builds; the gate cannot,
    because it does not own the one it is pointed at.
    """
    try:
        proc = subprocess.run(  # noqa: S603 - fixed executable, no shell
            [_GIT, "-C", str(root), "-c", "log.showSignature=false", *args],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=COMMAND_TIMEOUT,
            check=False,
        )
    except OSError as exc:  # git missing, or not executable
        raise ContainmentError(f"could not run `git {' '.join(args)}`: {exc}") from exc
    except subprocess.TimeoutExpired as exc:
        raise ContainmentError(
            f"`git {' '.join(args)}` did not return within {COMMAND_TIMEOUT}s"
        ) from exc
    if proc.returncode != 0:
        raise ContainmentError(
            f"`git {' '.join(args)}` exited {proc.returncode}: "
            f"{proc.stderr.strip() or proc.stdout.strip()}"
        )
    return proc.stdout


def resolve_merge_base(root: Path, base_ref: str = DEFAULT_BASE_REF) -> str:
    """The merge base of `base_ref` and HEAD, or a loud failure.

    This is hole 2, and it is worth being explicit about how quietly it fails
    when it is left to shell substitution. ``git merge-base`` has two distinct
    failure modes, both measured: an **unknown ref** exits 128, and **unrelated
    histories** exit 1 printing nothing at all. Interpolated unguarded, either
    one leaves the range ``..HEAD``, which git accepts and answers with silence
    at exit 0 — so the containment scan reports clean having examined nothing,
    which is indistinguishable from a clean tree.

    The resolved value is then confirmed to name a real commit, so a truncated
    or otherwise unusable answer cannot reach a revision range either.
    """
    try:
        out = _git(root, "merge-base", base_ref, "HEAD")
    except ContainmentError as exc:
        raise ContainmentError(
            f"cannot resolve the merge base of {base_ref} and HEAD, so the "
            f"branch history cannot be scanned -- refusing to report clean on "
            f"an unexamined range ({exc})"
        ) from exc
    base = out.strip()
    if not base:
        raise ContainmentError(
            f"`git merge-base {base_ref} HEAD` returned nothing (unrelated "
            "histories, or a missing base ref) -- refusing to report clean on "
            "an unexamined range"
        )
    _git(root, "cat-file", "-e", f"{base}^{{commit}}")
    return base


def require_repository_root(root: Path) -> None:
    """Refuse to scan from anywhere but the repository's own top level.

    Every path in this module is spelled the way git spells it at the top level
    -- ``specs/personal/...`` -- and that vocabulary is only valid when `root`
    *is* the top level. Git's commands disagree about relativity below it, and
    they disagree in both directions at once. Measured, with `root` one
    directory down:

    * ``git -C sub diff --cached --name-status`` answers **top-level**-relative
      (``sub/specs/personal/labs.md``), as do ``status --porcelain`` and every
      ``log --name-only`` walk;
    * ``git -C sub ls-files -s -v`` answers **cwd**-relative
      (``specs/personal/labs.md``) *and* silently restricts itself to the cwd
      subtree, so a staged file elsewhere in the repository is absent entirely.

    Both failures are silent and they are not the same failure. The first makes
    `is_personal_path` answer False for a path that is under the containment
    directory -- so the ``history`` scope, which is CI's backstop and runs no
    index comparison at all, **exits 0 over a real violation**. The second makes
    every `entries.get(path)` miss, so `staged_content_mismatches` raises "the
    index cannot be read consistently" and blames the index for what is a
    path-relativity problem.

    Refusing rather than rebasing the paths onto the top level, which is the
    tempting repair: that would silently scan a *different* repository from the
    one the caller named, over a containment directory that may not be the one
    it meant. This module's rule is that a precondition it cannot satisfy is a
    failure and never a pass, and "I was pointed somewhere my path vocabulary
    does not apply" is exactly that.

    Reachable without anyone doing anything strange, which is why it is checked
    rather than assumed: `REPO_ROOT` is derived from ``__file__``, so vendoring
    this checkout inside an outer repository -- or running the script by
    absolute path from another tree -- puts the gate below a top level it never
    chose. Raised by the first external review as the author's own open
    uncertainty; the mechanism turned out to be sharper than the uncertainty
    proposed.

    **Its spawn is not fusible with ``require_full_history``'s, and the
    arithmetic is recorded here because a review proposed exactly that.** The
    suggestion was ``git rev-parse --show-toplevel --is-shallow-repository`` in
    one call, on the argument that this guard cancels the saving the staged
    early-return buys in ``/savepoint``'s twice-per-checkpoint path. Measured
    spawn counts: ``worktree`` with nothing staged is **4**
    (``--show-toplevel``, ``ls-files``, ``status``, ``diff``) and
    ``--is-shallow-repository`` **does not appear at all**, because
    `require_full_history` returns before it for that scope. So the fusion
    saves nothing in the very scenario it was proposed for. It saves one spawn
    of eight in ``branch``, which runs once per landing rather than twice per
    checkpoint. The cancellation claim is also only half true: the early return
    saves two spawns and this guard spends one, a net 5 -> 4.
    """
    toplevel = _git(root, "rev-parse", "--show-toplevel").strip()
    if not toplevel:
        # **Defence in depth against a git that does not exist, and said so
        # explicitly because this module's bar is one negative fixture per
        # guard.** No measured state reaches it: a bare repository, a cwd
        # inside `.git`, and an empty `GIT_WORK_TREE` all exit **128** with
        # `fatal: this operation must be run in a work tree` (or `The empty
        # string is not a valid path`), which `_git` turns into a different
        # `ContainmentError` before this line runs. It is kept rather than
        # deleted because an empty answer at exit 0 would otherwise compare
        # equal to nothing and fall through to a scan -- silence answering a
        # precondition, which is the failure mode this whole module exists to
        # refuse. `test_an_empty_toplevel_answer_is_refused` pins the branch by
        # stubbing `_git`, so it is exercised without pretending the state is
        # natural.
        raise ContainmentError(
            "`git rev-parse --show-toplevel` returned nothing, so the "
            "repository root cannot be confirmed -- refusing to scan with an "
            "unverified path vocabulary"
        )
    # `.resolve()` on both sides, not a string compare: git answers with
    # forward slashes on Windows, and the caller's path may carry a symlink
    # (macOS `/tmp` -> `/private/tmp`) or a short 8.3 component.
    if Path(toplevel).resolve() != Path(root).resolve():
        raise ContainmentError(
            f"{root} is not the top level of its git repository ({toplevel} "
            f"is), and every path this gate matches is spelled relative to the "
            f"top level -- git reports the two scopes' paths inconsistently "
            f"from below it, so a containment path can read as clean"
        )


def is_shallow(root: Path) -> bool:
    """Whether the repository's history is truncated by a shallow clone."""
    return _git(root, "rev-parse", "--is-shallow-repository").strip() == "true"


def require_full_history(root: Path, scope: str) -> None:
    """Refuse a history-walking scope on a shallow clone.

    **This is hole 2 reproduced inside this gate, and it is the reason the check
    exists.** Measured on a `--depth 1` clone of this repository: `--scope
    history` exits **0** reporting "620 paths examined" while having seen 4
    commits of roughly 80. Nothing about that output looks wrong -- 620 is a
    substantial-sounding number -- so the scan reports clean over a history it
    never had. That is exactly the failure this whole module was written to end,
    one layer up.

    The CI job that runs the history scope checks out at ``fetch-depth: 0``
    today, so it is correct as configured. The guard is here because *nothing
    enforced that*: the setting lives in a different file from the scan, and the
    cost of removing it -- or of copying the step into a job that lacks it -- is
    silence rather than an error. A gate whose correctness depends on a
    checkout option in another file should say so itself.

    The `worktree` scope is deliberately exempt. It walks no history at all, so
    a shallow clone costs it nothing, and refusing there would break
    ``/savepoint`` for anyone working in one.
    """
    if scope == "worktree":
        return
    if is_shallow(root):
        raise ContainmentError(
            f"the {scope} scope walks history, and this is a shallow clone -- "
            "it would report clean having examined a truncated history, which "
            "is the exact failure this gate exists to prevent. Re-clone with "
            "full history, or check out with `fetch-depth: 0` in CI"
        )


def tracked_personal(root: Path) -> list[str]:
    """Tracked paths at or under the personal directory, case-insensitively.

    ``:(icase)`` rather than a plain pathspec: git's pathspec matching is
    case-sensitive even where the filesystem is not, so a force-added
    ``Specs/Personal/labs.md`` is invisible to ``-- specs/personal/``. No
    trailing slash, so a plain file at exactly ``specs/personal`` matches too;
    git matches a slashless pathspec at directory boundaries, so dropping it
    does not pull in a sibling like ``specs/personal-notes/``.

    Every scope runs this. A tracked file whose commit predates the merge base
    shows up in neither the porcelain (it is unmodified) nor a
    ``merge-base..HEAD`` walk (its commit is not in the range), so without it
    the two local scopes are blind to a personal file that has already landed.
    """
    return split_nul(_git(root, "ls-files", "-z", "--", f":(icase){PERSONAL_DIR}"))


def worktree_paths(root: Path) -> list[str]:
    """Every path the porcelain reports — staged, unstaged, or untracked.

    ``specs/personal/`` is gitignored, so an untracked file there is normally
    invisible here. That is the point rather than a limitation: its appearance
    means the ignore rule itself broke, which is a critical finding, and
    `tracked_personal` covers the force-added case the ignore never sees.

    ``--untracked-files=all`` is load-bearing. At git's default
    (``--untracked-files=normal``) an untracked *directory* is collapsed to a
    single record naming the directory, so a personal file whose parent is
    itself untracked is reported as ``?? specs/`` and the path this gate
    matches on never appears. Found by this gate's own test for the
    broken-ignore case, which failed against the default flag: the ignore rule
    was deleted, the file was created, and the scan reported clean. The
    collapse depends on whether some *sibling* directory happens to be tracked,
    which is not a property a containment scan may rest on.
    """
    return parse_status_z(
        _git(root, "status", "--porcelain", "--untracked-files=all", "-z")
    )


def branch_paths(root: Path, base: str) -> list[str]:
    """Every path touched by any commit in ``base..HEAD``.

    ``--diff-merges=first-parent`` is load-bearing, not formatting. ``git log``
    shows *no* paths for a merge commit by default, so without it a file
    introduced by the merge itself — the ordinary shape of resolving a conflict
    in favour of "keep the added file" — is examined by nothing. Measured: a
    merge that force-adds ``specs/personal/only_in_merge.md``, a path present in
    neither parent, is absent from the default walk and present with the flag.

    **Do not reach for ``--first-parent`` instead.** The names read almost
    identically and do opposite things: this flag changes only how a merge
    commit's diff is *rendered* and leaves traversal whole, while
    ``--first-parent`` restricts the *traversal* and walks straight past every
    commit on a merged-in side branch — which is where an add-then-delete pair
    lives.

    It over-lists: paths that arrived from the merged-in side and are already
    upstream appear too. That is the right direction to err — a path scanned
    needlessly costs a read, a path skipped costs the invariant.
    """
    return split_nul(
        _git(
            root,
            "log",
            "--diff-merges=first-parent",
            "--name-only",
            "--format=",
            "-z",
            f"{base}..HEAD",
        )
    )


def history_paths(root: Path) -> list[str]:
    """Every path touched by any commit reachable from any ref.

    ``--all`` rather than ``HEAD``: this is the CI backstop, and a personal file
    sitting on some other pushed branch is a live exposure whichever branch is
    being built. It needs a full clone — the job that runs it must check out at
    ``fetch-depth: 0`` — and a shallow clone would silently shrink the range,
    so a caller wiring this into CI owes that setting.
    """
    return split_nul(
        _git(
            root,
            "log",
            "--all",
            "--diff-merges=first-parent",
            "--name-only",
            "--format=",
            "-z",
        )
    )


@dataclass(frozen=True)
class IndexEntry:
    """One ``git ls-files -s -v -z`` record: ``<flag> <mode> <blob> <stage>``."""

    flag: str
    mode: str
    blob: str
    stage: int

    @property
    def hidden(self) -> bool:
        """Whether git's own working-tree diff is told to ignore this entry.

        ``S`` is ``skip-worktree``; **any lowercase tag** is
        ``assume-unchanged`` (``H`` becomes ``h``, and so on for the rest of the
        table). Both measured. This is the discriminator the whole check turns
        on, so it is a named property rather than an inline test.
        """
        return self.flag == "S" or self.flag.islower()


def index_entries(root: Path) -> dict[str, IndexEntry]:
    """Every index entry's flag, mode, blob and stage, in one git call.

    One spawn for the whole index, which is what lets the caller below stay at
    a constant three processes however many paths are staged. The previous
    shape asked git twice *per staged path*; on Windows, where a git spawn
    costs 30-50 ms and which is this project's primary development platform, a
    60-file checkpoint paid roughly 120 sequential spawns inside a gate
    ``/savepoint`` runs twice per checkpoint.

    An unmerged path appears **three times**, once per stage (measured: stages
    1, 2 and 3 for one conflicted file), so later records overwrite earlier
    ones here. That is harmless because the only question asked of a conflicted
    entry is whether its stage is non-zero, which every one of the three
    answers identically.
    """
    entries: dict[str, IndexEntry] = {}
    for record in split_nul(_git(root, "ls-files", "-s", "-v", "-z")):
        head, tab, path = record.partition("\t")
        fields = head.split()
        # `.isdecimal()`, not `.isdigit()`: the two differ on exactly the
        # characters that would turn this guard's promised `ContainmentError`
        # into an unhandled `ValueError` one line below. `'²'.isdigit()`
        # is True while `int('²')` raises, so a superscript in the stage
        # field passed the guard and blew up in the constructor -- a traceback
        # nothing owns, from the check written to prevent one. `.isdecimal()`
        # is the predicate `int()` actually implements.
        if not tab or len(fields) != 4 or not fields[3].isdecimal():
            raise ContainmentError(
                f"unparseable `git ls-files -s -v -z` record: {record!r}"
            )
        flag, mode, blob, stage = fields
        entries[path] = IndexEntry(flag=flag, mode=mode, blob=blob, stage=int(stage))
    return entries


@dataclass
class StagedScan:
    """The staged-content comparison: what it found, and how much it read.

    `examined` is carried for the same reason `ScanResult.examined` is, and
    covers the one source that previously had no counter at all: with the call
    wired into `check()` deleted outright, every one of this module's tests
    still passed and a clean run printed an evidence line identical to a
    working one. `mismatches` are violations; `notes` are visible divergences
    that the caller's *content* half needs to know about but that are not
    themselves containment failures.
    """

    mismatches: list[str]
    notes: list[str]
    examined: int


def staged_content_mismatches(root: Path) -> StagedScan:
    """Staged paths whose index blob is not the working-tree content (hole 4).

    A path list proves which paths are in the index, never that each holds the
    bytes the containment scan read, and the two diverge wherever ``git add``
    silently declines to update an entry. It does exactly that on a
    ``skip-worktree`` or ``assume-unchanged`` path: measured, a file staged
    carrying personal data and then cleaned in the working tree keeps the
    **dirty blob** in the index, satisfies every path check, and commits.
    ``git diff --name-only`` cannot stand in for this — ``skip-worktree`` is
    precisely what that command is told to ignore, so it reports clean on the
    one case this catches.

    **Only a divergence git hides is an error, and that line is load-bearing.**
    An earlier version reported *every* index/working-tree difference, which
    made the ordinary "``git add``, then keep editing" state — porcelain ``MM``,
    the most common mid-session index there is — fail the whole gate, blaming a
    ``skip-worktree`` entry that does not exist. Both callers hit it: this runs
    in ``/savepoint`` step 1 *before* that skill stages anything, and in
    ``/land`` step 3a on whatever the tree happens to hold. A gate that fails on
    the normal case is one an operator turns off, which is the argument ADR-0070
    used to reject mechanizing the content half and it applies here unchanged.

    The visible half is not dropped, it is **downgraded to a note**, because it
    still tells the content half something true: for those paths the bytes that
    would commit are not the bytes in the working tree. What makes it safe to
    report rather than fail is that nothing hides it — ``git status`` shows the
    path, ``git add`` updates it, and ``/savepoint`` step 2 reconciles
    ``git diff --cached --name-only`` against its enumerated path list and stops
    on any extra. The ``skip-worktree`` case defeats every one of those.

    Comparisons are blob-to-blob rather than byte-to-byte because
    ``git hash-object`` applies the path's ``.gitattributes`` filters by
    default, so a CRLF working file under ``eol=lf`` compares equal. A check
    that false-alarms on line endings is one an operator learns to skip.
    """
    # Read the staged set first, and return on an empty one before paying for
    # anything else. `/savepoint` step 1 runs *before* that skill stages
    # anything, so the empty index is the ordinary invocation rather than an
    # edge case, and the two calls below cost a full-index read plus a spawn to
    # answer a loop that never executes -- roughly 100 ms of waste on Windows,
    # twice per checkpoint, at the 30-50 ms per spawn `index_entries` cites.
    #
    # This cannot hide the unmerged refusal below it: an unmerged path is
    # reported here as a `U` record unconditionally, so an empty result means
    # there are no unmerged entries to find. That started as a single
    # measurement on an ordinary `UU` conflict, which is thin for a claim the
    # refusal's reachability rests on; a reviewer round then attacked it across
    # every unmerged shape reachable through `git update-index --index-info`
    # -- `AU`, `UA`, `UD`, `DU`, and the `DD` case that an ordinary merge
    # auto-resolves and which had to be forced through plumbing -- and every
    # one of them still produced a `U` record. Not a proof, but no longer one
    # data point.
    records = parse_name_status_z(_git(root, "diff", "--cached", "--name-status", "-z"))
    if not records:
        return StagedScan(mismatches=[], notes=[], examined=0)

    entries = index_entries(root)

    # An unmerged index is a "could not run", not a violation. `git rev-parse
    # :0:<path>` exits 128 there (*"is in the index, but not at stage 0"*), and
    # appending that to the violations list told the operator mid-conflict that
    # the tree was contaminated, in a message that never named the merge --
    # collapsing the split `/land` step 3a and `/savepoint` depend on. There is
    # also no answer to give: a conflicted path has no single staged version,
    # and `git commit` refuses outright until it is resolved.
    unmerged = sorted({path for path, entry in entries.items() if entry.stage != 0})
    if unmerged:
        shown = ", ".join(unmerged[:5])
        more = f" (and {len(unmerged) - 5} more)" if len(unmerged) > 5 else ""
        raise ContainmentError(
            f"the index holds unmerged entries, so there is no single staged "
            f"version to compare the working tree against: {shown}{more} -- "
            f"finish or abort the merge, then re-run"
        )

    # Git's own view of which staged paths differ from the working tree. It is
    # authoritative for every entry it is allowed to look at, and the exactly
    # complementary set -- the hidden entries -- is what the loop re-checks by
    # hand below.
    visibly_dirty = set(split_nul(_git(root, "diff-files", "--name-only", "-z")))

    mismatches: list[str] = []
    notes: list[str] = []
    examined = 0
    for status, path in records:
        if status.startswith("D"):
            continue  # a deletion contributes no bytes; the path list governs it
        entry = entries.get(path)
        if entry is None:
            # Carries what the loop had already established, for the same
            # reason `check()` carries its own: this raise fires *mid-loop*, so
            # a confirmed `skip-worktree` mismatch on an earlier path would
            # otherwise be discarded by a later path's index inconsistency --
            # the more urgent finding lost to the less urgent one. The index
            # can mutate under a concurrent `git add` from an editor
            # integration, so this is reachable without anything exotic.
            #
            # `examined` rides out with them, and it is the field that is easy
            # to forget: the two lists are visibly non-empty when this fires,
            # while the counter is just a number that silently becomes zero.
            # Dropping it made `Examined before stopping:` omit the staged
            # source entirely -- reporting *no* bytes verified on a run that
            # had verified `examined` of them, which understates the work done
            # on exactly the exit where the evidence is load-bearing.
            raise ContainmentError(
                f"`git diff --cached` names {path!r} but `git ls-files -s` does "
                f"not, so the index cannot be read consistently",
                found=mismatches,
                notes=notes,
                examined={STAGED_CONTENT: examined},
            )
        examined += 1

        if not entry.hidden:
            if path in visibly_dirty:
                notes.append(
                    f"staged content differs from the working tree, so the "
                    f"content half must read `git show :0:{path}` rather than "
                    f"the file as it stands: {path} (an ordinary unstaged edit "
                    f"-- `git status` shows it and `git add` resolves it)"
                )
            continue

        # Only entries git is told to hide reach here, so the per-path spawn
        # cost below is paid on a set that is empty in every ordinary checkout.
        if entry.mode not in ("100644", "100755"):
            # A symlink's blob is its target *string* while `git hash-object`
            # would hash the target's contents, and a gitlink has no bytes in
            # this repository at all -- so neither can be compared this way.
            # Reported rather than skipped: unverified is not clean.
            mismatches.append(
                f"staged path is hidden from `git diff` (flag {entry.flag}) and "
                f"is not a regular file (mode {entry.mode}), so what would be "
                f"committed cannot be compared against the working tree: {path}"
            )
            continue
        try:
            working = _git(root, "hash-object", "--", path).strip()
        except ContainmentError as exc:
            mismatches.append(
                f"staged path cannot be compared against the working tree, so "
                f"what would be committed is unverified: {path} ({exc})"
            )
            continue
        if entry.blob != working:
            mismatches.append(
                f"staged content is not what is in the working tree, so the "
                f"scan did not read what would be committed: {path} "
                f"(index {entry.blob[:12]}, worktree {working[:12]} -- the "
                f"entry is marked {entry.flag}, so `git add` declined to update "
                f"it and `git diff` does not show it)"
            )
    return StagedScan(mismatches=mismatches, notes=notes, examined=examined)


def patch_stream_command(base: str) -> str:
    """The command whose output the *content* half must be read against.

    Named rather than run. Every line any commit in the range ever added shows
    up as a ``+`` line here, including lines a later commit removed, so it is
    the only view that catches a value committed and then sanitized (hole 6).
    Deciding whether one of those lines is a real health value is judgement,
    which is why this function hands the caller a command instead of a verdict.
    """
    return f"git log --diff-merges=first-parent -p {base}..HEAD"


# The `examined` sources that enumerate paths and ask `is_personal_path` of
# each. Membership here is what earns an *unannotated* count -- the allowlist
# runs this way round deliberately. Keyed the other way (a denylist of sources
# needing a note) a future source is unannotated by default and reads as
# "distinct paths cleared for containment", which is the exact misreading the
# annotation was added to end, reproduced by omission instead of by drift.
_ENUMERATION_SOURCES = frozenset(
    {"tracked-personal", "worktree", "branch-history", "reachable-history"}
)

# A named constant because the string is the dict key *and* the assignment site
# in `check()`; spelled as a literal in both, a rename in one silently drops the
# annotation and the count goes back to reading as an enumeration.
STAGED_CONTENT = "staged-content"

_EXAMINED_ANNOTATIONS = {STAGED_CONTENT: "bytes verified"}

# What an unrecognized source is called until someone gives it a better phrase.
# It is deliberately the *safe* reading: a count nobody has classified is not a
# containment result, and saying so is cheaper than discovering it was read as
# one. `/land` step 3a's prose described all the counts alike for one release
# and was wrong about exactly the one that had no annotation.
_UNCLASSIFIED_ANNOTATION = "not a containment test"


def _evidence(examined: dict[str, int]) -> str:
    """The per-source counts as one line, each source saying what it counted."""
    if not examined:
        return "nothing examined"
    parts: list[str] = []
    for source, count in examined.items():
        if source in _ENUMERATION_SOURCES:
            parts.append(f"{source} {count}")
            continue
        annotation = _EXAMINED_ANNOTATIONS.get(source, _UNCLASSIFIED_ANNOTATION)
        parts.append(f"{source} {count} ({annotation})")
    return ", ".join(parts)


def _print_notes(notes: list[str]) -> None:
    """Notes, on every exit path that has any -- clean, violated, or refused."""
    for note in notes:
        print(f"  Note: {note}")


def _print_patch_stream(base: str | None) -> None:
    """The content half's instrument, on every exit that resolved a base.

    One function rather than three copies of the `if base is not None` test,
    because the copies were the defect: the clean exit had it and the other two
    did not, so a scan that resolved its base and *then* refused -- a deferred
    staged failure is exactly that -- handed the operator an enumeration with no
    instruction for `/land` step 3b. A violation exit is the same case.
    """
    if base is None:
        return
    print(
        "  Enumeration only. The content half is unread -- scan the patch "
        f"stream for values: {patch_stream_command(base)}"
    )


@dataclass
class ScanResult:
    """What a scan found, and the evidence that it looked.

    `examined` is carried rather than discarded because it is the direct answer
    to hole 2: an empty `errors` list means nothing if the scan walked an empty
    range, so a clean run has to be able to state its own evidence.

    It is a **per-source breakdown** rather than a total, because a total is the
    one shape that hides the failure. A branch scope reporting "1 path examined"
    reads as evidence right up until you notice the 1 came from the working tree
    and the history walk contributed nothing — which is what an unresolvable
    range looks like from the outside. Naming each source separately means a
    zero has somewhere to show up.

    `base` is the *resolved* merge base, not the ref name it came from — the
    patch stream the content half must read has to start where this scan
    started, and `origin/main..HEAD` is a different commit set from
    `<merge-base>..HEAD` on any branch that has merged its base back in.

    `notes` are things the *content* half must know and the enumeration half
    does not fail on — a staged path whose working-tree bytes are not the bytes
    that would commit, where git itself reports the difference. They never
    affect the exit code.
    """

    errors: list[str]
    examined: dict[str, int]
    # Required rather than `field(default_factory=list)`, matching `StagedScan`
    # and this repo's other dataclasses: under `pyright --strict` that factory
    # infers `list[Unknown]` and fails the type gate. Requiring it also means
    # every construction site states its evidence rather than defaulting to
    # silence, which is this class's whole argument.
    notes: list[str]
    base: str | None = None

    def evidence(self) -> str:
        """The per-source counts, for the line a clean run prints."""
        return _evidence(self.examined)


def check(
    root: Path | None = None,
    scope: str = "branch",
    base_ref: str | None = None,
) -> ScanResult:
    """Violations for `scope`, with the evidence that the scan ran.

    `root` defaults to `REPO_ROOT` but is resolved **in the body**, not in the
    signature. A default of `root: Path = REPO_ROOT` binds the module attribute
    once at definition time, so a caller rebinding `REPO_ROOT` -- which is what
    a test pointing the gate at a fixture repository does -- silently keeps
    scanning the real repository instead. That failed quietly here: the test
    passed a temp repo, the gate scanned this one, and the assertion that
    should have caught a planted violation reported clean.

    `base_ref` defaults to **None** rather than to `DEFAULT_BASE_REF` for the
    same class of reason: only `None` distinguishes "not supplied" from
    "supplied", and the scope precondition below needs that distinction. It
    lives here rather than in `main()`, where the first external review found
    it, because a guard in the CLI layer protects only CLI callers -- and this
    module is imported (by its own tests, and by anything CI grows next), so
    `check(scope="history", base_ref="origin/release")` reproduced the exact
    silent-ignore the guard was added to end, with a green suite.
    """
    if scope not in SCOPES:
        raise ContainmentError(f"unknown scope {scope!r}; expected one of {SCOPES}")
    if base_ref is not None and scope != "branch":
        raise ContainmentError(
            f"--base is consulted only by the branch scope, and the {scope} "
            f"scope was requested -- it would have been ignored silently"
        )
    if root is None:
        root = REPO_ROOT

    errors: list[str] = []
    notes: list[str] = []
    examined: dict[str, int] = {}
    base: str | None = None
    # Bound out here, not where it is assigned: the handler below reads it, and
    # every precondition ahead of the assignment can raise -- `UnboundLocalError`
    # from the very handler written to preserve findings.
    staged_failure: ContainmentError | None = None

    try:
        # First, ahead of even `tracked_personal`, and that ordering is the one
        # exception to the argument below. Every other precondition here leaves
        # earlier findings valid; this one invalidates them, because below the
        # top level the paths `tracked_personal` returns are spelled in a
        # different vocabulary from the ones every other source returns. There
        # is nothing to preserve from a scan whose path names cannot be trusted.
        require_repository_root(root)

        # `ls-files` reads the index and walks no history, so it answers on a
        # shallow clone and on a repository whose base ref cannot be resolved --
        # the two states the preconditions below refuse. Running it *first* is
        # what lets those refusals still name a force-added personal file,
        # instead of reporting a setup problem over a violation they had already
        # made knowable.
        tracked = _distinct(tracked_personal(root))
        examined["tracked-personal"] = len(tracked)
        errors.extend(
            f"tracked under the containment directory -- it is gitignored, so a "
            f"tracked entry means it was force-added: {path}"
            for path in tracked
        )

        # The porcelain and the index, before any history precondition -- the
        # same argument that hoisted `tracked_personal`, applied to the other
        # source that reads no history. It was not, and the gap was silent:
        # measured on a `--depth 1` clone whose working tree held an untracked
        # `specs/personal/labs.md`, `--scope branch` refused at
        # `require_full_history` and reported `found: []` while the porcelain
        # was naming the violation. That is the broken-ignore-rule case this
        # module calls a critical finding, lost to a precondition about
        # *history* that the worktree walk does not consult.
        #
        # Deferred rather than raised where it happens, and that is the whole
        # shape of this block. The staged-content sub-check is the only one that
        # refuses on a state the ordinary workflow produces -- an unresolved
        # merge, which `/land` step 3a meets whenever `origin/main` is merged in
        # before landing -- and it used to abort `check()` on the spot, taking
        # the branch and history walks with it. Those walks read history and
        # need no index at all, so a personal file force-added by an earlier
        # savepoint went unreported by anything, in exactly the state that most
        # warrants a scan.
        #
        # Reordering it after them was the obvious repair and is worse: notes
        # would then be produced only by the last thing that runs, so nothing
        # could ever fail *after* one was recorded, and no note could reach the
        # could-not-run path at all. Keeping it here and deferring the raise
        # gets both -- the notes exist for whatever fails later, and the
        # refusal still lands once the index-free walks have had their turn.
        if scope in ("worktree", "branch"):
            paths = _distinct(worktree_paths(root))
            examined["worktree"] = len(paths)
            errors.extend(
                f"working tree holds a containment path, so the ignore rule "
                f"itself has broken: {path}"
                for path in paths
                if is_personal_path(path)
            )
            try:
                staged = staged_content_mismatches(root)
            except ContainmentError as exc:
                staged_failure = exc
            else:
                examined[STAGED_CONTENT] = staged.examined
                errors.extend(staged.mismatches)
                notes.extend(staged.notes)

        # Only now, once every index-free source has reported. A truncated
        # history would let the two walks below report clean over commits they
        # cannot see -- but it says nothing about the porcelain or the index,
        # which is why this sits here rather than above them.
        require_full_history(root, scope)

        if scope == "branch":
            # `if ... is not None else`, never `base_ref or DEFAULT_BASE_REF`:
            # the `or` spelling swallows an **empty** ref back into the default,
            # which is the one substitution failure this whole module is about
            # (hole 2 -- an unset shell variable interpolated into a command).
            # An empty string must reach `resolve_merge_base` and fail there,
            # loudly, rather than be silently replaced with a base the operator
            # never named. Measured: `git merge-base "" HEAD` exits 128.
            base = resolve_merge_base(
                root, base_ref if base_ref is not None else DEFAULT_BASE_REF
            )
            paths = _distinct(branch_paths(root, base))
            examined["branch-history"] = len(paths)
            errors.extend(
                f"a commit on this branch touches a containment path, and its "
                f"blob rides every push of that commit: {path}"
                for path in paths
                if is_personal_path(path)
            )

        if scope == "history":
            paths = _distinct(history_paths(root))
            examined["reachable-history"] = len(paths)
            errors.extend(
                f"reachable history contains a containment path: {path}"
                for path in paths
                if is_personal_path(path)
            )

        # The index-free walks have run; the deferred refusal lands now.
        if staged_failure is not None:
            raise staged_failure
    except ContainmentError as exc:
        exc.found = errors + exc.found
        exc.notes = notes + exc.notes
        exc.examined = {**examined, **exc.examined}
        # A deferred staged failure that lost the race is still true. Only one
        # exception propagates and which one is decided by ordering, not by
        # usefulness: measured, an unresolved merge plus an unresolvable base
        # reported only the base, sending the operator after a `--base` fix
        # while the merge conflict that actually blocks `git commit` went
        # unmentioned. The identity test matters -- on the ordinary path the
        # deferred failure *is* `exc`, and listing it under itself would print
        # the same message twice.
        if staged_failure is not None and staged_failure is not exc:
            exc.also_failed = [str(staged_failure), *exc.also_failed]
        if exc.base is None:
            exc.base = base
        raise

    return ScanResult(errors=errors, examined=examined, base=base, notes=notes)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Check that no personal-data path escapes specs/personal/. "
            "Enumeration only -- whether a file's contents are synthetic stays "
            "a judgement for /land and /savepoint."
        )
    )
    parser.add_argument(
        "--scope",
        choices=SCOPES,
        default="branch",
        help=(
            "worktree: this checkpoint's chunk (/savepoint). "
            "branch: the whole branch against its merge base (/land). "
            "history: every reachable commit (CI). Default: branch."
        ),
    )
    parser.add_argument(
        "--base",
        default=None,
        help=f"base ref, branch scope only (default: {DEFAULT_BASE_REF})",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    # `args.base` is passed through untouched -- no `or DEFAULT_BASE_REF`, and
    # no scope guard here. Both used to live in this function and both belonged
    # in `check()`: the guard because a CLI-layer invariant protects only CLI
    # callers, and the `or` because it silently rewrote an empty `--base` into
    # `origin/main`. `default=None` is what makes "supplied" distinguishable
    # from "defaulted"; this layer's whole job is to turn a raise into an exit
    # code.
    try:
        result = check(scope=args.scope, base_ref=args.base)
    except ContainmentError as exc:
        # Not a violation — the scan never ran. Distinguished in the wording
        # because the remedies are opposite: fix the tree versus fix the setup.
        print(f"containment scan could not run ({args.scope} scope): {exc}")
        # What it managed to find before stopping is still a containment
        # finding, and a more urgent one than the setup problem that stopped it.
        if exc.found:
            print(
                f"  It had already found {len(exc.found)} violation(s), which "
                f"the failure above does not retract:"
            )
            for error in exc.found:
                print(f"    - {error}")
        if exc.also_failed:
            print(
                "  It could not run for more than one reason; the others do "
                "not go away when the one above is fixed:"
            )
            for failure in exc.also_failed:
                print(f"    - {failure}")
        _print_notes(exc.notes)
        if exc.examined:
            print(f"  Examined before stopping: {_evidence(exc.examined)}.")
        _print_patch_stream(exc.base)
        return 1
    if result.errors:
        print(f"personal-data containment violated ({len(result.errors)}):")
        for error in result.errors:
            print(f"  - {error}")
        # Printed on this path too. A note is the content half's one instruction
        # for a path whose staged bytes are not the working-tree bytes, and it
        # is no less true because something else on the branch failed -- the
        # operator fixes the violation and re-runs, and used to be told about
        # the noted path only once the tree was already clean.
        _print_notes(result.notes)
        # And the evidence, which this was the one exit not to print. `/land`
        # step 3a tells the operator to read the evidence line rather than the
        # exit code, and to treat `branch-history 0` on a branch with commits as
        # a scan that examined nothing -- advice with no line to apply it to, on
        # the exit where the tree is already known to be in trouble and a
        # second, unexamined source matters most.
        print(f"  Examined: {result.evidence()}.")
        _print_patch_stream(result.base)
        return 1
    print(
        f"personal-data containment holds in the {args.scope} scope: none of "
        f"the enumerated paths ({result.evidence()}) is under {PERSONAL_DIR}/."
    )
    _print_notes(result.notes)
    _print_patch_stream(result.base)
    return 0


if __name__ == "__main__":
    # Inside the `__main__` guard on purpose, and neither at module scope nor
    # in `main()`. A repository path can hold characters the console encoding
    # cannot represent, and printing one in a violation line would raise
    # `UnicodeEncodeError` -- the gate crashing instead of naming the very path
    # it was built to find. Both obvious placements break something, and this
    # repo has measured it rather than guessed: `_pytest.capture.EncodedFile`
    # and `TeeCaptureIO` are `TextIOWrapper` subclasses, so a reconfigure at
    # module scope fires on *import* and one inside `main()` fires on every
    # in-process call -- either way leaving pytest's own session-wide capture
    # streams at `errors="replace"` for whichever test happens to write next.
    # See `scripts/review_worktree.py`'s `_force_utf8_streams` for the full
    # account, and `scripts/repo_stats.py` for the live defect it caused. Here
    # it runs only when this file is executed as a script, which is the only
    # context that owns a real console.
    #
    # `isinstance` rather than `hasattr` for the same reason that module gives:
    # it names the type that actually has the method.
    for stream in (sys.stdout, sys.stderr):
        if isinstance(stream, io.TextIOWrapper):
            stream.reconfigure(encoding="utf-8", errors="replace")
    sys.exit(main())

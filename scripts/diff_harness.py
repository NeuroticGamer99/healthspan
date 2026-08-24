#!/usr/bin/env python3
"""Shared core for the differential harnesses — snapshot two revisions, diff them.

**Why a differential harness at all, and why it is not another test.** This
repository's markdown parsing shipped silent behaviour changes more than once,
and each landed *in the same commit as the test written for it*.
Mutation testing proves a test tracks the code it was written against; it cannot
prove the code still does what it did before, because the test and the change
arrive together. The instrument that actually found those regressions was a diff
of old-vs-new output over the same inputs, which is what a harness built on this
module mechanises.

**The rule this module exists to hold: snapshot the dependency closure per
revision, not the file.** A checker that imports a sibling script resolves that
sibling at run time, so materialising the checker alone leaves the *old* checker
driven by the *new* sibling — which is precisely the regression class the
harness is there to catch, reproduced inside the harness. `load_side` therefore
takes the whole set of scripts a side needs and writes them into a directory of
their own, so a sibling lookup anchored on ``__file__.parent`` resolves within
that side. Two further traps come with it, both measured:

- Python consults ``sys.modules`` before ``sys.path``, so a plain
  ``import <sibling>`` from inside a snapshot gets whichever side imported it
  first. Each module is loaded here under a per-side name for that reason; a
  caller that needs the *bare* name bound as well must bind it around the call
  and restore it afterwards, or the live process is left pointing at a staging
  directory that is about to be deleted.
- A missing script at a revision is a legitimate answer, not a failure: a script
  added on a branch does not exist at the baseline an ADR documents, and
  aborting on that makes every baseline old enough to be interesting
  unreachable. ``required`` names the scripts without which there is nothing to
  compare; everything else may be absent, and the caller decides what that means.

**The exit contract every harness on this module shares: 0 identical, 1 differ,
2 could not run.** `HarnessError` and `refuse` exist to keep the third distinct
from the second. Both harnesses once spelled every refusal ``raise
SystemExit(<str>)``, which exits **1** — so a harness that compared nothing
reported the same status as a real divergence.
"""

from __future__ import annotations

import importlib.util
import os
import re
import shutil
import subprocess
import sys
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from types import ModuleType

_GIT = shutil.which("git") or "git"
# Both derived from `__file__`, like every other path in this module's callers.
# The working-tree side used to be read from a cwd-relative `Path("scripts")`
# with git run at no particular `cwd`, so a harness only worked from the
# repository root: from `specs/` it raised `FileNotFoundError` out of
# `shutil.copyfile` -- an uncaught traceback rather than the documented "exit 2
# when the harness could not run". The silent variant is worse, and is why this
# is anchored rather than merely guarded: run from another checkout that also
# has the scripts and both sides come from *that* tree, while a caller that
# repoints a snapshot at "the repository" points it at this one, and the run
# prints a reassuring "identical".
SCRIPTS_DIR = Path(__file__).resolve().parent
REPO_ROOT = SCRIPTS_DIR.parent


class HarnessError(RuntimeError):
    """The harness could not run — **not** "the revisions disagree".

    The distinction is the whole exit contract: 0 identical, 1 differ, 2 could
    not run. Raised from anywhere in a harness and turned into exit 2 by the
    thin `main` wrapper each one carries, because the raises fire from inside
    the sentinel check that runs *before* a `_run`-level guard could see them.
    """


@dataclass(frozen=True)
class Side:
    """One revision's script closure, materialised and imported.

    `modules` is keyed by the script's file name -- the same spelling the caller
    passed to `load_side` -- so a harness asks for its subject by name rather
    than by an attribute this module would have to know about. `module()` is the
    accessor to prefer: it turns an absent script into a `HarnessError` naming
    the revision, which reaches exit 2, where an `AttributeError` on a bare
    field would reach exit 1 and read as a divergence.

    `home` is the directory this side's closure was materialised into, and it is
    carried for one named reason rather than for completeness: it is the handle
    the residual `load_side` documents requires. That residual is a sibling
    imported *lazily*, inside a function -- it resolves when it is called, which
    is after `load_side` has restored `sys.path`, so the caller has to put this
    directory back on the path around the call itself. Rebuilding it as
    ``staging / label`` would give the caller a copy of a layout rule this
    module decides.
    """

    label: str
    rev: str | None
    home: Path
    modules: Mapping[str, ModuleType]

    def module(self, name: str) -> ModuleType:
        """This side's `name`, or a `HarnessError` saying it is not there."""
        found = self.modules.get(name)
        if found is None:
            where = "the working tree" if self.rev is None else self.rev
            raise HarnessError(
                f"{self.label}: scripts/{name} does not exist at {where}, so "
                "this side has nothing to compare"
            )
        return found

    def has(self, name: str) -> bool:
        """Whether `name` was present at this side's revision."""
        return name in self.modules


# A hung git should fail the harness, not hang it.
#
# **No enumeration of the sibling runners here, and that is a correction of a
# correction.** This comment listed three as already carrying a timeout and was
# wrong about the third; the sentence that replaced it named `ledger`'s two as
# the only remaining gap, and *that* was wrong too -- `run_gates._tracked_markdown`
# was a sixth runner neither reading found, and it hangs a landing rather than a
# merge. `check_spec_links.md_sources` carried the identical wrong enumeration and
# was corrected in the identical wrong direction on the same commit, which is the
# evidence for the shape: a hand-counted family living in several files cannot
# be checked by reading any one of them. `tests/test_git_runners.py` enumerates by
# scanning, so there is nothing here left to go stale.
_GIT_TIMEOUT = 30


def _git(*args: str) -> subprocess.CompletedProcess[bytes]:
    """Run git under a timeout, turning a hang into a refusal.

    Both a hang and an absent git, because `_GIT` falls back to the bare name
    `git` when `shutil.which` finds nothing: unguarded, the `FileNotFoundError`
    that produces escapes `main`'s `except HarnessError` and exits 1 -- the code
    this harness reserves for "the revisions disagree" -- on a run that compared
    nothing. `check_personal_containment._git` is the sibling that already
    guarded both, and is the shape copied here.
    """
    try:
        return subprocess.run(  # noqa: S603
            [_GIT, *args],
            capture_output=True,
            check=False,
            cwd=REPO_ROOT,
            timeout=_GIT_TIMEOUT,
        )
    except OSError as exc:  # git missing, or not executable
        raise HarnessError(f"could not run `git {' '.join(args)}`: {exc}") from exc
    except subprocess.TimeoutExpired as exc:
        raise HarnessError(
            f"git {' '.join(args)} did not finish within {_GIT_TIMEOUT}s"
        ) from exc


def _git_show(rev: str, path: str, into: Path) -> bool:
    """Write ``rev:path`` to ``into``. False when the path does not exist there.

    A missing path is a legitimate answer rather than a failure -- a script
    added on a branch does not exist at the baseline an ADR documents, and
    aborting on that makes every baseline old enough to be interesting
    unreachable. Any *other* git failure raises.

    **Decided by exit code, not by matching git's stderr.** The two questions
    are asked separately -- does the revision resolve, and does the path exist
    within it -- because that is what tells a legitimately-absent script from a
    revision the operator mistyped. Two things came out of the string-matching
    form it replaces. It matched two literal English substrings, so on a
    machine with a localized git every absent-at-this-revision answer became a
    `HarnessError` and exit 2, which the module docstring says explicitly must
    not happen -- and `--base <pre-adoption rev>`, the reproduction command
    ADR-0061 prints, is exactly that case. And the two questions were
    indistinguishable to it: measured, git answers an unresolvable revision
    with *"exists on disk, but not in <rev>"*, the same wording it uses for a
    path added later, so a mistyped `--base` was read as absence and the
    refusal that followed named the file rather than the revision.
    """
    probe = _git("rev-parse", "--verify", "--quiet", f"{rev}^{{commit}}")
    if probe.returncode != 0:
        # Git's own words when it has any, and it does exactly where the fixed
        # message is wrong. `--quiet` silences the ordinary "no such object"
        # case, which is the one "is not a revision here" describes correctly
        # -- so an empty stderr *is* the signal that the plain wording fits.
        # When stderr is non-empty the fixed message is actively misleading:
        # `HEAD^{tree}` reports "expected commit type, but the object
        # dereferences to tree type" (and `git show HEAD^{tree}:<path>` reads
        # that path fine, 49,068 bytes measured, so calling it "not a revision"
        # contradicts the very next line), and a `.git`-less copy -- an sdist
        # or a `git archive` export -- reports "not a git repository", an
        # environment fault the old wording blamed on the revision the operator
        # typed.
        detail = probe.stderr.decode("utf-8", "replace").strip()
        raise HarnessError(
            f"cannot read {path}: {rev} is not a revision here"
            + (f" ({detail})" if detail else "")
        )
    if _git("rev-parse", "--verify", "--quiet", f"{rev}:{path}").returncode != 0:
        return False
    proc = _git("show", f"{rev}:{path}")
    if proc.returncode != 0:
        message = proc.stderr.decode("utf-8", "replace").strip()
        raise HarnessError(f"cannot read {path} at {rev}: {message}")
    into.write_bytes(proc.stdout)
    return True


def _same_file(recorded: str | None, path: Path) -> bool:
    """Whether `recorded` and `path` name the same file on disk.

    Cut item C1: this was `recorded == str(path)`, and two spellings of one
    path compare unequal. Measured with a directory junction *and* an 8.3 short
    path as the staging root: two `_import` calls for one file, and
    `HarnessError is HarnessError` came back `False` -- exactly what the reuse
    branch above exists to prevent, since two classes that are not the same
    object do not catch each other.

    Not reachable from pytest, which is what hid it: `_pytest/tmpdir.py`
    resolves the temp root before handing it over. `mkdtemp` does not -- it is
    `abspath`, not `realpath` -- so a CI runner's `C:/Users/RUNNER~1/...` or
    macOS's `/var` symlink to `/private/var` is the live trigger, on the real
    `python scripts/diff_check_spec_links.py` invocation rather than in a test.

    `samefile` rather than `resolve()`: it asks the filesystem (device and
    inode) instead of reimplementing its link and short-name rules. It raises
    when either side is gone, and the string comparison is the fallback there --
    a missing file cannot be the module we want to reuse anyway, so the fallback
    only has to be safe, not clever.
    """
    if not recorded:
        return False
    try:
        return os.path.samefile(recorded, path)
    except OSError:
        return recorded == str(path)


def _is_under(recorded: str | None, home: Path) -> bool:
    """Whether `recorded` names a file inside this side's staging directory."""
    if not recorded:
        return False
    try:
        return Path(recorded).resolve().is_relative_to(home.resolve())
    except OSError:  # pragma: no cover - a path the OS will not resolve
        return False


def _is_scripts_dir(entry: str) -> bool:
    """Whether a `sys.path` entry is the live ``scripts/`` directory.

    Compared as resolved paths, not as strings: a conftest, a caller's own
    module-scope insert and an editable install can each spell it differently,
    and a string match would leave the one spelling it missed on the path --
    which is the whole of what this guard is for.
    """
    try:
        return Path(entry).resolve() == SCRIPTS_DIR
    except OSError:  # pragma: no cover - a path the OS will not resolve
        return False


def _live_script_stems() -> set[str]:
    """Bare names in `sys.modules` currently answering from live ``scripts/``.

    Read from `sys.modules` rather than from a directory listing: the question
    is which bare names would *win over* the staging directory, and only a name
    already imported does that. A listing would also hide the whole of
    `scripts/` from a staged body that legitimately never imports it.
    """
    return {
        name
        for name, module in list(sys.modules.items())
        if _is_under(getattr(module, "__file__", None), SCRIPTS_DIR)
    }


def _import(module_name: str, path: Path) -> ModuleType:
    """Execute `path` as `module_name`, or refuse with a `HarnessError`.

    Executing a snapshot runs *an old revision's module body*, which is the one
    thing here that runs code the harness did not write and cannot predict. Any
    of it can fail — a `ModuleNotFoundError` for a dependency that revision
    still had and this environment no longer installs, a `SyntaxError` from a
    revision predating a language feature the running interpreter dropped, a
    module-scope guard that raises on purpose. Unguarded, every one of those
    left the process with a traceback at exit **1**, and 1 is the code this
    module reserves for "the revisions disagree" — so a run that could not so
    much as import a side reported the same status as one that compared both
    and found a real difference. The path is not exotic: ADR-0061 prints
    ``--base <pre-adoption rev>`` as its own reproduction command, and an old
    enough baseline is exactly where an unimportable module body lives.

    **`SystemExit` is caught alongside `Exception`, and leaving it out was the
    first version of this guard.** It derives from `BaseException`, so a plain
    ``except Exception`` does not see it — and "a module-scope guard that raises
    on purpose" is the sentence above naming precisely the shape that escapes,
    while this module's own top docstring records that these harnesses "once
    spelled every refusal ``raise SystemExit(<str>)``". So the revisions this
    tool exists to reach *are* the revisions that raise it, and the guard added
    to route them to exit 2 sent them to exit 1 instead. Measured against the
    shipped guard before it was widened: a staged body raising `SystemExit` came
    straight back out of `_import` uncaught. `_targets` in
    `diff_check_spec_links` had already spelled the pair correctly for the same
    reason, which made this an inconsistency inside one change rather than an
    open question. `KeyboardInterrupt` stays uncaught, on that function's
    argument: an operator's Ctrl-C is not a result.

    The half-initialised module is dropped from `sys.modules` on the way out.
    `_import` registers before executing (which is what lets a module import
    itself, and what `importlib` itself does), so a failure part-way through
    otherwise leaves a broken object under a bare name that a later bare import
    -- this process's own, or the *other* side's sibling lookup -- would find
    and use instead of importing the real one.
    """
    spec = importlib.util.spec_from_file_location(module_name, path)
    if spec is None or spec.loader is None:
        raise HarnessError(f"cannot import {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    try:
        spec.loader.exec_module(module)
    except (Exception, SystemExit) as exc:
        sys.modules.pop(module_name, None)
        # The path is in the message because two sides are always in play: an
        # operator told only `RuntimeError: <text>` knows what went wrong and
        # not *which side* it went wrong on, which is the first thing they need.
        raise HarnessError(
            f"cannot import {path}: {type(exc).__name__}: {exc}"
        ) from exc
    return module


def load_side(
    label: str,
    rev: str | None,
    staging: Path,
    scripts: Sequence[str],
    required: Sequence[str] = (),
) -> Side:
    """Materialise one revision's `scripts` under `staging` and import them.

    ``rev is None`` means the working tree. Every name in `scripts` is a bare
    file name under ``scripts/``; the whole set lands in a directory of its own,
    which is the dependency-closure rule the module docstring states. `required`
    is the subset without which the side is useless -- absent, that is a
    `HarnessError` at load time rather than a confusing failure later.

    Each module ends up registered under ``_diff_<label>_<stem>``, so the first
    side to load cannot shadow the second through ``sys.modules``. That failure
    stayed invisible for as long as the two revisions' files were
    byte-identical and surfaced the moment one of them gained a function the
    other lacked.

    It is *imported* under the bare name and then aliased, rather than imported
    under the private one, and the difference is not cosmetic: a snapshot whose
    sibling import runs at module scope has to find its own side, and it looks
    the sibling up by the bare name. Importing privately left the bare name
    free for the live copy to answer, and giving the bare name to a second
    private import produced two module objects for one file -- measured, a
    staged caller's `HarnessError` and the staged core's were different classes,
    which do not catch each other. The bare registration is torn down before
    this returns; see the body for what that costs and what it does not cover.
    """
    home = staging / label
    home.mkdir(parents=True)
    present: dict[str, Path] = {}
    for name in scripts:
        target = home / name
        if rev is None:
            # The same two questions `_git_show` asks of a revision, asked of
            # the working tree: absence is "this side does not have it", and
            # anything else is a fault. Unguarded, a closure member missing
            # here raised `FileNotFoundError` -- so the `required` refusal
            # below could **never fire for `rev is None`**, despite its
            # dedicated "the working tree" wording, and `main`'s
            # `except HarnessError` did not see it either: the run died at
            # exit 1, the code reserved for "the revisions disagree", having
            # compared nothing. Reachable end to end whenever a script is
            # present at the base revision and absent from the working tree --
            # a rename, a delete, a tree caught mid-rename. Latent today only
            # because the shipped closure has one member, which is the same
            # "gated by `_SCRIPTS = (PARSER,)`" standing the three sibling
            # closure defects sit on.
            try:
                shutil.copyfile(SCRIPTS_DIR / name, target)
            except FileNotFoundError:
                continue  # absent here; `required` below decides what that costs
            except OSError as exc:
                raise HarnessError(
                    f"cannot read scripts/{name} from the working tree: {exc}"
                ) from exc
            present[name] = target
        elif _git_show(rev, f"scripts/{name}", target):
            present[name] = target

    where = "the working tree" if rev is None else rev
    for name in required:
        if name not in present:
            raise HarnessError(
                f"{where}: scripts/{name} does not exist there, so this side "
                "has nothing to compare"
            )

    # Deliver the closure rule for a snapshot's *module-level* sibling import,
    # which needs both halves and had neither. `home` goes on `sys.path` so the
    # sibling can be found there at all -- a caller that inserted the live
    # `scripts/` directory (`diff_check_spec_links` does) otherwise wins, and
    # the staged script imports the live sibling: the old checker driven by the
    # new one, which is the regression class this harness exists to catch,
    # reproduced inside the harness.
    #
    # The bare names come out of `sys.modules` for the same span, because
    # Python consults it *before* `sys.path` -- so a sibling already imported
    # by this process wins over the staging directory no matter what the path
    # says, and `sys.path` alone would look like a fix while changing nothing.
    # Each snapshot re-registers its own sibling under the bare name as it
    # imports, which is what leaves it holding the staged copy afterwards.
    #
    # Both are restored in `finally`, and that matters more than it looks:
    # leaving either in place points the *live* process at a staging directory
    # that is about to be deleted, so a later test in the same worker would
    # parse with a snapshot rather than the working tree.
    #
    # The residual, stated because it is not closed here: a sibling imported
    # *lazily*, inside a function rather than at module scope, resolves when it
    # is called rather than now, and by then this has been restored. A closure
    # whose members import each other that way needs the caller to bind the
    # bare name around the call itself -- and to put this side's own directory
    # back on `sys.path` for the same span, which is what `Side.home` is
    # carried for.
    saved_path = list(sys.path)
    # Every bare name currently answering with a module from the **live**
    # `scripts/` directory, not only the declared stems.
    #
    # Cut item C2: the pop covered `scripts` alone, so a staged module that
    # imports a sibling the caller did not declare found the live copy sitting
    # in `sys.modules` and bound it -- measured, a staged caller's `REPO_ROOT`
    # came back as the live worktree root, which is this instrument comparing a
    # revision against the working tree and reporting whatever that yields.
    # Hiding the whole live set turns that from a silent wrong answer into a
    # `ModuleNotFoundError` inside `_import`, which becomes a `HarnessError` and
    # exit 2: an under-declared closure now refuses instead of quietly
    # half-staging itself. Latent while `_SCRIPTS` has one member, and the
    # reason these are blockers for the change that adds a second.
    stems = {Path(name).stem for name in scripts} | _live_script_stems()
    saved_modules = {stem: sys.modules.pop(stem, None) for stem in stems}
    # And the live `scripts/` directory comes off `sys.path` for the same span,
    # because hiding the *names* alone does not finish the job: measured, a
    # staged caller declaring only itself still bound a core built from the live
    # worktree file -- a fresh object rather than the process's own, but with
    # the live `REPO_ROOT` all the same, which is the outcome C2 names. Python
    # had simply walked past `home` to the live directory that a conftest, or
    # the caller's own module-scope insert, had put on the path.
    #
    # With both closed, an undeclared sibling raises `ModuleNotFoundError`
    # inside `_import`, which becomes a `HarnessError` and exit 2. That is the
    # contract this module already states for every other way a side cannot be
    # built: a run that could not stage what it needs must not report a
    # comparison. `saved_path` restores it.
    sys.path[:] = [entry for entry in sys.path if not _is_scripts_dir(entry)]
    sys.path.insert(0, str(home))
    modules: dict[str, ModuleType] = {}
    try:
        for name, path in present.items():
            stem = Path(name).stem
            # Under the *bare* name, and reusing whatever is already there for
            # this path. A sibling imported a moment ago may have pulled this
            # module in itself, and importing it a second time under a private
            # name would leave the side holding two distinct objects for one
            # file: measured, the staged caller's `HarnessError` and the
            # staged core's were different classes, which do not catch each
            # other. One object per file per side is the property that matters.
            existing = sys.modules.get(stem)
            if existing is not None and _same_file(
                getattr(existing, "__file__", None), path
            ):
                module = existing
            else:
                module = _import(stem, path)
            # The per-side alias is what keeps the module reachable and
            # distinct once the bare name is restored below, so two sides never
            # resolve to one object.
            sys.modules[f"_diff_{label}_{stem}"] = module
            modules[name] = module
    finally:
        sys.path[:] = saved_path
        for stem, previous in saved_modules.items():
            if previous is not None:
                sys.modules[stem] = previous
                continue
            # Cut item C3: this used to `pop` unconditionally, which **deletes
            # a live module the harness never registered**. `previous is None`
            # says only that the name was free when we started; anything could
            # have imported it since -- a staged body's own transitive import,
            # reaching the live copy. Evicting it means the next bare import
            # builds object #2 for that file, and a class from object #1 does
            # not `issubclass` against its twin: the same two-objects-one-file
            # failure the per-side aliasing above exists to prevent, arriving
            # through the cleanup.
            #
            # So only what came out of *this side's* staging directory is
            # removed. Anything else was not ours to take away.
            current = sys.modules.get(stem)
            if current is None or _is_under(getattr(current, "__file__", None), home):
                sys.modules.pop(stem, None)
    return Side(label=label, rev=rev, home=home, modules=modules)


# The `line <N>: ` prefix every harness over a line-oriented checker writes.
# Read rather than required: `render_diff` is shared, so an entry that does not
# carry one still has to sort somewhere deterministic instead of raising.
_ENTRY_LINE_RE = re.compile(r"^line (\d+): ")


def _entry_order(entry: str) -> tuple[int, str]:
    """Sort key putting a diff entry where the file puts it.

    `sorted()` over the raw strings orders them *lexicographically*, so `line
    10:` precedes `line 3:` and the operator's list stops tracking the file it
    describes past nine differing links. Worse for the routine whose whole job
    is deciding what an operator sees, `only_base` and `only_head` then stop
    running in parallel order, so a changed target and its counterpart can no
    longer be paired by eye.

    The string is the tie-break rather than the primary key, which keeps the
    output deterministic for two entries on one line and for any entry without
    the prefix -- those sort together, ahead of nothing and behind every
    numbered line, rather than interleaving unpredictably.
    """
    match = _ENTRY_LINE_RE.match(entry)
    return (int(match.group(1)) if match else -1, entry)


def render_diff(label: str, base: list[str], head: list[str]) -> bool:
    """Print a divergence if there is one. True when the two agree.

    Shared rather than copied per harness: it is the routine that decides what
    an operator sees, so two of them is two answers to the same question.
    """
    if base == head:
        return True
    print(f"\n--- {label}")
    # Counted, not membership-tested. The comparison is `==` while the report
    # was `x not in y`, so a line that merely *changed multiplicity* was
    # reported as present on both sides and vanished from the diff -- and it
    # vanished precisely when something else also differed, because the
    # empty-body branch below only fires when both lists come out empty.
    # Measured: base `['line 3: x.md', 'line 3: x.md', 'line 5: b.md']` against
    # head `['line 3: x.md', 'line 5: c.md']` printed only the b.md/c.md pair,
    # and the dropped duplicate never appeared. Duplicated `(line, target)`
    # tuples are ordinary here -- 8 files in this repository carry one, 16 in
    # total -- so this is reachable rather than theoretical.
    base_counts = Counter(base)
    head_counts = Counter(head)
    #
    # Sorted by *parsed line number*, not by the string. The `Counter` rewrite
    # that recovered the dropped duplicate above replaced order-preserving list
    # comprehensions with a bare `sorted()`, and the elements are the strings
    # `f"line {lineno}: {target}"` -- so the operator's list was ordered
    # lexicographically. Measured: `['line 2: a.md','line 10: b.md',
    # 'line 3: c.md']` against `['line 2: a.md']` printed `line 10` ahead of
    # `line 3`. Both properties the comprehensions had are wanted, and
    # `_entry_order` is what keeps them alongside the multiset fix.
    only_base = sorted((base_counts - head_counts).elements(), key=_entry_order)
    only_head = sorted((head_counts - base_counts).elements(), key=_entry_order)
    if not only_base and not only_head:
        # Same lines, same counts: the two differ only in *order*, which `==`
        # catches and a multiset cannot. Printing both lists is the only useful
        # thing left to say -- without this the operator is told something
        # changed and shown nothing.
        print("  same lines and counts, different order:")
        print(f"  base: {base}")
        print(f"  head: {head}")
        return False
    for line in only_base:
        print(f"  base only: {line}")
    for line in only_head:
        print(f"  head only: {line}")
    return False


def refuse(reason: str) -> int:
    """Report that the harness could not run, and return its exit code.

    One spelling of the exit-2 branch, in one place, because both halves of this
    contract have been got wrong before: the prefix was omitted at some refusal
    sites, and the code was 1 at others. A harness that compared nothing must be
    distinguishable from one that found a real difference.
    """
    print(f"harness could not run: {reason}")
    return 2

"""Unit tests for the shared differential core (scripts/diff_harness.py).

A differential harness is an *instrument*: what it certifies is agreement, so a
defect in it does not fail loudly — it reports that nothing changed. It has
already had exactly that failure once. Both sides imported the parser under its
bare module name, Python consults `sys.modules` before `sys.path`, so whichever
side loaded first drove *both* comparisons; the harness compared a revision
against itself and printed "identical". It stayed invisible for as long as the
two revisions' files were byte-identical, and surfaced only when one of them
gained a function the other lacked.

That is the failure this module exists to keep closed, and it is why the first
two tests are the ones that matter most: they assert the two sides are genuinely
two, rather than asserting the harness produces output.

`render_diff` is exercised through its caller, in
`tests/test_diff_check_spec_links.py`, deliberately and not by oversight — the
assertion there runs through the *harness* module's name, so an import that
quietly went away and grew a local copy would fail it. Duplicating it here would
give the same contract two independent encodings, which this repository has
already recorded as a cost rather than a redundancy.

Loading a side shells out to `git show`, so these tests require the working
directory to be the repository (pytest's rootdir here).
"""

from __future__ import annotations

import os
import subprocess
import sys
from collections.abc import Callable, Iterator
from pathlib import Path
from types import ModuleType
from typing import cast

import diff_harness as harness
import pytest

PARSER = "check_spec_links.py"
ABSENT = "never_existed_xyz.py"
# A commit no repository can contain. It reaches the *second* of git's two
# absence spellings without depending on any history, which is what lets these
# tests run on CI's shallow `fetch-depth: 1` checkout rather than skipping there.
UNRESOLVABLE = "0" * 40


# `clean_import_state` lives in tests/conftest.py, because the sibling suite
# needs the same property and a second copy would be one property with two
# encodings. See that fixture for what it reaches and what it does not.
pytestmark = pytest.mark.usefixtures("clean_import_state")


@pytest.fixture(scope="module")
def sides(
    tmp_path_factory: pytest.TempPathFactory,
) -> tuple[harness.Side, harness.Side]:
    """One side at HEAD and one from the working tree, loaded once."""
    staging = tmp_path_factory.mktemp("sides")
    return (
        harness.load_side("base", "HEAD", staging, (PARSER,), (PARSER,)),
        harness.load_side("head", None, staging, (PARSER,), (PARSER,)),
    )


# --- the defect this instrument already had -------------------------------


def test_each_side_gets_its_own_modules(
    sides: tuple[harness.Side, harness.Side],
) -> None:
    """Two sides are two, not one wearing two labels.

    If the sides shared a module object, every comparison would be a revision
    against itself and every report would read "identical" — the silent-agreement
    failure a harness cannot afford, because agreement is exactly what it is
    trusted to certify.
    """
    base, head = sides

    assert base.module(PARSER) is not head.module(PARSER)
    assert Path(base.module(PARSER).__file__ or "") != Path(
        head.module(PARSER).__file__ or ""
    )


def test_modules_are_registered_under_per_side_names_not_the_bare_one(
    sides: tuple[harness.Side, harness.Side],
) -> None:
    """The `sys.modules` rule, asserted on `sys.modules` rather than inferred.

    The test above compares the two module objects, which a naming scheme that
    collides would also pass on the *second* load if the first had already been
    evicted. This pins the mechanism instead: each side is registered under a
    name carrying its own label, so neither can be reached by a plain
    `import check_spec_links` from inside a snapshot.
    """
    base, head = sides

    assert sys.modules["_diff_base_check_spec_links"] is base.module(PARSER)
    assert sys.modules["_diff_head_check_spec_links"] is head.module(PARSER)
    assert sys.modules.get("check_spec_links") is not base.module(PARSER)
    assert sys.modules.get("check_spec_links") is not head.module(PARSER)


def test_the_working_tree_side_does_not_depend_on_the_cwd(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Both sides are anchored to `__file__`, like every sibling path.

    The working-tree side was read from a cwd-relative `Path("scripts")` and git
    ran with no `cwd=`, so a harness only worked from the repository root. From
    anywhere else it raised `FileNotFoundError` out of `shutil.copyfile` -- an
    uncaught traceback rather than the documented exit 2. The silent variant is
    worse: from another checkout that also has the scripts, both sides come from
    *that* tree and the run prints a reassuring "identical".
    """
    monkeypatch.chdir(tmp_path)

    side = harness.load_side("head", None, tmp_path / "staging", (PARSER,), (PARSER,))

    assert Path(side.module(PARSER).__file__ or "").name == PARSER


def test_a_snapshots_module_level_sibling_import_resolves_within_its_side(
    tmp_path: Path,
) -> None:
    """Report finding 7: the closure rule delivered, not merely stated.

    `diff_check_spec_links` does `from diff_harness import ...` at module scope
    and inserts the *live* `scripts/` directory at `sys.path[0]` itself. Before
    the fix a staged copy therefore bound the live `diff_harness` — the old
    script driven by the new sibling, which is the regression class this
    harness exists to catch, reproduced inside the harness. It was latent only
    because the shipped closure has one entry; it fails exactly when the
    `scripts`/`required` plumbing starts being used for what it was written
    for, and the run then prints a reassuring "identical".

    Asserted on object identity rather than on `__file__`, because identity is
    what a comparison actually depends on: two `HarnessError` classes that are
    not the same object do not catch each other.
    """
    closure = ("diff_check_spec_links.py", "diff_harness.py")
    side = harness.load_side("s", None, tmp_path / "staging", closure, closure)

    staged_caller = side.module("diff_check_spec_links.py")
    staged_core = side.module("diff_harness.py")

    assert staged_caller.HarnessError is staged_core.HarnessError
    assert staged_caller.HarnessError is not harness.HarnessError


def test_an_under_declared_closure_refuses_instead_of_binding_the_live_sibling(
    tmp_path: Path,
) -> None:
    """Cut item C2: the hide covered only the stems the caller declared.

    `diff_check_spec_links` imports `diff_harness` at module scope. Declared
    alone, the staged copy found the live core — first through `sys.modules`,
    which is consulted before `sys.path`, and then (once the names were hidden)
    by walking straight past the staging directory to the live `scripts/` a
    conftest or the caller's own module-scope insert had put on the path.
    Measured both times: the staged caller's `REPO_ROOT` came back as the live
    worktree root, which is this instrument comparing a revision against the
    working tree and reporting whatever that yields.

    Refusing is the contract this module already states for every other way a
    side cannot be built — a run that could not stage what it needs must not
    report a comparison — and it is reached here by the ordinary route, the
    `ModuleNotFoundError` becoming a `HarnessError` inside `_import`.

    Latent while `_SCRIPTS` has one member, which is why this and its two
    siblings are recorded as blockers for the change that adds a second rather
    than as live defects.
    """
    only_the_caller = ("diff_check_spec_links.py",)

    with pytest.raises(harness.HarnessError) as excinfo:
        harness.load_side(
            "s", None, tmp_path / "staging", only_the_caller, only_the_caller
        )

    assert "diff_harness" in str(excinfo.value), str(excinfo.value)


def test_a_declared_closure_still_binds_within_itself(tmp_path: Path) -> None:
    """The boundary the refusal above must not cross.

    Hiding the live `scripts/` from `sys.modules` *and* `sys.path` would be a
    fine way to break the closure rule itself, so the two-member case is
    asserted right beside it: the staged caller must bind the staged core, and
    not the live one. Without this, "refuse on an undeclared sibling" could be
    satisfied by refusing on every sibling.
    """
    both = ("diff_check_spec_links.py", "diff_harness.py")
    side = harness.load_side("s", None, tmp_path / "staging", both, both)

    caller = side.module("diff_check_spec_links.py")
    core = side.module("diff_harness.py")

    assert caller.HarnessError is core.HarnessError
    assert caller.HarnessError is not harness.HarnessError


def test_a_module_the_harness_did_not_register_is_not_evicted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Cut item C3: the cleanup deleted a live module it never created.

    The `finally` popped every declared stem whose saved value was `None`. But
    `previous is None` says only that the name was free when `load_side`
    started — anything could have imported it since, including a staged body's
    own transitive import reaching the live copy. Evicting it means the next
    bare import builds object #2 for that file, and a class from object #1 does
    not `issubclass` against its twin: the two-objects-one-file failure the
    per-side aliasing exists to prevent, arriving through the cleanup.

    The intruder is injected at `_import`, which is where a staged module body
    would do it, and it is given a `__file__` *outside* the staging directory —
    that is the whole discriminator, since what came out of this side's own
    staging directory still must be removed.
    """
    stem = Path(PARSER).stem
    monkeypatch.delitem(sys.modules, stem, raising=False)

    intruder = ModuleType(stem)
    intruder.__file__ = str(harness.SCRIPTS_DIR / PARSER)
    real_import = harness._import  # pyright: ignore[reportPrivateUsage]

    def import_and_intrude(module_name: str, path: Path) -> ModuleType:
        module = real_import(module_name, path)
        sys.modules[stem] = intruder
        return module

    monkeypatch.setattr(harness, "_import", import_and_intrude)

    harness.load_side("s", None, tmp_path / "staging", (PARSER,), (PARSER,))

    assert sys.modules.get(stem) is intruder
    # Clean up after ourselves: `monkeypatch.delitem` above recorded the name as
    # absent, so it restores nothing.
    del sys.modules[stem]


def test_a_module_from_this_sides_staging_is_still_evicted(tmp_path: Path) -> None:
    """The other half of C3, and the property the original pop was protecting.

    Leaving a staged module bound to its bare name points the live process at a
    staging directory that is about to be deleted, so a later test in the same
    worker would parse with a snapshot rather than the working tree — green on a
    real regression, nondeterministically. A fix that simply stopped popping
    would satisfy the test above and reintroduce this.
    """
    stem = Path(PARSER).stem
    before = sys.modules.get(stem)

    harness.load_side("s", None, tmp_path / "staging", (PARSER,), (PARSER,))

    assert sys.modules.get(stem) is before


@pytest.mark.parametrize(
    "spelling",
    [
        "{dir}/./{name}",
        "{dir}//{name}",
        "{dir}/sub/../{name}",
    ],
)
def test_two_spellings_of_one_path_are_recognised_as_the_same_file(
    spelling: str, tmp_path: Path
) -> None:
    """Cut item C1: the module-reuse guard compared path *strings*.

    `existing_file == str(path)` calls two spellings of one file different, so
    the guard fell through to a second `_import` and the side ended up holding
    two module objects for one file — measured with a directory junction and an
    8.3 short path as the staging root, `HarnessError is HarnessError` came back
    `False`, which is exactly what that branch exists to prevent since two
    classes that are not the same object do not catch each other.

    Not reachable from pytest, which is what hid it: `_pytest/tmpdir.py`
    resolves the temp root before handing it over, while `mkdtemp` is `abspath`
    rather than `realpath`. A CI runner's `C:/Users/RUNNER~1/...` or macOS's
    `/var` → `/private/var` symlink is the live trigger, on the real
    `python scripts/diff_check_spec_links.py` invocation.

    Those two triggers cannot be manufactured portably here — creating a
    junction needs Windows and a privilege, and `/private/var` is macOS's alone
    — so the predicate is driven with spellings that are portable and *are* the
    same defect: one path written two ways. A string comparison fails every row.
    """
    (tmp_path / "sub").mkdir()
    real = tmp_path / "mod.py"
    real.write_text("x = 1\n", encoding="utf-8")
    other = spelling.format(dir=tmp_path.as_posix(), name="mod.py")

    assert harness._same_file(other, real) is True  # pyright: ignore[reportPrivateUsage]
    # The boundary: a genuinely different file is still different, or the guard
    # would reuse whatever happened to be loaded.
    assert harness._same_file(str(tmp_path / "sub"), real) is False  # pyright: ignore[reportPrivateUsage]
    assert harness._same_file(None, real) is False  # pyright: ignore[reportPrivateUsage]
    # And a path that does not exist falls back to the string comparison rather
    # than raising out of `samefile`.
    gone = tmp_path / "never.py"
    assert harness._same_file(str(gone), gone) is True  # pyright: ignore[reportPrivateUsage]
    assert harness._same_file(str(gone), real) is False  # pyright: ignore[reportPrivateUsage]


def test_the_reuse_guard_itself_survives_a_respelt_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """C1 at its call site, which the predicate test above does not reach.

    A mechanism asserted at its definition and not where it is used is a shape
    this repository has been caught by before — measured on the sibling
    `on_failure` field, which was set, tested at its builder, and read by two
    lines nothing exercised. So the string comparison is mutated back and this
    is what must notice.

    The two-member closure is what makes the reuse branch live: importing the
    staged caller pulls the staged core in under its bare name, and the loop
    then reaches that same file with `sys.modules` already holding it. Giving
    the recorded `__file__` a `/./` spelling reproduces what a junction or an
    8.3 root does — one file, two strings — portably, where neither real
    trigger can be manufactured on all three CI legs.

    The assertion is the closure rule itself rather than an internal: a failed
    reuse means two module objects for one file, so the staged caller's
    `HarnessError` stops being the staged core's, and two classes that are not
    the same object do not catch each other.
    """
    real_import = harness._import  # pyright: ignore[reportPrivateUsage]

    def import_then_respell(module_name: str, path: Path) -> ModuleType:
        module = real_import(module_name, path)
        # Respell what is *in `sys.modules`*, not what `_import` returns. The
        # sibling the caller pulls in is registered by importlib rather than by
        # `_import`, and that entry is the one the reuse guard reads — patching
        # only the return value leaves the guard comparing two identical strings
        # and the whole test vacuous. (It was written that way first, and the
        # mutation went unnoticed.)
        for loaded in list(sys.modules.values()):
            recorded = getattr(loaded, "__file__", None)
            if recorded and Path(recorded).parent == path.parent:
                spelt = Path(recorded)
                # Same file, different string — exactly what `samefile` sees
                # through and `==` cannot.
                loaded.__file__ = str(spelt.parent) + os.sep + "." + os.sep + spelt.name
        return module

    monkeypatch.setattr(harness, "_import", import_then_respell)

    closure = ("diff_check_spec_links.py", "diff_harness.py")
    side = harness.load_side("s", None, tmp_path / "staging", closure, closure)

    assert (
        side.module("diff_check_spec_links.py").HarnessError
        is side.module("diff_harness.py").HarnessError
    )


def test_loading_a_side_leaves_the_live_process_untouched(tmp_path: Path) -> None:
    """The restore half, which is the dangerous one.

    `load_side` puts the staging directory on `sys.path` and lifts the
    closure's bare names out of `sys.modules` for the duration of the imports.
    Leaving either behind points the *live* process at a directory that is
    about to be deleted, so a later test in the same xdist worker would parse
    with a snapshot rather than the working tree — green on a real regression,
    and nondeterministically so.
    """
    before_path = list(sys.path)
    before_module = sys.modules.get("diff_harness")

    closure = ("diff_check_spec_links.py", "diff_harness.py")
    harness.load_side("s", None, tmp_path / "staging", closure, closure)

    assert sys.path == before_path
    assert sys.modules.get("diff_harness") is before_module


def test_a_side_names_the_directory_its_closure_was_staged_in(
    tmp_path: Path,
) -> None:
    """`Side.home` is the handle the documented lazy-import residual needs.

    A review found the field unread anywhere, which is a fair thing to notice
    and the wrong thing to delete. `load_side` restores `sys.path` and the bare
    `sys.modules` names before it returns, so a sibling imported *lazily* —
    inside a function rather than at module scope — resolves after that restore
    and finds the live copy. Closing that needs the caller to put this side's
    own directory back on the path around the call, and `home` is where that
    directory is named. Without it the caller rebuilds `staging / label` and
    owns a copy of a layout rule this module decides.

    Asserted against the imported module rather than against the expression
    that built the path: what has to be true is that `home` is where this
    side's code actually came from, not that two `Path` joins agree.
    """
    side = harness.load_side("head", None, tmp_path / "staging", (PARSER,), (PARSER,))

    assert (side.home / PARSER).is_file()
    assert Path(side.module(PARSER).__file__ or "").parent == side.home


# --- the closure, and what may be missing from it -------------------------


def test_a_script_absent_at_a_revision_is_an_answer_not_a_failure(
    tmp_path: Path,
) -> None:
    """An optional member of the closure may simply not exist at that revision.

    A script added on a branch does not exist at the baseline an ADR documents,
    and aborting on that made every baseline old enough to be interesting
    unreachable -- which would make the one reproduction command ADR-0061 prints
    fail on its own documented argument. `required` is what must be there;
    everything else is the caller's to interpret.
    """
    side = harness.load_side(
        "base", "HEAD", tmp_path / "staging", (PARSER, ABSENT), (PARSER,)
    )

    assert side.has(PARSER) is True
    assert side.has(ABSENT) is False
    # ...and the boundary: the present one still loads. Without this a
    # `load_side` that silently loaded nothing would pass the two lines above.
    assert side.module(PARSER).link_targets("[a](x.md)\n") == [(1, "x.md")]


def test_asking_for_an_absent_script_is_a_harness_error_naming_the_revision(
    tmp_path: Path,
) -> None:
    """`Side.module` refuses in the currency the exit contract understands.

    A bare attribute lookup would raise `AttributeError`, which reaches exit 1 —
    the code reserved for "the revisions disagree" — so a harness that had
    nothing to compare would report the same status as a real divergence. The
    revision is named because the operator's next move depends on it: a
    `--base` they can move, or a working tree they can fix.
    """
    side = harness.load_side(
        "base", "HEAD", tmp_path / "staging", (PARSER, ABSENT), (PARSER,)
    )

    with pytest.raises(harness.HarnessError) as excinfo:
        side.module(ABSENT)

    assert ABSENT in str(excinfo.value)
    assert "HEAD" in str(excinfo.value)


def test_a_required_script_absent_refuses_at_load_rather_than_at_use(
    tmp_path: Path,
) -> None:
    """Without the required script there is nothing to compare, and saying so
    early is the difference between one clear refusal and a confusing one later.
    """
    with pytest.raises(harness.HarnessError) as excinfo:
        harness.load_side(
            "base", "HEAD", tmp_path / "staging", (PARSER, ABSENT), (ABSENT,)
        )

    assert ABSENT in str(excinfo.value)


def test_a_closure_member_absent_from_the_working_tree_refuses(
    tmp_path: Path,
) -> None:
    """Report finding 12: the `rev is None` copy was the one unguarded path.

    `shutil.copyfile` raised `FileNotFoundError` for a closure member missing
    from the working tree, so the `required` refusal below it — which carries
    dedicated "the working tree" wording — could never fire on that branch, and
    `main`'s `except HarnessError` did not catch it either: the run died at exit
    1, the code reserved for "the revisions disagree", having compared nothing.

    Reachable whenever a script is present at the base revision and absent from
    the working tree: a rename, a delete, a tree caught mid-rename. It is latent
    today only because the shipped closure has one member — the same standing
    the three sibling closure defects sit on, and the reason they are treated as
    blockers for any change that adds a second.

    The refusal must also name *the working tree* rather than `None`, which is
    the property the sibling test below pins for the `module()` route and which
    has no meaning until this path can reach a refusal at all.
    """
    with pytest.raises(harness.HarnessError) as excinfo:
        harness.load_side("head", None, tmp_path / "staging", (ABSENT,), (ABSENT,))

    message = str(excinfo.value)
    assert "the working tree" in message, message
    assert ABSENT in message, message
    assert "None" not in message, message


def test_a_closure_member_absent_from_the_working_tree_but_not_required_is_fine(
    tmp_path: Path,
) -> None:
    """The boundary: absence is only fatal where `required` says so.

    `scripts` and `required` are separate arguments precisely so a closure can
    carry an optional member, and a guard that turned every missing file into a
    refusal would collapse them — passing the test above while breaking the
    plumbing both exist for. This is also the assertion that fails if the guard
    is widened from `FileNotFoundError` to a bare `except OSError: continue`,
    since that would swallow a real read fault as "absent".
    """
    side = harness.load_side(
        "head", None, tmp_path / "staging", (PARSER, ABSENT), (PARSER,)
    )

    assert side.has(PARSER)
    assert not side.has(ABSENT)


def test_the_working_tree_names_itself_rather_than_a_revision(
    tmp_path: Path,
) -> None:
    """`rev is None` is the working tree, and a refusal must not print `None`.

    The two spellings reach the same message, and `None` in it would send an
    operator looking for a revision that does not exist.
    """
    side = harness.load_side("head", None, tmp_path / "staging", (PARSER,), (PARSER,))

    with pytest.raises(harness.HarnessError) as excinfo:
        side.module(ABSENT)

    assert "the working tree" in str(excinfo.value)
    assert "None" not in str(excinfo.value)


def test_an_absent_path_is_decided_by_exit_code_not_by_gits_wording(
    tmp_path: Path,
) -> None:
    """Report finding 9: absence is a git exit code, not an English substring.

    `_git_show` used to decide "absent at this revision, not a failure" by
    matching two literal substrings of git's stderr. That regresses silently
    under a translated locale or a reworded git release, turning every
    legitimately-absent script into a hard refusal at exit 2 — which the module
    docstring says explicitly must not happen, and which `--base <pre-adoption
    rev>`, the reproduction command ADR-0061 prints, is exactly.

    Asserted against real git rather than a stub, so a change in git's own
    behaviour reddens this rather than passing a stub nobody updated. It
    depends on no repository history, so it runs on CI's `fetch-depth: 1`
    checkout rather than skipping there.
    """
    # A path git has never heard of, at a revision that resolves.
    missing = harness._git_show(  # pyright: ignore[reportPrivateUsage]
        "HEAD", f"scripts/{ABSENT}", tmp_path / "a"
    )

    assert missing is False

    # The boundary: a path that IS there returns True and writes real bytes.
    target = tmp_path / "b"

    present = harness._git_show(  # pyright: ignore[reportPrivateUsage]
        "HEAD", f"scripts/{PARSER}", target
    )

    assert present is True
    assert target.read_bytes().startswith(b"#!/usr/bin/env python3")


@pytest.mark.parametrize("rev", [UNRESOLVABLE, "no-such-ref-xyz"])
def test_a_revision_that_does_not_resolve_names_the_revision(
    rev: str, tmp_path: Path
) -> None:
    """The question absence must not swallow: was the *revision* the problem?

    Measured on the string-matching form: git answers an unresolvable revision
    with "exists on disk, but not in <rev>" — word for word what it says about
    a path added later — so a mistyped `--base` was read as absence, and the
    refusal that eventually came named the missing *file* rather than the bad
    revision. Asking git to resolve the revision first separates them.

    Both spellings are covered because they fail differently inside git: an
    all-zero SHA is a well-formed object name that no repository contains,
    while `no-such-ref-xyz` is not a valid object name at all. Neither depends
    on history.
    """
    with pytest.raises(harness.HarnessError) as excinfo:
        harness._git_show(  # pyright: ignore[reportPrivateUsage]
            rev, f"scripts/{PARSER}", tmp_path / "a"
        )

    assert rev in str(excinfo.value)
    assert "not a revision" in str(excinfo.value)
    # And nothing is appended, because `--quiet` silences git for exactly this
    # case. That is the discriminating half of the test below: the fixed
    # wording is right here and wrong wherever git had something to say.
    assert "(" not in str(excinfo.value), str(excinfo.value)


def test_a_revision_git_explains_carries_gits_own_explanation(
    tmp_path: Path,
) -> None:
    """Report finding 9: the rewrite replaced git's stderr with a fixed message.

    `--quiet` silences the ordinary "no such object" case — which is the one
    "is not a revision here" describes correctly — so the sibling test above
    passes with no detail appended. Where git *does* speak, the fixed message
    was actively misleading rather than merely terse.

    `HEAD^{tree}` is the measured case: it is a real object, `git show
    'HEAD^{tree}:scripts/check_spec_links.py'` reads that path fine (rc 0,
    49,068 bytes measured), and the string-matching form this replaced accepted
    it — but `^{commit}` rejects it with "expected commit type, but the object
    dereferences to tree type", and the harness answered "is not a revision
    here" about a revision it had just been given. A `.git`-less copy (an sdist,
    a `git archive` export) is the same shape with worse blame attached: git
    says "not a git repository" and the operator was told their revision was
    bad.

    Asserted against real git rather than a stub, like its siblings here, and
    it depends on no history so it runs on CI's `fetch-depth: 1` checkout.
    """
    with pytest.raises(harness.HarnessError) as excinfo:
        harness._git_show(  # pyright: ignore[reportPrivateUsage]
            "HEAD^{tree}", f"scripts/{PARSER}", tmp_path / "a"
        )

    message = str(excinfo.value)
    assert "not a revision" in message
    # Git's own wording, not a second copy of it kept here: the assertion is
    # that *something git said* survived, plus the one word that distinguishes
    # this cause from the silent one.
    assert "commit type" in message, message


def test_a_git_show_failure_that_is_not_absence_still_refuses(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The branch a review measured as uncovered — `git show` itself failing.

    Both `rev-parse` probes above answer "the revision resolves" and "the path
    is in it", so a non-zero `git show` after that is a git or filesystem fault
    rather than absence, and it must refuse with git's own stderr rather than
    be mistaken for either. Branch coverage over both diff suites reported
    these two lines missing: the guard existed and nothing drove it, so its
    message could have been rewritten to anything and stayed green.

    Driven through `_git` rather than by contriving a repository state that
    makes real git fail after two successful probes — that state is a corrupt
    object store, which a test must not manufacture in the working repository.
    The stub answers the two probes exactly as real git does here, so only the
    third call is synthetic.
    """
    real_git = harness._git  # pyright: ignore[reportPrivateUsage]

    def failing_show(*args: str) -> subprocess.CompletedProcess[bytes]:
        if args[:1] == ("show",):
            return subprocess.CompletedProcess(
                list(args), 128, b"", b"fatal: unable to read object\n"
            )
        return real_git(*args)

    monkeypatch.setattr(harness, "_git", failing_show)

    with pytest.raises(harness.HarnessError) as excinfo:
        harness._git_show(  # pyright: ignore[reportPrivateUsage]
            "HEAD", f"scripts/{PARSER}", tmp_path / "a"
        )

    message = str(excinfo.value)
    assert "cannot read" in message, message
    assert "unable to read object" in message, message
    # Not the absence answer and not the bad-revision answer: this branch has
    # to stay distinguishable from both, which is the whole reason it exists.
    assert "not a revision" not in message, message


# --- the exit contract ----------------------------------------------------


def test_a_module_body_that_raises_refuses_rather_than_exiting_1(
    tmp_path: Path,
) -> None:
    """Executing a snapshot runs an old revision's code, and it may not import.

    `exec_module` sat unguarded, so a `ModuleNotFoundError` for a dependency
    that revision had and this environment lacks — or a `SyntaxError`, or a
    module-scope guard that raises on purpose — left a traceback at exit **1**,
    the code this module reserves for "the revisions disagree". A harness that
    could not so much as import a side then reported the same status as one
    that compared both and found a real difference. ADR-0061 prints
    `--base <pre-adoption rev>` as its own reproduction command, so an old
    enough baseline is exactly where this lives.

    Three halves, and the middle one was missing. The message must carry the
    original failure — an operator handed only "cannot import" cannot tell a
    missing dependency from a syntax error — and it must carry the **path**,
    because two sides are always in play and the type alone does not say which
    side failed. Dropping `{path}` from the message left both this test and its
    harness-level sibling green, which is how the gap was found. The
    half-initialised module must not be left in `sys.modules` either, where a
    later bare import (this process's, or the *other* side's sibling lookup)
    would find it instead of importing the real one.
    """
    name = "broken_snapshot_module_xyz"
    broken = tmp_path / f"{name}.py"
    broken.write_text("raise RuntimeError('this revision cannot run here')\n")

    with pytest.raises(harness.HarnessError) as excinfo:
        harness._import(name, broken)  # pyright: ignore[reportPrivateUsage]

    assert "cannot import" in str(excinfo.value)
    assert str(broken) in str(excinfo.value)
    assert "RuntimeError: this revision cannot run here" in str(excinfo.value)
    assert name not in sys.modules


def test_a_module_body_that_raises_system_exit_is_also_a_refusal(
    tmp_path: Path,
) -> None:
    """`SystemExit` is a `BaseException`, so `except Exception` does not see it.

    The first version of this guard caught `Exception` alone, and the sentence
    justifying it — "a module-scope guard that raises on purpose" — named
    precisely the shape that escaped. It is not a hypothetical shape here: this
    module's own docstring records that these harnesses "once spelled every
    refusal `raise SystemExit(<str>)`", so the old revisions the tool exists to
    reach are the revisions that raise it, and the guard written to route them
    to exit 2 sent them to exit 1 instead. Measured against the shipped guard
    before it was widened: a staged body raising `SystemExit` came straight back
    out of `_import` uncaught.

    `_targets` in the sibling harness had already spelled the pair correctly for
    the same reason, which is why this is a consistency test as much as a
    coverage one.
    """
    name = "exiting_snapshot_module_xyz"
    broken = tmp_path / f"{name}.py"
    broken.write_text("raise SystemExit('old revision refuses at import')\n")

    with pytest.raises(harness.HarnessError) as excinfo:
        harness._import(name, broken)  # pyright: ignore[reportPrivateUsage]

    assert "SystemExit: old revision refuses at import" in str(excinfo.value)
    assert name not in sys.modules


def test_an_interrupt_during_a_snapshot_import_is_not_a_refusal(
    tmp_path: Path,
) -> None:
    """The boundary beside it: Ctrl-C is the operator, not a bad revision.

    Without this, widening the catch to `SystemExit` is indistinguishable from
    widening it to `BaseException` — which would record an operator's interrupt
    as "this revision cannot be imported" and let the run continue against one
    side. `_targets` draws the same line for the same reason.
    """
    name = "interrupting_snapshot_module_xyz"
    broken = tmp_path / f"{name}.py"
    broken.write_text("raise KeyboardInterrupt\n")

    with pytest.raises(KeyboardInterrupt):
        harness._import(name, broken)  # pyright: ignore[reportPrivateUsage]


def test_refuse_prints_the_prefix_and_returns_the_could_not_run_code(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """0 identical, 1 differ, 2 could not run — and the prefix an operator greps.

    Both halves have been got wrong before: refusals were spelled
    `raise SystemExit(<str>)`, which exits **1**, and some printed the reason
    with no prefix at all. One spelling in one place is what keeps "the harness
    broke" distinguishable from "the revisions disagree".
    """
    assert harness.refuse("the reason") == 2

    assert capsys.readouterr().out == "harness could not run: the reason\n"


def test_every_suite_that_loads_a_side_applies_the_shared_fixture() -> None:
    """The shape of the defect, not the one site it was found at.

    `clean_import_state` was autouse *inside this module*, and the sibling
    suite — which loads sides of its own and drives a `main` that loads two
    more — simply never got a copy. Measured on that sibling alone before the
    fix: four `_diff_*` entries outliving the module, each a module object
    whose `__file__` names a staging directory already deleted; one after,
    which is the module-scoped fixture's own, exactly what the shared
    fixture's docstring says it cannot reach.

    Fixing that one file leaves the next suite free to repeat it, so the rule
    is checked rather than remembered. Textual on purpose: the property is
    "this module opted in", which is a fact about the source, and importing
    every suite to inspect its marks would run their module-level code.

    The residual that is stated rather than closed: a suite that drives a
    harness's `main` without ever naming `load_side` is not matched — hence the
    second marker. Adequate for the two suites that exist, and fragile against a
    future one that reaches `load_side` through a shared helper carrying neither
    literal. No such indirection exists in the tree today.

    The residual that **is** closed, below: the file carrying this test always
    contains the mark's spelling in this very docstring, so the textual sweep
    can never flag *itself*. That is not theoretical — removing this module's
    own `pytestmark` line left both diff suites fully green, measured. So this
    file's opt-in is checked on the mark object rather than on its source text,
    which is the one form the sweep cannot express.
    """
    suite = Path(__file__).resolve().parent
    loaders = ("load_side(", "diff_check_spec_links")
    offenders: list[str] = []
    for path in sorted(suite.glob("test_*.py")):
        text = path.read_text(encoding="utf-8")
        if not any(marker in text for marker in loaders):
            continue
        if 'usefixtures("clean_import_state")' not in text:
            offenders.append(path.name)

    assert offenders == [], offenders

    # This module, by the other route. A `NameError` here is the intended
    # failure and not an accident: deleting the `pytestmark` line leaves the
    # name undefined, which is exactly the edit that must not pass quietly.
    assert "clean_import_state" in pytestmark.args, pytestmark.args


def test_the_shared_fixture_drops_what_a_test_changed_and_keeps_what_it_did_not() -> (
    None
):
    """Copilot, PR #100, and the correction the first remedy needed.

    Dropping `set(sys.modules) - set(saved)` reaches only the *added* keys, so
    a test that overwrote a key already present at snapshot escaped teardown.
    Both diff suites create such a key from a **module**-scoped fixture that
    runs first. Measured on tests/test_diff_check_spec_links.py before the fix:
    `_diff_only_check_spec_links` was replaced once per parameter by
    `test_a_renamed_constant_refuses_instead_of_being_silently_created`, and
    the third replacement outlived the entire session.

    The first remedy *restored* the saved object, and that was worse. Both
    suites reuse the labels `base` and `head`, so the second module to run
    inherits the first module's entries in its own `saved` — measured with
    `pytest tests/test_diff_harness.py tests/test_diff_check_spec_links.py`,
    every `main`-driven test in the second suite ended with both keys pointing
    back into the first suite's `sides0` staging. Hence the third assertion
    below: the saved object must not come back.

    The fixture's wiring is checked above; this checks what it *does*, which
    the wiring test cannot see. Driven through `__wrapped__` because pytest 9
    refuses a direct call on a fixture object, and an inner `pytester` session
    would cost a plugin registration in the root conftest to assert the same
    facts.

    The untouched key is asserted too, and it is the one that keeps this
    honest: a teardown that simply cleared every `_diff_*` key would satisfy
    the other three and pull a module-scoped side out from under the fixture
    that owns it, mid-module.
    """
    import conftest

    original = ModuleType("_diff_probe_preexisting")
    replacement = ModuleType("_diff_probe_preexisting")
    untouched = ModuleType("_diff_probe_untouched")
    added = ModuleType("_diff_probe_added")

    sys.modules["_diff_probe_preexisting"] = original
    sys.modules["_diff_probe_untouched"] = untouched
    try:
        # `__wrapped__` is the generator behind the fixture object. Cast because
        # pytest types the object itself and not what it wraps, so `--strict`
        # reads several unknowns off this line otherwise — which is what
        # reddened the gate. If a future pytest drops the attribute this fails
        # loudly, which is the right way to find out.
        wrapped = cast(
            "Callable[[], Iterator[None]]",
            conftest.clean_import_state.__wrapped__,  # pyright: ignore[reportAttributeAccessIssue, reportUnknownMemberType]
        )
        teardown = wrapped()
        next(teardown)  # setup: snapshots sys.modules

        # What a test does between setup and teardown: overwrite one, add one,
        # and leave the third alone.
        sys.modules["_diff_probe_preexisting"] = replacement
        sys.modules["_diff_probe_added"] = added

        with pytest.raises(StopIteration):
            next(teardown)

        # The replaced key is *gone*, not put back: `not in` is the assertion
        # that catches the restoring remedy too, so a separate
        # `is not original` check below it could never fail on its own.
        assert "_diff_probe_preexisting" not in sys.modules
        assert "_diff_probe_added" not in sys.modules
        assert sys.modules["_diff_probe_untouched"] is untouched
    finally:
        sys.modules.pop("_diff_probe_preexisting", None)
        sys.modules.pop("_diff_probe_untouched", None)
        sys.modules.pop("_diff_probe_added", None)

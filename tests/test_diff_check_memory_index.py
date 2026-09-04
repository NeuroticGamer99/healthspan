"""Unit tests for the differential harness (scripts/diff_check_memory_index.py).

The harness is an *instrument*: it is what this branch's behaviour claims rest
on, so a defect in it does not fail loudly — it reports agreement. It has already
had exactly that failure once. Both sides imported `check_spec_links` under the
bare module name, Python consults `sys.modules` before `sys.path`, so whichever
side loaded first drove *both* checkers; the harness compared a revision against
itself and printed "identical". It stayed invisible for as long as the two
revisions' parsers were byte-identical, and surfaced only when one of them gained
a function the other lacked.

That is the failure this module exists to keep closed, and it is why the first
test below is the one that matters most: it asserts the two sides are genuinely
two, rather than asserting the harness produces output.

Loading a side shells out to `git show`, so these tests require the working
directory to be the repository — which is pytest's rootdir here — and are scoped
to one module-level load because that cost is real.
"""

from __future__ import annotations

import subprocess
import sys
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import cast

import diff_check_memory_index as harness
import pytest


def _require_reachable(rev: str) -> None:
    """Skip unless `rev` is in this clone's object store.

    CI's `test` job checks out at the default `fetch-depth: 1` -- only the
    gitleaks job asks for full history -- so a revision pinned far enough back
    to be interesting is simply absent there, and `load_side` exits on it. That
    is a failure on all three OS legs and in any contributor's shallow clone,
    against a property that has nothing to do with either.

    One named helper rather than the probe written out at each site: it existed
    at one of the two and not at the other, which is how the unguarded site read
    as deliberate.
    """
    probe = subprocess.run(  # noqa: S603
        [harness._GIT, "cat-file", "-e", f"{rev}^{{commit}}"],  # pyright: ignore[reportPrivateUsage]
        capture_output=True,
        check=False,
        cwd=harness.REPO_ROOT,
    )
    if probe.returncode != 0:
        pytest.skip(f"{rev} is not reachable in this clone")


@pytest.fixture(autouse=True)
def clean_import_state() -> Iterator[None]:
    """Restore `sys.modules["check_spec_links"]` and `sys.path` after each test.

    A fixture rather than a `try`/`finally` inside the one test that needs it,
    and the reason is that the `finally` version **could not be pinned**. Its
    cleanup is unobservable while `_render` restores correctly, and `_render`'s
    own restore is caught by an assertion rather than by cleanup — so neither
    single mutation reached it. The attempted fix was a sentinel test doing a
    bare `import check_spec_links` afterwards, which works serially and is a
    measured false negative under `-n auto`: across three trials the two tests
    landed on different xdist workers every time, so the sentinel passed in a
    process the leak never touched. Nothing in this repository configures
    `--dist loadgroup`, so there is no marker that would fix the pairing either.

    A fixture removes the problem instead of pinning it. pytest guarantees
    teardown on failure, error and skip alike, so the property stops being this
    module's to prove — and it now covers every test here rather than the one
    that happened to mutate globals. Autouse because the harness reaches
    `sys.modules` through `check_memory_index._markdown()`'s bare import, which
    any test touching a `Side` can trigger.

    What is left for a mutation to catch is `_render`'s *own* restore, which is
    a property of the code under test and is asserted directly.
    """
    saved_module = sys.modules.get("check_spec_links")
    saved_path = list(sys.path)
    try:
        yield
    finally:
        sys.path[:] = saved_path
        if saved_module is None:
            sys.modules.pop("check_spec_links", None)
        else:
            sys.modules["check_spec_links"] = saved_module


@pytest.fixture(scope="module")
def sides(
    tmp_path_factory: pytest.TempPathFactory,
) -> tuple[harness.Side, harness.Side]:
    """One side at HEAD and one from the working tree, loaded once."""
    staging = tmp_path_factory.mktemp("sides")
    return (
        harness.load_side("base", "HEAD", staging),
        harness.load_side("head", None, staging),
    )


# --- the defect this instrument already had -------------------------------


def test_each_side_gets_its_own_modules(
    sides: tuple[harness.Side, harness.Side],
) -> None:
    """Two sides are two, not one wearing two labels.

    If the sides shared either module object, every comparison would be a
    revision against itself and every report would read "identical" — the
    silent-agreement failure this harness cannot afford, because agreement is
    exactly what it is trusted to certify.
    """
    base, head = sides

    assert base.module is not None
    assert head.module is not None
    assert base.module is not head.module
    assert base.parser is not head.parser
    assert Path(base.module.__file__ or "") != Path(head.module.__file__ or "")
    assert Path(base.parser.__file__ or "") != Path(head.parser.__file__ or "")


def test_rendering_a_side_binds_that_sides_parser(
    sides: tuple[harness.Side, harness.Side],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`_render` binds the bare name `check_spec_links` to the side being run.

    The checker reaches its parser through `_markdown()`, which does a plain
    `import check_spec_links`. Materialising each side into a directory of its
    own is necessary and *not sufficient*: the import hits `sys.modules` first.
    So the binding is what makes the isolation real, and this asserts the
    checker actually resolves to its own side's parser **while the call is
    running** — not merely that the dict entry was set.

    Observed from inside the call, deliberately. This used to read
    `sys.modules["check_spec_links"] is base.parser` *after* `_render`
    returned, which asserted a **leak**: the binding outlived the call, so the
    live process was left pointing at a snapshot in a staging directory that the
    harness then deleted. Any later test in the same xdist worker would have
    parsed with the committed parser rather than the working tree's — a
    working-tree regression passing green, nondeterministically under `-n auto`.
    A test that pins the leaked state is what makes such a leak survive review.
    """
    base, head = sides
    corpus = harness._materialise(harness.CORPORA["clean"], tmp_path / "clean")  # pyright: ignore[reportPrivateUsage]

    for side in (base, head):
        assert side.module is not None
        seen: list[object] = []
        real_check = cast("Callable[[Path], object]", side.module.check)

        def spy(
            memory_dir: Path,
            _real: Callable[[Path], object] = real_check,
            _seen: list[object] = seen,
        ) -> object:
            _seen.append(sys.modules["check_spec_links"])
            return _real(memory_dir)

        monkeypatch.setattr(side.module, "check", spy)
        harness._render(side, corpus)  # pyright: ignore[reportPrivateUsage]
        monkeypatch.undo()

        assert seen == [side.parser], seen


def test_rendering_leaves_the_live_process_untouched(
    sides: tuple[harness.Side, harness.Side], tmp_path: Path
) -> None:
    """`_render` restores `sys.modules` and `sys.path` on the way out.

    The other half of the binding above, and the half that is silent when it
    breaks. `_markdown()` inserts its own directory at `sys.path[0]` as well as
    caching under the bare module name, so an unrestored call leaves the live
    process resolving `check_spec_links` to a snapshot whose staging directory
    the harness has since deleted — measured, two dead directories ahead of the
    real `scripts/` on the path. Nothing goes red; a later test simply parses
    with the committed parser instead of the working tree's.
    """
    base, _head = sides
    corpus = harness._materialise(harness.CORPORA["clean"], tmp_path / "clean")  # pyright: ignore[reportPrivateUsage]
    staging = str(Path(base.parser.__file__ or "").parent)

    before_module = sys.modules.get("check_spec_links")
    # The staging directory must not be on the path *before* the call, or the
    # assertion after it is satisfied by an earlier test's leak rather than by
    # this call's restoration. Removing it here is what makes the check
    # discriminating: asserting `sys.path == before_path` alone survived a
    # mutation that deleted the restore, because a previous `_render` in the
    # same process had already put the directory there permanently.
    #
    # Safe to mutate in place because `clean_import_state` restores both globals
    # at teardown whatever this test does -- see that fixture for why the
    # hand-rolled `try`/`finally` this replaced could not be pinned.
    sys.path[:] = [entry for entry in sys.path if entry != staging]
    baseline = list(sys.path)

    harness._render(base, corpus)  # pyright: ignore[reportPrivateUsage]

    assert sys.modules.get("check_spec_links") is before_module
    assert staging not in sys.path, sys.path[:3]
    assert sys.path == baseline


# --- the comparison itself ------------------------------------------------


def test_diff_reports_agreement_and_divergence(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """True and silent when equal; False and printed when not.

    The printed side matters as much as the boolean: a harness that returned the
    right count while printing nothing would leave the operator with a number and
    no diff to make the choice with, and choosing is the whole point.
    """
    assert harness.render_diff("same", ["a", "b"], ["a", "b"]) is True
    assert capsys.readouterr().out == ""

    assert harness.render_diff("differs", ["a", "gone"], ["a", "new"]) is False
    printed = capsys.readouterr().out
    assert "base only: gone" in printed
    assert "head only: new" in printed


def test_a_raise_is_recorded_as_behaviour_not_a_crash(
    sides: tuple[harness.Side, harness.Side], tmp_path: Path
) -> None:
    """Turning a report into a refusal is a behaviour change worth catching.

    So `_render` must record the raise rather than propagate it — otherwise the
    first corpus that makes one side refuse aborts the whole run and every later
    corpus goes uncompared.
    """
    base, _ = sides
    absent = tmp_path / "not-a-directory"

    rendered = harness._render(base, absent)  # pyright: ignore[reportPrivateUsage]

    assert len(rendered) == 1
    assert rendered[0].startswith("RAISED ReconcileError")


def test_an_interrupt_is_not_recorded_as_a_corpus_result(
    sides: tuple[harness.Side, harness.Side],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Ctrl-C is the operator, not a finding.

    `_render` catches `(Exception, SystemExit)` and deliberately not
    `BaseException`: an external round found the wider catch in the checker
    itself, where it turned an interrupt into a clean-looking exit. The same
    swallow here would record the interrupt as the corpus's behaviour and let the
    run continue.
    """
    base, _ = sides
    corpus = harness._materialise(harness.CORPORA["clean"], tmp_path / "clean")  # pyright: ignore[reportPrivateUsage]

    def interrupt(_memory_dir: Path) -> None:
        raise KeyboardInterrupt

    monkeypatch.setattr(base.module, "check", interrupt)

    with pytest.raises(KeyboardInterrupt):
        harness._render(base, corpus)  # pyright: ignore[reportPrivateUsage]


# --- the corpus -----------------------------------------------------------


@pytest.mark.parametrize("name", sorted(harness.CORPORA))
def test_every_corpus_is_checkable(name: str, tmp_path: Path) -> None:
    """A corpus with no index is not a corpus — `check` refuses it outright.

    Such an entry would compare "RAISED" against "RAISED" and count as agreement
    forever, occupying a slot in the total while testing nothing. The count in
    the summary line is what an operator reads as coverage.
    """
    root = harness._materialise(harness.CORPORA[name], tmp_path / name)  # pyright: ignore[reportPrivateUsage]

    assert (root / "MEMORY.md").is_file(), f"corpus {name!r} has no index"


def test_an_unknown_corpus_name_is_refused_rather_than_silently_empty() -> None:
    """`--only nope` must not run zero corpora and report success.

    Exit 2 is the harness's could-not-run code; returning 0 over an empty set is
    the shape that reads as a clean pass.
    """
    assert harness.main(["--only", "no-such-corpus"]) == 2


# --- external review round 4 ----------------------------------------------


def test_a_side_that_cannot_parse_aborts_rather_than_agreeing(
    sides: tuple[harness.Side, harness.Side],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A shared exception is not agreement, and must not exit 0.

    `_render` turns any raise into a comparable string, so when both sides raise
    the SAME exception the run counted it as agreement. Under the bare `python`
    both Usage blocks and ADR-0061's reproduction command used to spell, that is
    exactly what happened: every corpus returned
    `RAISED ModuleNotFoundError: No module named 'markdown_it'`, and the harness
    reported "31 corpora, 31 identical, 0 diverged" at exit 0, having parsed
    nothing. The sentinel is what makes a run that examined nothing say so.
    """

    # Patched at `_render` rather than at a side's `check`, because `main` loads
    # its own sides: the fixture's module objects are not the ones it runs. What
    # this pins is the path from a RAISED report to exit 2, which is where the
    # old behaviour turned an unparsed run into "identical, 0 diverged".
    def raised(_side: harness.Side, _memory_dir: Path) -> list[str]:
        return ["RAISED ModuleNotFoundError: No module named 'markdown_it'"]

    monkeypatch.setattr(harness, "_render", raised)

    assert harness.main(["--only", "clean"]) == 2

    printed = capsys.readouterr().out
    assert "harness could not run" in printed, printed
    assert "markdown_it" in printed


def test_the_working_tree_side_does_not_depend_on_the_cwd(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Both sides are anchored to `__file__`, like every sibling path.

    The working-tree side was read from a cwd-relative `Path("scripts")` and git
    ran with no `cwd=`, so the harness only worked from the repository root.
    From anywhere else it raised `FileNotFoundError` out of `shutil.copyfile` --
    an uncaught traceback rather than the documented exit 2. The silent variant
    is worse: from another checkout that also has `scripts/check_memory_index.py`
    both sides come from *that* tree, and the run prints a reassuring
    "identical".
    """
    monkeypatch.chdir(tmp_path)

    side = harness.load_side("head", None, tmp_path / "staging")

    assert side.module is not None
    assert Path(side.parser.__file__ or "").name == "check_spec_links.py"
    assert Path(side.module.__file__ or "").name == "check_memory_index.py"


def test_a_baseline_predating_a_script_is_reachable_not_fatal(
    tmp_path: Path,
) -> None:
    """A script absent at the baseline is an answer, not a failure.

    `_SCRIPTS` forced both files at every revision, so `--base 3d7af0ac` --
    ADR-0061's own documented reproduction command -- aborted, because
    `check_memory_index.py` is new in that range while `check_spec_links.py` is
    not. Every baseline old enough to be interesting was therefore unreachable,
    which makes the ADR's adoption claim unreproducible by the instrument
    committed to reproduce it.
    """
    _require_reachable("3d7af0ac")
    side = harness.load_side("base", "3d7af0ac", tmp_path / "staging")

    assert side.parser is not None  # the parser predates the branch
    assert side.module is None  # ...the checker does not

    # ...and asking it to render says so rather than raising AttributeError.
    with pytest.raises(harness.HarnessError) as excinfo:
        harness._render(side, tmp_path)  # pyright: ignore[reportPrivateUsage]

    assert "check_memory_index.py" in str(excinfo.value)

    # And end to end: `main` maps this raise onto exit 2 as well. Kept here
    # rather than moved wholesale to `test_a_missing_baseline_is_exit_2_in_any_
    # clone`, because the two reach *different* raise sites. That test reaches
    # `load_side`'s "no parser to compare"; this one reaches `_render`'s
    # `side.module is None`, through `main`'s sentinel loop — the path where the
    # parser resolves and the checker does not. Removing this line when the
    # unconditional test was added left that site pinned nowhere, which both
    # reviewers caught: the remedy needed to *add* the unguarded test, not to
    # *drop* the guarded assertion. It stays skipped on a shallow clone, so the
    # coverage it restores is a full clone's, not CI's.
    assert harness.main(["--base", "3d7af0ac"]) == 2


def test_a_missing_baseline_is_exit_2_in_any_clone() -> None:
    """The exit contract, pinned somewhere CI actually runs it.

    0 identical, 1 differ, 2 could not run. Every refusal in the harness used to
    be `raise SystemExit(<str>)`, which exits **1** — the code reserved for "the
    revisions disagree" — so a harness that compared nothing reported the same
    status as a real divergence, and `main`'s own `return 2` guards sat
    downstream of raises that fired first.

    **An all-zero SHA rather than a real historical one, deliberately.** The
    sibling test above reaches `3d7af0ac` behind a reachability guard — a
    baseline far enough back that a depth-1 checkout cannot see it — and CI's
    `test` job (the only one that runs pytest) checks out at the default
    `fetch-depth: 1` on all three OS legs, with no override anywhere and no
    nightly full-history leg. So that test skips in every CI run that will ever
    gate this branch, and a test that skips on every leg is coverage only a
    contributor's full clone provides. This one depends on no history at all: an
    object no repository can contain is absent in a shallow clone and a full one
    alike, so it runs everywhere.

    No commit distance is quoted for that baseline, and the omission is the
    point: this sentence said "41 commits back" and was already wrong when it
    landed, the commit carrying it being the 42nd. A distance from a fixed
    revision to a moving `HEAD` increments on every commit, so it is a
    measurement with a shelf life of one — the failure the ADR-0061 hunk on this
    same branch names, where a sentence read "16 inputs" while the hunk rewriting
    it was adding the 17th.

    Guarding the sibling was the right fix for its own defect — unguarded, it
    *failed* on all three legs — but converting a red to a skip is not the same
    as keeping the property covered, and the branch's headline fix was the
    property in question.
    """
    absent = "0" * 40

    assert harness.main(["--base", absent]) == 2


def test_git_show_reports_both_absent_spellings_rather_than_raising(
    tmp_path: Path,
) -> None:
    """`_git_show` tolerates *both* of git's "not here" messages.

    It decides "absent at this revision, not a failure" by matching two literal
    substrings of git's stderr, and only one of them was reachable through
    `load_side` -- `_SCRIPTS` names two files that must exist on disk for the
    harness to run at all, so the other spelling had no route in and no test.
    Driven directly here, because a bare string match against another program's
    output is exactly the rule that should not go unexercised: it regresses
    silently under a translated locale or a reworded git release, turning a
    legitimate absent-at-this-revision answer into a hard `SystemExit`.

    Both spellings are asserted against real git rather than a stub, so a
    reworded message reddens this test rather than passing a stub nobody
    updated.
    """
    # "does not exist in" -- a path git has never heard of.
    missing = harness._git_show(  # pyright: ignore[reportPrivateUsage]
        "HEAD", "scripts/never_existed_xyz.py", tmp_path / "a"
    )

    assert missing is False

    # "exists on disk, but not in" -- a path that is present now and was not
    # at the branch's base. Skipped rather than failed if the base is absent,
    # so a shallow or partial clone does not redden an unrelated property.
    base = "3d7af0ac"
    _require_reachable(base)

    absent_then = harness._git_show(  # pyright: ignore[reportPrivateUsage]
        base, "scripts/check_memory_index.py", tmp_path / "b"
    )

    assert absent_then is False

    # ...and the boundary: a path that IS there returns True and writes bytes.
    target = tmp_path / "c"

    present = harness._git_show(  # pyright: ignore[reportPrivateUsage]
        "HEAD", "scripts/check_memory_index.py", target
    )

    assert present is True
    assert target.read_bytes().startswith(b"#!/usr/bin/env python3")

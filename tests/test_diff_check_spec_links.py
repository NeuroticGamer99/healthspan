"""Unit tests for the parser differential harness (scripts/diff_check_spec_links.py).

This harness backs a claim in [ADR-0061](../specs/adr/0061-markdown-link-check-gate.md):
that adopting a real CommonMark parser changed no link in this repository's own
markdown. A harness that silently compares *fewer* inputs than it says would make
that claim look measured while measuring almost nothing — and it had exactly that
defect when first written, which is what `test_repointing_reaches_the_real_corpus`
below exists to keep closed.

Loading a side shells out to `git show`, so these tests require the working
directory to be the repository (pytest's rootdir here).
"""

from __future__ import annotations

from pathlib import Path
from types import ModuleType

import diff_check_spec_links as harness
import diff_harness
import pytest
from diff_harness import HarnessError, Side, load_side

# `clean_import_state` lives in tests/conftest.py, shared with
# tests/test_diff_harness.py. This module had no copy of it, and its own loads
# -- the `side` fixture, `test_an_unrepointed_side_would_not_see_the_corpus`,
# and every `main()` call, which loads two more -- left four `_diff_*` entries
# behind, each a module object whose `__file__` names a staging directory that
# has already been deleted.
pytestmark = pytest.mark.usefixtures("clean_import_state")


@pytest.fixture(scope="module")
def side(tmp_path_factory: pytest.TempPathFactory) -> Side:
    """One side loaded from the working tree, repointed at the repository."""
    staging = tmp_path_factory.mktemp("sl-side")
    loaded = load_side("only", None, staging, harness._SCRIPTS, harness._SCRIPTS)  # pyright: ignore[reportPrivateUsage]
    return harness._repoint(loaded)  # pyright: ignore[reportPrivateUsage]


def _parser(side: Side) -> ModuleType:
    """The snapshotted `check_spec_links` this side carries.

    Asked for by name rather than read off an attribute, because that is the
    shared core's own accessor: an absent script becomes a `HarnessError`
    naming the revision, which the exit contract routes to 2.
    """
    return side.module(harness.PARSER)


# --- the defect this instrument already had -------------------------------


def test_repointing_reaches_the_real_corpus(side: Side) -> None:
    """The snapshotted parser enumerates *this* repository, not its staging dir.

    `check_spec_links` derives `REPO_ROOT` from `__file__.parent.parent` at
    import, which for a snapshotted copy is the staging directory. Without the
    repoint, `md_sources()` returns almost nothing and the harness reports
    agreement over the fixtures alone — measured: 16 inputs where it should have
    been 16 plus every markdown file in the repository.

    Both assertions are load-bearing. The count alone would pass for a walk that
    found some unrelated tree; naming a file that must be in the set is what ties
    it to this repository.
    """
    sources = _parser(side).md_sources()

    assert len(sources) > 50, len(sources)
    names = {path.name for path in sources}
    assert "CLAUDE.md" in names, sorted(names)[:20]


@pytest.mark.parametrize("name", ["REPO_ROOT", "SPECS_DIR", "PERSONAL_DIR"])
def test_a_renamed_constant_refuses_instead_of_being_silently_created(
    name: str, tmp_path_factory: pytest.TempPathFactory
) -> None:
    """Report finding 15: the rename guarantee held for one of three names.

    `_repoint` used plain attribute assignment, which creates rather than
    fails, and its docstring credited the guarantee to
    `test_repointing_reaches_the_real_corpus`. That test only ever saw
    `REPO_ROOT`: simulated per name, restoring `REPO_ROOT` to its
    staging-derived value made `md_sources()` return 0 files and reddened it,
    while `SPECS_DIR` and `PERSONAL_DIR` each left 131 files and left it green.
    Neither is read by `md_sources` at call time, so a rename of either would
    have left the harness comparing a differently-scoped corpus and printing a
    reassuring "identical" — the failure class this whole instrument exists to
    catch, inside the instrument.

    Driven by deleting the name from a loaded side, which is what a rename looks
    like from here. All three are parametrized rather than only the two that
    were unguarded: an assertion that covers the already-covered case too is
    what keeps the guard from being narrowed back to it.
    """
    staging = tmp_path_factory.mktemp("repoint-rename")
    loaded = load_side("only", None, staging, harness._SCRIPTS, harness._SCRIPTS)  # pyright: ignore[reportPrivateUsage]
    delattr(loaded.module(harness.PARSER), name)

    with pytest.raises(HarnessError) as excinfo:
        harness._repoint(loaded)  # pyright: ignore[reportPrivateUsage]

    message = str(excinfo.value)
    assert name in message, message
    # The refusal has to say *which side*, or an operator with a bad `--base`
    # cannot tell a renamed constant at the baseline from one in their tree.
    assert "only" in message, message


def test_an_unrepointed_side_would_not_see_the_corpus(
    tmp_path_factory: pytest.TempPathFactory,
) -> None:
    """The counter-case, so the test above cannot pass for the wrong reason.

    If a plain `load_side` already saw the repository, the repoint would be
    decoration and `test_repointing_reaches_the_real_corpus` would prove nothing
    about it. This pins that the two genuinely differ.
    """
    staging = tmp_path_factory.mktemp("sl-raw")
    raw = load_side("raw", None, staging, harness._SCRIPTS, harness._SCRIPTS)  # pyright: ignore[reportPrivateUsage]

    assert len(_parser(raw).md_sources()) < 5, _parser(raw).md_sources()


# --- the comparison -------------------------------------------------------


def test_targets_are_compared_with_their_line_numbers(side: Side) -> None:
    """Line numbers are part of the compared value, not decoration.

    A mask that found the same links while shifting a file's numbering is a real
    regression — every reported `file:line` would point at the wrong line — and
    comparing bare targets would call that identical.
    """
    rendered = harness._targets(side, "# H\n\n- [a](x.md)\n")  # pyright: ignore[reportPrivateUsage]

    assert rendered == ["line 3: x.md"]


def test_a_raise_is_recorded_as_behaviour(
    side: Side, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A parser that starts refusing is a behaviour change, not a crash."""

    def boom(_text: str) -> None:
        raise ValueError("bad markdown")

    monkeypatch.setattr(_parser(side), "link_targets", boom)

    assert harness._targets(side, "x") == ["RAISED ValueError: bad markdown"]  # pyright: ignore[reportPrivateUsage]


def test_a_parser_that_exits_is_recorded_as_behaviour_too(
    side: Side, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`SystemExit` is the other half of `_targets`' catch, and it was untested.

    The catch here is `(Exception, SystemExit)`, and `SystemExit` derives from
    `BaseException` — so `except Exception` alone would not see it. Nothing
    pinned that: the test above injects `ValueError`, which plain `Exception`
    already covers, and the interrupt test below is the boundary. Narrowing the
    catch to `Exception` left this whole module green, measured.

    That mattered beyond coverage. When `diff_harness._import` was widened to
    the same pair, the argument for it was *this* function — "the sibling had
    already spelled it correctly" — so an untested premise was carrying a fix
    in another file. A parser that starts refusing by exiting is exactly the
    shape the revisions this harness reaches used to have: the shared core's
    docstring records that these harnesses "once spelled every refusal
    `raise SystemExit(<str>)`".

    Recorded as a *result* rather than re-raised, which is the difference from
    `_import`: there, an old revision that cannot be imported means the harness
    cannot run (exit 2); here, a parser that refuses is a behaviour change worth
    diffing against the other side.
    """

    def exiting(_text: str) -> None:
        raise SystemExit("this revision refuses to parse")

    monkeypatch.setattr(_parser(side), "link_targets", exiting)

    assert harness._targets(side, "x") == [  # pyright: ignore[reportPrivateUsage]
        "RAISED SystemExit: this revision refuses to parse"
    ]


def test_an_interrupt_is_not_recorded_as_a_result(
    side: Side, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Ctrl-C is the operator, not a parse result."""

    def interrupt(_text: str) -> None:
        raise KeyboardInterrupt

    monkeypatch.setattr(_parser(side), "link_targets", interrupt)

    with pytest.raises(KeyboardInterrupt):
        harness._targets(side, "x")  # pyright: ignore[reportPrivateUsage]


def test_diff_reports_agreement_and_divergence(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The reporter is the shared core's, imported rather than copied.

    Asserted through *this* module's name so the import cannot quietly go away:
    a second copy of the routine that decides what an operator sees is two
    answers to one question, which is the drift this repository has already
    paid for four times over in its markdown parsers.
    """
    assert harness.render_diff("same", ["line 1: a.md"], ["line 1: a.md"]) is True
    assert capsys.readouterr().out == ""

    assert harness.render_diff("differs", ["line 1: a.md"], ["line 2: a.md"]) is False
    printed = capsys.readouterr().out
    assert "base only: line 1: a.md" in printed
    assert "head only: line 2: a.md" in printed


def test_a_multiplicity_only_divergence_prints_a_body(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Same lines, different count — the case that printed a header and nothing.

    The comparison is `==` but the report was membership, so a divergence in
    multiplicity or order alone told the operator something had changed and
    showed them nothing to choose with. Duplicated link tuples are ordinary in
    this corpus, so this is reachable rather than theoretical.
    """
    assert harness.render_diff("dup", ["a"], ["a", "a"]) is False

    printed = capsys.readouterr().out
    assert "head only: a" in printed


def test_a_multiplicity_change_survives_another_difference(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Report finding 14: the case the test above could not reach.

    The report was membership (`x not in y`) while the comparison was `==`, so
    a line that only changed *count* was reported present on both sides and
    dropped from the diff — and it dropped precisely when something else also
    differed, because the both-lists-empty branch is the only place the old
    code printed anything else. Measured: the duplicated `line 3: x.md` never
    appeared, only the b.md/c.md pair.

    Duplicated `(line, target)` tuples are ordinary in this corpus — 8 files
    carry one, 16 in total — so a masking change that drops one of a pair in a
    file that also shows any other difference was invisible to the operator
    reviewing the diff.
    """
    assert (
        harness.render_diff(
            "x",
            ["line 3: x.md", "line 3: x.md", "line 5: b.md"],
            ["line 3: x.md", "line 5: c.md"],
        )
        is False
    )

    printed = capsys.readouterr().out
    assert "base only: line 3: x.md" in printed, printed
    assert "base only: line 5: b.md" in printed, printed
    assert "head only: line 5: c.md" in printed, printed


def test_an_order_only_difference_still_prints_a_body(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Same lines, same counts, different order — what a multiset cannot see.

    `==` catches it and the counted diff comes back empty on both sides, so
    without this branch the operator is told something changed and shown
    nothing.
    """
    assert harness.render_diff("ord", ["a", "b"], ["b", "a"]) is False

    printed = capsys.readouterr().out
    assert "different order" in printed
    assert "base: ['a', 'b']" in printed


def test_the_diff_is_ordered_by_line_number_not_lexicographically(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Report finding 13: `sorted()` over the entry strings reordered the report.

    The `Counter` rewrite that recovered the dropped-duplicate case replaced
    order-preserving list comprehensions with a bare `sorted()`, and the
    elements are the strings `f"line {lineno}: {target}"` — so on any file with
    more than nine differing links the operator's list stopped tracking the file
    it describes. Measured: `line 10:` printed ahead of `line 3:`.

    Both properties are asserted, because the second is the one that costs an
    operator the most and a line-number sort is not obviously enough to give it
    back: `only_base` and `only_head` must run in *parallel* order, or a changed
    target and its counterpart can no longer be paired by eye — in the one
    routine whose job is deciding what an operator sees.

    Double-digit line numbers on purpose. Every entry under ten sorts the same
    way under both rules, so a single-digit fixture here would pass against the
    defect.
    """
    base = [f"line {n}: b{n}.md" for n in (2, 10, 3, 21)]
    head = [f"line {n}: h{n}.md" for n in (2, 10, 3, 21)]

    assert harness.render_diff("ordered", base, head) is False

    printed = capsys.readouterr().out
    base_lines = [ln for ln in printed.splitlines() if "base only:" in ln]
    head_lines = [ln for ln in printed.splitlines() if "head only:" in ln]

    assert base_lines == [
        "  base only: line 2: b2.md",
        "  base only: line 3: b3.md",
        "  base only: line 10: b10.md",
        "  base only: line 21: b21.md",
    ], printed
    # The parallel-order half: entry i of each list describes the same line.
    assert [ln.split("line ")[1].split(":")[0] for ln in base_lines] == [
        ln.split("line ")[1].split(":")[0] for ln in head_lines
    ], printed


def test_an_entry_without_a_line_prefix_still_sorts() -> None:
    """`render_diff` is shared, so the sort key may not *require* the prefix.

    A second harness over a different checker need not write `line <N>: `, and
    a key that parsed unconditionally would raise there — turning a cosmetic
    ordering fix into a crash in the routine every harness reports through.

    The contract asserted is the key's, not `sorted`'s: unprefixed entries sort
    together ahead of every numbered one, and among themselves by their own
    text. "Reversing the input gives the same output" was the first spelling
    here and is a tautology for any total key — ruff's C415 says so — so it
    tested nothing.
    """
    entries = ["zeta", "line 4: a.md", "alpha", "line 2: b.md"]

    assert sorted(entries, key=diff_harness._entry_order) == [  # pyright: ignore[reportPrivateUsage]
        "alpha",
        "zeta",
        "line 2: b.md",
        "line 4: a.md",
    ]


# --- the fixture corpus ---------------------------------------------------


def test_the_fixtures_carry_both_the_changed_shapes_and_their_boundaries() -> None:
    """A corpus of only-changed shapes is satisfied by "skip everything".

    Each shape the parser adoption changed is paired with the boundary next to
    it, and that pairing is what makes either one discriminating. Asserted by
    name so that deleting a boundary fixture fails here rather than quietly
    weakening the harness.
    """
    changed = {
        "fence-in-list-item",
        "fence-in-nested-list",
        "fence-tab-indented",
        "indented-code-block",
        "span-across-newline",
    }
    boundaries = {
        "span-across-blank-line",
        "ordinary-fence",
        "long-fence-quoting-short",
        "unbalanced-backtick",
        "plain-list",
    }

    assert changed <= set(harness.FIXTURES), changed - set(harness.FIXTURES)
    assert boundaries <= set(harness.FIXTURES), boundaries - set(harness.FIXTURES)


@pytest.mark.parametrize("name", sorted(harness.FIXTURES))
def test_every_fixture_contains_a_link(name: str) -> None:
    """A fixture with no link compares [] against [] and agrees forever.

    It would still occupy a slot in the "N inputs, N identical" line an operator
    reads as coverage — the fixture-that-cannot-fail shape, in the instrument
    rather than in the code under test.
    """
    assert "](" in harness.FIXTURES[name], harness.FIXTURES[name]


def test_fixtures_only_mode_skips_the_corpus(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """`--no-live` compares exactly the fixtures, and says how many.

    The *input count* is the invariant, not the verdict. This asserted
    `main([...]) == 0` — no divergence between HEAD and the working tree — which
    is a function of uncommitted state: `--base` defaults to HEAD and `--head`
    to the working tree, and `main` returns 1 whenever they differ. So the very
    workflow this harness exists for — edit the parser, run the harness, read
    the old-vs-new diff — turned this test red from the moment the edit existed,
    for a reason that is not a defect, leaving three ways out and all of them
    bad: stop touching the parser, commit before testing, or delete the
    assertion. Any later `main()` assertion here should pin a *refusal* path,
    whose verdict does not depend on the working tree.
    """
    exit_code = harness.main(["--no-live"])
    out = capsys.readouterr().out

    assert exit_code in (0, 1), exit_code  # 2 would mean the harness could not run
    assert f"{len(harness.FIXTURES)} inputs" in out
    assert "identical" in out


# --- external review round 4 ----------------------------------------------


def test_a_side_that_cannot_parse_aborts_rather_than_agreeing(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A shared exception is not agreement, and must not exit 0.

    `_targets` turns any raise into a comparable string, so when both sides
    raise the SAME exception the run counted it as agreement. Under the bare
    `python` this module's Usage block used to spell, that is exactly what
    happened: every fixture returned
    `RAISED ModuleNotFoundError: No module named 'markdown_it'` and the run
    printed "16 inputs, 16 identical, 0 diverged" at exit 0, having parsed
    nothing.

    The sentinel checks an expected *value*, not merely that nothing raised,
    because an empty link set is the other way a broken mask reports agreement.
    """

    def nothing(_side: Side, _text: str) -> list[str]:
        return []

    monkeypatch.setattr(harness, "_targets", nothing)

    assert harness.main(["--no-live"]) == 2

    printed = capsys.readouterr().out
    assert "harness could not run" in printed, printed
    assert "sentinel" in printed


def test_a_missing_baseline_is_exit_2_in_any_clone() -> None:
    """The `load_side` half of the exit contract, on this harness's own `main`.

    It belongs here rather than being left to the shared core's own suite:
    `load_side` is imported from `diff_harness`, so its `HarnessError` arrives
    through *this* module's `main`, which needs its own catch to turn it into
    exit 2. The enumeration tests below reach that catch from the guards a few
    lines up; this one reaches it from the import path they never touch.

    An all-zero SHA depends on no repository history, so it runs on CI's
    shallow `fetch-depth: 1` checkout rather than skipping there.
    """
    assert harness.main(["--base", "0" * 40]) == 2


def test_an_empty_live_enumeration_is_a_refusal_not_agreement(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The enumeration half of the failure whose parsing half the sentinel closes.

    `md_sources()` returning `[]` printed a full-agreement line over the
    fixtures alone, at exit 0 and byte-identical to `--no-live` — so a run that
    never touched the corpus it claims to cover was indistinguishable from one
    that did. That is precisely what `_repoint`'s own docstring says must not be
    "left to a reader to notice a suspiciously round input count", and the
    sentinel added for the parsing half does not reach it: the sentinel passes,
    because parsing works fine.
    """

    def nothing() -> list[Path]:
        return []

    real_repoint = harness._repoint  # pyright: ignore[reportPrivateUsage]

    def fake_repoint(side: Side) -> Side:
        side = real_repoint(side)
        monkeypatch.setattr(_parser(side), "md_sources", nothing, raising=False)
        return side

    monkeypatch.setattr(harness, "_repoint", fake_repoint)

    assert harness.main([]) == 2

    printed = capsys.readouterr().out
    assert "harness could not run" in printed, printed
    assert "enumerated no markdown files" in printed, printed


def test_an_unreadable_input_is_a_refusal_not_a_clean_run(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Report finding 14, second half: an incomplete run reported completeness.

    A file the harness could not read is a file it did not compare, so the run
    did not cover the corpus it claims to. Naming it in a `skipped` line was
    not enough — the summary still ended `0 diverged` and the process still
    exited 0, which is the answer a caller reading only the status acts on.
    This module reserves 2 for "could not run" precisely so an incomplete run
    is not mistaken for a clean one.
    """

    def unreadable(*_args: object, **_kwargs: object) -> str:
        raise OSError("device not ready")

    monkeypatch.setattr(Path, "read_text", unreadable)

    assert harness.main([]) == 2

    printed = capsys.readouterr().out
    assert "could not be read" in printed, printed
    assert "did not cover the whole corpus" in printed, printed


def test_a_skipped_input_alongside_a_real_divergence_exits_1(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Report finding 8: the refusal was ordered ahead of the divergence return.

    `if skipped: return refuse(...)` sat before `return 1 if diverged else 0`,
    so a run that both skipped an unreadable input *and* found real divergences
    exited 2 — contradicting this module's own contract ("1 when any differ")
    and reporting a printed behaviour change as an environment failure, which
    is the one status that invites "retry, nothing to see". Reproduced against
    the committed harness with one live corpus file made to raise `OSError`:
    it printed the counts ADR-0061 records as its reproduction and then exited
    2 as "could not run".

    The sibling test above monkeypatches `Path.read_text` **globally**, so
    every live source is skipped, only identical fixtures remain, and
    skipped-plus-diverged is never reached. This one skips exactly one file and
    leaves the rest comparable, which is the combination that was unwatched.

    The divergence is produced through `_targets` — the same seam
    `test_a_shared_exception_is_not_agreement` uses — returning a label-
    dependent answer, so the two sides genuinely disagree rather than being
    told they do. It answers the sentinel correctly, because that check runs
    first and a wrong answer there refuses at exit 2 before any of this is
    reached; the point of the test is the ordering, and short-circuiting into
    the same exit code it is trying to distinguish would make it vacuous.
    """
    real_read = Path.read_text
    skipped_one: list[str] = []

    def one_unreadable(self: Path, *args: object, **kwargs: object) -> str:
        if self.suffix == ".md" and not skipped_one:
            skipped_one.append(self.name)
            raise OSError("device not ready")
        return real_read(self, *args, **kwargs)  # pyright: ignore[reportCallIssue, reportArgumentType]

    def side_dependent(side: Side, text: str) -> list[str]:
        if text == harness._SENTINEL_TEXT:  # pyright: ignore[reportPrivateUsage]
            return list(harness._SENTINEL_EXPECTED)  # pyright: ignore[reportPrivateUsage]
        return [f"line 1: {side.label}.md"]

    monkeypatch.setattr(Path, "read_text", one_unreadable)
    monkeypatch.setattr(harness, "_targets", side_dependent)

    assert harness.main([]) == 1

    printed = capsys.readouterr().out
    assert skipped_one, "no input was skipped, so the ordering was never tested"
    # Both facts still reach the operator; only the exit code changed. The
    # incompleteness is on stdout twice — its own `skipped` line naming the
    # file and the error, and the count on the summary line — which is what
    # makes exit 1 honest here rather than a fact being dropped.
    assert "skipped (unreadable):" in printed, printed
    assert skipped_one[0] in printed, printed
    assert "1 unreadable" in printed, printed
    # Stated as "not zero" rather than as a number: the corpus size is not this
    # test's subject and a literal count here would rot on the next new file.
    assert " diverged" in printed, printed
    assert "0 diverged" not in printed, printed


def test_a_skipped_input_with_nothing_diverging_still_refuses(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The half the reordering must not break — and the reason it does not.

    Moving the divergence return ahead of the refusal is safe precisely because
    **only exit 0 claims completeness**. An incomplete run that found nothing
    must therefore still refuse rather than report a clean bill of health, and
    that is the branch the refusal now guards alone. Without this, deleting the
    refusal outright would leave the test above green.
    """
    real_read = Path.read_text
    skipped_one: list[str] = []

    def one_unreadable(self: Path, *args: object, **kwargs: object) -> str:
        if self.suffix == ".md" and not skipped_one:
            skipped_one.append(self.name)
            raise OSError("device not ready")
        return real_read(self, *args, **kwargs)  # pyright: ignore[reportCallIssue, reportArgumentType]

    monkeypatch.setattr(Path, "read_text", one_unreadable)

    assert harness.main([]) == 2

    printed = capsys.readouterr().out
    assert skipped_one, "no input was skipped, so nothing was tested"
    assert "harness could not run" in printed, printed
    assert "did not cover the whole corpus" in printed, printed


def test_an_enumeration_that_raises_is_exit_2_not_a_traceback(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """`md_sources()` sat outside every `try`, one line above a guarded call.

    Measured against a real revision whose `check_spec_links.py` has
    `link_targets` but no `md_sources`: `AttributeError` at exit 1, not the
    documented exit 2, while `_targets` one line down caught exactly that shape
    for the other call. `md_sources()` raises `RuntimeError` on a git failure
    down the same unguarded path, so both are guarded.
    """

    def boom() -> list[Path]:
        raise RuntimeError("git said no")

    real_repoint = harness._repoint  # pyright: ignore[reportPrivateUsage]

    def fake_repoint(side: Side) -> Side:
        side = real_repoint(side)
        monkeypatch.setattr(_parser(side), "md_sources", boom, raising=False)
        return side

    monkeypatch.setattr(harness, "_repoint", fake_repoint)

    assert harness.main([]) == 2

    printed = capsys.readouterr().out
    assert "harness could not run" in printed, printed
    assert "could not enumerate" in printed, printed


# --- the cut list this harness's own review round dropped ------------------


def test_a_side_that_cannot_be_imported_is_exit_2_not_a_traceback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The `_import` half of the exit contract, driven through this `main`.

    `diff_harness._import` executed a snapshot's module body unguarded, so an
    old revision that does not import here — a dependency this environment no
    longer installs, a syntax the running interpreter dropped — reached the
    operator as a traceback at exit **1**, which is the code reserved for "the
    revisions disagree". The `HarnessError` it now raises has to travel the
    whole way out through `load_side` and this module's `main` to become 2, and
    only a test at this level shows that it does.

    The working-tree side is the one broken, by repointing `SCRIPTS_DIR` at a
    directory holding an unimportable copy: that is the real copy path
    `load_side` takes for `rev is None`, so nothing about the import is stubbed.
    """
    broken_scripts = tmp_path / "scripts"
    broken_scripts.mkdir()
    (broken_scripts / harness.PARSER).write_text(
        "raise ModuleNotFoundError(\"No module named 'markdown_it'\")\n"
    )
    monkeypatch.setattr(diff_harness, "SCRIPTS_DIR", broken_scripts)

    assert harness.main([]) == 2

    printed = capsys.readouterr().out
    assert "harness could not run" in printed, printed
    assert "cannot import" in printed, printed
    assert "markdown_it" in printed, printed


def test_an_absent_parser_refuses_rather_than_reading_as_a_divergence() -> None:
    """Cut item 2: `_targets`' broad catch swallowed the refusal it must not.

    `Side.module` raises `HarnessError` for a script absent at this revision,
    which the exit contract routes to 2. Caught by `_targets` it became the
    ordinary result string `RAISED HarnessError: ...`, which differs from
    whatever the other side returned — so the run reported a *divergence* and
    exited 1, the code reserved for "the revisions disagree", produced by a side
    that was never there to disagree.

    The pairing partner is already above: `test_a_raise_is_recorded_as_behaviour`
    holds the other half — a raise from `link_targets` itself must still come
    back as a comparable string — so a fix that hoisted the whole *call* out of
    the catch passes this test and reddens that one.
    """
    absent = Side(label="base", rev="HEAD", home=Path("."), modules={})

    with pytest.raises(HarnessError) as excinfo:
        harness._targets(absent, "[a](x.md)\n")  # pyright: ignore[reportPrivateUsage]

    assert "does not exist at HEAD" in str(excinfo.value)

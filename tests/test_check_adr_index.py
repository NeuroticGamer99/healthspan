"""Unit tests for the ADR-index consistency gate (scripts/check_adr_index.py).

The gate mechanizes CLAUDE.md's rule that `specs/adr/README.md`'s index must
match the files and their `## Status` fields. It compares a file's status to
the index cell after stripping markdown link markup, so `strip_links` is the
one place a malformed strip turns "these agree" into a blocking failure on
correct markdown — the direction a gate cannot afford, because the remedy an
operator reaches for is editing a document that was already right.
"""

from __future__ import annotations

from pathlib import Path

import check_adr_index as gate
import pytest


def test_a_plain_link_is_reduced_to_its_text() -> None:
    """The case the gate exists for: a status that cites the superseding ADR."""
    assert (
        gate.strip_links("Superseded by [ADR-0023](0023-x.md)")
        == "Superseded by ADR-0023"
    )


def test_unlinked_text_survives_unchanged() -> None:
    """The boundary beside it, so the test above cannot pass for stripping all.

    Most statuses carry no link at all, and a pattern that ate text without one
    would fail every row while the test above still passed.
    """
    assert gate.strip_links("  Accepted  ") == "Accepted"


def test_a_nested_bracket_link_does_not_leave_half_of_itself_behind() -> None:
    """A `[^\\]]+` text run stops at the first `]`, which is the inner one.

    For a badge link the flat pattern consumed `[![alt](img.png)` and left
    `](0070-x.md)`, producing `![status](0070-x.md)`: an alt text glued to the
    outer destination, a string neither side of the comparison can produce — so
    the file and the index would be reported as disagreeing when they agree.
    The balanced form matches `check_spec_links.LINK_RE`, which was hardened
    for the same shape.

    Both halves are asserted. The equality alone would pass for a pattern that
    stripped everything; the residue check is what pins that no fragment of the
    outer link is left in the value the gate then compares.
    """
    stripped = gate.strip_links("Superseded by [![badge](img.png)](0070-x.md)")

    assert stripped == "Superseded by ![badge](img.png)"
    assert "0070-x.md" not in stripped


def _tiny_repo(tmp_path: Path, file_status: str, index_status: str) -> None:
    """A `specs/adr/` holding one ADR and a one-row index, both status-settable."""
    adr_dir = tmp_path / "specs" / "adr"
    # `exist_ok` so one test can rewrite the same tree with a different pair
    # and re-run the gate; the two files are overwritten, not appended to.
    adr_dir.mkdir(parents=True, exist_ok=True)
    (adr_dir / "0070-x.md").write_text(
        f"# ADR-0070\n\n## Status\n{file_status}\n", encoding="utf-8"
    )
    (adr_dir / "README.md").write_text(
        "# ADRs\n\n## Index\n\n"
        "| ADR | Title | Status |\n|---|---|---|\n"
        f"| [ADR-0070](0070-x.md) | X | {index_status} |\n",
        encoding="utf-8",
    )


@pytest.mark.parametrize(
    ("file_status", "index_status", "agree"),
    [
        # The badge nesting the `LINK_RE` hardening was written for, spelled the
        # same way on both sides. This is the case the 17-line comment claims to
        # have repaired and did not.
        (
            "Superseded by [![badge](img.png)](0070-x.md)",
            "Superseded by [![badge](img.png)](0070-x.md)",
            True,
        ),
        # A linked cell against a plain file value, and the reverse. Either
        # direction was a blocking failure while only one side was normalized.
        ("Superseded by ADR-0070", "Superseded by [ADR-0070](0070-x.md)", True),
        ("Superseded by [ADR-0070](0070-x.md)", "Superseded by ADR-0070", True),
        ("Accepted", "Accepted", True),
        # The discriminating rows: normalizing both sides must not make
        # everything agree. A gate that stripped its way to equality would pass
        # every row above and police nothing.
        ("Accepted", "Proposed", False),
        (
            "Superseded by [![badge](img.png)](0070-x.md)",
            "Superseded by ADR-0070",
            False,
        ),
    ],
)
def test_the_two_sides_are_normalized_the_same_way(
    file_status: str,
    index_status: str,
    agree: bool,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Report finding 11: `strip_links` reached only one side of the comparison.

    `actual = file_status(path)` was stripped while the index cell was compared
    raw as `index_status.strip()`, so hardening `LINK_RE` for the badge nesting
    changed *which* residue survived rather than whether the two could match.
    Measured: the balanced form leaves `Superseded by ![badge](img.png)` and the
    flat form left `Superseded by ![badge](0070-x.md)`; neither equals the plain
    index cell nor the raw badge cell, so the first row below — the very shape
    the pattern change cites — still failed the gate on both patterns.

    Asserted through `main()` rather than on `strip_links`' return value, which
    is the other half of the finding: the existing unit test pins the *residue*
    and would pass unchanged with the comparison still one-sided. Agreement is
    the property the gate exists to decide, so agreement is what is asserted.
    """
    _tiny_repo(tmp_path, file_status, index_status)
    monkeypatch.setattr(gate, "ADR_DIR", tmp_path / "specs" / "adr")
    monkeypatch.setattr(gate, "INDEX", tmp_path / "specs" / "adr" / "README.md")

    assert (gate.main() == 0) is agree


def test_the_gate_agrees_with_the_repository_it_ships_with() -> None:
    """End to end over the real `specs/adr/`, which is what CI runs.

    `strip_links` is reachable only through the whole check, and a pattern
    change that satisfied every unit above while breaking an actual row would
    show up here and nowhere else. Exit 0 is the gate's own contract.
    """
    assert gate.main() == 0


def test_a_supersession_link_must_point_at_the_adr_it_names() -> None:
    """The coverage `strip_links` gives up, restored beside it.

    `strip_links` substitutes the link *text*, so once both sides are stripped
    **any** destination compares equal: a tree whose `0022-old.md` declares
    `Superseded by ADR-0023` against an index cell reading
    `Superseded by [ADR-0023](0099-completely-wrong.md)` reconciles at exit 0.
    `check_spec_links` catches a destination that does not exist, never one
    that exists and is wrong — and a wrong one sends a reader following a
    supersession to the wrong ADR, which is the navigation this gate's own
    subject exists to keep honest.

    Not the `## Links` section, and the distinction is worth keeping straight:
    CLAUDE.md's ADR-governance rules 2 and 4 place their navigation link there,
    and nothing here reads it. This checks the two *status* values only.

    Honest about the trade: the pre-symmetry code *did* reject that pair, and
    rejected the correct `[ADR-0023](0023-new.md)` identically, so it
    validated no destination at all.
    """
    wrong = gate.link_target_errors(
        "0022-old.md",
        "Superseded by [ADR-0023](0099-completely-wrong.md)",
        "index cell",
    )
    assert len(wrong) == 1, wrong
    assert "ADR-0023" in wrong[0], wrong
    assert "0099-completely-wrong.md" in wrong[0], wrong

    # The hyphen in `f"{number}-"` is the whole boundary, and every wrong
    # destination above differs in the leading digits themselves — so all of
    # them survive a comparison that dropped the hyphen. `00230-other.md`
    # belongs to ADR-0230 and starts with `0023`, which is the one shape that
    # tells `startswith(f"{number}-")` and `startswith(number)` apart.
    boundary = gate.link_target_errors(
        "0022-old.md", "Superseded by [ADR-0023](00230-other.md)", "index cell"
    )
    assert len(boundary) == 1, boundary
    assert "00230-other.md" in boundary[0], boundary

    # The correct pair, an anchored spelling of it, a subdirectory spelling
    # (scope: the number against the filename, not that the path resolves —
    # `check_spec_links` owns that), and a status with no link at all must all
    # stay silent. That is the false positive the symmetric comparison removed
    # and this must not reintroduce.
    for benign in (
        "Superseded by [ADR-0023](0023-new.md)",
        "Superseded by [ADR-0023](0023-new.md#status)",
        "Extended by [ADR-0023](../adr/0023-new.md)",
        # The backslash arm of the separator class, which nothing else reaches.
        # Whether a destination spelled this way is a *good* link is
        # `check_spec_links`' question and it reports one loudly; this check's
        # only claim is that the final segment is the right ADR's file, and it
        # has to find that segment under either separator to say so.
        r"Extended by [ADR-0023](..\adr\0023-new.md)",
        # Names no file at all, so there is no filename to disagree with. This
        # is the one case the `#`-split is load-bearing for: without it the
        # stem reads `#status`, which starts with no ADR number, and a link
        # into the index's own page is reported as pointing at the wrong file.
        "Superseded by [ADR-0023](#adr-0023)",
        "Accepted",
    ):
        assert gate.link_target_errors("0022-old.md", benign, "index cell") == [], (
            benign
        )


def test_a_wrong_destination_fails_the_gate_even_when_the_status_text_agrees(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Through `main()`, because the call site is half the finding.

    `link_target_errors` returning the right list is worthless if `main()`
    never calls it, or calls it only on the branch where the status text
    already disagreed — the shape that matters is precisely the one where the
    two sides *do* agree, since stripping the links is what makes them agree.
    """
    _tiny_repo(
        tmp_path,
        "Superseded by ADR-0070",
        "Superseded by [ADR-0070](0099-completely-wrong.md)",
    )
    monkeypatch.setattr(gate, "ADR_DIR", tmp_path / "specs" / "adr")
    monkeypatch.setattr(gate, "INDEX", tmp_path / "specs" / "adr" / "README.md")

    assert gate.main() == 1

    # And the same tree with the destination corrected reconciles, so the
    # assertion above cannot be passing because the fixture is broken some
    # other way.
    _tiny_repo(
        tmp_path,
        "Superseded by ADR-0070",
        "Superseded by [ADR-0070](0070-x.md)",
    )
    assert gate.main() == 0


@pytest.mark.parametrize(
    ("file_status", "branch"),
    [
        ("Superseded by ADR-0070", "the status text agrees after stripping"),
        ("Proposed", "the status text disagrees"),
        (None, "the file has no readable '## Status' value"),
    ],
)
def test_the_destination_check_runs_on_every_status_branch(
    file_status: str | None,
    branch: str,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The property the call-site comment claims, held rather than asserted.

    `main()` runs `link_target_errors` outside the `actual is None` branch and
    after the status comparison, on purpose: the destination is a separate
    claim from the status text, so a cell can carry the right words and the
    wrong link — or the wrong words and the wrong link, or no readable status
    at all. Only the first of those three was covered, which is the branch the
    check was *written* for and therefore the one a regression would keep.

    **Asserted on stdout, not on the exit code**, and that is the whole design
    of this test. Two of the three rows fail the gate for a reason of their own
    — a status mismatch, a missing `## Status` — so `main() == 1` holds in them
    whether or not the destination was ever examined. An exit-code assertion
    would pass against a call site moved back inside the agreeing branch, which
    is exactly the regression this exists to catch.
    """
    # The third row's status is overwritten below; `_tiny_repo` needs *some*
    # value to build a well-formed file first.
    _tiny_repo(
        tmp_path,
        file_status or "Proposed",
        "Superseded by [ADR-0070](0099-completely-wrong.md)",
    )
    adr_dir = tmp_path / "specs" / "adr"
    if file_status is None:
        (adr_dir / "0070-x.md").write_text(
            "# ADR-0070\n\nThis file has no status heading at all.\n",
            encoding="utf-8",
        )
    monkeypatch.setattr(gate, "ADR_DIR", adr_dir)
    monkeypatch.setattr(gate, "INDEX", adr_dir / "README.md")

    assert gate.main() == 1, branch
    reported = capsys.readouterr().out
    assert "is not that ADR's file" in reported, branch
    # The destination too, and not as belt-and-braces. Asserting the suffix
    # alone, this test passed **in isolation** against a `link_target_errors`
    # whose message dropped `{number}` and `{dest}` for hardcoded text: it
    # could not tell "the check ran and identified this cell's link" from "the
    # check ran and printed a generic string". The full-suite run still caught
    # that mutant, but only through a sibling test asserting a different
    # property, which is the sibling's coverage and not this one's.
    assert "0099-completely-wrong.md" in reported, branch


def test_the_file_s_own_status_link_is_checked_and_named_as_the_file_s(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The side this repository's only real supersession actually uses.

    `specs/adr/README.md`'s row for ADR-0001 spells its status as plain text
    (`Accepted (partially superseded by ADR-0023)`) while
    `0001-mcp-server-language.md`'s own `## Status` carries the link. Measured
    over the corpus: that is the *only* `## Status` holding an
    `[ADR-NNNN](dest)` at all, and no index cell holds one. So a destination
    check reading the index alone is a check that cannot fire on the single
    live instance of the shape it exists to catch — which is what `file_status`
    returning `strip_links(...)` made unavoidable, by destroying the
    destination before any caller could look at it.

    The label is asserted, not just the error: with `source` defaulted rather
    than required, a file-side defect would be reported against the index cell
    and an operator would go and edit a document that was already right.
    """
    adr_dir = tmp_path / "specs" / "adr"
    adr_dir.mkdir(parents=True)
    (adr_dir / "0070-x.md").write_text(
        "# ADR-0070\n\n## Status\nSuperseded by [ADR-0071](0099-completely-wrong.md)\n",
        encoding="utf-8",
    )
    (adr_dir / "README.md").write_text(
        "# ADRs\n\n## Index\n\n"
        "| ADR | Title | Status |\n|---|---|---|\n"
        "| [ADR-0070](0070-x.md) | X | Superseded by ADR-0071 |\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(gate, "ADR_DIR", adr_dir)
    monkeypatch.setattr(gate, "INDEX", adr_dir / "README.md")

    # The status texts agree once stripped, so nothing but the destination
    # check can fail this tree — the exit code is discriminating here.
    assert gate.main() == 1
    reported = capsys.readouterr().out
    assert "0099-completely-wrong.md" in reported
    assert "its '## Status'" in reported, reported
    assert "index cell" not in reported, reported

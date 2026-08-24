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
    """An `specs/adr/` holding one ADR and a one-row index, both status-settable."""
    adr_dir = tmp_path / "specs" / "adr"
    adr_dir.mkdir(parents=True)
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

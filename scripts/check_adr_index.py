"""Verify that specs/adr/README.md's index matches the ADR files on disk.

Mechanizes the ADR-governance rule (CLAUDE.md): the index must always match
the actual files and their `## Status` fields. Checked in CI by the
docs-consistency gate (see .github/workflows/ci.yml and ADR-0045).

Checks, per index row:
  - the linked file exists
  - the ADR number in the link text matches the filename prefix
  - the index Status cell equals the file's `## Status` value
    (markdown links are stripped before comparison, so
    "Superseded by [ADR-0023](...)" matches "Superseded by ADR-0023")
  - an `[ADR-NNNN](dest)` in *either* status value -- the index cell or
    the file's own `## Status` -- points at ADR-NNNN's own file, which
    stripping the links is precisely what stops the check above from
    seeing. Not the `Superseded by:` / `Extended by:` links in an ADR's
    `## Links` section; those are a different artifact and unchecked.

And globally:
  - every NNNN-*.md file in specs/adr/ (except the 0000 template) has
    exactly one index row

Exit code 0 when consistent; 1 with one line per discrepancy otherwise.
Stdlib only; all files are read as UTF-8.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

ADR_DIR = Path(__file__).resolve().parent.parent / "specs" / "adr"
INDEX = ADR_DIR / "README.md"
TEMPLATE = "0000-template.md"

ROW_RE = re.compile(r"^\| \[ADR-(\d{4})\]\(([^)]+)\) \| (.+?) \| (.+?) \|\s*$")
# One bracket level deep, matching `check_spec_links.LINK_RE` -- deliberately
# the same shape rather than a second, flatter answer to "what is a markdown
# link". A `[^\]]+` text run stops at the first `]`, which for a badge link
# (`[![alt](img.png)](0070-x.md)`) is the *inner* image's, so the substitution
# consumed `[![alt](img.png)` and left `](0070-x.md)` behind: measured, the
# flat form turned that into `![status](0070-x.md)` -- an alt text glued to the
# outer destination, a string neither side of the comparison could produce, so
# the status cell and the file would be reported as disagreeing when they do
# not. That is a blocking gate failing on correct markdown, which is the same
# direction as the defect the sibling pattern was hardened for.
#
# Measured over this repository at the revision that changed it: **0**
# divergence between the two patterns across every ADR `## Status` value and
# every index row cell -- no ADR carries a nested-bracket status today. The
# change buys agreement between the repository's two link readers, not a fix
# to a live corpus failure.
LINK_RE = re.compile(r"\[((?:[^\[\]]|\[[^\[\]]*\])*)\]\([^)]+\)")
# An `[ADR-NNNN](dest)` navigation link, with the number and the destination
# kept apart so the two can be compared. `strip_links` throws the destination
# away by design -- it substitutes the link *text* -- which is right for the
# status comparison and leaves the destination checked by nothing at all.
ADR_LINK_RE = re.compile(r"\[ADR-(\d{4})\]\(([^)]+)\)")


def strip_links(text: str) -> str:
    """A status value with its markdown link markup removed.

    "Superseded by [ADR-0023](0023-x.md)" has to compare equal to the index's
    "Superseded by ADR-0023", which is what this is for.
    """
    return LINK_RE.sub(r"\1", text).strip()


def link_target_errors(filename: str, cell: str, source: str) -> list[str]:
    """Where an `[ADR-NNNN](dest)` in a status value points somewhere else.

    `source` names which of the two status values this one is, and is required
    rather than defaulted: both callers below pass a different label, and a
    default would let the wrong one through silently -- an error line naming
    the index cell for a defect in the file, which is the class of wrongness
    this repository ranks worst because nothing about it looks wrong.

    `check_spec_links` catches a destination that does not exist; nothing
    caught one that exists and is **wrong**. Built a synthetic tree where
    `0022-old.md` declares `Superseded by ADR-0023` and the index cell read
    `Superseded by [ADR-0023](0099-completely-wrong.md)`: the gate printed
    "ADR index consistent: 3 entries match" at exit 0, because `strip_links`
    reduces both spellings to `Superseded by ADR-0023`.

    Be precise about which rule this serves, because the obvious answer is
    wrong. CLAUDE.md's ADR-governance rules 2 and 4 put their navigation link
    in the ADR's own `## Links` section -- 60 `Extended by:` links across 29
    files, measured -- and none of those is read from here. What this checks is
    the link some status values carry, which rule 2 reaches only through
    "correcting the `## Status` field", and which rule 6's "keep the index
    current" reaches on the index side.

    One such link exists today: ADR-0001's `## Status`. It is correct, and it
    is why this now reads both status values rather than the index alone.

    Honest about what was given up to get here: before the comparison was made
    symmetric, the gate *did* reject that pair -- and rejected the correct
    `[ADR-0023](0023-new.md)` identically, because it was rejecting every link
    in the cell rather than validating any destination. Stripping both sides
    removed a real false positive; this restores the coverage that went with
    it, rather than leaving the trade unrecorded.

    Scope: the number against the filename, which is the claim the link makes.
    Whether the path resolves at all is `check_spec_links`' question and is
    deliberately not re-asked here, so a destination naming the right file
    under a wrong directory passes this check and fails that one. It is also
    only the *status* values -- the `Superseded by:` / `Extended by:` links in
    an ADR's own `## Links` section are a different artifact and are not read
    from here.
    """
    errors: list[str] = []
    for number, dest in ADR_LINK_RE.findall(cell):
        stem = re.split(r"[\\/]", dest.split("#", 1)[0])[-1]
        if stem and not stem.startswith(f"{number}-"):
            errors.append(
                f"{filename}: {source} links ADR-{number} to {dest!r}, "
                f"which is not that ADR's file"
            )
    return errors


def file_status(path: Path) -> str | None:
    """The first non-blank line under `## Status`, **with its links intact**.

    It used to return `strip_links(candidate)`, and that threw away the one
    thing the destination check needs. Measured over this repository: exactly
    one ADR carries an `[ADR-NNNN](dest)` in its `## Status` -- ADR-0001's
    `Accepted (partially superseded by [ADR-0023](0023-distribution-mechanism.md))`
    -- and its *index* row spells the same fact as plain text. So the file is
    where this repository's only supersession to date actually writes the link,
    and stripping here is what made that destination unreachable by any check.
    `main()` strips for the comparison; nothing else calls this.
    """
    lines = path.read_text(encoding="utf-8").splitlines()
    for i, line in enumerate(lines):
        if line.strip() == "## Status":
            for candidate in lines[i + 1 :]:
                if candidate.strip():
                    return candidate.strip()
            return None
    return None


def main() -> int:
    errors: list[str] = []
    indexed_files: dict[str, str] = {}

    for line in INDEX.read_text(encoding="utf-8").splitlines():
        row = ROW_RE.match(line)
        if not row:
            continue
        number, filename, _title, index_status = row.groups()

        if filename in indexed_files:
            errors.append(f"index lists {filename} more than once")
            continue
        indexed_files[filename] = index_status

        if not filename.startswith(f"{number}-"):
            errors.append(
                f"index row ADR-{number} links to {filename}, "
                f"whose name does not start with {number}-"
            )

        path = ADR_DIR / filename
        if not path.is_file():
            errors.append(f"index row ADR-{number} links to missing file {filename}")
            continue

        raw_status = file_status(path)
        actual = None if raw_status is None else strip_links(raw_status)
        # `strip_links` on **both** sides, which is what makes this an
        # agreement test rather than a comparison of two different
        # normalizations. The file's value was stripped and the index cell was
        # compared raw, so hardening `LINK_RE` for the badge nesting changed
        # *which* residue survived and not whether the two could ever match:
        # measured, a file status of `Superseded by [![badge](img.png)](0070-x.md)`
        # stripped to `Superseded by ![badge](img.png)` under the balanced form
        # and to `Superseded by ![badge](0070-x.md)` under the flat one, and
        # neither equals the plain index cell `Superseded by ADR-0070` nor the
        # raw badge cell. So the blocking failure the pattern change was written
        # to remove -- "the status cell and the file reported as disagreeing
        # when they do not" -- survived it untouched, on either pattern. One
        # side normalized is not normalization.
        indexed = strip_links(index_status)
        if actual is None:
            errors.append(f"{filename} has no readable '## Status' value")
        elif actual != indexed:
            errors.append(
                f"{filename}: index says status '{indexed}' "
                f"but the file says '{actual}'"
            )
        # Runs whether or not the status text matched, and outside the
        # `actual is None` branch too: the destination is a separate claim
        # from the status, and a cell can carry the right words and the wrong
        # link. Stripping both sides is what makes that possible -- the two
        # spellings agree on the text the comparison above sees.
        #
        # `test_the_destination_check_runs_on_every_status_branch` holds this,
        # and holds it on **stdout** rather than on the exit code: two of the
        # three status branches already fail for a reason of their own, so an
        # exit-code assertion passes with this line moved back inside the
        # agreeing branch -- which is the regression the comment disclaims.
        #
        # Both status values are asked, not just the index's. The index cell is
        # the shape the check was ported for; the *file* is where the only
        # supersession this repository has actually written puts its link
        # (ADR-0001, see `file_status`), so checking the index alone would have
        # been a check that could not fire on the one live instance of the
        # thing it exists to catch.
        errors.extend(link_target_errors(filename, index_status, "index cell"))
        if raw_status is not None:
            errors.extend(link_target_errors(filename, raw_status, "its '## Status'"))

    on_disk = {
        p.name for p in ADR_DIR.glob("[0-9][0-9][0-9][0-9]-*.md") if p.name != TEMPLATE
    }
    for missing in sorted(on_disk - set(indexed_files)):
        errors.append(f"{missing} exists but has no index row")
    for phantom in sorted(set(indexed_files) - on_disk):
        # Missing-file errors are already reported per-row above; this
        # catches rows whose filename doesn't match the NNNN-*.md pattern.
        if not (ADR_DIR / phantom).is_file():
            continue
        errors.append(
            f"index row for {phantom} does not match the NNNN-*.md convention"
        )

    if errors:
        print(f"ADR index inconsistent ({len(errors)} problem(s)):")
        for e in errors:
            print(f"  - {e}")
        return 1

    print(f"ADR index consistent: {len(indexed_files)} entries match {INDEX}.")
    return 0


if __name__ == "__main__":
    sys.exit(main())

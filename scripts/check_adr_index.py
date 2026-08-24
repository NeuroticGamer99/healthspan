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


def strip_links(text: str) -> str:
    """A status value with its markdown link markup removed.

    "Superseded by [ADR-0023](0023-x.md)" has to compare equal to the index's
    "Superseded by ADR-0023", which is what this is for.
    """
    return LINK_RE.sub(r"\1", text).strip()


def file_status(path: Path) -> str | None:
    lines = path.read_text(encoding="utf-8").splitlines()
    for i, line in enumerate(lines):
        if line.strip() == "## Status":
            for candidate in lines[i + 1 :]:
                if candidate.strip():
                    return strip_links(candidate)
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

        actual = file_status(path)
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

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
LINK_RE = re.compile(r"\[([^\]]+)\]\([^)]+\)")


def strip_links(text: str) -> str:
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
        if actual is None:
            errors.append(f"{filename} has no readable '## Status' value")
        elif actual != index_status.strip():
            errors.append(
                f"{filename}: index says status '{index_status.strip()}' "
                f"but the file says '{actual}'"
            )

    on_disk = {
        p.name
        for p in ADR_DIR.glob("[0-9][0-9][0-9][0-9]-*.md")
        if p.name != TEMPLATE
    }
    for missing in sorted(on_disk - set(indexed_files)):
        errors.append(f"{missing} exists but has no index row")
    for phantom in sorted(set(indexed_files) - on_disk):
        # Missing-file errors are already reported per-row above; this
        # catches rows whose filename doesn't match the NNNN-*.md pattern.
        if not (ADR_DIR / phantom).is_file():
            continue
        errors.append(f"index row for {phantom} does not match the NNNN-*.md convention")

    if errors:
        print(f"ADR index inconsistent ({len(errors)} problem(s)):")
        for e in errors:
            print(f"  - {e}")
        return 1

    print(f"ADR index consistent: {len(indexed_files)} entries match {INDEX}.")
    return 0


if __name__ == "__main__":
    sys.exit(main())

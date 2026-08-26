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

import posixpath
import re
import sys
from collections.abc import Mapping
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
# A fixed prefix the destination and the owning filename are both normalized
# under, so `../adr/x.md` and `x.md` -- one file spelled two ways -- compare
# equal while `../reviews/x.md` does not. Its value is arbitrary and cancels;
# what it must be is non-empty, so a leading `..` has a segment to consume.
_ANCHOR = "adr/"


def _under_anchor(path: str) -> str:
    """A status-link destination normalized under `_ANCHOR`, for comparison.

    `posixpath.join`, **not** string concatenation. Concatenating was the first
    spelling and a reviewer measured what it cost: `_ANCHOR + "/0023-new.md"`
    is `"adr//0023-new.md"`, whose doubled slash `normpath` collapses, so a
    **root-absolute** destination compared equal to the relative one and was
    silently accepted. `join` gives absolute paths their real meaning -- they
    win, and therefore mismatch -- which is the correct answer here, because
    `/0023-new.md` does not resolve to `specs/adr/0023-new.md` under any
    reading. It is also the shape `check_spec_links` skips as out of scope
    (ADR-0061 §3), so nothing else would ever have looked at it.
    """
    return posixpath.normpath(posixpath.join(_ANCHOR, path))


def strip_links(text: str) -> str:
    """A status value with its markdown link markup removed.

    "Superseded by [ADR-0023](0023-x.md)" has to compare equal to the index's
    "Superseded by ADR-0023", which is what this is for.
    """
    return LINK_RE.sub(r"\1", text).strip()


def adr_files_by_number() -> dict[str, str]:
    """Every ADR number on disk mapped to the one filename that carries it.

    Built once per run and handed to `link_target_errors`, rather than globbed
    inside it: the destination check needs the *exact* file a number owns, and
    a function that reads the filesystem itself is one whose tests either touch
    the real corpus or monkeypatch a global to avoid it.

    **Never use this for a completeness question.** Being keyed by number, it
    collapses any two files sharing a four-digit prefix to one, so it cannot
    answer "which files exist". `main()` did exactly that for one commit and a
    reviewer measured both faces of it: an orphan file going unreported, and a
    correctly-named file reported as violating the naming convention. Anything
    asking what is on disk asks the glob.
    """
    return {
        p.name[:4]: p.name
        for p in ADR_DIR.glob("[0-9][0-9][0-9][0-9]-*.md")
        if p.name != TEMPLATE
    }


def link_target_errors(
    filename: str, cell: str, source: str, owners: Mapping[str, str]
) -> list[str]:
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
    in the ADR's own `## Links` section, which this repository uses throughout,
    and none of those is read from here. (No count, deliberately: this one grew
    by one on the very branch that first wrote it down.) What this checks is
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

    **The comparison is against the exact filename that number owns**, and it
    used to be against the `NNNN-` prefix. Copilot found what the prefix let
    through, on PR #103: `[ADR-0023](../reviews/0023-notes.md)` names a file
    whose basename starts with `0023-` while ADR-0023 is
    `0023-distribution-mechanism.md`, so this check passed it and
    `check_spec_links` passed it too because the decoy exists. Two green gates
    on a link pointing at the wrong document. The prefix was chosen on the
    stated grounds that "`check_spec_links` owns whether the path resolves" --
    true, and it does not own *which* file, which is the half that matters
    here. A boundary resting on a sibling's coverage is worth only as much as
    that coverage, and this one was never checked against it.

    A destination naming no file at all -- `[ADR-0023](#adr-0023)` -- is now
    reported rather than skipped, for the same reason: `check_spec_links`
    skips pure anchors by design (ADR-0061 §3 records anchor validity as out
    of scope), so nothing else would ever look at it.

    **The whole destination is compared, not its basename**, and the
    comparison is purely lexical. CodeRabbit found what basenames still let
    through, on the same PR: `[ADR-0023](../reviews/0023-distribution-mechanism.md)`
    carries the right filename in the wrong directory, so it passed here and
    would pass `check_spec_links` too if such a file existed. That residue was
    documented here and then *contradicted* by ADR-0078, which claimed green
    certifies a status link "names its own ADR's file" without qualification --
    a gate whose owning record claims more than it delivers, which is the
    failure ADR-0078 exists to govern.

    `posixpath.normpath` under a fixed anchor rather than `Path.resolve()`:
    both status values live in `specs/adr/`, so a lexical comparison under a
    shared prefix answers exactly this question, while `resolve()` would touch
    the filesystem and reopen the `../../..`-escaping-`REPO_ROOT` question this
    repository already has open. The anchor exists so `../adr/x.md` and `x.md`
    normalize together; a `..` that escapes it simply yields a different string
    and mismatches, which is the right answer.

    Scope: this is only the *status* values -- the
    `Superseded by:` / `Extended by:` links in an ADR's own `## Links` section
    are a different artifact and are not read from here.
    """
    errors: list[str] = []
    for number, dest in ADR_LINK_RE.findall(cell):
        target = dest.split("#", 1)[0].replace("\\", "/")
        stem = target.rsplit("/", 1)[-1]
        owner = owners.get(number)
        if not stem:
            errors.append(
                f"{filename}: {source} links ADR-{number} to {dest!r}, "
                f"which names no file"
            )
        elif owner is None:
            errors.append(
                f"{filename}: {source} links ADR-{number} to {dest!r}, "
                f"but no {number}-*.md exists"
            )
        elif _under_anchor(target) != _under_anchor(owner):
            errors.append(
                f"{filename}: {source} links ADR-{number} to {dest!r}, "
                f"which is not that ADR's file ({owner})"
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
    # Resolved once, before the loop: a link in row N's status may name any
    # other ADR, so the check needs the whole mapping rather than this row's.
    owners = adr_files_by_number()

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
        errors.extend(link_target_errors(filename, index_status, "index cell", owners))
        if raw_status is not None:
            errors.extend(
                link_target_errors(filename, raw_status, "its '## Status'", owners)
            )

    # From the filesystem, **not** from `owners.values()`. That refactor was a
    # measured regression: `owners` is keyed by number, so two files sharing a
    # four-digit prefix collapse to one and whichever loses the collision is
    # misreported -- either its orphan status goes unseen, or, depending on
    # glob order, a correctly-named file is reported as "does not match the
    # NNNN-*.md convention". Both measured. The second is a blocking gate
    # failing on correct input, which is the direction this gate cannot
    # afford, and the completeness guarantee in this module's docstring is the
    # thing being broken. Completeness questions ask the filesystem.
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

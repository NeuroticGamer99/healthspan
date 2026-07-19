#!/usr/bin/env python3
r"""Verify that every relative markdown link under specs/ resolves to a file.

Mechanizes the docs-integrity half of the docs-consistency CI gate
([ADR-0045](../specs/adr/0045-repository-workflow-and-ci-enforcement.md) §6,
extended by ADR-0061): a cross-file link in specs/ that points at a moved or
deleted target is a dead link, and inside an immutable Accepted ADR even the
corrective edit carries governance ceremony. The ADR-index check
(check_adr_index.py) guards the index *table*; this guards the prose *links*.

Scope and rules:
  - Crawls every ``*.md`` under specs/, EXCEPT specs/personal/ (gitignored,
    absent in CI). A link *target* is validated wherever it resolves -- a
    specs/ file linking ``../../scripts/foo.py`` is checked too.
  - A link is inline ``[text](target)``; an image's ``![alt](target)`` target is
    validated the same way (a dead local image is a real defect). Only relative
    targets are checked: a URI scheme (``http(s):``, ``mailto:``, ``tel:`` -- two
    or more scheme chars, matched case-insensitively), a pure ``#anchor``, and a
    root-absolute (``/x``) or protocol-relative (``//host/x``) target are all
    skipped -- the last two are resolved by GitHub against the repo root, which
    this gate does not model. A ``#fragment`` is stripped before resolving --
    file existence is validated, anchors are not (a ``#L123`` line anchor cannot
    be checked against a moving file).
  - Targets that resolve under specs/personal/ are skipped (unvalidatable --
    the tree is gitignored), never reported.
  - Fenced code blocks and inline code spans are removed before scanning, so an
    example link quoted in code -- e.g. an arc42-cell reference written
    `` `[adr/](adr/)` `` -- is not mistaken for a live link. Fence handling
    follows CommonMark's opening rules (<=3-space indent; a backtick fence's
    info string may not contain a backtick) and closing rules (same character,
    length >= the opener, no info string), so neither a longer fence quoting a
    shorter one nor an inline ``` span in prose inverts the state.

Not handled -- accepted limitations for this corpus, documented so a future
widening is deliberate rather than a surprise:
  - Reference-style links (``[text][ref]`` with a ``[ref]: target`` definition):
    the corpus uses none; only inline links are matched.
  - Two CommonMark link shapes are silently missed (neither is in the corpus):
    a backslash-escaped ``]`` in link text (``[a \] b](t.md)``), and the
    image-badge nesting ``[![alt](img.png)](target.md)`` (only ``img.png`` is
    checked, not the outer target).
  - Targets containing ``)``, a space, ``<...>`` wrapping, or a ``%20`` escape:
    the tree uses none; such a target is reported dead *loudly*, not skipped.
  - Links split across a hard line wrap, and links inside an HTML comment, are
    scanned as ordinary prose -- line-based scanning is what keeps the reported
    line numbers, and the fence logic, simple.
  - Existence is checked against the working-tree filesystem. In CI that equals
    the git checkout, so the gate's authoritative run validates git truth; a
    *local* run may diverge -- an untracked linked file, or a case-only mismatch
    on a case-insensitive filesystem, passes locally but fails CI. CI is
    authoritative.

Exit code 0 when every link resolves; 1 with one line per dead link otherwise.
Stdlib only; all files are read as UTF-8.
"""

from __future__ import annotations

import os
import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SPECS_DIR = REPO_ROOT / "specs"
PERSONAL_DIR = SPECS_DIR / "personal"

LINK_RE = re.compile(r"\[[^\]]*\]\(([^)]+)\)")
# A code span closes on a backtick run of the SAME length, guarded at both ends
# so a 1-backtick opener does not close on one backtick of a longer ``` run.
CODE_SPAN_RE = re.compile(r"(?<!`)(`+)(?!`)(?:.*?)(?<!`)\1(?!`)")
# A fence opener: <=3-space indent, then a run of 3+ backticks or tildes.
FENCE_OPEN_RE = re.compile(r"^ {0,3}(`{3,}|~{3,})(.*)$")
# A fence closer: the same, but nothing after the run except whitespace.
FENCE_CLOSE_RE = re.compile(r"^ {0,3}(`{3,}|~{3,})[ \t]*$")
TITLE_RE = re.compile(r'(\S+)\s+"[^"]*"\Z')  # a real [t](path "title") suffix
SCHEME_RE = re.compile(r"[a-zA-Z][a-zA-Z0-9+\-]+:")  # a URI scheme (2+ chars, no dot)


def strip_code_spans(text: str) -> str:
    """Blank out inline code spans so a link quoted in code is not read as live."""
    return CODE_SPAN_RE.sub(" ", text)


def _fence_open(line: str) -> tuple[str, int] | None:
    """(char, length) if the line opens a fenced code block, else None."""
    m = FENCE_OPEN_RE.match(line)
    if m is None:
        return None
    run, info = m.group(1), m.group(2)
    # A backtick fence's info string may not contain a backtick; otherwise a
    # prose line carrying a ```...``` inline span would open a phantom fence.
    if run[0] == "`" and "`" in info:
        return None
    return (run[0], len(run))


def _fence_closes(line: str, fence: tuple[str, int]) -> bool:
    """Whether line closes the open fence: same char, length >=, no info string."""
    m = FENCE_CLOSE_RE.match(line)
    if m is None:
        return False
    run = m.group(1)
    return run[0] == fence[0] and len(run) >= fence[1]


def link_targets(md_text: str) -> list[tuple[int, str]]:
    """Return (line number, raw target) for every markdown link outside code."""
    targets: list[tuple[int, str]] = []
    fence: tuple[str, int] | None = None  # the currently-open fence, or None
    for lineno, raw in enumerate(md_text.splitlines(), start=1):
        if fence is not None:
            if _fence_closes(raw, fence):
                fence = None
            continue  # every line inside a fence (incl. its close) is not a link
        opener = _fence_open(raw)
        if opener is not None:
            fence = opener
            continue
        for match in LINK_RE.finditer(strip_code_spans(raw)):
            targets.append((lineno, match.group(1)))
    return targets


def resolve_target(source: Path, target: str) -> Path | None:
    """Absolute (``..``-normalized, symlink-preserving) path a relative link
    points at, or None if it is not a checkable relative file link."""
    stripped = target.strip()
    if not stripped:
        return None
    # Drop a trailing link title -- [t](path "title") -- but only when one is
    # actually present, so a path containing a space is not silently truncated
    # to its first token (it is reported dead loudly instead).
    title = TITLE_RE.match(stripped)
    url = title.group(1) if title else stripped
    if url.startswith("#") or SCHEME_RE.match(url):
        return None
    path_part = url.split("#", 1)[0]  # drop the #fragment; anchors are not checked
    # A root-absolute (/specs/...) or protocol-relative (//host/x) target is not
    # a checkable relative link: GitHub resolves the former against the repo
    # root, which this gate does not model, so skip it rather than false-report.
    # (path_part is otherwise always non-empty here -- url is non-empty and not
    # #-leading -- so no empty-string guard is needed.)
    if path_part.startswith("/"):
        return None
    return Path(os.path.normpath(source.parent / path_part))


def check() -> list[str]:
    errors: list[str] = []
    md_files = sorted(
        p for p in SPECS_DIR.rglob("*.md") if not p.is_relative_to(PERSONAL_DIR)
    )
    for source in md_files:
        rel = source.relative_to(REPO_ROOT).as_posix()
        try:
            text = source.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            errors.append(f"{rel}: not valid UTF-8 (cannot check links)")
            continue
        for lineno, target in link_targets(text):
            resolved = resolve_target(source, target)
            if resolved is None:
                continue
            if resolved.is_relative_to(PERSONAL_DIR):
                continue
            if not resolved.exists():
                errors.append(f"{rel}:{lineno}: dead link -> {target}")
    return errors


def main() -> int:
    errors = check()
    if errors:
        print(f"spec link check failed ({len(errors)} dead link(s)):")
        for e in errors:
            print(f"  - {e}")
        return 1
    print(f"spec links consistent: every relative link under {SPECS_DIR} resolves.")
    return 0


if __name__ == "__main__":
    sys.exit(main())

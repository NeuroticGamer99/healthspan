#!/usr/bin/env python3
r"""Verify that every relative link in the repo's markdown resolves to a file.

Mechanizes the docs-integrity half of the docs-consistency CI gate
([ADR-0045](../specs/adr/0045-repository-workflow-and-ci-enforcement.md) §6,
extended by ADR-0061): a cross-file link in tracked prose that points at a
moved or deleted target is a dead link, and inside an immutable Accepted ADR
even the corrective edit carries governance ceremony. The ADR-index check
(check_adr_index.py) guards the index *table*; this guards the prose *links*.
(The filename keeps its original ``spec_links`` name for reference stability;
the crawl was widened from specs/ to all tracked markdown by ADR-0061's
BRIEF-1 revision.)

Scope and rules:
  - Crawls every ``*.md`` git considers part of the repo -- tracked, plus
    untracked-but-not-ignored (``git ls-files --cached --others
    --exclude-standard``), so a brand-new doc is gated before it is ever
    staged -- EXCEPT specs/personal/ (gitignored, absent in CI). Outside a
    git checkout -- the unit suite's tmp trees -- it falls back to an rglob
    of the repo root, pruned of vendored trees, with the same personal/
    exclusion. A link *target* is validated wherever it resolves -- a file
    linking ``../../scripts/foo.py`` is checked too.
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
  - An inline code span that crosses a line break is not tracked (spans are
    matched per line), so a link on an interior line of a multi-line `` `...` ``
    span is scanned as live -- a *loud* false positive. Tracking cross-line
    span state risks the swallow-the-rest-of-the-file failure the fence logic
    was hardened against, and multi-line code is normally a fenced block, which
    *is* handled -- so this is left as a documented limitation.
  - Source *enumeration* is git truth (``git ls-files``), but target
    *existence* is still checked against the working-tree filesystem. In CI
    the two coincide, so the gate's authoritative run validates git truth; a
    *local* run may diverge -- an untracked linked file, or a case-only
    mismatch on a case-insensitive filesystem, passes locally but fails CI.
    CI is authoritative.

Exit code 0 when every link resolves; 1 with one line per dead link otherwise.
Stdlib only, where "stdlib" now includes a ``subprocess`` call to PATH-resolved
git for source enumeration (ADR-0061's revised tradeoff); all files are read
as UTF-8.
"""

from __future__ import annotations

import os
import re
import subprocess
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
        stripped = strip_code_spans(raw)
        for match in LINK_RE.finditer(stripped):
            # An escaped opener (\[...]) is not a link. An odd run of backslashes
            # before the [ escapes it; an even run escapes the backslashes
            # themselves, leaving the [ live.
            before = stripped[: match.start()]
            if (len(before) - len(before.rstrip("\\"))) % 2 == 1:
                continue
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


# Directories the non-git fallback walk must never enter: vendored and
# generated trees whose *.md are not this repo's prose. The git branch needs no
# such list -- git's ignore rules already exclude them -- which is the ADR's
# whole argument for git truth; this keeps the fallback from reintroducing the
# hazard on the one path git cannot police.
FALLBACK_PRUNE = frozenset(
    {".git", ".venv", ".venv-wsl", "node_modules", "__pycache__"}
)


def md_sources() -> list[Path]:
    """Every markdown file the gate crawls, sorted for deterministic output.

    Git truth when REPO_ROOT is a checkout (``.git`` is a directory in a normal
    clone and a *file* in a linked worktree, so ``exists()`` covers both):
    ``--cached`` is the tracked set, ``--others --exclude-standard`` adds files
    written but not yet added -- /land runs this gate *before* staging, and a
    brand-new doc is exactly where a dead link is most likely -- while git's
    ignore rules keep ``.venv/`` and scratch out without a prune list. Outside
    a checkout (the unit suite's tmp trees) an rglob of REPO_ROOT stands in,
    pruned of the vendored trees git would have excluded.

    The ``is_file()`` filter drops a tracked path deleted from the working tree
    (``--cached`` still names it; there are no bytes to scan, and the dead
    links *in other files that pointed at it* are what the gate should report).
    The set dedupes an unmerged path, which ``ls-files`` names once per merge
    stage. Both branches exclude specs/personal/: git never lists it
    (gitignored), and the fallback filter keeps the tmp-tree behaviour
    identical, so a test fixture under personal/ is never scanned either way.
    """
    if (REPO_ROOT / ".git").exists():
        proc = subprocess.run(
            [  # noqa: S607 - PATH-resolved git, as every other repo tool runs it
                "git",
                "ls-files",
                "--cached",
                "--others",
                "--exclude-standard",
                "-z",
                "--",
                "*.md",
            ],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            encoding="utf-8",
            check=False,
        )
        if proc.returncode != 0:
            # Attach git's own stderr: a bare CalledProcessError says only
            # "exit status 128", which blames this gate for a git failure.
            raise RuntimeError(
                f"git ls-files failed (exit {proc.returncode}): {proc.stderr.strip()}"
            )
        files = [REPO_ROOT / p for p in proc.stdout.split("\0") if p]
    else:
        files = [
            p
            for p in REPO_ROOT.rglob("*.md")
            if not FALLBACK_PRUNE.intersection(p.relative_to(REPO_ROOT).parts)
        ]
    return sorted(
        {p for p in files if p.is_file() and not p.is_relative_to(PERSONAL_DIR)}
    )


def check() -> list[str]:
    errors: list[str] = []
    md_files = md_sources()
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
    print("spec links consistent: every relative link in repo markdown resolves.")
    return 0


if __name__ == "__main__":
    sys.exit(main())

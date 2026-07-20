#!/usr/bin/env python3
"""Report the repository's size and shape: files, lines, and derived signal.

A permanent home for the ad-hoc "repo size so far" tally. Groups the tree into
the categories that carry meaning for this project (implementation, tests,
scripts, migrations, general specs, review notes, ADRs) and, per category,
counts files, physical lines, a code/comment/blank split, and bytes. It then
derives the ratios and the ADR-status breakdown that were previously eyeballed.

The code/comment/blank split -- not raw physical lines -- is the point: a
40-line module docstring and 40 statements are very different, and this repo
leans hard on long docstrings. Classification per language:

  - Python: comment = a whole-line ``#`` comment OR any line inside a *docstring*
    (module/class/function), the latter found by parsing the AST so that an
    assigned multi-line string -- ``x = \"\"\"...\"\"\"`` -- correctly counts as
    code, not comment. A trailing comment on a code line counts as code (the
    line does work). If a file does not parse, it degrades to the ``#``-only
    rule and is noted.
  - SQL: comment = a whole-line ``--`` comment. Block comments (``/* ... */``)
    are not modelled -- the migrations use none; such a line counts as code.
  - Markdown: no comment concept -- every non-blank line is "code" (content),
    comment is always 0. (HTML ``<!-- -->`` comments are not special-cased.)

  In every language a blank line (empty after strip) is counted blank first,
  including blank lines *inside* a Python docstring.

Category membership:
  - Python -- implementation: ``src/**/*.py``
  - Python -- tests: ``tests/**/*.py``
  - Python -- scripts: ``scripts/**/*.py``
  - SQL -- migrations: ``src/**/*.sql`` (the migration runner's numbered files)
  - Specs -- general: ``specs/*.md`` (top level only, non-recursive)
  - Specs -- reviews: ``specs/reviews/**/*.md``
  - ADRs: ``specs/adr/*.md`` (every file, incl. the README index and template)
  ``__pycache__`` is excluded everywhere.

  ``specs/personal/`` is deliberately EXCLUDED and never counted: it is
  gitignored personal-data-only (CLAUDE.md) and absent in CI, so it is not part
  of the shippable surface. A footnote records the exclusion; no counts, no
  content.

The ADR-status breakdown reads each numbered ``NNNN-*.md`` (excluding the
``0000-template.md``) ``## Status`` field, matching the convention that
``scripts/check_adr_index.py`` already relies on.

Output: a markdown report on stdout (``--json`` for a machine-readable dump).
Exit 0 always; any unreadable file is reported as a warning and skipped.
Stdlib only; all files are read as UTF-8 (a leading BOM is tolerated). Physical
lines are split on ``\\n``/``\\r``/``\\r\\n`` only, matching the AST's newline set.
"""

from __future__ import annotations

import argparse
import ast
import json
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

REPO_ROOT = Path(__file__).resolve().parent.parent
SRC = REPO_ROOT / "src"
TESTS = REPO_ROOT / "tests"
SCRIPTS = REPO_ROOT / "scripts"
SPECS = REPO_ROOT / "specs"
ADR = SPECS / "adr"
REVIEWS = SPECS / "reviews"

ADR_TEMPLATE = "0000-template.md"

# Category labels are defined once here and referenced from categories() (the
# producer), render_markdown (the consumer), and the tests, so a mismatch is a
# NameError, not a silently-empty dict.get that degrades a ratio to n/a.
LABEL_IMPL = "Python — implementation (src/)"
LABEL_TESTS = "Python — tests (tests/)"
LABEL_SCRIPTS = "Python — scripts (scripts/)"
LABEL_MIGRATIONS = "SQL — migrations"
LABEL_SPECS = "Specs — general (specs/*.md)"
LABEL_REVIEWS = "Specs — reviews (specs/reviews/)"
LABEL_ADR = "ADRs (specs/adr/)"


def _rel(path: Path) -> str:
    """Repo-relative POSIX path for display, falling back to the path itself if
    it does not live under REPO_ROOT (an odd cwd, a symlink out of the tree)."""
    try:
        return path.relative_to(REPO_ROOT).as_posix()
    except ValueError:
        return path.as_posix()


def _pyfiles(root: Path) -> list[Path]:
    if not root.exists():
        return []
    return sorted(p for p in root.rglob("*.py") if "__pycache__" not in p.parts)


def _mdfiles(root: Path, *, recursive: bool) -> list[Path]:
    if not root.exists():
        return []
    return sorted(root.rglob("*.md") if recursive else root.glob("*.md"))


# (label, language, files) -- ordered as the report prints them.
def categories() -> list[tuple[str, str, list[Path]]]:
    return [
        (LABEL_IMPL, "python", _pyfiles(SRC)),
        (LABEL_TESTS, "python", _pyfiles(TESTS)),
        (LABEL_SCRIPTS, "python", _pyfiles(SCRIPTS)),
        (LABEL_MIGRATIONS, "sql", sorted(SRC.rglob("*.sql")) if SRC.exists() else []),
        (LABEL_SPECS, "markdown", _mdfiles(SPECS, recursive=False)),
        (LABEL_REVIEWS, "markdown", _mdfiles(REVIEWS, recursive=True)),
        (LABEL_ADR, "markdown", _mdfiles(ADR, recursive=False)),
    ]


@dataclass
class Counts:
    files: int = 0
    physical: int = 0
    code: int = 0
    comment: int = 0
    blank: int = 0
    nbytes: int = 0

    def add(self, other: FileCount) -> None:
        self.files += 1
        self.physical += other.physical
        self.code += other.code
        self.comment += other.comment
        self.blank += other.blank
        self.nbytes += other.nbytes


@dataclass
class FileCount:
    physical: int
    code: int
    comment: int
    blank: int
    nbytes: int


def python_docstring_lines(text: str) -> tuple[set[int], bool]:
    """Line numbers (1-based) covered by any docstring, and whether it parsed.

    A docstring is the string literal that is the first statement of a module,
    class, or function -- exactly what ``ast.get_docstring`` recognizes, so an
    assigned or otherwise-positioned string is not mistaken for one. On a syntax
    error the caller falls back to the ``#``-only rule; the flag says so.
    """
    try:
        tree = ast.parse(text)
    except SyntaxError:
        return set(), False
    lines: set[int] = set()
    for node in ast.walk(tree):
        if not isinstance(
            node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)
        ):
            continue
        if ast.get_docstring(node, clean=False) is None:
            continue
        stmt = node.body[0]  # get_docstring guarantees an Expr holding a str Constant
        if isinstance(stmt, ast.Expr) and isinstance(stmt.value, ast.Constant):
            doc = stmt.value
            if doc.end_lineno is not None:
                lines.update(range(doc.lineno, doc.end_lineno + 1))
    return lines, True


def _physical_lines(text: str) -> list[str]:
    """Split into physical lines on ``\\n``/``\\r``/``\\r\\n`` only -- the newlines
    Python's parser recognizes -- so the enumerate index stays aligned with the
    AST line numbers from ``python_docstring_lines``. ``str.splitlines`` also
    breaks on form feed, NEL, and other Unicode separators the parser ignores;
    using it here would desync the two sides and miscount post-separator
    docstring lines as code."""
    normalized = text.replace("\r\n", "\n").replace("\r", "\n")
    parts = normalized.split("\n")
    if parts and parts[-1] == "":
        parts.pop()  # a trailing newline yields no extra line (splitlines parity)
    return parts


def classify(path: Path, lang: str) -> tuple[FileCount, str | None]:
    """Count one file. Returns (counts, warning-or-None)."""
    data = path.read_bytes()
    # utf-8-sig tolerates a leading BOM (harmless when absent): a BOM would
    # otherwise make ast.parse raise -> a spurious unparseable warning, and stay
    # glued to the first line (str.strip does not drop U+FEFF), miscounting it.
    text = data.decode("utf-8-sig")
    lines = _physical_lines(text)
    warning: str | None = None

    doc_lines: set[int] = set()
    if lang == "python":
        doc_lines, ok = python_docstring_lines(text)
        if not ok:
            warning = (
                f"{_rel(path)}: could not parse as Python; docstrings counted as code"
            )

    code = comment = blank = 0
    for i, raw in enumerate(lines, start=1):
        s = raw.strip()
        if s == "":
            blank += 1
        elif _is_comment(s, lang, in_docstring=i in doc_lines):
            comment += 1
        else:
            code += 1
    return FileCount(len(lines), code, comment, blank, len(data)), warning


def _is_comment(stripped: str, lang: str, *, in_docstring: bool) -> bool:
    """Whether a non-blank line is a comment: inside a docstring, or a whole-line
    ``#`` (Python) / ``--`` (SQL) comment. A trailing comment on code is not one
    (the line does work); Markdown has no comment concept."""
    if in_docstring:
        return True
    if lang == "python":
        return stripped.startswith("#")
    if lang == "sql":
        return stripped.startswith("--")
    return False


def adr_status_breakdown() -> dict[str, int]:
    """Bucket every numbered ADR by the first word of its ``## Status`` field."""
    buckets: dict[str, int] = {}
    if not ADR.exists():
        return buckets
    for path in sorted(ADR.glob("*.md")):
        name = path.name
        if name == ADR_TEMPLATE or not name[:4].isdigit():
            continue
        status = _adr_status(path) or "Unknown"
        head = status.split()[0] if status.split() else "Unknown"
        buckets[head] = buckets.get(head, 0) + 1
    return buckets


def _adr_status(path: Path) -> str | None:
    lines = path.read_text(encoding="utf-8").splitlines()
    for i, line in enumerate(lines):
        if line.strip() == "## Status":
            for candidate in lines[i + 1 :]:
                if candidate.strip():
                    return candidate.strip()
            return None
    return None


@dataclass
class Report:
    per_category: dict[str, Counts]
    warnings: list[str]
    adr_status: dict[str, int]


def build_report() -> Report:
    report = Report(per_category={}, warnings=[], adr_status=adr_status_breakdown())
    for label, lang, files in categories():
        counts = Counts()
        for path in files:
            try:
                fc, warn = classify(path, lang)
            except UnicodeDecodeError:
                report.warnings.append(f"{_rel(path)}: not valid UTF-8 (skipped)")
                continue
            counts.add(fc)
            if warn:
                report.warnings.append(warn)
        report.per_category[label] = counts
    return report


def _ratio(numer: int, denom: int) -> str:
    return f"{numer / denom:.2f}:1" if denom else "n/a"


def _kib(nbytes: int) -> str:
    return f"{nbytes / 1024:,.1f}"


def render_markdown(report: Report) -> str:
    header = ["Category", "Files", "Lines", "Code", "Comment", "Blank", "Size (KiB)"]
    rows: list[list[str]] = []
    for label, c in report.per_category.items():
        rows.append(
            [
                label,
                f"{c.files:,}",
                f"{c.physical:,}",
                f"{c.code:,}",
                f"{c.comment:,}",
                f"{c.blank:,}",
                _kib(c.nbytes),
            ]
        )
    total = Counts()
    for c in report.per_category.values():
        total.files += c.files
        total.physical += c.physical
        total.code += c.code
        total.comment += c.comment
        total.blank += c.blank
        total.nbytes += c.nbytes
    rows.append(
        [
            "**Total**",
            f"**{total.files:,}**",
            f"**{total.physical:,}**",
            f"**{total.code:,}**",
            f"**{total.comment:,}**",
            f"**{total.blank:,}**",
            f"**{_kib(total.nbytes)}**",
        ]
    )

    widths = [len(h) for h in header]
    for row in rows:
        for i, cell in enumerate(row):
            widths[i] = max(widths[i], len(cell))
    # Left-align the label column, right-align the numeric columns.
    aligns = ["<"] + [">"] * (len(header) - 1)

    def fmt(row: list[str]) -> str:
        cells = [f"{c:{a}{w}}" for c, a, w in zip(row, aligns, widths, strict=True)]
        return "| " + " | ".join(cells) + " |"

    sep = (
        "|"
        + "|".join(
            (":" + "-" * (w + 1)) if a == "<" else ("-" * (w + 1) + ":")
            for a, w in zip(aligns, widths, strict=True)
        )
        + "|"
    )

    per = report.per_category
    impl = per[LABEL_IMPL]
    tests = per[LABEL_TESTS]
    scripts = per[LABEL_SCRIPTS]
    migrations = per[LABEL_MIGRATIONS]
    docs_physical = sum(
        per[key].physical for key in (LABEL_SPECS, LABEL_REVIEWS, LABEL_ADR)
    )
    code_total = impl.code + tests.code + scripts.code + migrations.code

    lines_out: list[str] = ["## Repo size so far", ""]
    lines_out.append(fmt(header))
    lines_out.append(sep)
    for row in rows:
        lines_out.append(fmt(row))
    lines_out.append("")
    lines_out.append("**Ratios**")
    lines_out.append("")
    lines_out.append(
        f"- Tests : implementation — {_ratio(tests.physical, impl.physical)} "
        f"by physical lines, {_ratio(tests.code, impl.code)} by code lines"
    )
    lines_out.append(
        f"- Docs : code — {_ratio(docs_physical, code_total)} "
        f"(all markdown lines vs all Python + SQL code lines)"
    )
    lines_out.append(f"- Migrations — {migrations.files} file(s)")

    if report.adr_status:
        order = ["Accepted", "Proposed", "Superseded", "Deprecated", "Rejected"]
        parts: list[str] = []
        for key in order:
            if key in report.adr_status:
                parts.append(f"{report.adr_status[key]} {key}")
        for key in sorted(report.adr_status):
            if key not in order:
                parts.append(f"{report.adr_status[key]} {key}")
        total_adr = sum(report.adr_status.values())
        lines_out.append(
            f"- ADR status — {total_adr} numbered ADRs: " + ", ".join(parts)
        )

    lines_out.append("")
    lines_out.append(
        "_`specs/personal/` is excluded (gitignored personal data, absent in CI) — "
        "not part of the shippable surface and not counted here._"
    )

    if report.warnings:
        lines_out.append("")
        lines_out.append("**Warnings**")
        lines_out.append("")
        for w in report.warnings:
            lines_out.append(f"- {w}")

    return "\n".join(lines_out) + "\n"


def render_json(report: Report) -> str:
    payload = {
        "categories": {
            label: {
                "files": c.files,
                "physical": c.physical,
                "code": c.code,
                "comment": c.comment,
                "blank": c.blank,
                "bytes": c.nbytes,
            }
            for label, c in report.per_category.items()
        },
        "adr_status": report.adr_status,
        "warnings": report.warnings,
    }
    return json.dumps(payload, indent=2)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Report repository size and shape.")
    parser.add_argument(
        "--json", action="store_true", help="emit a machine-readable JSON dump"
    )
    args = parser.parse_args(argv)
    # The report embeds em dashes; force UTF-8 so a Windows cp1252 console (or a
    # redirect) cannot raise UnicodeEncodeError. See CLAUDE.md encoding note.
    # sys.stdout is a TextIOWrapper at runtime but typed TextIO; cast to reach
    # .reconfigure without a strict-typing complaint, and guard for exotic streams.
    stdout = cast(Any, sys.stdout)
    if hasattr(stdout, "reconfigure"):
        stdout.reconfigure(encoding="utf-8")
    report = build_report()
    print(render_json(report) if args.json else render_markdown(report))
    return 0


if __name__ == "__main__":
    sys.exit(main())

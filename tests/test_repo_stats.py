"""Unit tests for the repo-size reporter (scripts/repo_stats.py).

Two layers: the pure counters (python_docstring_lines, classify,
adr_status_breakdown) against constructed files, and build_report/render_* over
a throwaway repo under tmp_path -- never the real tree, so a count can never
depend on the live repo's size and these tests stay stable as it grows.
"""

import json
from pathlib import Path

import pytest
import repo_stats as rs

# --- python_docstring_lines ----------------------------------------------


def test_docstring_lines_marks_module_docstring() -> None:
    lines, ok = rs.python_docstring_lines('"""a\nb\nc"""\nx = 1\n')
    assert ok is True
    assert lines == {1, 2, 3}


def test_docstring_lines_ignores_assigned_multiline_string() -> None:
    # An assigned string is code, not a docstring -- the AST distinguishes it,
    # a naive triple-quote scanner would not. This is the reason for using ast.
    lines, ok = rs.python_docstring_lines('x = """a\nb"""\n')
    assert ok is True
    assert lines == set()


def test_docstring_lines_reports_parse_failure() -> None:
    lines, ok = rs.python_docstring_lines("def broken(:\n")
    assert ok is False
    assert lines == set()


# --- classify -------------------------------------------------------------


def test_classify_python_splits_docstring_comment_code_blank(tmp_path: Path) -> None:
    # A docstring line is comment; a trailing comment on a statement is code
    # (the line does work); a whole-line # is comment; empty is blank.
    src = tmp_path / "m.py"
    src.write_text(
        '"""doc\nline2\n"""\nx = 1  # trailing\n# pure\n\n', encoding="utf-8"
    )
    fc, warn = rs.classify(src, "python")
    assert warn is None
    assert (fc.physical, fc.code, fc.comment, fc.blank) == (6, 1, 4, 1)


def test_classify_python_unparseable_warns_and_uses_hash_rule(tmp_path: Path) -> None:
    src = tmp_path / "broken.py"
    src.write_text("def f(:\n# still a comment\ncode\n", encoding="utf-8")
    fc, warn = rs.classify(src, "python")
    assert warn is not None
    assert "could not parse" in warn
    # Falls back to the #-only rule: one comment, two code, no docstring credit.
    assert (fc.comment, fc.code) == (1, 2)


def test_classify_sql_uses_double_dash_comment(tmp_path: Path) -> None:
    src = tmp_path / "m.sql"
    src.write_text("-- header\nSELECT 1;\n\n", encoding="utf-8")
    fc, _ = rs.classify(src, "sql")
    assert (fc.code, fc.comment, fc.blank) == (1, 1, 1)


def test_classify_markdown_has_no_comment_column(tmp_path: Path) -> None:
    # A '#' heading is content, not a comment -- markdown has no comment concept.
    src = tmp_path / "d.md"
    src.write_text("# Title\n\ntext\n", encoding="utf-8")
    fc, _ = rs.classify(src, "markdown")
    assert (fc.code, fc.comment, fc.blank) == (2, 0, 1)


def test_classify_reports_bytes(tmp_path: Path) -> None:
    src = tmp_path / "d.md"
    src.write_bytes(b"abc\n")
    fc, _ = rs.classify(src, "markdown")
    assert fc.nbytes == 4


# --- /apply-review regressions (findings 1 and 2) -------------------------


def test_classify_python_form_feed_keeps_docstring_aligned(tmp_path: Path) -> None:
    # Finding 1: a form feed (legal Python whitespace) is a line boundary for
    # str.splitlines but NOT for the AST. Splitting on it would desync the two
    # and count the post-FF docstring line as code. _physical_lines splits on
    # \n/\r/\r\n only, so the whole docstring stays comment.
    src = tmp_path / "m.py"
    src.write_bytes(b'"""line1\x0cline2\nreal2"""\nx = 1\n')
    fc, warn = rs.classify(src, "python")
    assert warn is None
    # Two physical lines of docstring (comment), one statement (code), no blank.
    assert (fc.physical, fc.code, fc.comment, fc.blank) == (3, 1, 2, 0)


def test_classify_python_strips_utf8_bom(tmp_path: Path) -> None:
    # Finding 2: a BOM-prefixed but otherwise valid file must parse cleanly (no
    # spurious "could not parse" warning) and the first line must not carry a
    # glued U+FEFF that misclassifies a docstring/comment as code.
    src = tmp_path / "m.py"
    src.write_bytes(b"\xef\xbb\xbf" + b'"""doc"""\nx = 1\n')
    fc, warn = rs.classify(src, "python")
    assert warn is None
    assert (fc.code, fc.comment) == (1, 1)


# (The _is_comment branches are exercised through classify above: docstring,
# whole-line and trailing # in the Python test, -- in the SQL test, and the
# markdown-heading-as-content case in the markdown test.)


# --- adr_status_breakdown -------------------------------------------------


def test_adr_status_breakdown_buckets_by_first_word(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    adr = tmp_path / "adr"
    adr.mkdir()
    monkeypatch.setattr(rs, "ADR", adr)
    (adr / "0000-template.md").write_text("## Status\n\nProposed\n", encoding="utf-8")
    (adr / "0001-a.md").write_text("## Status\n\nAccepted\n", encoding="utf-8")
    (adr / "0002-b.md").write_text("## Status\n\nProposed\n", encoding="utf-8")
    # A link in the status cell must not change the first-word bucket.
    (adr / "0003-c.md").write_text(
        "## Status\n\nSuperseded by [ADR-0009](0009-x.md)\n", encoding="utf-8"
    )
    (adr / "README.md").write_text("index\n", encoding="utf-8")  # non-numbered: skip
    assert rs.adr_status_breakdown() == {
        "Accepted": 1,
        "Proposed": 1,  # the template's Proposed is excluded
        "Superseded": 1,
    }


# --- build_report / render_* over a throwaway repo ------------------------


def _repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point the module's path globals at a tmp repo laid out like the real one."""
    src = tmp_path / "src" / "pkg"
    (src / "migrations").mkdir(parents=True)
    (tmp_path / "tests").mkdir()
    (tmp_path / "scripts").mkdir()
    (tmp_path / "specs" / "reviews").mkdir(parents=True)
    (tmp_path / "specs" / "adr").mkdir()
    monkeypatch.setattr(rs, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(rs, "SRC", tmp_path / "src")
    monkeypatch.setattr(rs, "TESTS", tmp_path / "tests")
    monkeypatch.setattr(rs, "SCRIPTS", tmp_path / "scripts")
    monkeypatch.setattr(rs, "SPECS", tmp_path / "specs")
    monkeypatch.setattr(rs, "ADR", tmp_path / "specs" / "adr")
    monkeypatch.setattr(rs, "REVIEWS", tmp_path / "specs" / "reviews")
    return tmp_path


def test_build_report_counts_each_category(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = _repo(tmp_path, monkeypatch)
    (repo / "src" / "pkg" / "mod.py").write_text("x = 1\n", encoding="utf-8")
    (repo / "src" / "pkg" / "migrations" / "0001.sql").write_text(
        "SELECT 1;\n", encoding="utf-8"
    )
    (repo / "tests" / "test_x.py").write_text("y = 2\n", encoding="utf-8")
    (repo / "scripts" / "s.py").write_text("z = 3\n", encoding="utf-8")
    (repo / "specs" / "top.md").write_text("prose\n", encoding="utf-8")
    (repo / "specs" / "reviews" / "r.md").write_text("notes\n", encoding="utf-8")
    (repo / "specs" / "adr" / "0001-a.md").write_text(
        "## Status\n\nAccepted\n", encoding="utf-8"
    )
    report = rs.build_report()
    files = {label: c.files for label, c in report.per_category.items()}
    assert files[rs.LABEL_IMPL] == 1
    assert files[rs.LABEL_TESTS] == 1
    assert files[rs.LABEL_SCRIPTS] == 1
    assert files[rs.LABEL_MIGRATIONS] == 1
    assert files[rs.LABEL_SPECS] == 1  # top level only
    assert files[rs.LABEL_REVIEWS] == 1
    assert files[rs.LABEL_ADR] == 1
    assert report.adr_status == {"Accepted": 1}
    assert report.warnings == []


def test_build_report_excludes_pycache_and_nested_specs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = _repo(tmp_path, monkeypatch)
    cache = repo / "src" / "pkg" / "__pycache__"
    cache.mkdir()
    (cache / "mod.cpython-314.pyc").write_text("junk\n", encoding="utf-8")
    # A .md one level below specs/ (but not under reviews/) is NOT "general".
    (repo / "src" / "pkg" / "notes.md").write_text("x\n", encoding="utf-8")
    report = rs.build_report()
    assert report.per_category[rs.LABEL_IMPL].files == 0
    assert report.per_category[rs.LABEL_SPECS].files == 0


def test_build_report_warns_on_non_utf8_without_crashing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A Windows-1252 file (the corruption CLAUDE.md warns of) is skipped with a
    # clean warning, not a traceback.
    repo = _repo(tmp_path, monkeypatch)
    (repo / "specs" / "bad.md").write_bytes(b"\xff\xfe title\n")
    report = rs.build_report()
    assert len(report.warnings) == 1
    assert "not valid UTF-8" in report.warnings[0]


def test_build_report_survives_non_utf8_adr(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # adr_status_breakdown() reads every ADR for its status BEFORE the guarded
    # classify loop; a non-UTF-8 ADR must not crash the run (docstring: "Exit 0
    # always"). Its status bucket is dropped, but the same file's category read
    # in classify still emits the not-valid-UTF-8 warning, so it is reported.
    repo = _repo(tmp_path, monkeypatch)
    (repo / "specs" / "adr" / "0001-good.md").write_text(
        "## Status\n\nAccepted\n", encoding="utf-8"
    )
    (repo / "specs" / "adr" / "0002-bad.md").write_bytes(b"## Status\n\n\xff\xfe\n")
    report = rs.build_report()  # must not raise
    assert report.adr_status == {"Accepted": 1}  # bad ADR dropped, good one kept
    assert any("0002-bad.md" in w and "not valid UTF-8" in w for w in report.warnings)


def test_render_markdown_has_table_totals_ratios_and_footnote(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = _repo(tmp_path, monkeypatch)
    (repo / "src" / "pkg" / "mod.py").write_text("x = 1\n", encoding="utf-8")
    (repo / "specs" / "adr" / "0001-a.md").write_text(
        "## Status\n\nAccepted\n", encoding="utf-8"
    )
    out = rs.render_markdown(rs.build_report())
    assert "## Repo size so far" in out
    assert "**Total**" in out
    assert "Tests : implementation" in out
    assert "ADR status" in out
    assert "1 Accepted" in out
    # The personal-data exclusion is stated in the output, not just the docstring.
    assert "specs/personal/" in out
    assert "excluded" in out


def test_render_json_is_valid_and_structured(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = _repo(tmp_path, monkeypatch)
    (repo / "src" / "pkg" / "mod.py").write_text("x = 1\n", encoding="utf-8")
    payload = json.loads(rs.render_json(rs.build_report()))
    assert set(payload) == {"categories", "adr_status", "warnings"}
    impl = payload["categories"]["Python — implementation (src/)"]
    assert set(impl) == {"files", "physical", "code", "comment", "blank", "bytes"}
    assert impl["files"] == 1


def test_main_prints_and_returns_zero(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _repo(tmp_path, monkeypatch)
    assert rs.main([]) == 0
    assert "## Repo size so far" in capsys.readouterr().out
    assert rs.main(["--json"]) == 0
    assert "categories" in capsys.readouterr().out

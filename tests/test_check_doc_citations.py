"""The ADR-0073 citation gate (scripts/check_doc_citations.py).

The gate exists because ADR-0073 trades restatement for citation, and the one
failure that trade introduces — a rewritten skill silently dropping its
pointer — is invisible in the resulting prose. A gate that cannot fail is that
same defect one layer out, so every assertion below breaks exactly one
precondition and requires the gate to notice. The live-repo case alone would
stay green if `check()` returned `[]` unconditionally.

Nothing here hardcodes a caller path. `CITATIONS` is the only source of which
files exist in a fixture, so renaming a caller and updating the registry — the
two-file edit ADR-0073 describes — moves this suite with it instead of raising
`FileNotFoundError` from a path the fixture never wrote.
"""

from __future__ import annotations

from pathlib import Path

import check_doc_citations
import pytest

# Which documents each caller is registered under, derived once.
_CALLER_DOCS: dict[str, list[str]] = {}
for _doc, _callers in check_doc_citations.CITATIONS.items():
    for _caller in _callers:
        _CALLER_DOCS.setdefault(_caller, []).append(_doc)

_SINGLY_REGISTERED = sorted(c for c, docs in _CALLER_DOCS.items() if len(docs) == 1)

# Derived, never hand-copied: a literal list gives a newly registered caller no
# case at all, silently — the same drift the gate's own registry is criticized
# for, one layer up in the file that is supposed to catch it.
_REGISTERED_PAIRS = [
    (doc, caller)
    for doc, callers in check_doc_citations.CITATIONS.items()
    for caller in callers
]

_NEEDLE_ROWS = [
    (doc, caller, needles)
    for doc, callers in check_doc_citations.CITATIONS.items()
    for caller, needles in callers.items()
    if needles
]


def test_the_live_repository_conforms() -> None:
    """The repo's own citations hold — the gate's day job."""
    assert check_doc_citations.check() == []


def test_the_repo_is_the_thing_being_checked() -> None:
    """Every registered path resolves, so `check()` is not passing vacuously.

    Not independent detection — a wrong registry path also reddens
    `test_the_live_repository_conforms`, which is the honest statement of what
    this adds. What it adds is the *signal*: this names the offending path,
    where the sibling reports an unexpected error list and leaves the reader to
    work out which row is wrong.
    """
    for doc, callers in check_doc_citations.CITATIONS.items():
        assert (check_doc_citations.REPO_ROOT / doc).is_file(), doc
        for caller in callers:
            assert (check_doc_citations.REPO_ROOT / caller).is_file(), caller


def test_the_registry_is_not_empty() -> None:
    """An emptied `CITATIONS` would pass every other test in this file.

    The mapping is hand-maintained, so the mode where it is gutted rather than
    corrupted has to be asserted against directly.

    `_NEEDLE_ROWS` is asserted here too, and that is not symmetry. Emptying the
    one needle tuple turns `…_a_registered_needle_is_missing` into a bare
    `SKIPPED` with no reason while every other test stays green — a registered
    protection silently ceasing to be exercised, which is the exact failure the
    needles were added to catch, recurring one layer up in the suite that
    guards the gate. A skip is invisible against a two-digit skip count in CI.
    """
    assert check_doc_citations.CITATIONS
    assert all(callers for callers in check_doc_citations.CITATIONS.values())
    assert _REGISTERED_PAIRS
    assert _NEEDLE_ROWS, "a needle row was removed; its test would silently skip"
    assert _SINGLY_REGISTERED, "no singly-registered caller left to exercise"


def _fixture_repo(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[Path, dict[str, list[str]]]:
    """A miniature repo satisfying every registered citation, already patched in.

    Takes `monkeypatch` and redirects `REPO_ROOT` itself, matching
    `tests/test_check_spec_links.py`'s `_repo` and
    `tests/test_check_markdownlint_config_sync.py`'s `_point_at`. A helper that
    returns a path and leaves the redirect to the caller has already cost this
    suite a measured false pass: one test was green with its `monkeypatch` line
    absent, because `check()` then read the real repository and returned `[]`
    for an unrelated reason.

    Returns the mapping of caller -> documents it cites, so tests rewriting a
    caller reuse this derivation instead of recomputing it from `CITATIONS`.
    """
    cited: dict[str, list[str]] = {}
    for doc, callers in check_doc_citations.CITATIONS.items():
        doc_path = tmp_path / doc
        doc_path.parent.mkdir(parents=True, exist_ok=True)
        doc_path.write_text("The owning document.\n", encoding="utf-8")
        for caller in callers:
            cited.setdefault(caller, []).append(doc)

    for caller, docs in cited.items():
        _write_caller(tmp_path, caller, docs)

    monkeypatch.setattr(check_doc_citations, "REPO_ROOT", tmp_path)
    return tmp_path, cited


def _write_caller(repo: Path, caller: str, docs: list[str]) -> None:
    """Write `caller` citing exactly `docs`, each with its registered needles."""
    body: list[str] = []
    for doc in docs:
        body.append(f"Step 7 defers to `{doc}`, which governs.\n")
        for needle in check_doc_citations.CITATIONS[doc][caller]:
            body.append(f"It is the {needle} rule.\n")
    path = repo / caller
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(body), encoding="utf-8")


def test_the_fixture_repo_passes_before_it_is_broken(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The baseline the mutation cases below are measured against.

    Without it, a fixture that never satisfied the gate would make each
    failure case pass for the wrong reason.
    """
    _fixture_repo(tmp_path, monkeypatch)
    assert check_doc_citations.check() == []


@pytest.mark.parametrize(("doc", "dropped"), _REGISTERED_PAIRS)
def test_the_gate_fails_when_any_single_caller_drops_the_citation(
    doc: str, dropped: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One case per registered (document, caller) pair — the drift this exists for.

    Parametrized per site rather than mutating all of them at once: a gate that
    only noticed when *every* caller broke would pass a suite that mutated them
    together, which is the exact under-reporting shape ADR-0073's second rule is
    about.
    """
    repo, cited = _fixture_repo(tmp_path, monkeypatch)
    # Drop exactly this one citation, keeping the caller's others: a caller
    # registered under two documents would otherwise break both, turning a
    # single-cause test into a two-error assertion that isolates nothing.
    _write_caller(repo, dropped, [d for d in cited[dropped] if d != doc])

    errors = check_doc_citations.check()
    assert len(errors) == 1, errors
    assert dropped in errors[0]
    assert doc in errors[0]


@pytest.mark.parametrize(("doc", "caller", "needles"), _NEEDLE_ROWS)
def test_the_gate_fails_when_a_registered_needle_is_missing(
    doc: str,
    caller: str,
    needles: tuple[str, ...],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The path alone must not satisfy a row that registered needles.

    This is the measured defect the needles exist for: `/apply-review` already
    carried `.claude/bot-review-triage.md` for an unrelated §4 rule, so the row
    added to gate §1's peer-search rule could be satisfied with that rule
    deleted — the gate exited 0 reporting six healthy citations.
    """
    repo, cited = _fixture_repo(tmp_path, monkeypatch)
    # The path present, every needle absent — exactly the unrelated-mention shape.
    body = "".join(f"Step 7 defers to `{d}`, which governs.\n" for d in cited[caller])
    (repo / caller).write_text(body, encoding="utf-8")

    errors = check_doc_citations.check()
    assert len(errors) == len(needles), errors
    for needle, error in zip(needles, errors, strict=True):
        assert caller in error
        assert needle in error


def test_the_gate_fails_when_a_caller_file_is_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A renamed caller is when the citation is likeliest to have been lost,
    so an absent caller must fail rather than be skipped."""
    if not _SINGLY_REGISTERED:
        pytest.skip("no caller is registered under exactly one document")
    repo, _ = _fixture_repo(tmp_path, monkeypatch)
    (repo / _SINGLY_REGISTERED[0]).unlink()

    errors = check_doc_citations.check()
    assert len(errors) == 1, errors
    assert "missing from the repository" in errors[0]


def test_a_doubly_registered_missing_caller_is_reported_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One cause, one line — for a caller registered under two documents.

    The outer loop visits it once per owning document, so a naive report emits
    the identical "missing from the repository" line twice and doubles the
    violation count for a single rename.

    Built on a **synthetic** registry rather than the live one. Exactly one
    caller happens to be doubly registered today and that is incidental, not
    structural: a registry edit removing the overlap would quietly turn this
    into `pytest.skip`, and this is the sole test pinning the dedupe. The
    behaviour is not conditional on the registry having that shape, so its test
    must not be either.
    """
    shared = ".claude/skills/shared/SKILL.md"
    monkeypatch.setattr(
        check_doc_citations,
        "CITATIONS",
        {
            "docs/first.md": {shared: ()},
            "docs/second.md": {shared: ("a distinguishing phrase",)},
        },
    )
    repo, _ = _fixture_repo(tmp_path, monkeypatch)
    (repo / shared).unlink()

    errors = check_doc_citations.check()
    assert len(errors) == 1, errors
    assert shared in errors[0]
    assert "missing from the repository" in errors[0]


def test_each_caller_is_read_once_however_many_rows_cite_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Pins the cache's stated property, which no correctness test can see.

    Mis-keying the cache is caught by the drop-citation cases, but removing it
    outright is invisible — behaviour is identical and the file is merely read
    again. The comment on `texts` claims "read once", and a claim nothing
    checks is one that should not have been written.
    """
    shared = ".claude/skills/shared/SKILL.md"
    monkeypatch.setattr(
        check_doc_citations,
        "CITATIONS",
        {"docs/first.md": {shared: ()}, "docs/second.md": {shared: ()}},
    )
    _fixture_repo(tmp_path, monkeypatch)

    reads: list[str] = []
    unpatched = Path.read_text

    def counting(
        self: Path,
        encoding: str | None = None,
        errors: str | None = None,
        newline: str | None = None,
    ) -> str:
        reads.append(str(self))
        return unpatched(self, encoding=encoding, errors=errors, newline=newline)

    monkeypatch.setattr(Path, "read_text", counting)
    assert check_doc_citations.check() == []
    assert reads, "the gate read nothing — the fixture is not being exercised"
    assert len(reads) == len(set(reads)), reads


def test_the_gate_fails_when_the_owning_document_is_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The failure no other CI gate catches.

    The citations are code spans rather than markdown links, so renaming or
    deleting the owning document leaves `check_spec_links.py` silent while
    every pointer to it dangles.
    """
    repo, _ = _fixture_repo(tmp_path, monkeypatch)
    doc = next(iter(check_doc_citations.CITATIONS))
    (repo / doc).unlink()

    errors = check_doc_citations.check()
    assert len(errors) == 1, errors
    assert "owning document is missing" in errors[0]
    assert "code spans" in errors[0], "the error must say why link-check misses it"


def test_a_missing_owning_document_does_not_also_report_its_callers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Pins `check()`'s `continue`, which the sibling above cannot.

    With the fixture's callers all citing the document, dropping that
    `continue` still yields zero caller errors, so both tests stay green while
    a single deleted document would print one cause plus every consequence.
    The callers here are therefore written *without* the citation: the count
    separates the two behaviours instead of coinciding for both.
    """
    repo, cited = _fixture_repo(tmp_path, monkeypatch)
    doc = next(iter(check_doc_citations.CITATIONS))
    callers = check_doc_citations.CITATIONS[doc]
    for caller in callers:
        _write_caller(repo, caller, [d for d in cited[caller] if d != doc])
    (repo / doc).unlink()

    errors = check_doc_citations.check()
    assert len(errors) == 1, errors
    assert "owning document is missing" in errors[0]
    assert not any("no longer cites" in error for error in errors)


def test_the_error_message_names_the_file_and_the_document(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An operator reading CI output must not have to open the script to learn
    which file broke and what it stopped pointing at."""
    doc, caller = _REGISTERED_PAIRS[0]
    repo, _ = _fixture_repo(tmp_path, monkeypatch)
    (repo / caller).write_text("No citation.\n", encoding="utf-8")

    errors = check_doc_citations.check()
    assert any(caller in e and doc in e and "ADR-0073" in e for e in errors), errors


def test_main_exits_zero_and_reports_the_totals_when_citations_hold(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """`main()` is what CI runs, and its exit code is the whole contract.

    `check()` being correct proves nothing about the script's behaviour: with
    no test here, mutating the failure branch's `return 1` to `return 0` leaves
    every other test in this file green while the CI gate stops failing. That
    mutation was run and survived, which is why this test exists.
    """
    assert check_doc_citations.main() == 0
    out = capsys.readouterr().out
    pairs = sum(len(callers) for callers in check_doc_citations.CITATIONS.values())
    # Each number bound to its noun, not merely present. Measured: asserting
    # the bare digits let a swap of the two values in `main()`'s f-string —
    # "2 citations across 6 owning documents" — pass unnoticed.
    assert f"{pairs} citations" in out
    assert f"{len(check_doc_citations.CITATIONS)} owning documents" in out


def test_main_exits_one_and_prints_each_violation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The failing half of the same contract — a gate that cannot exit non-zero
    is not a gate, however correct the function behind it."""
    _, caller = _REGISTERED_PAIRS[0]
    repo, _ = _fixture_repo(tmp_path, monkeypatch)
    (repo / caller).write_text("No citation.\n", encoding="utf-8")

    assert check_doc_citations.main() == 1
    out = capsys.readouterr().out
    assert caller in out
    assert "broken (" in out, "the summary must say how many violations there are"


def test_a_disclaimed_mention_still_satisfies_the_check(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Pins a **known limitation**, not a desired behaviour.

    The check is presence-detection: the owning document's path appearing
    anywhere in the caller satisfies it, including inside a code fence or in a
    sentence disclaiming the citation outright. That is the deliberate trade —
    `scripts/check_reviewer_agents.py` makes the identical one, and the
    alternative is a semantic judgement ADR-0073 explicitly declines to
    mechanize. Needles narrow it for a specific row; they do not change it.

    **Carries its own positive control**, because `[]` is also what a broken
    setup produces. Measured: with the redirect absent this test was green,
    `check()` having read the real repository. So the control runs first —
    strip the citation and require a complaint — before asserting that the
    disclaimed form passes.
    """
    # A needle-free row: a needle is precisely what a disclaiming sentence
    # would *not* carry, so a row that has one cannot demonstrate this limit.
    pair = next(
        (
            (d, c)
            for d, c in _REGISTERED_PAIRS
            if not check_doc_citations.CITATIONS[d][c]
        ),
        None,
    )
    # A bare `next()` would raise StopIteration here, which pytest reports as
    # an error rather than a readable failure. Dormant today; it fires the day
    # every row gains a needle.
    assert pair is not None, "no needle-free row left to demonstrate the limitation"
    doc, caller = pair
    repo, cited = _fixture_repo(tmp_path, monkeypatch)
    others = [d for d in cited[caller] if d != doc]

    _write_caller(repo, caller, others)
    assert check_doc_citations.check() != [], (
        "positive control: with the path absent the gate must complain, "
        "or this fixture is not what is being measured"
    )

    # Other citations restored properly; only `doc` is reduced to a disclaimer.
    with (repo / caller).open("a", encoding="utf-8") as handle:
        handle.write(f"This skill used to defer to `{doc}`, but no longer does.\n")
    # `check() == []`, not a filtered subset: a filter keyed on this doc and
    # caller would miss a "no longer cites <other doc>" error, which is how the
    # weaker form stayed sound only by accident of registry ordering.
    assert check_doc_citations.check() == []

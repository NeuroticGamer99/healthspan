"""The reviewer-agent gate (scripts/check_reviewer_agents.py).

Two ADRs own what it checks, and this file is one of the places that used to
name only the first. ADR-0068 supplies the `isolation: worktree` prohibition
and the `.claude/reviewer-isolation.md` citation, each stated in prose and
enforced by nothing until the gate existed. ADR-0076 supplies the third
assertion: `test-reviewer.md` must still name its `Acceptance:` report field.

A gate that cannot fail is the same defect one layer out, so each assertion
below breaks exactly one precondition and requires the gate to notice — the
live-repo case alone would stay green if `check()` returned `[]`
unconditionally. The acceptance assertion needs a matched *pair* to be pinned
at all: a negative case (field absent, so exactly one error naming the right
file) and a positive one (field present, so clean). Either alone is satisfied
by a check that never consults the text.
"""

from __future__ import annotations

from pathlib import Path

import check_reviewer_agents
import pytest

_FRONTMATTER = """---
name: spec-reviewer
description: A reviewer.
tools: Read, Grep, Glob, Bash
model: sonnet
---

Body text citing .claude/reviewer-isolation.md for the mechanism.
Each finding ends with an **Acceptance:** line naming the mutation its remedy
must survive.
"""

# The same body with the acceptance field removed. The field name and the two
# agent filenames appear in this file as **literals**, never as the module's own
# constants: an assertion reading `check_reviewer_agents.ACCEPTANCE_FILE` moves
# with the implementation, so repointing that constant at `spec-reviewer.md`
# repoints the oracle too and the test can only ever confirm "the error names
# whatever the constant currently says". Measured — that mutation left this file
# green until the assertions below were changed to literals.
#
# The literal is the bare name, matching what the gate pins: the field is spelled
# several ways across the documents that carry it, and only the name is common to
# all of them. Stripping it here therefore removes the field under every spelling.
_WITHOUT_ACCEPTANCE = _FRONTMATTER.replace("Acceptance:", "nothing")


def test_the_live_agent_files_conform() -> None:
    """The repo's own two agent files pass — the gate's day job."""
    assert check_reviewer_agents.check() == []


def test_the_repo_is_the_thing_being_checked() -> None:
    """Both files really are found, so `check()` is not passing vacuously.

    `check()` reports a missing file rather than skipping it, but that only
    holds while the paths are right; asserting the constants resolve keeps a
    renamed directory from turning this whole suite into a no-op.
    """
    for name in check_reviewer_agents.AGENT_FILES:
        assert (check_reviewer_agents.AGENT_DIR / name).is_file(), name


def test_frontmatter_parses_to_its_top_level_keys() -> None:
    fields = check_reviewer_agents.parse_frontmatter(_FRONTMATTER)
    assert fields is not None
    assert fields["name"] == "spec-reviewer"
    assert fields["model"] == "sonnet"
    assert check_reviewer_agents.FORBIDDEN_KEY not in fields


@pytest.mark.parametrize(
    ("text", "reason"),
    [
        ("no fence at all\n", "a file with no frontmatter"),
        ("---\nname: x\n", "an unterminated frontmatter block"),
    ],
)
def test_unparseable_frontmatter_is_not_read_as_clean(text: str, reason: str) -> None:
    """None, never `{}`. A shape this parser cannot read means the agent
    definition is broken, and a check that treats "unparseable" as "no
    forbidden key" is the false pass the gate exists to prevent."""
    assert check_reviewer_agents.parse_frontmatter(text) is None, reason


def test_the_gate_fails_on_the_forbidden_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The assertion that matters: `isolation:` in frontmatter is caught.

    Any value, not just `worktree` — the rule is that this harness feature
    does not decide these agents' trees at all.
    """
    agent_dir = tmp_path / "agents"
    agent_dir.mkdir()
    for name in check_reviewer_agents.AGENT_FILES:
        (agent_dir / name).write_text(
            _FRONTMATTER.replace("model: sonnet", "model: sonnet\nisolation: worktree"),
            encoding="utf-8",
        )
    monkeypatch.setattr(check_reviewer_agents, "AGENT_DIR", agent_dir)

    errors = check_reviewer_agents.check()
    assert len(errors) == len(check_reviewer_agents.AGENT_FILES), errors
    for error in errors:
        assert "isolation" in error
        assert "report clean" in error, "the error must say what goes wrong"


def test_the_gate_fails_when_the_procedure_doc_is_no_longer_cited(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A prohibition with no pointer to the replacement reads as an oversight,
    so dropping the citation is a violation in its own right."""
    agent_dir = tmp_path / "agents"
    agent_dir.mkdir()
    for name in check_reviewer_agents.AGENT_FILES:
        (agent_dir / name).write_text(
            _FRONTMATTER.replace(check_reviewer_agents.PROCEDURE_DOC, "somewhere else"),
            encoding="utf-8",
        )
    monkeypatch.setattr(check_reviewer_agents, "AGENT_DIR", agent_dir)

    errors = check_reviewer_agents.check()
    assert len(errors) == len(check_reviewer_agents.AGENT_FILES), errors
    for error in errors:
        assert check_reviewer_agents.PROCEDURE_DOC in error


def test_the_gate_fails_when_the_acceptance_field_is_dropped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Dropping the `Acceptance:` instruction is a silent loss, so it fails here.

    A fixer handed a finding with no criterion cannot notice a field they never
    knew to expect; nothing downstream compares one report against the last.

    This is the **negative** half only, and it is not sufficient alone — the test
    below is the positive half. Both files here are written *without* the field,
    so `test-reviewer.md` is a genuine violation whether or not the check
    consults the text at all, and a clause that fires unconditionally satisfies
    every assertion in this body. Review measured exactly that.
    """
    agent_dir = tmp_path / "agents"
    agent_dir.mkdir()
    for name in check_reviewer_agents.AGENT_FILES:
        (agent_dir / name).write_text(_WITHOUT_ACCEPTANCE, encoding="utf-8")
    monkeypatch.setattr(check_reviewer_agents, "AGENT_DIR", agent_dir)

    errors = check_reviewer_agents.check()
    assert len(errors) == 1, errors
    assert errors[0].startswith("test-reviewer.md"), errors[0]
    assert "Acceptance:" in errors[0]


def test_only_the_test_reviewer_owes_an_acceptance_field(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The positive half: the field present clears the check, and `spec-reviewer`
    is exempt by decision rather than merely untested.

    Written after review found the negative case above insufficient on its own.
    Two mutations survived that one and die here. Making the clause fire
    unconditionally (`if name == ACCEPTANCE_FILE:`) errors on a `test-reviewer.md`
    that carries the field. Repointing `ACCEPTANCE_FILE` at `spec-reviewer.md`
    errors on the file deliberately exempted — it is read-only by mandate, runs
    no mutation, and so has no criterion to give.
    """
    agent_dir = tmp_path / "agents"
    agent_dir.mkdir()
    (agent_dir / "test-reviewer.md").write_text(_FRONTMATTER, encoding="utf-8")
    (agent_dir / "spec-reviewer.md").write_text(_WITHOUT_ACCEPTANCE, encoding="utf-8")
    monkeypatch.setattr(check_reviewer_agents, "AGENT_DIR", agent_dir)

    assert check_reviewer_agents.check() == []


def test_the_acceptance_check_runs_per_file_not_once_after_the_loop(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`test-reviewer.md` is *last* in `AGENT_FILES`, which makes two different
    implementations indistinguishable to every other test in this file.

    A clause dedented out of `for name in AGENT_FILES:` runs once, after the
    loop, against whatever `name`/`text` the final iteration left bound — and
    those are `test-reviewer.md`'s, so it produces the right answer for the
    wrong reason. Review measured exactly that: the dedent left all nine other
    tests green.

    Reversing the order separates the two. The check must still fire for
    `test-reviewer.md` when some other file is processed last; under the dedent
    the trailing `name` is `spec-reviewer.md`, the guard is False, and no error
    is raised at all.
    """
    agent_dir = tmp_path / "agents"
    agent_dir.mkdir()
    (agent_dir / "test-reviewer.md").write_text(_WITHOUT_ACCEPTANCE, encoding="utf-8")
    (agent_dir / "spec-reviewer.md").write_text(_FRONTMATTER, encoding="utf-8")
    monkeypatch.setattr(check_reviewer_agents, "AGENT_DIR", agent_dir)
    monkeypatch.setattr(
        check_reviewer_agents, "AGENT_FILES", ("test-reviewer.md", "spec-reviewer.md")
    )

    errors = check_reviewer_agents.check()
    assert len(errors) == 1, errors
    assert errors[0].startswith("test-reviewer.md"), errors[0]


def test_check_reports_a_missing_agent_file_rather_than_skipping_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`check()`'s own guard branches were covered only one level down.

    `parse_frontmatter` is unit-tested directly, but nothing drove `check()`
    through a missing or unparseable file — so the module's stated guarantee
    ("unparseable fails rather than passing vacuously") was unproven at the
    layer that consumes it, which is also the layer the acceptance assertion
    now sits behind. Review measured both guards surviving mutation.
    """
    agent_dir = tmp_path / "agents"
    agent_dir.mkdir()
    (agent_dir / "test-reviewer.md").write_text(_FRONTMATTER, encoding="utf-8")
    # spec-reviewer.md deliberately absent
    monkeypatch.setattr(check_reviewer_agents, "AGENT_DIR", agent_dir)

    errors = check_reviewer_agents.check()
    assert len(errors) == 1, errors
    assert errors[0].startswith("spec-reviewer.md"), errors[0]
    assert "missing" in errors[0]


def test_unparseable_frontmatter_stops_that_file_at_one_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The `continue` after `fields is None` is load-bearing, not tidiness.

    Without it, execution falls through to `FORBIDDEN_KEY in fields` against
    `None`. Asserting the *single* error is what pins it: a fall-through raises
    instead of returning a list, and a second error against the same file would
    mean the guard stopped nothing.
    """
    agent_dir = tmp_path / "agents"
    agent_dir.mkdir()
    (agent_dir / "test-reviewer.md").write_text(_FRONTMATTER, encoding="utf-8")
    (agent_dir / "spec-reviewer.md").write_text(
        "no fence at all, so no frontmatter block\n", encoding="utf-8"
    )
    monkeypatch.setattr(check_reviewer_agents, "AGENT_DIR", agent_dir)

    errors = check_reviewer_agents.check()
    assert len(errors) == 1, errors
    assert errors[0].startswith("spec-reviewer.md"), errors[0]
    assert "frontmatter" in errors[0]


def test_main_fails_loudly_without_blaming_a_single_owning_adr(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """`main()`'s banner wording is part of a decision, so something holds it.

    The gate answers to two ADRs, and a banner attributing every failure to
    ADR-0068 misfiles the ones that are not about isolation — the reasoning
    ADR-0076 §7 gives for widening it. Nothing else in this file calls `main()`,
    so the wording could otherwise revert while assertion 3 stayed in place and
    every gate stayed green.

    The assertion is on the property, not the sentence: for a failure that is
    *not* about isolation, no single owning ADR is named and the neutral phrase
    is present. Pinning the whole string would redden on a rewording that left
    the decision intact.

    This fixture violates one rule only, which is the easy half — see the mixed
    fixture below for the case where an isolation violation legitimately *does*
    name ADR-0068 in the same run.

    Both owners are rejected, case-insensitively: "no single owning ADR" is what
    this test's name promises, and checking one of the two let the other pass.
    """
    agent_dir = tmp_path / "agents"
    agent_dir.mkdir()
    for name in check_reviewer_agents.AGENT_FILES:
        (agent_dir / name).write_text(_WITHOUT_ACCEPTANCE, encoding="utf-8")
    monkeypatch.setattr(check_reviewer_agents, "AGENT_DIR", agent_dir)

    assert check_reviewer_agents.main() == 1
    banner = capsys.readouterr().out
    assert "required invariants" in banner
    neutral = banner.casefold()
    assert "adr-0068" not in neutral, banner
    assert "adr-0076" not in neutral, banner


def test_main_attributes_each_violation_to_its_own_rule(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Mixed violations — the half the single-rule fixture above cannot reach.

    That test shows the *header* is rule-neutral. It cannot show the harder
    property: when an isolation violation and an acceptance violation fire in
    the same run, ADR-0068 must be named on the line genuinely about it and on
    no other. One edit breaking both rules at once is the realistic regression
    shape, and a banner that blamed ADR-0068 for everything would still satisfy
    every other test here.

    It also pins `main()`'s per-error print loop and its violation count, both
    of which review measured as decorative: emptying the loop body, and
    replacing `len(errors)` with a literal, each left the whole suite green.
    """
    agent_dir = tmp_path / "agents"
    agent_dir.mkdir()
    both_broken = _WITHOUT_ACCEPTANCE.replace(
        "model: sonnet", "model: sonnet\nisolation: worktree"
    )
    for name in check_reviewer_agents.AGENT_FILES:
        (agent_dir / name).write_text(both_broken, encoding="utf-8")
    monkeypatch.setattr(check_reviewer_agents, "AGENT_DIR", agent_dir)

    assert check_reviewer_agents.main() == 1
    printed = capsys.readouterr().out.splitlines()
    lines = [ln for ln in printed if ln.startswith("  - ")]

    # The header is the only place the total is stated, and it was decorative
    # as far as the suite was concerned: review replaced `len(errors)` with a
    # hardcoded literal and every test stayed green.
    assert "(3)" in printed[0], printed[0]

    isolation = [ln for ln in lines if "frontmatter sets" in ln]
    acceptance = [ln for ln in lines if "report field" in ln]
    assert len(isolation) == 2, lines  # one per agent file
    assert len(acceptance) == 1, lines  # test-reviewer.md alone owes the field
    assert all("ADR-0068" in ln for ln in isolation), isolation
    assert "ADR-0068" not in acceptance[0], acceptance[0]


def test_main_exits_zero_on_the_live_agent_files(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The success path through `main()`, not `check()`.

    Paired deliberately with the failure case above: an exit code asserted only
    on the red path is satisfied by a `main()` that returns 1 unconditionally.

    The banner names each of the three things it checked, and all three are
    asserted. `"conform"` alone was not enough — review replaced the whole
    message with an unrelated literal that merely contained that word, and the
    suite stayed green. Literals, not the module's constants, so the oracle
    cannot drift with the implementation it is checking.
    """
    assert check_reviewer_agents.main() == 0
    banner = capsys.readouterr().out
    assert "conform" in banner
    assert "isolation" in banner, banner
    assert ".claude/reviewer-isolation.md" in banner, banner
    assert "Acceptance:" in banner, banner

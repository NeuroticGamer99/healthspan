"""The ADR-0068 reviewer-agent gate (scripts/check_reviewer_agents.py).

The gate exists because the `isolation: worktree` prohibition was stated in
three prose locations and enforced by nothing. A gate that cannot fail is the
same defect one layer out, so each assertion below breaks exactly one
precondition and requires the gate to notice — the live-repo case alone would
stay green if `check()` returned `[]` unconditionally.
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
"""


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

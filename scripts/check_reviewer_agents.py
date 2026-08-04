#!/usr/bin/env python3
"""Verify the reviewer agent files still obey ADR-0068's isolation rules.

ADR-0068 forbids one specific edit — adding ``isolation: worktree`` to
``.claude/agents/spec-reviewer.md`` or ``.claude/agents/test-reviewer.md`` —
and the prohibition was written into three prose locations (the ADR plus a
bolded paragraph atop each agent file) with nothing checking any of them.
Three copies of an unenforced rule is the drift shape the ADR itself
criticises the serialization discipline for.

The failure the rule prevents is undetectable from the output. The harness's
built-in worktree feature checks out **committed** tracked files only, while
these reviewers' subject is the **uncommitted** tree — so an agent launched
that way reviews a change containing none of the work and reports clean. On a
branch whose HEAD equals its base, which is how this work item was developed,
that is a *guaranteed* false pass: the reviewers would find nothing because
there is nothing there, and say so in the same words they use for real
convergence.

Two assertions, both cheap:

1. no ``isolation`` key in either agent file's YAML frontmatter;
2. both files still point at ``.claude/reviewer-isolation.md``, which owns the
   mechanism that replaces it — a prohibition with no pointer to the
   alternative is how the next author concludes the feature was simply
   forgotten.

Frontmatter is hand-parsed (stdlib has no YAML module) and only to the depth
these files use: a ``---`` fenced block of top-level ``key: value`` lines. A
file with no frontmatter fails rather than passing vacuously — that shape
means the agent definition itself is broken, and a check that treats
"unparseable" as "clean" is the false pass this script exists to stop.

Exit 0 when both files conform; 1 with one line per violation otherwise.
Stdlib only; files are read as UTF-8.
"""

from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
AGENT_DIR = REPO_ROOT / ".claude" / "agents"
AGENT_FILES = ("spec-reviewer.md", "test-reviewer.md")

# The key that must never appear. Checked by name rather than against the
# literal `isolation: worktree`, because every value is wrong here: the rule is
# that this harness feature does not decide these agents' trees at all.
FORBIDDEN_KEY = "isolation"

# The document that owns the replacement mechanism.
PROCEDURE_DOC = ".claude/reviewer-isolation.md"

FENCE = "---"


def parse_frontmatter(text: str) -> dict[str, str] | None:
    """Top-level ``key: value`` pairs from a leading ``---`` block, or None.

    None means "no frontmatter block", which callers must treat as a failure
    rather than as an empty mapping.
    """
    lines = text.splitlines()
    if not lines or lines[0].strip() != FENCE:
        return None
    fields: dict[str, str] = {}
    for line in lines[1:]:
        if line.strip() == FENCE:
            return fields
        if not line.strip() or line.startswith((" ", "\t")):
            continue  # blank, or a continuation of the previous value
        key, sep, value = line.partition(":")
        if sep:
            fields[key.strip()] = value.strip()
    return None  # unterminated block


def check() -> list[str]:
    errors: list[str] = []
    for name in AGENT_FILES:
        path = AGENT_DIR / name
        if not path.is_file():
            errors.append(f"{name}: missing from {AGENT_DIR}")
            continue
        text = path.read_text(encoding="utf-8")
        fields = parse_frontmatter(text)
        if fields is None:
            errors.append(f"{name}: no parseable YAML frontmatter block")
            continue
        if FORBIDDEN_KEY in fields:
            errors.append(
                f"{name}: frontmatter sets '{FORBIDDEN_KEY}: "
                f"{fields[FORBIDDEN_KEY]}' — ADR-0068 forbids it. A built-in "
                "worktree carries committed state only, so this reviewer would "
                "read a tree with none of the uncommitted work under review and "
                "report clean"
            )
        if PROCEDURE_DOC not in text:
            errors.append(
                f"{name}: does not cite {PROCEDURE_DOC}, which owns the "
                "isolation mechanism this agent is launched with"
            )
    return errors


def main() -> int:
    errors = check()
    if errors:
        print(f"reviewer agent definitions violate ADR-0068 ({len(errors)}):")
        for error in errors:
            print(f"  - {error}")
        return 1
    print(
        f"reviewer agent definitions conform: no '{FORBIDDEN_KEY}' key, both "
        f"cite {PROCEDURE_DOC}."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())

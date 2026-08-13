#!/usr/bin/env python3
"""Verify the reviewer agent files still hold their required invariants.

**Two ADRs own what this gate checks, and it is named for neither.** ADR-0068
supplies assertions 1 and 2; ADR-0076 supplies assertion 3. The gate was called
"ADR-0068's isolation rules" — here, in ``run_gates.py``'s summary, in its CI
step name, and in the failure banner below — for as long as that was the whole
of it. All four were widened when it stopped being, because a gate that can fail
for a reason its own name excludes teaches the reader to misfile the failure.

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

Three assertions, all cheap:

1. no ``isolation`` key in either agent file's YAML frontmatter;
2. both files still point at ``.claude/reviewer-isolation.md``, which owns the
   mechanism that replaces it — a prohibition with no pointer to the
   alternative is how the next author concludes the feature was simply
   forgotten;
3. ``test-reviewer.md`` still names its ``Acceptance:`` report field, the line
   requiring each finding to state the mutation the *remedy* must survive
   (ADR-0076).

Assertion 3 is a different species from the first two and is deliberately
scoped to one file. ``spec-reviewer`` is read-only by mandate and routes
enforcement questions to this agent, so it has no mutation to state and does
not carry the field. What is pinned is the field's *name*, which is the
contract a report consumer reads — the same kind of stable identifier as the
path in assertion 2, not the prose around it, and not one markdown rendering
of it either (see ``ACCEPTANCE_FIELD``).

**What assertion 3 does not prove.** It reads the instruction in the agent
file; it cannot observe a report. An agent that carries the instruction and
omits the field anyway fails nothing here. It is worth having regardless,
because the reader harmed by a silent deletion — a fixer handed a finding with
no criterion — is exactly the reader who cannot notice a field they never knew
to expect.

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

# The report field naming the mutation a finding's remedy must survive, and the
# one file obliged to carry it. `spec-reviewer` is read-only by mandate — it
# runs no mutation, so it has nothing to state here.
#
# Deliberately unbolded. The agent file writes this name in bold, as a code
# span, and bare inside `Acceptance: none — <why>`; the ADR adds a bold code
# span. Every one of them is the same field, so pinning `**Acceptance:**` bound
# the gate to one rendering of the name rather than to the name: it did not
# match the `none` spelling at all, and a cosmetic markdown change — bold to
# code span, the ordinary way a field name gets normalized — would have
# reddened the gate with nothing substantive altered.
ACCEPTANCE_FIELD = "Acceptance:"
ACCEPTANCE_FILE = "test-reviewer.md"

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
        if name == ACCEPTANCE_FILE and ACCEPTANCE_FIELD not in text:
            errors.append(
                f"{name}: does not name its '{ACCEPTANCE_FIELD}' report field "
                "— every finding must state the mutation its remedy has to "
                "survive, so the fixer who did not write the finding is still "
                "handed a pass/fail criterion for the repair"
            )
    return errors


def main() -> int:
    errors = check()
    if errors:
        print(
            "reviewer agent definitions violate their required invariants "
            f"({len(errors)}):"
        )
        for error in errors:
            print(f"  - {error}")
        return 1
    print(
        f"reviewer agent definitions conform: no '{FORBIDDEN_KEY}' key, both "
        f"cite {PROCEDURE_DOC}, {ACCEPTANCE_FILE} names its "
        f"'{ACCEPTANCE_FIELD}' field."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())

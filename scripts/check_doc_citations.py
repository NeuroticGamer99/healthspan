#!/usr/bin/env python3
"""Verify that every caller citing an owning harness document still cites it.

ADR-0073 replaces restatement with citation: a rule written five times in five
vocabularies becomes one statement plus five pointers to it. That trade removes
content drift — one copy of the rule is left — and introduces exactly one
failure in its place. A caller gets rewritten, its prose keeps the behaviour in
different words, and the pointer to the single source of truth quietly
disappears. What remains looks correct and is governed by nothing. That failure
is enumerable and textual, which is why this file exists; ADR-0073 §3 owns the
reasoning, including why the judgement half stays ungated.

Two assertions per owning document:

1. the owning document exists. A rename or deletion leaves every citation
   dangling and nothing else in CI notices, because the citations are code
   spans rather than markdown links — so ``scripts/check_spec_links.py`` never
   sees them. **That is the tradeoff the code-span form buys, and it is not
   free**: markdown-link citations would have been covered by the link gate for
   *every* file including unregistered ones, closing the discovery blind spot
   below. They are not used because a relative link spells the target
   differently from each directory (``../../operator-handoff.md`` from a skill),
   so there would be no single string to search for, and assertion 2 — the half
   that catches a *dropped* citation rather than a *deleted target* — needs one.
   A section-scoped needle needs it doubly.
2. every caller listed for it still cites it: the document's repo-relative path,
   plus any extra needles registered for that caller.

**Extra needles exist because path presence alone can be satisfied by
accident.** Measured: ``.claude/skills/apply-review/SKILL.md`` already contained
``.claude/bot-review-triage.md`` before ADR-0073, in an unrelated §4 reference,
so registering it for §1's peer-search rule gated nothing — the entire cited
step could be deleted and this script still exited 0. A needle naming something
only the intended citation carries closes that. A row whose caller mentions the
document for one reason only needs none.

A caller that has gone missing fails rather than being skipped. A check that
reads "absent" as "compliant" is the false pass this gate exists to prevent, and
a renamed caller is exactly when a citation is most likely to have been lost. It
is reported once per caller rather than once per (document, caller) pair: one
cause, one line.

Registering a new caller is a manual step, and this script cannot catch a *new*
citing site that forgets both the pointer and its own row below — the same blind
spot ``check_reviewer_agents.py`` has for a hypothetical third reviewer agent.
The gate covers drift in a known set, not discovery.

Exit 0 when every citation holds; 1 with one line per violation otherwise.
Stdlib only; files are read as UTF-8.
"""

from __future__ import annotations

import io
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

# Owning document -> {caller: extra needles that must also appear}.
#
# Read the caller side as "who defers to this document", not "who mentions it".
# A file that names one in passing does not belong here; a file whose
# instructions are incomplete without it does.
#
# An empty needle tuple means the document's path alone identifies the citation.
# Add a needle wherever the caller could carry that path for an unrelated
# reason — see the module docstring for the measured case.
CITATIONS: dict[str, dict[str, tuple[str, ...]]] = {
    # ADR-0073 rule 1 — how a session hands the operator a path or command.
    # The document is new, so in every caller the path appears only as this
    # citation; no needles needed.
    ".claude/operator-handoff.md": {
        ".claude/skills/land/SKILL.md": (),
        ".claude/skills/review-prep/SKILL.md": (),
        ".claude/skills/review-handoff/SKILL.md": (),
        ".claude/skills/apply-review/SKILL.md": (),
        ".claude/skills/ship/SKILL.md": (),
    },
    # ADR-0073 rule 2 — the peer-site search a finding obliges before a fix.
    # `/apply-review` cites §4 of this same document elsewhere, for an unrelated
    # rule that predates ADR-0073, so the path alone proves nothing about §1.
    # The bullet's bold lead-in, "Under-reporting", is what only the
    # peer-search citation carries — not the §1 heading text, which is
    # "Triage each finding" and appears in neither file.
    ".claude/bot-review-triage.md": {
        ".claude/skills/apply-review/SKILL.md": ("Under-reporting",),
    },
    # The local gate runner. The four skill callers used to restate CI's gate
    # commands and pinned versions; measured, two of those copies had already
    # drifted apart, and `/land`'s named tool invocations that do not exist in
    # this project. `CLAUDE.md` converted nothing — it is a pointer added where
    # the runner had no discoverability of its own, so the selection rule is
    # "cites the runner", not "used to restate it". The runner derives the
    # pinned versions from ci.yml; the commands themselves are its own.
    #
    # No needles: the script is new, so in every caller the path appears only as
    # this citation. What a caller may *not* do is re-add a command list beside
    # the pointer — that is judgement, and stays ungated for the reasons
    # ADR-0073 §3 gives.
    "scripts/run_gates.py": {
        ".claude/skills/land/SKILL.md": (),
        ".claude/skills/ship/SKILL.md": (),
        ".claude/skills/apply-review/SKILL.md": (),
        ".claude/skills/wi/SKILL.md": (),
        "CLAUDE.md": (),
    },
}


def check() -> list[str]:
    errors: list[str] = []
    # caller -> contents, read once however many rows cite it
    texts: dict[str, str] = {}
    missing_callers: set[str] = set()

    for doc, callers in CITATIONS.items():
        if not (REPO_ROOT / doc).is_file():
            errors.append(
                f"{doc}: owning document is missing — every citation to it "
                "dangles, and the link-check gate cannot see them because "
                "they are code spans rather than markdown links"
            )
            continue
        for caller, needles in callers.items():
            if caller not in texts:
                path = REPO_ROOT / caller
                if not path.is_file():
                    # Once per caller, not once per row citing it: a caller
                    # registered under two documents is one cause, one line.
                    if caller not in missing_callers:
                        missing_callers.add(caller)
                        errors.append(f"{caller}: missing from the repository")
                    continue
                texts[caller] = path.read_text(encoding="utf-8")

            text = texts[caller]
            if doc not in text:
                errors.append(
                    f"{caller}: no longer cites {doc}, which owns the rule "
                    "this file defers to. Restating the rule here instead is "
                    "the drift ADR-0073 replaced with a citation"
                )
                continue
            for needle in needles:
                if needle not in text:
                    errors.append(
                        f"{caller}: cites {doc} but not its {needle!r} rule — "
                        "the bare path is also carried by an unrelated "
                        "reference, so it cannot show this citation survived "
                        "(ADR-0073 §3)"
                    )
    return errors


def main() -> int:
    errors = check()
    if errors:
        print(f"harness document citations broken ({len(errors)}):")
        for error in errors:
            print(f"  - {error}")
        return 1
    pairs = sum(len(callers) for callers in CITATIONS.values())
    print(
        f"harness document citations hold: {pairs} citations across "
        f"{len(CITATIONS)} owning documents."
    )
    return 0


if __name__ == "__main__":
    # Inside the `__main__` guard on purpose; scripts/check_spec_links.py
    # documents the measured reasons, and they govern. Needed here because
    # every line this gate prints names a path.
    for stream in (sys.stdout, sys.stderr):
        if isinstance(stream, io.TextIOWrapper):
            stream.reconfigure(encoding="utf-8", errors="replace")
    sys.exit(main())

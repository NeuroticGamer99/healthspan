#!/usr/bin/env python3
r"""Own the review ledger's on-disk grammar: allocate, collapse, verify (ADR-0072).

The ledger is one artifact with two file kinds, both under
``specs/reviews/angle-ledger/`` and both named from *identity* rather than from
write time (ADR-0072 sec.8):

  - ``branches/<b6>/round-<N>.md`` -- one fragment per external review round,
    created by ``/review-brief`` when it allocates ``N``, filled by
    ``/review-handoff``, amended by ``/apply-review``. ``<b6>`` is the first six
    hex of sha256 over the branch name.
  - ``digests/<PR//100>/pr<PR>.md`` -- one digest per merged PR, composed by
    ``/squash-merge`` from that branch's fragments, which are then deleted.

Three subcommands, one module, because they are three ends of *one* contract and
splitting them is how two representations drift apart (the failure ADR-0072 sec.7
records happening three times to one document):

  ``next-round``     what ``/review-brief`` allocates. Reads both the fragments
                     on disk *and* the rounds a digest already captured.
  ``collapse``       what ``/squash-merge`` runs before merging.
  ``check-collapsed`` the CI gate: ``main`` carries no uncollapsed fragment.

**Why the allocator reads digests.** ``max(existing fragments) + 1`` alone
restarts at 1 once a collapse empties the branch's directory, re-issuing numbers
the digest already holds -- named in ADR-0072 sec.10 among the rules "enforced by
nothing", and raised on PR #95 by CodeRabbit as two separate Major findings.
The digest records the rounds it captured, so the allocation survives its own
collapse. That is the ``open-questions.md`` entry "When does a ledger allocation
become durable?", whose recorded trigger is this work item.

**Why the collapse refuses rather than assumes.** ADR-0072 sec.8's crash-recovery
rule -- an existing digest is authoritative and is never recomposed; a re-run
finishes the deletion and stops -- is safe only under the invariant that no new
fragment joins a branch's directory once its digest exists. Nothing enforced
that. Here the recovery path *checks* it: a fragment the digest did not capture
stops the run instead of being deleted, because deleting it is the data loss the
invariant existed to prevent.

**Clean by construction, permanently** (ADR-0072 sec.8). A generator that emits
lint-dirty markdown converts a one-time cleanup into a recurring one, so the
composed digest is validated against the structural rules the gates enforce
before it is written, and the run fails rather than emitting something a later
gate will reject. Inlining -- never linking -- is likewise not a style choice:
``scripts/check_spec_links.py`` resolves every relative markdown link, and the
fragments are deleted by the same run that writes the digest.

Exit 0 on success (including "nothing to do"), 1 on any refusal **or
environment failure** -- the two are different things and `main` reports them
separately, but both are exit 1, because every caller branches on the status
rather than on the kind. The answer
goes to stdout and the reason for a refusal to stderr, because `/review-brief`
captures stdout (``n=$(... next-round)``) and a refusal printed there becomes the
round number. Stdlib only; every file is read and written as UTF-8 without a BOM and
with LF line endings, per ``CLAUDE.md`` sec. PowerShell file encoding.
"""

from __future__ import annotations

import argparse
import hashlib
import io
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

LEDGER_REL = "specs/reviews/angle-ledger"
BRANCHES_REL = f"{LEDGER_REL}/branches"
DIGESTS_REL = f"{LEDGER_REL}/digests"

# Fragments are `round-<N>.md` and nothing else lives beside them; an unexpected
# file stops a collapse rather than being silently dropped or silently inlined.
# `[1-9][0-9]*` rather than `\d+`: `\d` matches non-ASCII decimal digits, and a
# leading zero would map `round-01.md` and `round-1.md` to one round number --
# measured, they produced a digest reading `Rounds captured: 1, 1` with round 1
# inlined twice. Anything else in the directory is refused by name below.
FRAGMENT_RE = re.compile(r"^round-([1-9][0-9]*)\.md$")

# The two machine-read lines of a digest header. They are the whole contract
# between the collapse (writer) and the allocator (reader).
DIGEST_HASH_RE = re.compile(r"^- \*\*Branch hash:\*\* `([0-9a-f]{6})`$", re.MULTILINE)
DIGEST_ROUNDS_RE = re.compile(r"^- \*\*Rounds captured:\*\* (.+)$", re.MULTILINE)

# One captured round: its number, and the fingerprint of the text the digest
# inlined for it. `[^`]+` rather than a hex class so a malformed fingerprint is
# reported by the comparison that uses it rather than silently reread as absent.
ROUND_ENTRY_RE = re.compile(r"^(?P<n>[^\s(]+) \(`(?P<fp>[^`]+)`\)$")

ATX_HEADING_RE = re.compile(r"^(?P<indent> {0,3})(?P<hashes>#{1,6})(?P<rest>\s.*|)$")

# A setext underline: `=` makes the line above a level-1 heading, `-` a level-2.
# The callers additionally require the line above to be non-blank, since a rule
# above a blank line is a thematic break rather than an underline.
#
# A multi-column table's delimiter row is excluded by the pattern itself:
# `|---|---|` contains pipes and so never matches. The callers additionally
# require the line above to contain no `|`, and that guard is *not* redundant
# with the pattern, which is what one round of review concluded and the next
# measured to be wrong. A single-column GFM table wraps its header in pipes and
# may leave the one-cell delimiter row bare:
#
#     | Header |
#     ---
#
# The delimiter row matches the pattern, the header row does not contain it, and
# without the guard that fragment is refused as a setext heading. Deleting the
# guard on the reasoning that CommonMark reads this as a heading ignores that
# GFM reads it as a table, and this corpus is GFM.
#
# The tradeoff, and its exact extent: the guard can miss a genuine level-2
# setext heading beneath a paragraph line that happens to contain a pipe. It
# gives up nothing else — it is scoped to dash underlines, because a GFM
# delimiter row is dash-only, so `=` under a pipe-bearing line stays detected.
# What remains costs a markdown-lint failure on the PR — loud, and before the
# merge. Refusing a valid table instead stops `/squash-merge` after every check
# is already green, which is the worse of the two and the harder to diagnose.
#
# `-{2,}` is a deliberate divergence from CommonMark, which makes a lone `-`
# beneath a paragraph a level-2 setext heading. Here it is far likelier to be an
# empty list bullet in a generated fragment, and the two errors are not
# symmetric: missing a setext heading fails the markdown-lint gate on the PR,
# loudly and before the merge, while a false refusal stops `/squash-merge` after
# every check is already green.
SETEXT_UNDERLINE_RE = re.compile(r"^ {0,3}(?:=+|-{2,})[ \t]*$")
FENCE_RE = re.compile(r"^ {0,3}(?P<char>`{3,}|~{3,})(?P<info>.*)$")

# A digest bucket holds a hundred PRs, so no digest directory ever exceeds a
# hundred entries at any merge rate (ADR-0072 sec.8).
DIGEST_BUCKET = 100


class LedgerError(Exception):
    """A refusal: the reason is the message, and the caller prints it."""


# ---------------------------------------------------------------------------
# Paths and identity
# ---------------------------------------------------------------------------


def branch_hash(branch: str) -> str:
    """First six hex of sha256 over the branch name, with no trailing newline.

    The name is hashed rather than used as a path component because a branch
    name contains ``/`` and, measured on this platform, normalizes differently
    across APIs when used as one (ADR-0072 sec.8, sec.9).

    This is the only implementation. ``/review-brief`` computed the same value
    in shell (``printf '%s' "$branch" | shasum -a 256 | cut -c1-6``) until a
    review pointed out that one identity rule in two languages is a rule that
    can disagree -- over a non-UTF-8 shell encoding, or a branch changed between
    the two invocations -- and the cost of disagreeing is a fragment written
    where the allocator never looked. The skill now asks for the directory. The
    trailing-newline detail is why ``printf`` rather than ``echo`` was correct
    there and why nothing here appends one: hashing the newline yields a
    different directory for the same branch.
    """
    return hashlib.sha256(branch.encode("utf-8")).hexdigest()[:6]


def fragment_dir(root: Path, b6: str) -> Path:
    return root / BRANCHES_REL / b6


def digest_path(root: Path, pr: int) -> Path:
    return root / DIGESTS_REL / str(pr // DIGEST_BUCKET) / f"pr{pr}.md"


def current_branch(root: Path) -> str:
    """The checked-out branch, or a refusal on a detached HEAD.

    ``symbolic-ref`` and not ``rev-parse --abbrev-ref``, which prints the
    literal string ``HEAD`` when detached -- every such session would then hash
    the same string and share one ledger directory, and a per-branch collapse
    would sweep up rounds belonging to unrelated states. ``/review-brief``
    carries the same spelling for the same reason.
    """
    proc = subprocess.run(
        ["git", "symbolic-ref", "--quiet", "--short", "HEAD"],  # noqa: S607
        cwd=root,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    if proc.returncode != 0 or not proc.stdout.strip():
        raise LedgerError(
            "detached HEAD: no branch to key the ledger on. "
            "Check out the branch the round belongs to, or pass --branch."
        )
    return proc.stdout.strip()


def _git(root: Path, *args: str) -> str:
    # S603 is about shell injection, and the answer to it is the list form: argv
    # is a list built here, never a shell string. "Fixed" would be the wrong word
    # -- `*args` comes from this function's callers -- and the distinction is
    # worth the extra line, since `tests/test_ledger.py` records that a
    # destructive verb passed by a future caller is how the ledger's read/write/
    # delete guard gets bypassed without that test seeing it.
    proc = subprocess.run(  # noqa: S603 - argv is a list, never a shell string
        ["git", *args],  # noqa: S607
        cwd=root,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    if proc.returncode != 0:
        raise LedgerError(
            f"`git {' '.join(args)}` failed ({proc.returncode}): "
            f"{proc.stderr.strip() or proc.stdout.strip()}"
        )
    return proc.stdout


# ---------------------------------------------------------------------------
# Reading what is on disk
# ---------------------------------------------------------------------------


def read_fragments(directory: Path) -> list[tuple[int, Path]]:
    """Every ``round-<N>.md`` in a branch directory, ascending by ``N``.

    Any other entry stops the caller: the collapse inlines what it finds and
    then deletes it, so an unrecognized file would be destroyed without ever
    reaching the digest.
    """
    if not directory.is_dir():
        return []
    found: list[tuple[int, Path]] = []
    unexpected: list[str] = []
    for entry in sorted(directory.iterdir()):
        match = FRAGMENT_RE.match(entry.name) if entry.is_file() else None
        if match is None:
            unexpected.append(entry.name)
            continue
        found.append((int(match.group(1)), entry))
    if unexpected:
        raise LedgerError(
            f"{directory} holds entries that are not round fragments: "
            f"{', '.join(unexpected)}. A collapse deletes this directory, so it "
            "refuses to run while it holds anything it cannot inline."
        )
    return sorted(found)


def fingerprint(inlined: str) -> str:
    """A round's content identity: sha256 over the exact text the digest holds.

    Twelve hex characters over the *demoted* fragment text -- the bytes
    ``compose_digest`` inlines, not the file on disk -- so the same function
    answers for the writer and for the re-entry check that compares against it.
    """
    return hashlib.sha256(inlined.encode("utf-8")).hexdigest()[:12]


def parse_digest(text: str) -> tuple[str, dict[int, str]]:
    """The branch hash, and each captured round mapped to its content fingerprint.

    One header line carries both, deliberately. An earlier version recorded the
    round numbers alone, which let the re-entry check ask only "is this round
    captured" -- and a round number cannot distinguish a fragment from the same
    fragment amended afterwards. Recording the numbers on one line and the
    fingerprints on another would give this file two representations of one
    fact, which is the drift ADR-0072 sec.7 documents happening three times to
    one document; so it is one line, and the round number stays scannable.
    """
    hash_match = DIGEST_HASH_RE.search(text)
    rounds_match = DIGEST_ROUNDS_RE.search(text)
    if hash_match is None or rounds_match is None:
        raise LedgerError(
            "digest is missing its `Branch hash:` or `Rounds captured:` header "
            "line; those two lines are the whole contract between the collapse "
            "and the allocator (ADR-0072 sec.8) and cannot be reconstructed."
        )
    captured: dict[int, str] = {}
    highest = 0
    for part in rounds_match.group(1).split(","):
        entry = ROUND_ENTRY_RE.match(part.strip())
        if entry is None:
            raise LedgerError(
                f"digest records an unreadable round entry {part.strip()!r}; "
                "`Rounds captured:` is a comma-separated ascending list of "
                "`<n> (`<fingerprint>`)` entries."
            )
        number = entry.group("n")
        # `.isdecimal()`, not `.isdigit()`: the two differ on exactly the
        # characters `int()` refuses -- `'²'.isdigit()` is True while
        # `int('²')` raises ValueError, so an `isdigit` guard turns a
        # refusal into a traceback. `scripts/check_personal_containment.py`
        # carries the same spelling for the same measured reason.
        if not number.isdecimal():
            raise LedgerError(
                f"digest records an unreadable round number {number!r}; "
                "`Rounds captured:` is a comma-separated ascending list."
            )
        # Strictly ascending, which is what three of this file's own error
        # strings and ADR-0072 §8 already assert and what `compose_digest`
        # writes, since `read_fragments` returns its fragments sorted. Nothing
        # checked it. Measured: `1 (`aa`), 1 (`bb`)` parsed to `{1: 'bb'}` with
        # the first entry silently dropped by the assignment below, and
        # `3 (`aa`), 1 (`bb`)` parsed without complaint. Either leaves the
        # re-entry check comparing a fragment against a fingerprint that is not
        # the one the digest inlined for it -- a false refusal in one direction
        # and, in the mirror case, a divergent fragment matching the wrong
        # entry and being deleted.
        value = int(number)
        if value <= highest:
            raise LedgerError(
                f"digest records round {value} after round {highest}; "
                "`Rounds captured:` is a comma-separated ascending list, and a "
                "repeated or out-of-order entry cannot be matched to a fragment."
            )
        highest = value
        captured[value] = entry.group("fp")
    return hash_match.group(1), captured


def digests_for_branch(root: Path, b6: str) -> list[tuple[Path, dict[int, str]]]:
    """Every digest that captured rounds from this branch hash.

    Normally one. More than one means the branch name was reused across PRs,
    which is legitimate and which the allocator must account for rather than
    take the first answer it finds.
    """
    digests_root = root / DIGESTS_REL
    if not digests_root.is_dir():
        return []
    out: list[tuple[Path, dict[int, str]]] = []
    for path in sorted(digests_root.rglob("pr*.md")):
        recorded, rounds = parse_digest(_read_text(path))
        if recorded == b6:
            out.append((path, rounds))
    return out


# ---------------------------------------------------------------------------
# Composing the digest
# ---------------------------------------------------------------------------


def scan_fences(markdown: str) -> tuple[list[tuple[str, bool]], bool]:
    """Each line paired with whether it is fenced, plus whether a fence is open.

    Returns ``(lines, unclosed)``. The flag is returned rather than raised
    because the two callers want different things from it: the demoter treats an
    unclosed fence as content it must not touch, while the validator reports it
    as a defect in the composed document.

    One implementation, two consumers. ``demote_headings`` and
    ``validate_digest`` both need to know what is fenced, and they carried the
    same open/close state machine twice until a review pointed out that a fix to
    one -- MD012's fence-blindness was the fix in question -- has to be made
    twice or the two disagree about what is inside a fence. That is the drift
    this module's own docstring argues against for the digest's grammar; the
    argument applies to its parser too.

    A fence line itself reports as *inside*: it is content for demotion (it is
    not a heading) and content for the blank-line rule (it is not blank).
    """
    scanned: list[tuple[str, bool]] = []
    fence: tuple[str, int] | None = None
    for line in markdown.split("\n"):
        fence_match = FENCE_RE.match(line)
        if fence_match is not None:
            run = fence_match.group("char")
            char, length = run[0], len(run)
            info = fence_match.group("info")
            if fence is None:
                # A backtick fence's info string may not contain a backtick
                # (CommonMark), so a prose line that opens with an inline
                # ``...`` span is prose and not a fence.
                # `scripts/check_spec_links.py`'s `_fence_open` already carries
                # this rule, and the two models disagreed: measured, a line
                # beginning with a triple-backtick inline span opened a phantom
                # fence here, left every heading after it undemoted (`## S`
                # came back `## S`), and reported the fragment as carrying an
                # unclosed fence -- a false refusal reaching `/squash-merge`
                # after every check is green, the worse of the two failures
                # this module's own comments rank. Two fence models for one
                # corpus is the drift the module docstring argues against for
                # the digest's grammar; the argument applies to its parser too.
                if char == "`" and "`" in info:
                    scanned.append((line, False))
                    continue
                fence = (char, length)
            elif char == fence[0] and length >= fence[1] and not info.strip():
                fence = None
            scanned.append((line, True))
            continue
        scanned.append((line, fence is not None))
    return scanned, fence is not None


def _is_setext(line: str, previous: str) -> bool:
    """Whether ``line`` underlines ``previous`` into a heading."""
    match = SETEXT_UNDERLINE_RE.match(line)
    if match is None or not previous.strip():
        return False
    # The pipe guard applies to dash underlines only. A GFM table's delimiter
    # row is dash-only, so `=` under a pipe-bearing line is never a table and
    # suppressing it would give up level-1 setext detection for nothing. An
    # earlier version applied the guard to both and described the loss as the
    # intrinsic cost of fixing the table false-refusal, which it is not.
    return not (line.lstrip().startswith("-") and "|" in previous)


def _refuse_on_setext(scanned: list[tuple[str, bool]]) -> None:
    """Refuse a fragment that titles itself with a setext underline.

    ``Round 1`` over ``=======`` is a level-1 heading that neither the demoter
    nor the title count can see, because both model ATX only. A wholly-setext
    fragment is lint-clean on its own (MD003 asks for *consistency*), so nothing
    upstream catches it either -- and the digest it composes into carries two
    level-1 headings and mixed heading styles, failing MD025 and MD003 at the
    moment `/squash-merge` is merging the PR. Refusing here is the whole point
    of validating before the write: the guarantee is that a composed digest
    never reaches a gate that will reject it.
    """
    previous = ""
    for line, in_fence in scanned:
        if not in_fence and _is_setext(line, previous):
            raise LedgerError(
                f"a fragment carries a setext heading ({previous.strip()!r} "
                f"underlined with {line.strip()!r}). Fragments are generated with "
                "ATX headings; this composer models only those, so it would "
                "neither demote nor count it and the digest would fail MD025 and "
                "MD003 at merge time. Rewrite the heading as ATX."
            )
        previous = "" if in_fence else line


def demote_headings(markdown: str) -> str:
    """Shift every ATX heading one level, leaving fenced content untouched.

    A fragment's own title is ``# Round <N>``; inlined verbatim, a digest would
    hold several level-1 headings, which is both MD025 and a lie about the
    document's shape. Fence tracking is not defensive dressing: a fragment
    quotes shell, and a ``# comment`` line inside a fence is content, not a
    heading.
    """
    scanned, unclosed = scan_fences(markdown)
    if unclosed:
        raise LedgerError(
            "a fragment leaves a code fence open. `validate_digest` checks the "
            "*composed* text, where the next fragment's fence closes this one "
            "and the document reports balanced -- measured, round 1's open "
            "fence swallowed round 2's `# Round 2` heading and its entire body "
            "as fenced content, the digest passed both this module's validation "
            "and `pymarkdownlnt`, and the collapse then deleted the fragments. "
            "That round survives only as text inside a code block. Balance the "
            "fence in the fragment."
        )
    _refuse_on_setext(scanned)
    out: list[str] = []
    for line, in_fence in scanned:
        if in_fence:
            out.append(line)
            continue
        heading = ATX_HEADING_RE.match(line)
        if heading is None:
            out.append(line)
            continue
        hashes = heading.group("hashes")
        if len(hashes) >= 6:
            raise LedgerError(
                "a fragment carries a level-6 heading, which cannot be demoted "
                "to make room for the digest's title. Fragments are generated "
                "with a level-1 title and level-2/3 sections; a deeper one means "
                "the fragment layout changed and this composer has not."
            )
        out.append(f"{heading.group('indent')}#{hashes}{heading.group('rest')}")
    return "\n".join(out)


def compose_digest(
    pr: int, b6: str, branch: str, fragments: list[tuple[int, Path]]
) -> str:
    """The digest text: a header the allocator reads, then the inlined rounds."""
    inlined = [(n, demote_headings(_read_text(path).strip())) for n, path in fragments]
    rounds = ", ".join(f"{n} (`{fingerprint(text)}`)" for n, text in inlined)
    body = "\n\n".join(text for _, text in inlined)
    header = "\n".join(
        (
            f"# PR #{pr} review ledger digest",
            "",
            f"- **Branch hash:** `{b6}`",
            f"- **Branch:** `{branch}`",
            f"- **Rounds captured:** {rounds}",
            "",
            "Composed by `scripts/ledger.py collapse` from the round fragments",
            "this branch accumulated, which the same run deleted (ADR-0072 §8).",
            "Their content is **inlined** below and never linked:",
            "`scripts/check_spec_links.py` resolves every relative markdown link,",
            "so a link to a deleted fragment fails CI. The `Branch hash` and",
            "`Rounds captured` lines above are machine-read: the round numbers so",
            "an allocation survives its own collapse, and the fingerprint beside",
            "each — sha256 over the text inlined below for that round — so a",
            "re-entered collapse can tell a fragment it already captured from the",
            "same fragment amended afterwards.",
            "",
        )
    )
    return f"{header}\n{body}\n"


def validate_digest(text: str) -> None:
    """Refuse to emit markdown a gate would reject (ADR-0072 sec.8).

    Structural only -- the rules whose violation a *generator* introduces. The
    inlined content is already gate-clean, because fragments are tracked
    markdown and PyMarkdown lints every tracked file; what a composer can break
    is the seams between them.
    """
    problems: list[str] = []
    scanned, unclosed = scan_fences(text)
    titles = 0
    blank_run = 0
    previous = ""
    depth = 0
    for line, in_fence in scanned:
        if in_fence:
            blank_run = 0
            previous = ""
            continue
        heading = ATX_HEADING_RE.match(line)
        if heading is not None:
            level = len(heading.group("hashes"))
            if level == 1:
                titles += 1
            # MD001: a heading may not skip a level, and `titles` structurally
            # cannot see a violation. A fragment with no `#` of its own demotes
            # to `###` and sits directly under the digest's own `#`; a fragment
            # *with* one also contributes 0 after demotion, so the count is
            # identical either way. Measured: `validate_digest` accepted such a
            # digest and `pymarkdownlnt@0.9.39` under this repo's config
            # rejected it -- "MD001 ... [Expected: h2; Actual: h3]" -- at the
            # moment `/squash-merge` was merging the PR, which is the outcome
            # validating before the write exists to prevent. Fragments are
            # hand-amended by `/apply-review`, so the shape is reachable.
            if depth and level > depth + 1:
                problems.append(
                    f"a heading level jump from h{depth} to h{level} "
                    "(MD001 allows one level at a time)"
                )
            depth = level
        # A setext underline titles the line above it; counted here so the MD025
        # arithmetic sees the same headings PyMarkdown does. `demote_headings`
        # refuses these outright, so reaching this branch means a digest was
        # composed by some path that did not demote.
        if _is_setext(line, previous):
            if line.lstrip().startswith("="):
                titles += 1
            problems.append("a setext heading (MD003 expects atx)")
        # MD012 exempts code blocks, and a fragment quoting captured command
        # output is exactly where a double blank line legitimately appears --
        # this check was a substring scan over the whole document and refused
        # digests PyMarkdown accepts.
        blank_run = blank_run + 1 if not line.strip() else 0
        if blank_run == 2:
            problems.append("consecutive blank lines (MD012)")
        previous = line
    if unclosed:
        problems.append("an unclosed code fence")
    if titles != 1:
        problems.append(f"{titles} level-1 headings (MD025 requires exactly one)")
    if not text.endswith("\n") or text.endswith("\n\n"):
        problems.append("no single trailing newline (MD047)")
    problems = list(dict.fromkeys(problems))
    if problems:
        raise LedgerError(
            "the composed digest would not be lint-clean: "
            + "; ".join(problems)
            + ". Refusing to write it (ADR-0072 §8 requires generated ledger "
            "markdown to be clean by construction)."
        )


# ---------------------------------------------------------------------------
# Subcommands
# ---------------------------------------------------------------------------


def next_round(root: Path, branch: str) -> int:
    """The round number ``/review-brief`` allocates next for this branch."""
    b6 = branch_hash(branch)
    highest = 0
    for number, _ in read_fragments(fragment_dir(root, b6)):
        highest = max(highest, number)
    for _, captured in digests_for_branch(root, b6):
        highest = max(highest, max(captured, default=0))
    return highest + 1


def collapse(root: Path, pr: int, branch: str, base: str) -> str:
    """Fold a branch's fragments into its per-PR digest. Idempotent."""
    b6 = branch_hash(branch)
    directory = fragment_dir(root, b6)
    digest = digest_path(root, pr)
    fragments = read_fragments(directory)

    if digest.exists():
        recorded, captured = parse_digest(_read_text(digest))
        if recorded != b6:
            raise LedgerError(
                f"{digest} records branch hash {recorded}, not {b6} (branch "
                f"{branch!r}). Either the PR number is wrong or the branch was "
                "renamed after its rounds were written; ADR-0072 §8's remedy for "
                "a rename is to move the branch's ledger directory by hand."
            )
        if not fragments and not directory.is_dir():
            return (
                f"already collapsed: {_rel(root, digest)} exists, no fragments remain."
            )
        if fragments:
            # Content, not round numbers, and deliberately not `git status`
            # either. The first version of this check compared numbers, so a
            # re-run deleted a fragment `/apply-review` had amended after the
            # digest was written and its content then existed nowhere. The
            # obvious repair -- reuse the dirty-ledger refusal here -- is wrong
            # and was measured wrong: a crash leaves the earlier deletions
            # uncommitted, so the directory is *expected* to be dirty on this
            # path, and refusing on that blocks the recovery it guards. What
            # matters is whether the digest holds these bytes.
            _refuse_on_uncaptured(root, digest, captured, directory, fragments)
        removed = [_rel(root, path) for _, path in fragments]
        _remove_dir(directory)
        if not removed:
            # `already collapsed`, not `finished an interrupted collapse`: an
            # interrupted collapse is one that wrote the digest and did not
            # finish deleting, so it leaves fragments behind, and this branch is
            # reached only when there are none. The label is operator-facing and
            # the mislabel cost a failing step: `/squash-merge` routes
            # `finished an interrupted collapse` to `git add`/`git commit`, and
            # removing an untracked empty directory stages nothing, so the
            # checklist hit a `git commit` that exits 1 with nothing to commit.
            return (
                f"already collapsed: {_rel(root, digest)} exists, and the "
                "now-empty fragment directory beside it is removed."
            )
        return (
            f"finished an interrupted collapse: {_rel(root, digest)} was already "
            f"complete; removed {len(removed)} fragment(s) it had captured: "
            f"{', '.join(removed)}."
        )

    if not fragments:
        return "nothing to collapse: this branch wrote no round fragments."

    _refuse_on_dirty_ledger(root, directory)
    _refuse_on_foreign_fragments(root, base, directory, fragments)

    text = compose_digest(pr, b6, branch, fragments)
    validate_digest(text)
    digest.parent.mkdir(parents=True, exist_ok=True)
    _write_text(digest, text)
    _remove_dir(directory)
    rounds = ", ".join(str(n) for n, _ in fragments)
    return (
        f"collapsed round(s) {rounds} into {_rel(root, digest)} and removed "
        f"{_rel(root, directory)}."
    )


def check_collapsed(root: Path) -> list[str]:
    """Every uncollapsed fragment reachable from here. Empty is the pass.

    ``root`` is anchored first, because an empty answer from the wrong
    directory is indistinguishable from an empty answer from the right one:
    pointed at any empty directory -- no repository present at all -- this
    returned "collapsed" and exit 0. `scripts/run_gates.py` already has a name
    for that outcome, ``GateResult.EMPTY``, and the reason beside it: a gate
    that examined nothing is not a pass. ``specs/reviews/`` is the anchor
    rather than the ledger directory itself, since the ledger legitimately does
    not exist until a branch has run its first round.
    """
    anchor = root / "specs" / "reviews"
    if not anchor.is_dir():
        raise LedgerError(
            f"{anchor} does not exist, so this gate examined nothing. That is "
            "not a pass: it means --root (or the working directory) does not "
            "point at this repository."
        )
    branches_root = root / BRANCHES_REL
    if not branches_root.is_dir():
        return []
    return sorted(
        str(path.relative_to(root)).replace("\\", "/")
        for path in branches_root.rglob("*")
        if path.is_file()
    )


# ---------------------------------------------------------------------------
# Refusals that protect a collapse
# ---------------------------------------------------------------------------


def _refuse_on_dirty_ledger(root: Path, directory: Path) -> None:
    """No uncommitted fragment may be collapsed.

    An untracked fragment has never reached the branch, so inlining it would
    publish content no review saw and then delete the only copy; a modified one
    would land content the PR's checks never ran against. Both are the
    durability half of ADR-0072 sec.5 -- allocation writes something, but only a
    commit makes it durable.
    """
    rel = _rel(root, directory)
    status = _git(root, "status", "--porcelain", "--", rel).strip()
    if status:
        raise LedgerError(
            f"{rel} has uncommitted changes:\n"
            + "\n".join(f"  {line}" for line in status.split("\n"))
            + "\nCommit them (`/savepoint`) before collapsing: a fragment that "
            "never reached the branch would be inlined from the working tree and "
            "then deleted."
        )


def _refuse_on_uncaptured(
    root: Path,
    digest: Path,
    captured: dict[int, str],
    directory: Path,
    fragments: list[tuple[int, Path]],
) -> None:
    """No fragment is deleted unless the digest holds its exact content.

    ADR-0072 sec.8's recovery rule -- an existing digest is authoritative, so
    the re-run finishes the deletion and stops -- is safe only while every
    fragment still on disk is one the digest already captured. The first
    implementation of this check compared round *numbers*, which cannot see the
    case that actually happens: a fragment whose number the digest holds and
    whose bytes it does not, because `/apply-review` amended it after the
    digest was written. Deleting that is the data loss the rule exists to
    prevent, arrived at by way of the rule itself.

    The comparison is per round and by fingerprint, and the obvious cheaper
    version is wrong. Asking whether a fragment's demoted text appears anywhere
    in the digest is sound forward (captured implies present) and unsound in the
    direction the check actually uses it: two rounds whose demoted bodies
    coincide make round 1's amended content a substring of round 2's captured
    copy, and the run deletes it reporting success. That was measured against a
    working implementation of the substring form, which is why the digest
    records what it captured *per round* rather than only which rounds.

    **The fingerprint alone is not enough, for a reason that is not about
    hashing.** It is read from the digest's *header*, so it certifies only that
    the header describes this fragment -- and a digest whose header landed
    while its bodies did not answers every question this check asks. So the
    demoted text must also be *present* in the digest. That substring test is
    unsound as the sole check, exactly as the paragraph above says; as a
    conjunct with the per-round fingerprint it is sound, because a complete
    digest inlines each round's text verbatim. ``_write_text`` makes the
    truncated digest unreachable in the first place; this is the second half of
    the same fix, and it is the half that also catches a digest truncated by
    something other than this module.

    A fragment the composer cannot *demote* is reported here too, as content
    that cannot be what the digest holds. It reaches this path only by having
    been amended after the digest was written -- composing a setext, level-6 or
    unclosed-fence fragment would have refused -- but ``demote_headings`` raises
    first, and its message tells the operator to rewrite the heading. Following
    that advice edits the surviving fragment, changing the one copy of the
    content the digest does *not* hold, and never surfaces the message saying
    both artifacts are intact and must be reconciled by hand.
    """
    body = _read_text(digest)
    unrecorded = sorted(n for n, _ in fragments if n not in captured)
    amended: list[int] = []
    for n, path in fragments:
        if n not in captured:
            continue
        # `_read_text` is deliberately outside the `try`: a fragment that is not
        # UTF-8 is not "content that differs", and folding its refusal in here
        # would swallow the message naming the file and its encoding.
        text = _read_text(path).strip()
        try:
            inlined = demote_headings(text)
        except LedgerError:
            amended.append(n)
            continue
        if fingerprint(inlined) != captured[n] or inlined not in body:
            amended.append(n)
    amended.sort()
    if not unrecorded and not amended:
        return
    reasons: list[str] = []
    if unrecorded:
        reasons.append(f"round(s) {unrecorded} the digest never captured")
    if amended:
        reasons.append(
            f"round(s) {amended} whose content differs from what the digest holds"
        )
    raise LedgerError(
        f"{_rel(root, digest)} already exists and is authoritative, but "
        f"{_rel(root, directory)} holds {' and '.join(reasons)}. "
        "Refusing to delete them and refusing to recompose the digest: the "
        "first is data loss, the second overwrites a complete digest with one "
        "built from a subset of its own inputs (ADR-0072 §8). Recover by "
        "hand -- the digest and the fragments are both intact."
    )


def _refuse_on_foreign_fragments(
    root: Path, base: str, directory: Path, fragments: list[tuple[int, Path]]
) -> None:
    """Every fragment must be one this branch added (ADR-0072 sec.8's cross-check).

    Per-branch directories make a foreign fragment unlikely rather than
    impossible -- two branches whose names collide in six hex characters share
    one -- and the consequence of guessing wrong is deleting another branch's
    uncollapsed rounds.

    ``<base>...HEAD`` is the three-dot form ADR-0072 sec.8 specifies, and it is
    the correct spelling for "what this branch added". Two things about it are
    measured rather than argued, and the difference between them matters:

    **Measured:** swapping ``...`` for ``..`` here leaves the whole suite green.
    So the tests do not constrain this choice; it rests on sec.8 and on the two
    forms meaning different things, not on a fixture that fails without it. A
    test asserting otherwise was written and deleted for exactly that reason.

    **Not measured, and stated as the weaker claim it is:** the two forms diverge
    only for a path present in *both* the base tip and HEAD, which needs a branch
    name reused across a merge that skipped its collapse, landing content under
    this same ``<b6>``. That is a narrow chain rather than an impossible one. It
    is tempting to call it unreachable because ``main`` carries no fragments --
    but ``ledger-collapsed`` *detects* that after a merge rather than preventing
    it, so the gate cannot make a prior state unreachable. Reading it as if it
    could is the fail-open shape this repository has paid for before.
    """
    rel = _rel(root, directory)
    added = {
        line.strip()
        for line in _git(
            root, "diff", "--name-only", "--diff-filter=A", f"{base}...HEAD", "--", rel
        ).split("\n")
        if line.strip()
    }
    foreign = sorted(
        _rel(root, path) for _, path in fragments if _rel(root, path) not in added
    )
    if foreign:
        raise LedgerError(
            f"{rel} holds fragment(s) this branch did not add: {', '.join(foreign)}. "
            f"The collapse deletes the whole directory, and `{base}...HEAD` says these "
            "came from somewhere else. Refusing rather than sweeping them up."
        )


# ---------------------------------------------------------------------------
# Filesystem helpers -- explicit about encoding and line endings
# ---------------------------------------------------------------------------


def _rel(root: Path, path: Path) -> str:
    return str(path.relative_to(root)).replace("\\", "/")


def _read_text(path: Path) -> str:
    """UTF-8 text, or a refusal naming the file -- never a traceback.

    ``UnicodeDecodeError`` is not a ``LedgerError``, so ``main``'s handler does
    not catch it: a fragment an editor saved as cp1252 -- an em dash written as
    the single byte ``0x97`` -- killed the process with a stack trace naming
    the codec instead of the documented "exit 1 with the reason on stderr".
    ``/review-brief`` and ``/squash-merge`` branch on that exit status. This is
    the same shape the ``.isdecimal()`` guard in ``parse_digest`` closes for
    round numbers, left in place for the file reads. **Every** read in this
    module goes through here, so a new one cannot reopen it —
    ``tests/test_ledger.py`` pins that rather than this sentence asserting it,
    because the sentence carried a count and the count went stale inside the
    round that wrote it.
    """
    try:
        return path.read_text(encoding="utf-8")
    except UnicodeDecodeError as exc:
        raise LedgerError(
            f"{path} is not valid UTF-8: {exc.reason} at byte {exc.start}. "
            "Ledger files are written UTF-8 without a BOM (CLAUDE.md § "
            "PowerShell file encoding); re-save it and run this again."
        ) from exc


def _write_text(path: Path, text: str) -> None:
    """UTF-8 without a BOM, LF endings, installed atomically.

    ``Path.write_text`` opens in text mode, so on Windows it would translate
    every ``\n`` to ``\r\n`` -- against ``.gitattributes`` ``eol=lf``, and the
    digest is em-dash and arrow prose whose bytes the encoding must not touch.

    The staging-file-then-``os.replace`` pair is the same-directory atomic swap
    ADR-0047 §7 records for the crypto sidecar and ``scripts/review_worktree.py``
    uses for its state file, and here it closes a data-loss path rather than an
    untidy one. A bare ``write_bytes`` can leave a digest holding its header --
    the ``Rounds captured:`` line included -- and none of the bodies that follow
    it. Nothing on the re-entry path would notice: ``parse_digest`` reads only
    those two header lines, and the fingerprints match because they were
    written *with* the header, so every fragment is deleted against a digest
    that holds no rounds. A crash mid-write is the exact scenario this module's
    recovery path exists for, so a partial digest must never be reachable under
    the digest's own name.

    **The staging file is removed if the swap fails**, because a failed
    publication leaves bytes that are not the digest and are not recoverable
    state. Left behind, `pr<PR>.md.tmp` sits untracked beside the digests until
    someone runs `/squash-merge`, whose step 2 stages the ledger with
    ``git add -A -- specs/reviews/angle-ledger`` -- so the stray is not merely
    untidy, it rides into the merge commit. Measured before this cleanup
    existed. ``missing_ok`` because the failure may be `os.replace` finding the
    staging file already gone.
    """
    staging = path.with_name(path.name + ".tmp")
    staging.write_bytes(text.encode("utf-8"))
    try:
        os.replace(staging, path)
    except OSError:
        staging.unlink(missing_ok=True)
        raise


def _remove_dir(directory: Path) -> None:
    if directory.is_dir():
        shutil.rmtree(directory)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _positive_int(value: str) -> int:
    number = int(value)
    if number <= 0:
        raise argparse.ArgumentTypeError("must be a positive integer")
    return number


def build_parser() -> argparse.ArgumentParser:
    # `__doc__` is None under `python -OO`, which strips docstrings.
    parser = argparse.ArgumentParser(description=(__doc__ or "").split("\n")[0])
    parser.add_argument(
        "--root",
        type=Path,
        default=REPO_ROOT,
        help="repository root (default: the repository this script lives in)",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    nxt = sub.add_parser(
        "next-round", help="print the round number /review-brief should allocate"
    )
    nxt.add_argument(
        "--branch", help="branch to key the ledger on (default: checked out)"
    )

    col = sub.add_parser(
        "collapse", help="fold this branch's fragments into its PR digest"
    )
    col.add_argument("--pr", type=_positive_int, required=True, help="the PR number")
    col.add_argument(
        "--branch", help="branch to key the ledger on (default: checked out)"
    )
    col.add_argument(
        "--base",
        default="origin/main",
        help="base ref for the provenance cross-check (default: origin/main)",
    )

    frag = sub.add_parser(
        "fragment-dir", help="print this branch's fragment directory, repo-relative"
    )
    frag.add_argument(
        "--branch", help="branch to key the ledger on (default: checked out)"
    )

    sub.add_parser(
        "check-collapsed", help="fail if any uncollapsed round fragment is present"
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    root: Path = args.root
    try:
        if args.command == "check-collapsed":
            stragglers = check_collapsed(root)
            if stragglers:
                print(
                    f"uncollapsed review-ledger fragments ({len(stragglers)}):",
                    file=sys.stderr,
                )
                for path in stragglers:
                    print(f"  - {path}", file=sys.stderr)
                print(
                    "`main` carries one digest per merged PR and no fragments "
                    "(ADR-0072 §8). A merge that skipped `scripts/ledger.py "
                    "collapse` leaves these behind; collapse them in a PR.",
                    file=sys.stderr,
                )
                return 1
            print("review ledger collapsed: no uncollapsed fragments present.")
            return 0

        branch = args.branch or current_branch(root)
        if args.command == "next-round":
            print(next_round(root, branch))
            return 0
        if args.command == "fragment-dir":
            print(f"{BRANCHES_REL}/{branch_hash(branch)}")
            return 0
        print(collapse(root, args.pr, branch, args.base))
        return 0
    except LedgerError as exc:
        # stderr, not stdout: `/review-brief` captures this command's stdout
        # into a shell variable, so a refusal printed there becomes the round
        # number it goes on to name a fragment after. Every sibling script in
        # `scripts/` reports its errors the same way.
        print(str(exc), file=sys.stderr)
        return 1
    except OSError as exc:
        # The docstring promises exit 1 with the reason on stderr for any
        # failure, and every subcommand here touches the filesystem: functions
        # throughout this module call something that raises `OSError` and none
        # of them wraps it. No count is given, deliberately -- the first draft
        # of this comment said "ten", and a reviewer measured 9 or 11 depending
        # on whether a function reaching `OSError` only through `_read_text`,
        # `_write_text` or `_remove_dir` is counted. Both readings are
        # defensible, which is what makes the number the wrong thing to assert;
        # what matters is that every one of them is reachable only from inside
        # this `try`. Measured before this clause existed -- a
        # collapse whose digest path was unreadable died with a
        # `PermissionError` traceback out of `_read_text`, which is the same
        # contract breach the `UnicodeDecodeError` guard there already closes
        # for the decode half. `subprocess.run` is on that list too, so a
        # missing `git` arrives here as `FileNotFoundError` rather than a
        # stack trace.
        #
        # A separate clause rather than `except (LedgerError, OSError)`: a
        # `LedgerError` is a refusal this module composed and its text stands
        # alone, while an `OSError` is the environment failing and reads as
        # `[Errno 13] Permission denied: <path>` -- printing them through one
        # handler would present a full disk as a ledger refusal. Splitting them
        # also keeps this file clear of the bare-comma `except` trap CLAUDE.md
        # documents, since neither clause carries a tuple.
        print(f"filesystem error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    # Inside the `__main__` guard on purpose, matching the seven sibling
    # scripts: `scripts/check_personal_containment.py` records the measured
    # reasons (pytest's capture streams are TextIOWrapper subclasses, so module
    # scope or `main()` would reconfigure the test session's own streams). This
    # module prints paths it found and interpolates git's stderr verbatim, both
    # of which can carry bytes a strict locale cannot encode -- measured here,
    # a `print` of non-cp1252 characters through Git Bash raised
    # UnicodeEncodeError, crashing instead of naming the file it found.
    for stream in (sys.stdout, sys.stderr):
        if isinstance(stream, io.TextIOWrapper):
            stream.reconfigure(encoding="utf-8", errors="replace")
    sys.exit(main())

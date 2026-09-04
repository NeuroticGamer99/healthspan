#!/usr/bin/env python3
"""Reconcile the auto-memory directory against its ``MEMORY.md`` index.

Auto memory has **no concurrency control of any kind** — no locking, no merge,
no conflict detection. Anthropic's own docs describe the layout and say nothing
about simultaneous sessions, and the upstream report of cross-session corruption
(anthropics/claude-code#23769) was auto-closed as a duplicate of a duplicate and
locked, with none of its requested mitigations implemented. Detection is the
only half available at this layer, so this script is a detector, not a guard.

Two failures were measured on this project in a single day, both of them
**orphans** — a memory file written and its index row never added. An orphaned
memory is invisible: ``MEMORY.md`` is what loads at session start, so a file no
row points at is never recalled and the write accomplished nothing. That is
silent by construction, which is why it needs a check rather than care.

Every measurement in this file was taken against the operator's own corpus, so
**no memory of it may be named here**. That is the rule for the next author, not
a certificate that this file already complies: nothing gates the content half of
the containment scan, and a name outlived the very sweep whose record says it
came out (``specs/open-questions.md``). A memory corpus is **treated as**
personal data under ``CLAUDE.md``'s containment rule, and *which* memories exist
is provenance even with no content attached — the same rule that makes
attributing a format quirk to one of the owner's actual sources personal. The
corpus's aggregate **shape** is taken as the publishable half, so the counts
here are stated as counts and the files behind them are not identified.

That second half is not settled, and this docstring is not where it gets
settled: ADR-0078 §4 records the treatment as an inference rather than a
quotation, and ``specs/open-questions.md`` carries what genuinely remains open —
whether counts over the corpus are themselves provenance — with the trigger that
would decide it. The conservative half (never name a memory) is unconditional.

**This script is unlike every other ``check_*.py`` in this repository and the
difference matters.** Its subject lives *outside* the repo, at
``~/.claude/projects/<project>/memory/``, and is machine-local — the docs are
explicit that auto memory is "not shared across machines". So **CI cannot run
it**, it earns no row in the repo-invariants register, and nothing here should
be modelled on ``check_adr_index.py``'s CI-gated shape. It is run on demand —
by a session, or by hand — and gated by nothing.

It was written to run from a ``SessionStart`` hook, and that hook is gone. Four
external review rounds returned 15, 24, 26 and 27 findings with no decline —
each total being 15 reported plus the remainder that round demoted below its own
15-finding cap — and the severity of every early round traced to *hook context*
rather than to the checking logic: output injected into every session, an
exit-code inversion, four quoting layers in a bootstrap, and a ``suppress`` wrap
that silently falsified the mutation table of the ADR proposing it. Ten of round
3's fifteen were in this file either way — dropping the hook removed the blast
radius, not the defects. ADR-0078 holds the counts and how they were derived;
they are repeated here only as far as this file's own argument needs them.

What is lost with the hook is **frequency**: a hook fired unasked, so it
caught the session that wrote a memory and then did nothing else. Nothing does
that now, which ``specs/open-questions.md`` records as the trigger to revisit.

Six checks, and the two that only warn are deliberate:

1. **orphan** — a ``*.md`` file no index row links (error; the measured failure)
2. **dangling** — an index row whose target file is gone (error; a stale index
   sends a later session looking for something that is not there)
2a. **duplicate** — two rows pointing at one memory (error). The other half of
   the concurrent-write failure orphans are the first half of: with no locking,
   two sessions each adding a row for the same memory is as likely as one
   forgetting to.
3. **load limit** — ``MEMORY.md`` past the documented 200-line / 25 KB read
   limit (error). Content beyond it "is not loaded at session start", so rows
   past the cut are silently invisible while the file still looks complete. A
   near-limit file warns instead, because the failure arrives with no other
   signal. Frontmatter and block-level HTML comments are stripped before the
   measurement, matching how the loader measures it.
4. **unresolved wiki-link** — a ``[[name]]`` with no ``name.md`` beside it
   (**warning only, permanently**). The memory convention is that a link to a
   memory that does not exist yet is legitimate — it marks something worth
   writing later. Promoting this to an error would break the convention it is
   meant to serve. Wiki-links inside inline code spans and fenced blocks are
   **not links** — they are how this corpus writes examples, including in the
   memory that specifies this checker — and are skipped.
5. **identity** — a memory's frontmatter ``name:`` must equal its filename stem
   (error). This is the check the first draft lacked, and its absence is what
   made that draft wrong: the convention resolves ``[[name]]`` by the target's
   ``name:`` field while this script resolves by filename, so every divergence
   between the two became a false "dead link". 17 of 90 memories had diverged
   before the 2026-08-19 normalisation. Pinning the invariant is what makes
   *either* resolution rule correct, and keeps them from drifting apart again.

Resolving the memory directory is the one place this can go quietly wrong, so it
fails loudly instead: an unresolvable directory, or an ``autoMemoryDirectory``
setting that would redirect it, stops the run rather than letting the script
reconcile a directory nobody asked about. That is the same lesson
``check_personal_containment.py`` records about deriving a root from
``__file__`` — a checker pointed at the wrong tree reports clean about it.

Exit 0 when the reconciliation ran and found no errors (warnings still exit 0);
1 when it found errors **or could not run**. Stdlib apart from ``markdown-it-py``,
which it reaches through ``check_spec_links.py`` rather than importing itself --
so a missing dependency surfaces through the same one-line exit-1 path as any
other refusal, never as a traceback.
"""

from __future__ import annotations

import argparse
import io
import json
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from types import ModuleType
from typing import cast

INDEX_NAME = "MEMORY.md"

# Documented loader limits: "The first 200 lines of MEMORY.md, or the first
# 25KB, whichever comes first, are loaded at the start of every conversation."
LINE_LIMIT = 200
BYTE_LIMIT = 25 * 1024

# Warn this far into either limit. A file at 100% is already dropping rows, and
# the drop is silent, so the useful signal has to arrive before that.
WARN_FRACTION = 0.8

_GIT = shutil.which("git") or "git"

# `[[name]]` inside memory bodies. Index-row links are matched by `_LINK` below
# and interpreted by `row_target`, which is a separate job: a link is markup, a
# *row* is a claim about the corpus, and reading every link in the document as a
# row is what produced duplicate-row errors against ordinary prose.
_WIKI_LINK = re.compile(r"\[\[([^\]\[]+)\]\]")

# Stripped before measuring, because the loader strips them too.
#
# Frontmatter is an opening `---` line, then key lines, then a closing `---`.
# Requiring that shape rather than pairing with the next `---` anywhere is what
# keeps a document *starting with a thematic break* from having its first
# section deleted before the size measurement: measured, `---\n\nHello\n\n---\n`
# used to consume "Hello", so an index could measure shorter than it loads and
# pass a limit its tail is already falling off.
# The line immediately after the opening `---` must be non-blank; the rest of the
# block may contain anything, blank lines included. That asymmetry is the whole
# discriminator: YAML frontmatter opens straight into a key, while a document
# beginning with a *thematic break* has a blank line there. Requiring every inner
# line to be non-blank -- the first attempt at this -- got the thematic break
# right and then lost the name of any memory whose frontmatter contained a legal
# blank line, reporting it as declaring none. `\r?` throughout because
# `declared_name` is handed *raw* text: `check`'s own reads translate newlines,
# but the guard is on the function, and a CRLF memory whose frontmatter this
# failed to recognise would report as declaring no name at all.
_FRONTMATTER = re.compile(
    r"\A---[ \t]*\r?\n[ \t]*\S[^\n]*\n(?:[^\n]*\n)*?---[ \t]*\r?\n"
)
# The single-line form. There is deliberately no DOTALL sibling: a document-wide
# `<!--.*?-->` pairs an opener quoted in a code span with any later closer and
# deletes everything between, which both walks below now avoid by tracking
# comment state line by line over *masked* lines.
_INLINE_COMMENT = re.compile(r"<!--.*?-->")

# An index *row* is a list item or a table row. Prose that happens to link a
# memory is not a row, and reading it as one produced a "duplicate index row"
# error naming a concurrency incident that never happened.
#
# A space or end-of-line after the marker is required, per CommonMark: without
# it `**bold**` prose matched as a row, and bold lead-ins are this corpus's
# dominant idiom. The indent is uncapped because a *nested* row is still a row;
# capping it at CommonMark's 0-3 turned every indented and wrapped row into a
# false orphan.
_ROW_LINE = re.compile(r"^[ \t]*(?:[-*+]|\d+[.)])(?:[ \t]|$)|^[ \t]*\|")

# `name:` in a memory's frontmatter. The lookahead keeps a CRLF file's \r out of
# the captured name: `declared_name` takes raw text, and a trailing \r is a name
# that diverges from the filename stem while looking identical to a reader.
# Unreachable through `check`, whose reads translate newlines first (measured),
# so `test_declared_name_drops_a_carriage_return` drives the function directly.
#
# Applied to the leading `---` block alone, never the whole file. `re.M` over the
# body returned a `name:` from anywhere -- including inside a code fence -- so a
# memory declaring no frontmatter name was reported as *mismatching* a value it
# never declared, and one whose body happened to carry `name: <its own stem>`
# passed while declaring nothing. The corpus holds a memory about agent
# frontmatter fields, which is exactly the document that carries such examples.
_NAME_FIELD = re.compile(r"^name:[ \t]*(.+?)[ \t]*(?=\r?$)", re.M)

# Only the user-level file has a fixed location. The project-scoped pair is built
# from the resolved root at call time (`_settings_files`) rather than held here,
# because a bare `Path(".claude")` binds to the *process cwd* -- and a caller's
# cwd need not be the project, which is the whole reason `--root` exists. Bound
# to the cwd, the project's own `autoMemoryDirectory`
# redirect went unread on the `CLAUDE_PROJECT_DIR` path, and the script would
# then reconcile the derived default this module's docstring says it must refuse
# to touch; an unrelated `.claude/settings.json` under some other cwd could
# equally abort a healthy run.
_USER_SETTINGS_NAMES = ("settings.json",)
_PROJECT_SETTINGS_NAMES = ("settings.json", "settings.local.json")


def _user_settings() -> tuple[Path, ...]:
    """The user-scope settings files, resolved lazily.

    Lazily because `Path.home()` **raises** when no home directory can be
    resolved from the environment, and at module scope that raise happens on
    *import* -- before `main` is reached, so every guard inside it is
    unreachable and the caller sees a traceback rather than a message. It was
    the only module-scope statement in this file that could raise, and the
    property survives the hook that first made it urgent.
    """
    try:
        home = Path.home()
    except (RuntimeError, OSError):  # fmt: skip
        return ()
    return tuple(home / ".claude" / n for n in _USER_SETTINGS_NAMES)


def _settings_files(root: Path | None) -> tuple[Path, ...]:
    """Every settings file that could redirect the memory directory."""
    user = _user_settings()
    if root is None:
        return user
    return user + tuple(root / ".claude" / n for n in _PROJECT_SETTINGS_NAMES)


class ReconcileError(Exception):
    """A precondition failed, so the reconciliation did not run.

    Distinct from a finding: a finding means the check looked and saw something,
    this means it never looked. Both exit 1; only this one means the result
    cannot be trusted either way.
    """


class ProjectNotFoundError(ReconcileError):
    """No Claude Code project directory exists for this root.

    Its own class because it is the one precondition failure that is *not* a
    fault: it means no memory has ever been written for this project, which is
    the ordinary state of a fresh worktree. `--skip-if-absent` forgives this and
    nothing else -- an absent `memory/` inside an existing project directory is
    the same thing one level down, while a directory that exists and holds
    memories with no `MEMORY.md` stays an error.
    """


@dataclass
class Report:
    errors: list[str] = field(default_factory=list[str])
    warnings: list[str] = field(default_factory=list[str])
    examined: dict[str, int] = field(default_factory=dict[str, int])

    def evidence(self) -> str:
        if not self.examined:
            return "nothing examined"
        return ", ".join(f"{k} {v}" for k, v in self.examined.items())


def project_slug(root: Path) -> str:
    """The directory name Claude Code derives from a project path.

    Observed, not documented: **every character outside ``[A-Za-z0-9-]`` becomes
    a hyphen**, case preserved. ``c:\\Dev\\health\\healthspan`` becomes
    ``c--Dev-health-healthspan``.

    The set was measured 2026-08-20 rather than inferred, because an earlier
    version replaced only ``:``, ``\\`` and ``/`` -- derived from a single
    example path that happened to contain nothing else -- and so mis-derived the
    slug for **every worktree session**, the mode this repository works in most:
    ``…\\.claude\\worktrees\\x`` gave ``…-.claude-worktrees-x`` where the real
    directory is ``…--claude-worktrees-x``. `resolve_memory_dir` then raised,
    the hook printed a fault at every session start, and nothing caught it
    because every end-to-end fixture built its directory *from this function*.

    Measuring it needed pairs matched by something no candidate rule touches --
    the sequence of alphanumeric runs -- so each rule could be scored on pairs
    it had not selected. Against the 7 real projects on this machine whose paths
    could be located that way: replacing only ``:\\/`` reproduced 4, adding
    ``.``, ``_`` and space reproduced 5, and this rule reproduced 7. The two the
    middle rule missed carry ``+`` and ``=``, which is why the set is stated as
    a complement rather than a list -- a list is what was wrong before, and a
    longer list would only have moved the boundary.

    Case is preserved, which is the fragile part on Windows, where the same repo
    reached through a differently-cased path yields a different slug -- hence
    `_case_insensitive_match`.
    """
    return re.sub(r"[^A-Za-z0-9-]", "-", str(root))


def _redirected_by_setting(root: Path | None = None) -> Path | None:
    """The first settings file that sets ``autoMemoryDirectory``, if any.

    **A settings file that exists but cannot be read is a refusal, not a
    `continue`.** Skipping it meant the one case where the redirect is *unknown*
    was treated as the case where it is absent, so the script went on to
    reconcile the derived default -- "a checker pointed at the wrong tree
    reports clean about it", this module's own stated failure, reached through
    the guard written to prevent it. A `settings.json` written by Windows
    PowerShell 5.1 carries a BOM and raises `JSONDecodeError`; a UTF-16 one
    raises `UnicodeDecodeError`, which is a `ValueError` and so escaped this
    handler, `resolve_memory_dir` and `main` alike.

    A *missing* file is still an ordinary skip: absence is knowable, garbage is
    not.
    """
    for path in _settings_files(root):
        try:
            text = path.read_text(encoding="utf-8-sig")
        except FileNotFoundError:
            continue
        except OSError as exc:
            raise ReconcileError(f"cannot read {path}: {exc}") from exc
        except ValueError as exc:
            # A UTF-16 settings file fails at the *read*, not at the parse.
            raise ReconcileError(
                f"{path} is not readable as text ({exc}), so whether it "
                "redirects the memory directory cannot be known -- fix the "
                "file, or pass --memory-dir explicitly"
            ) from exc
        try:
            data = json.loads(text)
        # One clause, because both concrete types are `ValueError` subclasses:
        # `json.JSONDecodeError` for a BOM'd or malformed file, and
        # `UnicodeDecodeError` for a UTF-16 one -- and it was the second that
        # escaped the old handler, since it is neither `OSError` nor a JSON error.
        except ValueError as exc:
            raise ReconcileError(
                f"{path} is unreadable as JSON ({exc}), so whether it redirects "
                "the memory directory cannot be known -- fix the file, or pass "
                "--memory-dir explicitly"
            ) from exc
        if isinstance(data, dict) and cast("dict[str, object]", data).get(
            "autoMemoryDirectory"
        ):
            return path
    return None


def _case_insensitive_match(projects: Path, wanted: str) -> Path | None:
    """The project directory whose name differs from ``wanted`` only in case.

    A separate function so that it can be *tested anywhere*, which the branch it
    replaced could not be. Windows and macOS's default volumes are
    case-insensitive, so ``projects / wanted`` already resolves to a
    differently-cased directory and `resolve_memory_dir` returns before reaching
    this at all — measured 2026-08-19 on this machine's filesystem. The fallback
    is therefore live only where the filesystem is case-sensitive, which of the
    three CI legs is Linux alone, and a test driving it end to end would pass
    everywhere while proving something on one leg. This function does its own
    casefold comparison over ``iterdir`` instead of asking the filesystem, so it
    behaves identically on every platform and a test of it means the same thing
    on each.
    """
    if not projects.is_dir():
        return None
    folded = wanted.casefold()
    for candidate in projects.iterdir():
        if candidate.is_dir() and candidate.name.casefold() == folded:
            return candidate
    return None


def resolve_memory_dir(root: Path) -> Path:
    """The project's memory directory, or a loud failure.

    Never guesses past the point where a guess could be wrong: if the setting
    that relocates memory is present anywhere, this refuses rather than
    reconciling the default location that is no longer in use.

    Raises `ProjectNotFoundError` -- a `ReconcileError` subclass -- when the project
    has no directory at all, so a caller can distinguish "this project has never
    written a memory" from "the reconciliation could not run", and only
    `--skip-if-absent` forgives the first.
    """
    redirect = _redirected_by_setting(root)
    if redirect is not None:
        raise ReconcileError(
            f"{redirect} sets 'autoMemoryDirectory', so the memory directory is "
            "not at the derived default -- pass --memory-dir explicitly rather "
            "than reconciling a directory that is no longer in use"
        )

    projects = Path.home() / ".claude" / "projects"
    derived = projects / project_slug(root)
    if derived.is_dir():
        return derived / "memory"

    # Case-only mismatch: the slug keeps whatever casing the path was spelled
    # with, and on Windows the same repo can be reached both ways.
    matched = _case_insensitive_match(projects, derived.name)
    if matched is not None:
        return matched / "memory"

    # Name the slug looked for and the root searched -- never the siblings. The
    # enumeration this replaced listed every Claude Code project directory on the
    # machine into a message that lands in the session transcript, and those
    # names carry the operator's username and the subject matter of unrelated
    # work. With the slug defect above it fired at every worktree session start,
    # turning "a clean corpus costs zero tokens" into a machine inventory per
    # session. `--memory-dir` is the escape hatch for a genuinely missing slug.
    if projects.is_dir():
        raise ProjectNotFoundError(
            f"no project directory matching {derived.name!r} under {projects} "
            "-- pass --memory-dir explicitly"
        )
    raise ProjectNotFoundError(
        f"{projects} does not exist -- pass --memory-dir explicitly"
    )


def _normalize(stem: str) -> str:
    """A memory name with separator style flattened, for typo detection only.

    ``feedback-some-rule`` and ``feedback_some_rule`` normalize alike, which is
    what separates a mistyped link from a genuine forward reference. Never used
    to *resolve* a link -- only to decide which of the two a miss is.
    """
    return stem.replace("-", "_").casefold()


# The four memory types, which every filename carries as its prefix. A link
# that drops the prefix -- and spells the remainder with hyphens rather than
# underscores -- matched nothing under separator flattening alone, and so was
# reported as a *legitimate forward reference*. Two live links of that shape
# sat in the warning list. A false negative in a checker is worse than a false
# positive: nobody goes looking for what it did not say.
_TYPE_PREFIXES = ("user_", "feedback_", "project_", "reference_")


def _unprefixed(stem: str) -> str | None:
    """`stem` with its leading type prefix removed, or None if it has none."""
    normal = _normalize(stem)
    for prefix in _TYPE_PREFIXES:
        if normal.startswith(prefix):
            return normal[len(prefix) :]
    return None


def _cut_spans(text: str, spans: list[re.Match[str]]) -> str:
    """`text` with each span's column range removed, right to left.

    The ranges come from the *masked* guide and are applied to both the guide
    and the source line, which is what keeps the two the same length as each
    other after the cut. Right to left so an earlier removal cannot shift a
    later span's offsets.
    """
    for match in reversed(spans):
        text = text[: match.start()] + text[match.end() :]
    return text


def loaded_text(raw: str) -> str:
    """`MEMORY.md` as the loader measures it: frontmatter and comments removed.

    Comment state is tracked line by line over *masked* lines, exactly as
    `prose_lines` does and for exactly the same reason. This function kept a
    document-wide `<!--.*?-->` with `re.DOTALL` after `prose_lines` had given
    one up, and it is the worse of the two places to keep it: a `<!--` quoted in
    a code span in any row pairs with any later `-->` and deletes most of the
    index *before the load limit is measured*. Measured on a 190-row index, one
    such row took it from `index-lines 195` -- a near-limit warning -- to
    `index-lines 3` and silence, which is a false pass on the one check whose
    failure this module's docstring says arrives with no other signal.

    Unlike `prose_lines`, the *content* of code is kept: the loader loads it and
    it counts toward the limit, so the masked lines are consulted only to decide
    where a comment begins and ends. Where masking changed a line's length the
    columns no longer correspond, so that line is taken as carrying no marker --
    the loud direction, since keeping a comment costs a few counted bytes while
    dropping live rows is what has to stay impossible. Original line terminators
    are preserved, because a CRLF index is measured in bytes.
    """
    md = _markdown()
    body = _FRONTMATTER.sub("", raw)
    lines = md.LINE_BREAK_RE.split(body)
    breaks = [match.group(0) for match in md.LINE_BREAK_RE.finditer(body)]
    guides = md.code_masked_lines(body, mask_html_blocks=False)
    out: list[str] = []
    in_comment = False
    for index, line in enumerate(lines):
        guide = guides[index] if len(guides[index]) == len(line) else ""
        terminator = breaks[index] if index < len(breaks) else ""
        touched = False
        if in_comment:
            head, close, _rest = guide.partition("-->")
            if not close:
                continue  # the whole line is inside the comment
            in_comment = False
            touched = True
            line = line[len(head) + len(close) :]
            guide = guide[len(head) + len(close) :]
        inline_comments = list(_INLINE_COMMENT.finditer(guide))
        if inline_comments:
            # Cut the *same* columns out of both, rather than running the
            # substitution over each independently. `line` still carries the
            # markers the mask blanked out of `guide` -- a `<!-- -->` written
            # inside a code span is the ordinary case -- so two independent subs
            # remove a different number of characters from each and every column
            # after them is off by the difference. Measured before the fix, on
            # ``` `<!-- a -->` and <!-- b --> tail <!-- open ```: the code span's
            # content was deleted, although this function's contract is that the
            # content of code is *kept* and counted, and the live `<!-- open`
            # opener survived into text documented as having comments removed.
            line = _cut_spans(line, inline_comments)
            guide = _cut_spans(guide, inline_comments)
            touched = True
        head, marker, _rest = guide.partition("<!--")
        if marker:
            in_comment = True
            touched = True
            line = line[: len(head)]
        # A line a comment consumed entirely leaves no line at all, terminator
        # included -- otherwise a block comment would contribute empty rows to
        # the line count the load limit is measured against, which is a false
        # "past the load limit" against a healthy index.
        if touched and not line:
            continue
        out.append(line + terminator)
    return "".join(out)


def _markdown() -> ModuleType:
    """`scripts/check_spec_links.py`, imported rather than copied.

    Deliberately **not** `functools.cache`d, though it is called hundreds of
    times per run and an external round noted the cost. The differential harness
    rebinds `sys.modules["check_spec_links"]` around each side's `check()` so
    the two revisions do not share one parser, and that rebinding only reaches
    the checker because this function re-resolves the name every call. Caching
    would make the first resolution permanent per module object and quietly
    return the harness to comparing a revision with itself -- the exact failure
    it exists to catch, traded for a few milliseconds.

    Four generations of hand-rolled CommonMark parsing lived in this file and
    every one shipped defects: a document-wide span that blanked every link
    between two unpaired backticks; a fence opener admitted at any indent while
    its closer was held to 0-3, so a list-indented fence swallowed the file; a
    run backtracking at ~n^2.7 inside a hook that blocks session start; and then
    a *copy* of the module below, which an external round measured wrong in five
    further places -- including a dropped escape guard, while the record here
    claimed it had been "adopted verbatim".

    The copy's stated justification was that a module reached by `runpy` from a
    hook has no `sys.path` of its own and so cannot import a sibling. **That was
    false**, and checkable from the next file along: `check_gate_invocation.py`
    is loaded by the same bootstrap and does exactly this. Sharing the rules is
    what stops them drifting, and a fifth private parser would not have been the
    last one either.

    Deferred into a function, following that same sibling: a module-level import
    sits outside every guard in this file, so a broken or missing dependency
    would kill the process before `main` is reached -- the launch-failure class
    ADR-0077 §5 says must not be reachable.
    """
    scripts = str(Path(__file__).resolve().parent)
    if scripts not in sys.path:
        sys.path.insert(0, scripts)
    import check_spec_links

    return check_spec_links


def prose_lines(raw: str) -> list[str]:
    """Every line of `raw` that renders as prose, with code and comments gone.

    Line by line, and the *whole* walk is line by line -- including the comment
    strip, which used to run document-wide with `re.DOTALL` at the top of this
    function. That pre-pass undid the point of the rewrite: a `<!--` inside a
    code span paired with any later `-->` and blanked every row between, and
    applied to memory bodies it deleted genuinely dead wiki-links before the
    scan could see them.

    A comment opened and closed on one line is stripped in place; one that spans
    lines is tracked the way a fence is, which is what a line-based walk can do
    without reintroducing the pairing hazard.
    """
    md = _markdown()
    in_comment = False
    out: list[str] = []
    # Code is masked first, by the shared CommonMark parser, one entry per source
    # line. That replaces the fence walk this function used to run itself -- a
    # walk that shared the sibling's two measured defects, so a fence indented
    # under a list item opened nothing here either and every `[[link]]` inside
    # such a block was scanned as live prose.
    #
    # Comment tracking still runs here, after the mask, and the order is
    # load-bearing in one direction: a `<!--` or `-->` inside a code span is
    # already blanked by the time it is read, which is what stops an opener
    # quoted in code from pairing with a real closer far below it and deleting
    # every row between.
    # `mask_html_blocks=False`, the same as `loaded_text` and for the same
    # reason: this module models **the loader**, which reads `MEMORY.md` as text
    # and never renders it. The default blanks an HTML block outright, which is
    # right for the link gate -- a link inside raw HTML is not a link -- and
    # wrong here in the direction that costs most. Measured on the default: a
    # `<details>` fold whose rows are not blank-separated from the tag, and a row
    # led by an HTML comment, both vanish from `index-rows` and raise a hard
    # "orphaned memory" error, while `loaded_text` goes on counting those same
    # lines toward the load limit -- so the two halves of `check()` disagreed
    # about one file. A `[[link]]` inside such a fold went the silent way and was
    # never scanned at all.
    #
    # What this gives up: a fence nested inside an HTML block produces no `fence`
    # token, so a `[[link]]` quoted in one is scanned as live and warns. That is
    # check 4, which is warning-only and permanently so, against an error the
    # corpus cannot argue with -- the loud direction, chosen deliberately.
    for original in md.code_masked_lines(raw, mask_html_blocks=False):
        line = original
        if in_comment:
            head, close, rest = line.partition("-->")
            if not close:
                out.append("")
                continue
            in_comment = False
            line = " " * len(head + close) + rest
        line = _INLINE_COMMENT.sub(lambda m: " " * len(m.group(0)), line)
        head, marker, _rest = line.partition("<!--")
        if marker:
            in_comment = True
            line = head
        out.append(line)
    return out


def linkable_text(raw: str) -> str:
    """`raw` with code, fenced blocks and HTML comments removed.

    A `[[name]]` written inside backticks is documentation *of* the syntax --
    a memory that specifies a convention quotes the convention, and the first
    draft reported two such quoted examples as broken cross-references.
    """
    return "\n".join(prose_lines(raw))


def frontmatter(raw: str) -> str:
    """The leading `---` block alone, or the empty string if there is none."""
    match = _FRONTMATTER.match(raw)
    return match.group(0) if match else ""


def declared_name(raw: str) -> str | None:
    """The memory's frontmatter `name:`, or None if it declares none.

    Bounded to the frontmatter block, because `name:` is an ordinary word that
    memories about agent configuration write in their bodies and inside fences.
    """
    match = _NAME_FIELD.search(frontmatter(raw))
    return match.group(1) if match else None


def row_target(destination: str) -> str | None:
    """The memory filename an index-row destination names, or None.

    None means "not a row target", which covers five different things and all
    five were once read as one: a link out of the corpus (`../adr/x.md`), a URI,
    an anchor-only link, a non-markdown file, and the index's own
    self-reference. Reading them as row targets produced a *dangling index row*
    naming `MEMORY.md` itself as missing, and a *duplicate index row* for an
    ordinary cross-reference.

    Case-insensitive throughout, because the rest of the reconciliation is: a
    `.MD` suffix, or a self-reference spelled `memory.md`, otherwise slipped
    past checks the surrounding code performs case-insensitively and produced an
    error at every session start against a correctly indexed corpus.
    """
    md = _markdown()
    stripped = destination.strip()
    if not stripped:
        return None
    title = md.TITLE_RE.match(stripped)
    url = title.group(1) if title else stripped
    if url.startswith("<") and url.endswith(">"):  # <pointy> destination
        url = url[1:-1]
    if url.startswith("#") or md.SCHEME_RE.match(url):
        return None
    path_part, _, fragment = url.partition("#")  # an anchored row is still a row
    if not path_part.casefold().endswith(".md"):
        return None
    # `./name.md` points into the corpus; `sub/name.md` and `../x/name.md` do
    # not. Normalising the bare `./` prefix is what keeps a perfectly ordinary
    # relative row from being read as a link out and reported a false orphan.
    if path_part.startswith("./"):
        path_part = path_part[2:]
    if set(path_part) & {"/", "\\"}:
        return None
    # A colon surviving the scheme test above is a one-character scheme or a
    # Windows drive-relative path -- `- [Drive](c:x.md)` -- neither of which is
    # a memory in this corpus. The shared `SCHEME_RE` requires two or more
    # characters before the colon on purpose, so that `C:\path` is not read as a
    # URI; that leaves `c:x.md` looking like a bare in-corpus filename, measured
    # here as a dangling index row against a corpus with nothing wrong with it.
    # Rejected on this side rather than by widening the shared regex, which
    # would change what the repository-wide link gate treats as a URI.
    if ":" in path_part:
        return None
    # The index is not one of its own rows, however it is spelled or anchored.
    if path_part.casefold() == INDEX_NAME.casefold():
        return None
    return f"{path_part}#{fragment}" if fragment else path_part


def _row_links(text: str) -> list[str]:
    """Every link destination in one logical row, escapes honoured."""
    md = _markdown()
    out: list[str] = []
    for match in md.LINK_RE.finditer(text):
        before = text[: match.start()]
        # An escaped opener (\[...]) is not a link: an odd run of backslashes
        # before the `[` escapes it, an even run escapes the backslashes. Copied
        # code dropped this guard, so an index documenting its own row syntax
        # counted the example as a row. An image `![alt](x)` is not a row
        # either -- it renders a picture, not a reference.
        if (len(before) - len(before.rstrip("\\"))) % 2 == 1:
            continue
        if before.endswith("!"):
            continue
        out.append(match.group(1))
    return out


def md_targets(raw: str) -> list[str]:
    """Every index **row**'s target, in order, over the index's prose lines.

    A list rather than a set: two rows pointing at one memory is the other half
    of the concurrent-write failure this script exists to detect -- an anchored
    Edit applied twice, or two sessions each adding a row -- and a set made it
    invisible, since `files - linked` and `linked - files` are both empty while
    the row count silently under-reports.

    **Only rows count**, which is the correction that makes the duplicate check
    usable. Every `.md` link anywhere in the document used to be a row, so the
    index's own prose raised a duplicate naming a concurrency incident that had
    not happened.

    **A row is a logical row, not a line.** Markdown lazy continuation puts the
    tail of a long row on the next line, unmarked; matched line by line, that
    tail stopped being part of any row and its link became a false orphan. Lines
    are joined onto the open row until a blank line or the next marker ends it.
    """
    targets: list[str] = []
    current: list[str] | None = None

    def flush() -> None:
        nonlocal current
        if current is not None:
            for destination in _row_links(" ".join(current)):
                target = row_target(destination)
                if target is not None:
                    targets.append(target)
            current = None

    for line in prose_lines(raw):
        if _ROW_LINE.match(line):
            flush()
            current = [line]
        elif current is not None and line.strip():
            current.append(line)  # lazy continuation of the open row
        else:
            flush()
    flush()
    return targets


def check(memory_dir: Path) -> Report:
    report = Report()
    if not memory_dir.is_dir():
        raise ReconcileError(f"{memory_dir} is not a directory")
    index_path = memory_dir / INDEX_NAME
    if not index_path.is_file():
        raise ReconcileError(f"no {INDEX_NAME} in {memory_dir}")

    # `utf-8-sig`, not `utf-8`, at both read sites. A BOM is invisible to a
    # reader and defeats `_FRONTMATTER`'s `\A---` anchor, so one file saved by a
    # third-party editor produced ``declares no frontmatter `name:``` against a
    # well-formed memory. On the index the same BOM is the silent direction:
    # the frontmatter survives stripping and is then counted toward a limit the
    # loader applies to content it discards. The live corpus has no BOM today
    # but does carry CRLF files, so third-party editors do touch these.
    # Read **once**, untranslated, and derive the translated form here. Two
    # reads reintroduced exactly the TOCTOU the `sources` comment below calls a
    # correctness fix rather than a tidy-up: an index edited between them made
    # the row reconciliation describe one file while the load-limit numbers
    # described another, and a concurrent session editing `MEMORY.md` is the
    # event this whole script exists to catch.
    #
    # `newline=""` is what the byte and line counts need -- `read_text`'s
    # default translation drops one byte per line on a CRLF index, under-counting
    # a file whose tail the loader is already dropping. Everything else wants the
    # translated text, so the translation is done here, once, replicating
    # universal-newline handling (CRLF first, then a lone CR).
    #
    # Both reads are guarded, and `UnicodeDecodeError`/`OSError` are surfaced as
    # `ReconcileError` rather than left to escape: `UnicodeDecodeError` is a
    # `ValueError`, so neither is a `ReconcileError` and one file saved by a
    # Windows-1252 editor crashed the run with a traceback instead of the
    # documented one-line exit 1. The comment above is the argument for the
    # guard -- third-party editors demonstrably touch these files, and every
    # memory here contains em dashes, the character `CLAUDE.md` records
    # PowerShell silently producing in that encoding.
    try:
        untranslated = index_path.read_text(encoding="utf-8-sig", newline="")
    except (UnicodeDecodeError, OSError) as exc:
        raise ReconcileError(f"cannot read {index_path}: {exc}") from exc
    raw = untranslated.replace("\r\n", "\n").replace("\r", "\n")

    # The directory is read **once** into `{path: text}` and all three checks run
    # off that mapping. Three independent globs and 2N reads is wasted I/O in a
    # check whose whole budget is an interpreter startup -- but the reason it is a
    # correctness fix rather than a tidy-up is that the scans could disagree: a
    # file appearing or vanishing between globs made `examined["files"]` count
    # one set while the identity and wiki-link results described another.
    #
    # `is_file()` because a *directory* named `notes.md` is a `*.md` glob hit,
    # and reading one raises `PermissionError` on Windows and `IsADirectoryError`
    # on POSIX -- two types for one input, so a handler written against either is
    # wrong on the other leg. Excluding it at the producing site beats catching
    # it at two.
    #
    # The index is excluded case-insensitively: the filesystem opens `Memory.md`
    # as the index *and* a case-sensitive `!=` left it in `sources`, so the index
    # was examined as though it were a memory -- reported orphaned, and reported
    # as declaring no name.
    # Keyed by *name*, not by `Path`. `Path('Alpha.md') == Path('alpha.md')` on
    # Windows, with equal hashes, so a `Path`-keyed dict silently kept the first
    # key and the *other* file's text: measured on a case-sensitive NTFS
    # directory, two colliding memories became `files: 1`, the real orphan went
    # unreported, and the identity check invented an error against a file whose
    # `name:` was correct ("Alpha.md declares name 'alpha'"). The filesystem
    # decides whether two names are one file; the dict must not decide it too.
    sources: dict[str, str] = {}
    for path in sorted(memory_dir.glob("*.md")):
        if not path.is_file() or path.name.casefold() == INDEX_NAME.casefold():
            continue
        try:
            sources[path.name] = path.read_text(encoding="utf-8-sig")
        except (UnicodeDecodeError, OSError) as exc:
            # Guarded for the same reason as the index read above, and named
            # rather than skipped: a memory the checker could not read is one it
            # did not reconcile, and the sibling gate's `continue` would report
            # it orphaned on the strength of not having read it.
            raise ReconcileError(f"cannot read {path}: {exc}") from exc
    files = set(sources)

    # Destinations keep their `#anchor`; the *file* a row names does not.
    destinations = md_targets(raw)
    targets = [d.partition("#")[0] for d in destinations]

    report.examined["files"] = len(files)
    report.examined["index-rows"] = len(destinations)

    # **One normalisation, applied at every comparison site.** Three loops used
    # to reconcile rows against files with three different key treatments --
    # duplicates case-sensitively, orphans and dangling rows case-insensitively
    # -- and each pair of them disagreed about a different corpus. A duplicate
    # row differing only in case was caught by no check at all: the orphan loop
    # saw the file linked, the dangling loop saw both rows resolve, and the
    # duplicate loop saw two distinct strings. That is precisely the "two
    # sessions each adding a row for one memory" incident the error text below
    # describes -- the concurrent-write half this script exists to catch.
    #
    # Casefold is now that one normalisation, and it is applied to filenames and
    # to whole destinations alike. The `#anchor` is a **separate axis** and is
    # deliberately not folded into the first: two rows naming different sections
    # of one memory are an ordinary index, not a concurrency incident, so a
    # destination keeps its anchor while the *file* question strips it.
    by_fold_files: dict[str, list[str]] = {}
    for name in sorted(files):
        by_fold_files.setdefault(name.casefold(), []).append(name)
    # Distinct spellings, not a multiset: N rows naming one memory still link
    # one memory, so repetition is the duplicate check's business (below) and
    # must not make a second file look linked here.
    by_fold_rows: dict[str, list[str]] = {}
    for name in sorted(set(targets)):
        by_fold_rows.setdefault(name.casefold(), []).append(name)

    orphaned: list[str] = []
    dangling: list[str] = []
    for fold in sorted(set(by_fold_files) | set(by_fold_rows)):
        names = list(by_fold_files.get(fold, []))
        rows = list(by_fold_rows.get(fold, []))
        # Rows and files pair off by exact spelling first -- what a
        # case-sensitive filesystem does -- and only the leftovers pair by
        # fold, which is what a case-insensitive one does with the remainder.
        unpaired: list[str] = []
        for name in names:
            if name in rows:
                rows.remove(name)
            else:
                unpaired.append(name)
        while unpaired and rows:
            rows.pop(0)
            unpaired.pop(0)
        orphaned.extend(unpaired)
        # A leftover row is dangling only when *no* file folds to its name. If
        # one does, the row resolves -- on either filesystem -- and whether it
        # is redundant is decided by destination identity, not by counting.
        if rows and not names:
            dangling.extend(rows)

    # Duplicates: one destination, anchor included, named by more than one row.
    # Folded, so the case-differing pair that fell through all three old checks
    # is caught; anchored, so distinct sections of one memory are not.
    #
    # The threshold is how many memories that destination could be naming, not
    # one: on a case-sensitive filesystem `alpha.md` and `Alpha.md` are two
    # memories, and an index carrying a correct row for each is reconciled, not
    # duplicated -- measured, that corpus is clean on NTFS with per-directory
    # case sensitivity enabled. Where the fold group holds fewer files than
    # *distinct* spellings, the surplus rows cannot all be naming distinct
    # memories. A destination naming no file at all keeps a threshold of 1, so a
    # dangling row written twice still reports both faults, as it did before.
    #
    # Two conditions rather than one, and the second is what a threshold on its
    # own cannot express: N *identical* spellings can only ever name ONE memory,
    # however many files fold together. Counting occurrences against the file
    # count let a doubled row through whenever a case-colliding memory existed
    # -- on a case-sensitive directory holding `alpha.md` and `Alpha.md`, an
    # index carrying `- [1](alpha.md)` twice gives 2 rows against a threshold of
    # 2, and `2 > 2` is False. That pair is literally "one anchored Edit applied
    # twice", the incident the error text below describes, and nothing else
    # named it: the orphan loop reports only the unlinked `Alpha.md`.
    by_fold_dest: dict[str, list[str]] = {}
    for dest in destinations:
        by_fold_dest.setdefault(dest.casefold(), []).append(dest)
    duplicated: list[tuple[list[str], int]] = []
    for fold, spellings in sorted(by_fold_dest.items()):
        distinct = sorted(set(spellings))
        here = max(len(by_fold_files.get(fold.partition("#")[0], [])), 1)
        if len(spellings) > len(distinct) or len(distinct) > here:
            duplicated.append((distinct, len(spellings)))

    # Emitted in three blocks, in the order the checks were originally written,
    # so the reported text of an unaffected corpus is byte-identical.
    for spellings, count in duplicated:
        # One spelling reads as it always did. Several means the rows differ
        # only in case, so naming any one of them would point the reader at a
        # row that is not the problem -- the *difference* is the problem, and on
        # a case-insensitive filesystem all of them open the same memory.
        dest = (
            spellings[0]
            if len(spellings) == 1
            else "one memory under {} spellings differing only in case ({})".format(
                len(spellings), ", ".join(spellings)
            )
        )
        report.errors.append(
            f"duplicate index row -- {INDEX_NAME} points at {dest} "
            f"{count} times. Auto memory has no merge or "
            "conflict detection, so two sessions adding a row for one memory "
            "(or one anchored Edit applied twice) produces exactly this"
        )
    for name in orphaned:
        report.errors.append(
            f"orphaned memory -- no row in {INDEX_NAME} points at it, so it "
            f"is never loaded and the write accomplished nothing: {name}"
        )
    for name in dangling:
        report.errors.append(
            f"dangling index row -- {INDEX_NAME} links a file that does not "
            f"exist: {name}"
        )

    # The load limit, measured the way the loader measures it: over the file's
    # real bytes, and counting real newlines.
    #
    # `read_text` translates newlines, so a CRLF index lost one byte per line --
    # the silent direction, under-counting a file whose tail the loader is
    # already dropping. And `str.splitlines()` splits on seven characters the
    # loader does not treat as line breaks -- vertical tab, form feed, the
    # file/group/record separators, NEL, and the Unicode line and paragraph
    # separators -- so an index quoting any of them counted lines that are not
    # there: the loud direction, a false "past the load limit" against a
    # healthy index. Counting newlines and measuring the untranslated text fixes one
    # each.
    body = loaded_text(untranslated)
    # Counted over the three line breaks CommonMark and the loader recognise,
    # not over `"\n"` alone: a lone-CR file has no newline at all, so counting
    # newlines made a 500-row index measure as **1 line** against a 200-line
    # limit — silence on the one check whose failure arrives with no other
    # signal. `str.splitlines()` is still the wrong instrument the other way,
    # splitting on seven characters the loader does not treat as line breaks;
    # the parser's own `LINE_BREAK_RE` is the shared answer to both.
    breaks = len(_markdown().LINE_BREAK_RE.findall(body))
    lines = breaks + (0 if not body or body.endswith(("\n", "\r")) else 1)
    size = len(body.encode("utf-8"))
    report.examined["index-lines"] = lines
    report.examined["index-bytes"] = size
    if lines > LINE_LIMIT or size > BYTE_LIMIT:
        report.errors.append(
            f"{INDEX_NAME} is past the load limit ({lines}/{LINE_LIMIT} lines, "
            f"{size}/{BYTE_LIMIT} bytes) -- everything beyond the cut is "
            "silently dropped at session start while the file still looks "
            "complete. Shorten rows or move detail into topic files"
        )
    elif lines > LINE_LIMIT * WARN_FRACTION or size > BYTE_LIMIT * WARN_FRACTION:
        report.warnings.append(
            f"{INDEX_NAME} is nearing the load limit ({lines}/{LINE_LIMIT} "
            f"lines, {size}/{BYTE_LIMIT} bytes); past it, rows stop loading "
            "with no other signal"
        )

    # Identity: `name:` must equal the filename stem. The convention resolves
    # `[[name]]` by the `name:` field; this script resolves by filename. That
    # is only sound while the two agree, so the agreement is checked rather
    # than assumed -- 17 of 90 memories had drifted apart before anyone looked.
    misnamed = 0
    for name, text in sources.items():
        stem = Path(name).stem
        declared = declared_name(text)
        if declared is None:
            misnamed += 1
            report.errors.append(
                f"{name} declares no frontmatter `name:`, so nothing can "
                "resolve a [[link]] to it by name"
            )
        elif declared != stem:
            misnamed += 1
            report.errors.append(
                f"{name} declares name {declared!r}, which is not its "
                f"filename stem {stem!r} -- a [[link]] written either way "
                "resolves for one reader and not the other"
            )
    report.examined["identity-mismatches"] = misnamed

    # Wiki-links. The convention says a link to a memory that does not exist
    # yet is legitimate, so an unresolved link cannot simply be an error -- but
    # that blanket reading hides a second, real failure. Split them:
    #
    #   [[foo-bar]] where foo_bar.md EXISTS  -> a typo. The link is dead and
    #       nothing says so; separator style is the only difference.
    #   [[foo-bar]] where nothing matches    -> a forward reference, which the
    #       convention explicitly permits.
    #
    # This checker's first run against the live directory reported 12 dead
    # links and was believed. **Seven of the twelve were false** -- they
    # resolved fine by the target's frontmatter `name:`, which is what the
    # convention actually resolves by, while this script resolved by filename.
    # The identity check above is what makes resolving by filename correct;
    # without it, this block reports the divergence between the two rules as
    # damage. The count is kept here as the reason the check exists, not as a
    # finding: re-measure before quoting it anywhere.
    stems = {name[:-3] for name in files}
    # Every tier maps to a *sorted list*, never a single winner. `by_normal` was
    # a dict comprehension over a set, so on a normalize collision -- say
    # `project_a-b` and `project_a_b` both present -- which one a dead link named
    # was chosen by set iteration order, i.e. by `PYTHONHASHSEED`: measured, ten
    # seeds produced each answer five times, so the same bytes gave two different
    # error texts across runs. The second tier already named every candidate for
    # exactly this reason; the first now agrees with it.
    by_normal: dict[str, list[str]] = {}
    for stem in stems:
        by_normal.setdefault(_normalize(stem), []).append(stem)
    # Second tier, checked only after the first misses: the same memory named
    # without its type prefix. An ambiguous suffix (two types carrying the same
    # name) still reports dead -- it names every candidate rather than guessing
    # one, because the link is dead either way and silence is the failure mode
    # this tier exists to remove.
    by_suffix: dict[str, list[str]] = {}
    for stem in stems:
        suffix = _unprefixed(stem)
        if suffix is not None:
            by_suffix.setdefault(suffix, []).append(stem)
    broken: dict[str, str] = {}
    forward: set[str] = set()
    # The index is scanned here and nowhere else in `check`. `sources` excludes
    # it deliberately -- it is not a memory, so it is neither orphanable nor
    # obliged to declare a `name:` -- but that exclusion is wrong for exactly
    # this check: a dead `[[link]]` in a row's hook was never examined at all,
    # in the one file guaranteed to load every session. Measured, the identical
    # typo reported dead inside any memory reported `wiki-links-broken 0,
    # wiki-links-forward 0` and exit 0 from `MEMORY.md`.
    for text in [*sources.values(), raw]:
        scannable = linkable_text(text)
        for target in _WIKI_LINK.findall(scannable):
            if target in stems:
                continue
            normal = _normalize(target)
            candidates = by_normal.get(normal, [])
            if not candidates:
                # Only an **un-prefixed** target is looked up by bare suffix.
                #
                # Two external rounds disagreed here and the second is right. One
                # asked for the target to be un-prefixed too, so a link carrying
                # the *wrong* prefix would resolve as a near-miss; that makes
                # `[[reference_thing]]` a hard error whenever `project_thing`
                # exists. But the prefix encodes the memory's **type**, so those
                # are two different memories and a link to the one that does not
                # exist yet is exactly the forward reference check 4 calls
                # "warning only, permanently". The error text was wrong for the
                # case too -- it said "a dropped type prefix" when the prefix was
                # present and different. Measured on the live corpus: 80 suffixes
                # across 4 prefixes with no collision today, so the first reused
                # suffix would have turned every cross-type forward reference
                # into a permanent session-start error.
                candidates = (
                    by_suffix.get(normal, []) if _unprefixed(normal) is None else []
                )
            if candidates:
                broken[target] = ", ".join(sorted(candidates))
            else:
                forward.add(target)
    report.examined["wiki-links-broken"] = len(broken)
    report.examined["wiki-links-forward"] = len(forward)
    for target, match in sorted(broken.items()):
        # The message names no *specific* difference, deliberately. It used to
        # say "differing only in separator style or a dropped type prefix", and
        # on a case-only miss neither is true: `_normalize` casefolds, so the
        # two tiers reach a near-miss through separator style, case, or a
        # dropped prefix, and naming two of the three is wrong whenever the
        # third is the cause. A wrong explanation on a hard error is what an
        # operator debugs instead of the corpus.
        report.errors.append(
            f"dead wiki-link [[{target}]] -- the memory exists as {match!r}, "
            "a near miss under separator style, case or a type prefix, so this "
            "link resolves to nothing"
        )
    for target in sorted(forward):
        report.warnings.append(
            f"[[{target}]] names no existing memory (legitimate -- the "
            "convention permits marking one worth writing later)"
        )
    return report


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Reconcile the auto-memory directory against MEMORY.md. Detects "
            "orphaned files, dangling rows, and an index past its load limit."
        )
    )
    parser.add_argument(
        "--memory-dir",
        type=Path,
        default=None,
        help="the memory directory; derived from the git root when omitted",
    )
    parser.add_argument(
        "--root",
        type=Path,
        default=None,
        help=(
            "the repository the memory directory is derived from "
            "(default: the git root)"
        ),
    )
    parser.add_argument(
        "--quiet",
        action="store_true",
        help="print nothing when clean (for a caller that runs often)",
    )
    parser.add_argument(
        "--skip-if-absent",
        action="store_true",
        help=(
            "say nothing when the memory directory does not exist at all "
            "(no memory has ever been written for this project)"
        ),
    )
    return parser


def repo_root(start: Path | None = None) -> Path:
    """The **repository** this path belongs to, which is not the worktree.

    Auto memory keys on the repository: *"The <project> path is derived from the
    git repository, so all worktrees and subdirectories within the same repo
    share one auto memory directory"* (`code.claude.com/docs/en/memory`). Session
    *transcript* directories key on the working directory instead, and the two
    look identical from inside a main checkout -- which is how the difference
    went unnoticed.

    So this asks for ``--git-common-dir`` rather than ``--show-toplevel``. In a
    main checkout the two agree. In a **linked worktree** they do not:
    ``--show-toplevel`` returns the worktree, whose slug names a project
    directory that holds no ``memory/`` at all, so the checker resolved nothing,
    `--skip-if-absent` forgave it, and the hook went **permanently silent** in
    the working mode this repository uses most -- "a hook that silently stops
    firing is indistinguishable from one never installed" (ADR-0077 §8), reached
    through the guard written to prevent it. Measured: from a worktree,
    ``--git-common-dir`` is the main checkout's ``.git`` and its parent is the
    repository root that owns the corpus.

    **A sibling script defines a `repo_root()` that deliberately answers the
    opposite.** ``scripts/review_worktree.py`` asks ``--show-toplevel``, because
    what *it* needs is the working tree it is placing scratch paths inside and
    running containment comparisons against. From a linked worktree the two
    functions disagree by design, and both are right for their own question.
    Named here so the divergence reads as a decision rather than as drift, and
    so neither is ever "corrected" to match the other.
    """
    # A named start that is not a directory cannot be asked about — git needs a
    # cwd that exists — and it is not an error either: by the docs' out-of-repo
    # rule the path *is* the project root. Returning it keeps `--root` usable as
    # a plain override, which is how the CLI and several tests use it.
    if start is not None and not start.is_dir():
        return start
    try:
        out = subprocess.run(  # noqa: S603 - fixed executable, no shell
            [_GIT, "rev-parse", "--git-common-dir", "--show-toplevel"],
            cwd=str(start) if start is not None else None,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=30,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ReconcileError(f"could not locate the git root: {exc}") from exc
    if out.returncode != 0:
        # "Outside a git repo, the project root is used instead" (memory docs),
        # so *that* case is an answer rather than a failure. **Only that case.**
        #
        # Treating every non-zero exit alike laundered a real resolution failure
        # into a plausible wrong root, which then raised the one error
        # `--skip-if-absent` forgives -- so a broken `gitdir:`, a malformed
        # `.git/config`, or `safe.directory` dubious ownership each produced a
        # silent, byte-identical-to-clean run. All four measured at rc 128 and
        # none of them says "not a git repository". The message is the only
        # signal git gives to tell them apart, which is a thin instrument -- but
        # a thin instrument that fails loudly beats one that cannot fail.
        if "not a git repository" not in out.stderr.casefold():
            detail = out.stderr.strip() or f"exit {out.returncode}"
            raise ReconcileError(
                f"git could not resolve the repository for "
                f"{start or Path.cwd()}: {detail}"
            )
        return (start or Path.cwd()).resolve()
    # Relative ('.git') in a main checkout, absolute in a linked worktree.
    lines = out.stdout.splitlines()
    common = Path(lines[0].strip())
    if not common.is_absolute():
        common = (start or Path.cwd()) / common
    common = common.resolve()
    # `parent(--git-common-dir)` is the repository root only where the common
    # dir is the checkout's own `.git`. Two ordinary layouts break that, both
    # measured with real git:
    #
    #   --separate-git-dir  common = <anywhere>/elsewhere.git
    #                       parent = <anywhere>  -- an arbitrary directory whose
    #                       slug can name a **different project's** corpus, the
    #                       wrong-tree read this module's docstring calls its
    #                       cardinal sin.
    #   submodule           common = super/.git/modules/<name>
    #                       parent = super/.git/modules -- inside the gitdir.
    #
    # The discriminator is the common dir's own name, which is exactly `.git` in
    # the two layouts where the parent is right (a main checkout, and a linked
    # worktree pointing at the main checkout's `.git` -- the case this function
    # exists for) and something else in both layouts where it is wrong.
    # `--show-toplevel` is the working tree, which is the right answer for a
    # submodule and for a separated gitdir, and the wrong one for a linked
    # worktree -- so it is the fallback rather than the rule. It costs no second
    # spawn: `rev-parse` answers both in one call.
    if common.name == ".git":
        return common.parent
    toplevel = lines[1].strip() if len(lines) > 1 else ""
    return Path(toplevel).resolve() if toplevel else common.parent


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    # An absent directory is not an inconsistent one: it means no memory has ever
    # been written for this **repository**, so there is no index for anything to
    # disagree with. `check` and `resolve_memory_dir` are both right to refuse it
    # -- a caller who named a directory deserves to hear that it is missing --
    # but a caller that names none and runs unasked deserves silence instead.
    #
    # Absence has two levels and the flag forgives both: no project directory at
    # all, and no `memory/` inside one. **A worktree is not either of them**, and
    # an earlier version of this comment said it was. Worktrees share the
    # repository's memory directory (`repo_root`), so a worktree session finding
    # nothing now means the same thing a main-checkout session finding nothing
    # means: this project has never written a memory. That correction is what
    # narrows the flag from silencing a defect to silencing a genuine absence.
    try:
        # `--root` is a **starting point, not the answer**, and that distinction
        # is what makes the repository fix reach a real caller at all. Written as
        # `args.root or repo_root()`, the corrected resolution was dead code on
        # the only path that matters: the bootstrap always passes the checkout
        # holding the script, which in a linked worktree *is* the worktree, so
        # `args.root` was always truthy and `repo_root` never ran. The function
        # was right and unreachable, and the test that pinned it called it
        # directly -- proving a property of a branch production never took.
        # `--memory-dir` short-circuits, and hoisting the root resolution out of
        # this expression is what broke that: `repo_root` then ran
        # unconditionally, so the one flag five error messages in this module
        # offer as the escape hatch stopped being an escape from git at all --
        # and the hook spawned `git rev-parse` at every session start, measured
        # at +157 ms on a 307 ms hook. On a machine where the spawn fails, the
        # flag could not rescue the run either.
        if args.memory_dir:
            memory_dir = args.memory_dir
        else:
            root = repo_root(Path(args.root) if args.root else None)
            memory_dir = resolve_memory_dir(root)
        if args.skip_if_absent and not memory_dir.is_dir():
            return 0
        report = check(memory_dir)
    except ProjectNotFoundError as exc:
        if args.skip_if_absent:
            return 0
        print(f"memory index NOT reconciled: {exc}")
        return 1
    except ReconcileError as exc:
        print(f"memory index NOT reconciled: {exc}")
        return 1
    except ImportError as exc:
        # `markdown-it-py` is reached through `check_spec_links`, and this
        # script is run by hand under the repository's registered `python3`
        # rather than under `uv run` the way the spec-links gate is -- so a
        # missing dependency is a live outcome, not a hypothetical. Unguarded it
        # escaped as a traceback at exit 1 with no "memory index NOT reconciled"
        # line, which is indistinguishable to a caller reading only the exit
        # status from the real orphan this script exists to surface. ADR-0078 §1
        # pins the one-line form; `--skip-if-absent` does not apply, because a
        # broken environment is not an absent corpus.
        print(
            f"memory index NOT reconciled: cannot load the markdown parser: {exc}. "
            "Run it under `uv run --locked python scripts/check_memory_index.py`, or "
            "install the dev dependencies"
        )
        return 1

    for warning in report.warnings:
        print(f"  note: {warning}")
    if report.errors:
        print(f"memory index inconsistent ({len(report.errors)}):")
        for error in report.errors:
            print(f"  - {error}")
        print(
            "  Fix by adding the missing row(s) with an anchored Edit -- never "
            f"rewrite {INDEX_NAME} wholesale, which drops rows a concurrent "
            "session added."
        )
        return 1
    # Under `--quiet` the warnings print and the evidence line does not. It used
    # to print whenever any warning existed, which made ADR-0078's "suppresses the
    # clean-run summary and nothing else" rule false in the one case that recurs:
    # a forward-reference wiki-link is legitimate and permanent by design, so a
    # single one cost two lines at every invocation, forever.
    if not args.quiet:
        print(f"memory index reconciled: {report.evidence()}.")
    return 0


def _reconfigure_streams() -> None:
    """Make stdout/stderr UTF-8 tolerant, for a *process* that is about to exit.

    Never called at module scope or from `main()`, and `isinstance` rather than
    `hasattr`, for the reasons `scripts/check_personal_containment.py` records:
    a memory file name can hold characters the console cannot encode, and a
    reconfigure reached during a test run would retune pytest's own capture
    streams (they are `TextIOWrapper` subclasses) for whatever test writes next.
    Its one caller below is a process entry point, which is what makes it safe;
    that is also why any test that reaches it runs the script in a subprocess.
    """
    for stream in (sys.stdout, sys.stderr):
        if isinstance(stream, io.TextIOWrapper):
            stream.reconfigure(encoding="utf-8", errors="replace")


if __name__ == "__main__":
    _reconfigure_streams()
    sys.exit(main())

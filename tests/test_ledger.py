"""The review ledger's on-disk grammar (scripts/ledger.py, ADR-0072 §5, §8).

The collapse is small in line count and large in failure modes, which is why it
was split into its own work item rather than buried under a skill rewrite. The
modes worth a test are the ones no reading of the code makes obvious:

1. **A re-entered collapse must never recompose a digest.** ADR-0072 §8 states
   the rule; the tests here pin both halves of it — a completed collapse re-run
   leaves the digest byte-identical, and a crash between writing the digest and
   deleting the fragments finishes the deletion instead of composing a second
   digest over a subset of its own inputs.
2. **The invariant §8 leans on is now checked rather than assumed.** "No new
   fragment joins a branch's directory once its digest exists" was, in ADR-0072
   §10's own words, enforced by nothing. ``test_a_fragment_the_digest_never_
   captured_stops_the_collapse`` is the load-bearing test: it puts an uncaptured
   fragment beside a complete digest and requires a refusal, because the
   documented recovery action — finish the deletion and stop — would destroy it.
3. **The allocation survives its own collapse.** ``max(existing) + 1`` restarts
   at 1 against an emptied directory. ``test_the_round_number_survives_a_
   collapse`` is the test that would have caught the defect CodeRabbit raised
   as two separate Major findings on PR #95.
4. **Inlining is only safe because the two paths sit at equal depth.** A digest
   inlines fragment content verbatim, relative links included.
   ``test_a_digest_sits_at_the_same_depth_as_the_fragments_it_inlines`` pins that
   with a hand-written expectation rather than deriving it from the constants it
   is checking — re-sharding digests to a flat directory would silently break
   every relative link a fragment carried, and ``check_spec_links.py`` would then
   redden on a file nobody edited.

The heading demoter gets its own cluster because it is the one piece of real
parsing here: a ``#`` inside a fenced code block is content, and a fragment
quotes shell.

Git-backed tests build a repository per test rather than reusing the real one —
the collapse deletes directories and reads ``<base>...HEAD``, and a fixture that
leaned on the working repository would be reading whatever branch the suite
happened to run on. Every ``git`` call runs with an explicit, cleared environment
so an ambient ``GIT_DIR``/``GIT_INDEX_FILE`` cannot reach into it (a leak this
repository has already been bitten by once, on PR #82).
"""

from __future__ import annotations

import ast
import os
import re
import subprocess
from pathlib import Path

# `scripts/` is importable via tests/conftest.py's sys.path entry, which
# `pyproject.toml`'s pyright note records as its owner; no test module repeats it.
import ledger
import pytest

# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------


# The convention four sibling modules already carry — `test_check_spec_links`,
# `test_check_personal_containment`, `test_no_task_output_citations` and
# `test_review_worktree`. This module was the one that did not, which a bot
# review caught. Clearing the discovery variables below stops an ambient
# repository leaking in; it does nothing about ambient *config*, so a machine
# with `commit.gpgsign`, `core.hooksPath` or `init.templateDir` set globally
# would fail these commits or run foreign hooks on them.
_NEUTRAL_GIT_ENV = {"GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_SYSTEM": os.devnull}


def _git(cwd: Path, *args: str) -> str:
    env = dict(os.environ)
    for leak in ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_OBJECT_DIRECTORY"):
        env.pop(leak, None)
    env.update(
        {
            "GIT_AUTHOR_NAME": "t",
            "GIT_AUTHOR_EMAIL": "t@example.invalid",
            "GIT_COMMITTER_NAME": "t",
            "GIT_COMMITTER_EMAIL": "t@example.invalid",
            **_NEUTRAL_GIT_ENV,
        }
    )
    proc = subprocess.run(  # noqa: S603 - fixed argv built here, not from input
        ["git", *args],  # noqa: S607 - PATH-resolved git, as the skill runs it
        cwd=cwd,
        capture_output=True,
        text=True,
        encoding="utf-8",
        env=env,
        check=True,
    )
    return proc.stdout


def _repo(tmp_path: Path) -> Path:
    repo = tmp_path / "r"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    (repo / "seed.txt").write_text("seed\n", encoding="utf-8")
    _git(repo, "add", "seed.txt")
    _git(repo, "commit", "-qm", "seed")
    return repo


# The injected failure for the atomic-install test. A module constant so the raise
# and the `match=` cannot drift apart into a clause that matches anything.
INTERRUPTED = "interrupted between the write and the rename"

FRAGMENT = """# Round {n} — {branch}

## Angle record

- **Date:** 2026-08-18
- **Loop:** external
- **Effort:** high

### Do-not-re-run, carried into this round

- Nothing yet.
"""


def _write_fragment(repo: Path, branch: str, n: int, body: str | None = None) -> Path:
    directory = ledger.fragment_dir(repo, ledger.branch_hash(branch))
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"round-{n}.md"
    path.write_bytes((body or FRAGMENT.format(n=n, branch=branch)).encode("utf-8"))
    return path


def _commit_ledger(repo: Path, message: str) -> None:
    _git(repo, "add", "-A", "--", ledger.LEDGER_REL)
    _git(repo, "commit", "-qm", message)


def _on_branch(repo: Path, branch: str) -> None:
    _git(repo, "checkout", "-q", "-b", branch)


def _fragments(repo: Path, branch: str) -> list[tuple[int, Path]]:
    return ledger.read_fragments(ledger.fragment_dir(repo, ledger.branch_hash(branch)))


@pytest.mark.parametrize("ambient", ["GIT_CONFIG_GLOBAL", "GIT_CONFIG_SYSTEM"])
def test_ambient_git_config_cannot_reach_the_fixture_repositories(
    ambient: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Pins `_NEUTRAL_GIT_ENV`, which four sibling modules adopt untested.

    Every git-backed test here commits into a throwaway repository, and `_git`
    inherits the environment. Clearing `GIT_DIR` and friends stops an ambient
    *repository* leaking in and does nothing about ambient *config*, so a
    developer with `commit.gpgsign` set globally fails every fixture commit at
    exit 128 — measured on `check_spec_links` by an external review, which is
    why the four siblings carry this neutralization. A bot review caught that
    this module did not.

    The hostile config names a `gpg.program` that cannot exist, so the failure
    does not depend on whether the machine running the suite has a working gpg
    — the sibling comment's own scenario is otherwise untestable on a developer
    box that happens to sign cleanly.

    **Both keys, and the case list is hand-written rather than derived from the
    dict.** The first version hostiled only `GIT_CONFIG_GLOBAL`, so dropping
    `GIT_CONFIG_SYSTEM` from `_NEUTRAL_GIT_ENV` left all 103 tests green — the
    docstring claimed to pin the constant and pinned half of it. Parametrizing
    over `_NEUTRAL_GIT_ENV`'s own keys would repeat that in a subtler form: a
    removed key removes its own case, so the suite would still pass. The names
    below are therefore typed out, and the assertion under them fails if the
    constant and this list stop agreeing in either direction.
    """
    assert set(_NEUTRAL_GIT_ENV) == {"GIT_CONFIG_GLOBAL", "GIT_CONFIG_SYSTEM"}, (
        "the neutralized variables and this test's hand-written case list have "
        f"diverged: {sorted(_NEUTRAL_GIT_ENV)}"
    )

    hostile = tmp_path / "hostile.gitconfig"
    hostile.write_text(
        "[commit]\n\tgpgsign = true\n[gpg]\n\tprogram = no-such-gpg-binary\n",
        encoding="utf-8",
    )
    monkeypatch.setenv(ambient, str(hostile))

    # Without `_NEUTRAL_GIT_ENV` in `_git`'s env this raises CalledProcessError
    # from the seed commit; with it, the hostile file is never consulted.
    repo = _repo(tmp_path)

    assert (repo / "seed.txt").is_file()
    assert _git(repo, "rev-parse", "--abbrev-ref", "HEAD").strip() == "main"


# --------------------------------------------------------------------------
# Identity: the hash a skill computes in shell must be the one Python computes
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("branch", "expected"),
    [
        ("main", "0d6e40"),
        ("chore/brief-5b-ledger-collapse", "bafeb5"),
        ("feat/x", "79b4cc"),
        # Non-ASCII, and the row that gives this test its teeth: the three above
        # are pure ASCII, where UTF-8 and the platform's legacy code page agree
        # byte for byte, so every one of them survives `.encode("latin-1")`.
        # `é` is 0xC3 0xA9 in UTF-8 and 0xE9 in Latin-1, so only here do the two
        # hash differently (a79d68 against a643b6).
        ("feat/café", "a79d68"),
    ],
)
def test_the_branch_hash_matches_the_shell_spelling_the_skill_prescribes(
    branch: str, expected: str
) -> None:
    """Hand-written oracle, produced by the other implementation of this rule.

    ``/review-brief`` computes ``printf '%s' "$branch" | shasum -a 256 | cut
    -c1-6``; these values came from running exactly that. Deriving the
    expectation from ``hashlib`` here would test that sha256 is sha256 and would
    go on passing if this function started hashing a trailing newline, or the
    branch name in the platform's encoding — either of which sends the two
    implementations to different directories for the same branch.

    The encoding half of that claim needs a non-ASCII row to be true of the
    corpus rather than merely of the intent; see the parametrization.
    """
    assert ledger.branch_hash(branch) == expected


def test_the_hash_covers_the_name_without_a_trailing_newline() -> None:
    assert ledger.branch_hash("main") != ledger.branch_hash("main\n")


# --------------------------------------------------------------------------
# Heading demotion
# --------------------------------------------------------------------------


def test_every_heading_drops_one_level() -> None:
    out = ledger.demote_headings("# Title\n\ntext\n\n## Section\n\n### Sub\n")
    assert out == "## Title\n\ntext\n\n### Section\n\n#### Sub\n"


def test_a_hash_inside_a_fence_is_content_and_not_a_heading() -> None:
    """The mutation this catches: dropping fence tracking from the demoter.

    A fragment quotes shell, and the ledger's own commands are comment-heavy.
    Demoting inside a fence corrupts the command a later reader copies.
    """
    source = "# Title\n\n```bash\n# not a heading\ngit status\n```\n\n## After\n"
    out = ledger.demote_headings(source)
    assert "\n# not a heading\n" in out
    assert out.startswith("## Title")
    assert out.endswith("### After\n")


def test_tilde_fences_are_tracked_too() -> None:
    out = ledger.demote_headings("~~~\n# inside\n~~~\n\n# outside\n")
    assert "\n# inside\n" in out
    assert out.endswith("## outside\n")


def test_a_longer_closing_run_still_closes_its_fence() -> None:
    """A fence closes on a run at least as long as the one that opened it."""
    out = ledger.demote_headings("```\n# in\n````\n\n# out\n")
    assert "\n# in\n" in out
    assert out.endswith("## out\n")


def test_a_fence_is_not_closed_by_one_carrying_an_info_string() -> None:
    out = ledger.demote_headings("```\n# in\n```python\n# still in\n```\n\n# out\n")
    assert "\n# in\n" in out
    assert "\n# still in\n" in out
    assert out.endswith("## out\n")


def test_an_indented_code_line_is_not_demoted() -> None:
    assert ledger.demote_headings("    # indented\n") == "    # indented\n"


def test_a_hash_with_no_following_space_is_not_a_heading() -> None:
    assert ledger.demote_headings("#hashtag\n") == "#hashtag\n"


@pytest.mark.parametrize(
    "underline", ["=========", "---------"], ids=["level-1", "level-2"]
)
def test_a_setext_heading_refuses_rather_than_slipping_past_the_composer(
    underline: str,
) -> None:
    """The one heading syntax neither function modelled.

    A wholly-setext fragment is lint-clean on its own — MD003 asks for
    consistency — so nothing upstream catches it, and the digest it composes
    into carries two level-1 headings and mixed styles, failing MD025 and MD003
    at the moment `/squash-merge` is merging the PR.
    """
    with pytest.raises(ledger.LedgerError, match="setext heading"):
        ledger.demote_headings(f"Round one\n{underline}\n\ntext\n")


@pytest.mark.parametrize(
    "source",
    [
        "# T\n\n| a | b |\n|---|---|\n| 1 | 2 |\n",  # a table delimiter row
        "# T\n\ntext\n\n---\n\nmore\n",  # a thematic break after a blank line
        "# T\n\n```text\nheading\n=======\n```\n",  # setext inside a fence
        # A lone `-` under a paragraph. CommonMark calls this a level-2 setext
        # heading; here it is treated as an empty list bullet, which is the
        # deliberate divergence `SETEXT_UNDERLINE_RE`'s comment records. Without
        # the `-{2,}` bound this fragment is refused, and no other test in this
        # file reaches that boundary — every fixture's dash has text after it.
        "# T\n\ntext\n-\n\nmore\n",
        # A single-column GFM table: the header wraps in pipes, the one-cell
        # delimiter row need not. This is the case the `"|" not in previous`
        # guard exists for, and the case the two-column fixture above cannot
        # reach — `|---|---|` contains a pipe, so the pattern excludes it before
        # any guard runs. Deleting the guard refused this fragment outright.
        "# T\n\n| Header |\n---\n\nmore\n",
        # The same table with its delimiter row indented. CommonMark allows up
        # to three leading spaces before a block still counts as unindented, and
        # `SETEXT_UNDERLINE_RE` matches that — so the guard's dash/equals
        # discriminator has to `lstrip()` before reading the first character.
        # Without this case the `.lstrip()` call is unexercised: dropping it
        # leaves every other fixture green.
        "# T\n\n| Header |\n   ---\n\nmore\n",
    ],
    ids=[
        "table-delimiter",
        "thematic-break",
        "fenced",
        "lone-dash-bullet",
        "single-column-table",
        "indented-delimiter-row",
    ],
)
def test_what_looks_like_setext_but_is_not_passes_through(source: str) -> None:
    """The refusal must not fire on the shapes that resemble it.

    A false refusal here lands in `/squash-merge` after the checks are green,
    which is the worst place to discover an over-eager guard.
    """
    assert ledger.demote_headings(source)


def test_a_double_blank_line_inside_a_fence_is_not_md012() -> None:
    """MD012 exempts code blocks; this check did not, and refused digests
    PyMarkdown accepts — on exactly the captured-output fragments the demoter's
    fence tracking exists for."""
    ledger.validate_digest("# T\n\n```text\nline\n\n\nmore\n```\n")


def test_a_double_blank_line_outside_a_fence_is_still_md012() -> None:
    """The other direction: making MD012 fence-aware must not lose it."""
    with pytest.raises(ledger.LedgerError, match="consecutive blank lines"):
        ledger.validate_digest("# T\n\ntext\n\n\nmore\n")


def test_an_equals_underline_is_still_caught_beneath_a_pipe_bearing_line() -> None:
    """The pipe guard is scoped to dash underlines, and this is why.

    A GFM delimiter row is dash-only, so `=` under a line containing a pipe is
    never a table. Applying the guard to both characters would give up level-1
    setext detection for nothing — which the guard's first version did, while
    describing the loss as the intrinsic cost of fixing the table refusal.
    """
    with pytest.raises(ledger.LedgerError, match="setext heading"):
        ledger.demote_headings("# T\n\n| Header |\n===\n\nmore\n")


def test_a_level_six_heading_refuses_rather_than_producing_a_level_seven() -> None:
    with pytest.raises(ledger.LedgerError, match="level-6 heading"):
        ledger.demote_headings("###### deep\n")


def test_a_fragment_that_leaves_a_fence_open_is_refused() -> None:
    """`scan_fences` always knew; the demoter discarded the answer.

    The per-fragment refusal is the only place this is visible. `validate_digest`
    sees the *composed* text, where the next fragment's fence closes this one.
    """
    with pytest.raises(ledger.LedgerError, match="leaves a code fence open"):
        ledger.demote_headings("# Round 1\n\n```text\nstuff")


def test_one_fragments_open_fence_cannot_swallow_the_next_round() -> None:
    """The composed document balances, so nothing downstream can catch this.

    Round 1 opens a fence it never closes; round 2's closing fence closes it
    instead. Measured before the fix: `validate_digest` accepted the result,
    `pymarkdownlnt` reported zero errors, and round 2's `## Round 2` heading
    scanned as fenced content — so the collapse deleted both fragments and that
    round survived only as text inside a code block.

    Asserting on the *composer* rather than on `demote_headings` directly is
    deliberate: this is the path the data loss actually took, and a fix that
    refused in the demoter while `compose_digest` called something else would
    pass a unit test and lose the round anyway.
    """
    first = "# Round 1\n\n```text\nstuff"
    second = "# Round 2\n\n```sh\ncmd\n```\n\nafter"
    scanned, _ = ledger.scan_fences(f"{first}\n\n{second}")
    assert ("## Round 2", True) not in [(line, fenced) for line, fenced in scanned], (
        "the composed text must not report round 2's heading as fenced"
    )

    with pytest.raises(ledger.LedgerError, match="leaves a code fence open"):
        ledger.demote_headings(first)


@pytest.mark.parametrize(
    "line",
    [
        "```text``` is the fence marker used above.",
        "``` ``` ``` all inline.",
    ],
)
def test_a_prose_line_opening_with_an_inline_span_is_not_a_fence(line: str) -> None:
    """CommonMark: a backtick fence's info string may not contain a backtick.

    `scripts/check_spec_links.py` already carried this rule — in a `_fence_open`
    helper it has since replaced with a real CommonMark parser — and this module
    did not, so the two disagreed about the same corpus. The cost was not
    cosmetic: every heading after such a line went undemoted and the
    fragment was reported as carrying an unclosed fence — a false refusal
    reaching `/squash-merge` after every check is green.
    """
    source = f"# Round 1\n\n{line}\n\n## Section\n"
    assert ledger.scan_fences(source)[1] is False
    assert ledger.demote_headings(source) == (f"## Round 1\n\n{line}\n\n### Section\n")


def test_a_tilde_fence_may_still_carry_backticks_in_its_info_string() -> None:
    """The rule is backtick-specific; scoping it wider would lose real fences."""
    source = "# T\n\n~~~`weird`\n# not a heading\n~~~\n\n## After\n"
    out = ledger.demote_headings(source)
    assert "# not a heading" in out
    assert "### After" in out


# --------------------------------------------------------------------------
# The digest's shape, and the contract its header carries
# --------------------------------------------------------------------------


def test_the_composed_digest_round_trips_through_its_own_parser(tmp_path: Path) -> None:
    """Writer and reader are two ends of one contract, so test them against
    each other rather than against a copy of the format in the test."""
    repo = tmp_path / "r"
    branch = "feat/x"
    for n in (1, 2, 3):
        _write_fragment(repo, branch, n)
    fragments = _fragments(repo, branch)
    text = ledger.compose_digest(96, ledger.branch_hash(branch), branch, fragments)
    recorded, captured = ledger.parse_digest(text)
    assert recorded == ledger.branch_hash(branch)
    assert sorted(captured) == [1, 2, 3]
    # Each fingerprint is the one the writer computed over the text it inlined,
    # which is what lets the reader tell a captured fragment from an amended one.
    for n, path in fragments:
        inlined = ledger.demote_headings(path.read_text(encoding="utf-8").strip())
        assert captured[n] == ledger.fingerprint(inlined)
        assert inlined in text


def test_the_fingerprint_is_twelve_hex_of_sha256_over_the_inlined_text() -> None:
    """Hand-written oracle, because the round-trip test cannot supply one.

    That test asserts `captured[n] == fingerprint(inlined)` — writer and reader
    calling the same helper — so it pins that the two ends agree, never that
    they agree on the *right* thing. Measured: truncating the slice to `[:2]`,
    and replacing the body with a constant, both leave it green.

    `2cf24dba5fb0` is the first twelve hex of sha256 over `hello`, produced by
    `printf '%s' hello | shasum -a 256`. It pins the algorithm, the truncation
    and the length at once, and it comes from outside this module.
    """
    assert ledger.fingerprint("hello") == "2cf24dba5fb0"


def test_two_different_rounds_get_different_fingerprints() -> None:
    """A degenerate hash is what makes the amended-fragment refusal vacuous."""
    assert ledger.fingerprint("# Round 1\n\nalpha\n") != ledger.fingerprint(
        "# Round 2\n\nbeta\n"
    )


def test_the_digest_carries_exactly_one_title(tmp_path: Path) -> None:
    repo = tmp_path / "r"
    for n in (1, 2):
        _write_fragment(repo, "feat/x", n)
    fragments = _fragments(repo, "feat/x")
    text = ledger.compose_digest(96, ledger.branch_hash("feat/x"), "feat/x", fragments)
    assert [line for line in text.split("\n") if line.startswith("# ")] == [
        "# PR #96 review ledger digest"
    ]
    assert "## Round 1 — feat/x" in text
    assert "## Round 2 — feat/x" in text


def test_composing_the_same_fragments_twice_produces_the_same_bytes(
    tmp_path: Path,
) -> None:
    """Nothing in a digest may derive from write time (ADR-0072 §8)."""
    repo = tmp_path / "r"
    _write_fragment(repo, "feat/x", 1)
    fragments = _fragments(repo, "feat/x")
    first = ledger.compose_digest(96, "79b4cc", "feat/x", fragments)
    second = ledger.compose_digest(96, "79b4cc", "feat/x", fragments)
    assert first == second


@pytest.mark.parametrize(
    ("bad", "reason"),
    [
        ("# one\n\n# two\n", "level-1 headings"),
        ("# one\n\n\n\ntext\n", "consecutive blank lines"),
        ("# one\n\ntext", "trailing newline"),
        ("# one\n\ntext\n\n", "trailing newline"),
        ("# one\n\n```bash\nunclosed\n", "unclosed code fence"),
    ],
)
def test_the_composer_refuses_to_emit_markdown_a_gate_would_reject(
    bad: str, reason: str
) -> None:
    """Clean by construction, permanently (ADR-0072 §8) — checked before the
    write, so a dirty digest never reaches the branch that would have to fix it."""
    with pytest.raises(ledger.LedgerError, match=reason):
        ledger.validate_digest(bad)


def test_a_valid_digest_passes_its_own_validator(tmp_path: Path) -> None:
    repo = tmp_path / "r"
    _write_fragment(repo, "feat/x", 1)
    fragments = _fragments(repo, "feat/x")
    ledger.validate_digest(ledger.compose_digest(96, "79b4cc", "feat/x", fragments))


def test_a_fragment_with_no_title_of_its_own_is_refused_for_md001(
    tmp_path: Path,
) -> None:
    """`titles` structurally cannot see this, so MD001 is modelled separately.

    A fragment whose top heading is `##` demotes to `###` and lands directly
    under the digest's own `#`. The title count is 1 either way — a fragment
    *with* a `#` also contributes 0 after demotion — so the arithmetic that
    guards MD025 is blind to it by construction, not by oversight.

    Measured before the fix: `validate_digest` accepted the composed digest and
    `pymarkdownlnt@0.9.39` under this repository's own config rejected it with
    "MD001 ... [Expected: h2; Actual: h3]" — at the moment `/squash-merge` was
    merging the PR, which is the outcome validating-before-the-write exists to
    prevent. Fragments are hand-amended by `/apply-review`, so the shape is
    reachable rather than hypothetical.
    """
    repo = tmp_path / "r"
    _write_fragment(repo, "feat/x", 1, "## Round 1 with no level-1 title\n\ntext\n")
    fragments = _fragments(repo, "feat/x")
    with pytest.raises(ledger.LedgerError, match="h1 to h3"):
        ledger.validate_digest(ledger.compose_digest(96, "79b4cc", "feat/x", fragments))


def test_a_heading_level_jump_deeper_in_a_fragment_is_refused() -> None:
    """MD001 is not only about the first heading under the title."""
    with pytest.raises(ledger.LedgerError, match="h2 to h4"):
        ledger.validate_digest("# T\n\n## Section\n\n#### Too deep\n")


def test_returning_to_a_shallower_heading_is_not_an_md001_jump() -> None:
    """Going back up any distance is legal; only descending must step by one."""
    ledger.validate_digest("# T\n\n## A\n\n### B\n\n## C\n\n### D\n")


def test_a_heading_jump_inside_a_fence_is_not_an_md001_jump() -> None:
    """The MD001 walk has to be fence-aware like every other rule here."""
    ledger.validate_digest("# T\n\n## A\n\n```text\n#### quoted\n```\n\n## B\n")


def test_a_fence_in_a_fragment_does_not_trip_the_validator(tmp_path: Path) -> None:
    repo = tmp_path / "r"
    _write_fragment(
        repo, "feat/x", 1, "# Round 1\n\n```bash\n# a comment\ngit status\n```\n"
    )
    fragments = _fragments(repo, "feat/x")
    ledger.validate_digest(ledger.compose_digest(96, "79b4cc", "feat/x", fragments))


def test_a_digest_sits_at_the_same_depth_as_the_fragments_it_inlines(
    tmp_path: Path,
) -> None:
    """Inlining preserves relative links only because the depths match.

    A fragment at ``angle-ledger/branches/<b6>/round-1.md`` linking to
    ``../../../../adr/0072-...`` reaches the ADR; the same text inlined into
    ``angle-ledger/digests/0/pr96.md`` reaches it too — and only because both
    files sit two directories below ``angle-ledger/``. Re-shard digests to
    ``digests/pr96.md`` and every inlined link breaks, with
    ``check_spec_links.py`` reddening on a file nobody edited. The expectation
    is written out by hand rather than computed from the two path helpers, which
    would move together with any such change and report nothing.
    """
    repo = tmp_path / "r"
    fragment = ledger.fragment_dir(repo, "abc123") / "round-1.md"
    digest = ledger.digest_path(repo, 96)
    assert fragment.relative_to(repo).parts[:-1] == (
        "specs",
        "reviews",
        "angle-ledger",
        "branches",
        "abc123",
    )
    assert digest.relative_to(repo).parts[:-1] == (
        "specs",
        "reviews",
        "angle-ledger",
        "digests",
        "0",
    )

    target = repo / "specs" / "adr" / "0072.md"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("adr\n", encoding="utf-8")
    link = "../../../../adr/0072.md"
    assert (fragment.parent / link).resolve() == target.resolve()
    assert (digest.parent / link).resolve() == target.resolve()


def test_digests_shard_a_hundred_prs_to_a_bucket(tmp_path: Path) -> None:
    repo = tmp_path / "r"
    assert ledger.digest_path(repo, 99).parent.name == "0"
    assert ledger.digest_path(repo, 100).parent.name == "1"
    assert ledger.digest_path(repo, 4321).parent.name == "43"


# --------------------------------------------------------------------------
# Reading what is on disk
# --------------------------------------------------------------------------


def test_fragments_are_ordered_by_round_number_and_not_by_name(tmp_path: Path) -> None:
    """The mutation this catches: sorting the directory listing as strings,
    which puts round 10 between rounds 1 and 2 and silently reorders a digest."""
    repo = tmp_path / "r"
    for n in (2, 10, 1):
        _write_fragment(repo, "feat/x", n)
    found = _fragments(repo, "feat/x")
    assert [n for n, _ in found] == [1, 2, 10]


def test_an_unrecognized_file_beside_the_fragments_stops_the_run(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "r"
    directory = ledger.fragment_dir(repo, ledger.branch_hash("feat/x"))
    _write_fragment(repo, "feat/x", 1)
    (directory / "notes.md").write_text("hand-written\n", encoding="utf-8")
    with pytest.raises(ledger.LedgerError, match="not round fragments"):
        ledger.read_fragments(directory)


def test_a_missing_directory_reads_as_no_fragments(tmp_path: Path) -> None:
    assert ledger.read_fragments(tmp_path / "nope") == []


@pytest.mark.parametrize(
    "text",
    [
        "# PR #96\n\n- **Rounds captured:** 1\n",
        "# PR #96\n\n- **Branch hash:** `abc123`\n",
    ],
)
def test_a_digest_missing_half_its_contract_is_unreadable(text: str) -> None:
    with pytest.raises(ledger.LedgerError, match=r"Branch hash|Rounds captured"):
        ledger.parse_digest(text)


def test_a_malformed_digest_stops_the_allocator(tmp_path: Path) -> None:
    """Reached through `next_round`, not by calling the parser directly.

    The two tests above hand `parse_digest` a string; neither puts a bad digest
    on disk and asks the allocator for a number. Skipping an unreadable digest
    instead of refusing is the dangerous repair here — the allocator would then
    under-count and re-issue round numbers the corrupt digest still holds, which
    is silent-wrong, while a refusal is loud and costs a human one look.

    Fail-closed is deliberate even though the bad digest may belong to another
    branch: `digests_for_branch` parses before it filters, so one corrupt digest
    stops every branch's allocation. That is the cheap direction to be wrong in.
    """
    repo = tmp_path / "r"
    digest = ledger.digest_path(repo, 96)
    digest.parent.mkdir(parents=True, exist_ok=True)
    digest.write_bytes(b"# PR #96 review ledger digest\n\nheader lines gone.\n")

    with pytest.raises(ledger.LedgerError, match=r"Branch hash|Rounds captured"):
        ledger.next_round(repo, "feat/x")


def test_a_malformed_digest_stops_a_collapse(tmp_path: Path) -> None:
    """The same gap on the other production path.

    `collapse` parses the PR's own digest to decide whether it is re-entering.
    Treating an unreadable one as absent would recompose over a digest whose
    contents nothing could read — the never-recompose rule broken by the one
    input that cannot be checked against it.
    """
    repo = _repo(tmp_path)
    branch = "feat/x"
    _on_branch(repo, branch)
    _write_fragment(repo, branch, 1)
    _commit_ledger(repo, "round 1")
    digest = ledger.digest_path(repo, 96)
    digest.parent.mkdir(parents=True, exist_ok=True)
    digest.write_bytes(b"# PR #96 review ledger digest\n\nheader lines gone.\n")

    with pytest.raises(ledger.LedgerError, match=r"Branch hash|Rounds captured"):
        ledger.collapse(repo, 96, branch, "main")

    assert _fragments(repo, branch), "the fragments must survive a refusal"


@pytest.mark.parametrize(
    "digits",
    # chr(0x0661) is ARABIC-INDIC DIGIT ONE, written as a code point rather than
    # a literal: `\d` accepts it as a digit and `[0-9]` does not, which is half
    # of why the pattern changed.
    ["01", chr(0x0661)],
    ids=["leading-zero", "non-ascii-digit"],
)
def test_a_fragment_name_that_is_not_a_plain_round_number_is_refused(
    tmp_path: Path, digits: str
) -> None:
    """`\\d+` accepted both, and the first one collided.

    `round-01.md` beside `round-1.md` produced a digest reading
    `Rounds captured: 1, 1` with round 1 inlined twice, and handed the next
    brief a number that collides with a fragment already in the digest.
    """
    directory = ledger.fragment_dir(tmp_path, "abc123")
    directory.mkdir(parents=True)
    (directory / f"round-{digits}.md").write_bytes(b"# Round\n")
    with pytest.raises(ledger.LedgerError, match="not round fragments"):
        ledger.read_fragments(directory)


def test_a_superscript_digit_is_refused_rather_than_raising() -> None:
    """`'\\u00b2'.isdigit()` is True and `int('\\u00b2')` raises, so the guard let
    an uncaught ValueError through the contract that promises a refusal.

    `scripts/check_personal_containment.py` already documents this exact trap
    and already spells it `.isdecimal()`; this module was the site that did not.
    """
    with pytest.raises(ledger.LedgerError, match="unreadable round number"):
        ledger.parse_digest(
            f"- **Branch hash:** `abc123`\n- **Rounds captured:** {chr(0xB2)} (`aa`)\n"
        )


def test_the_gate_refuses_a_root_that_is_not_this_repository(tmp_path: Path) -> None:
    """A gate that examined nothing is not a pass — `run_gates.py` already has a
    name for that outcome, and this one reported success against an empty
    directory with no repository in it at all."""
    with pytest.raises(ledger.LedgerError, match="examined nothing"):
        ledger.check_collapsed(tmp_path)


def test_the_fragment_directory_is_available_as_a_subcommand(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """`/review-brief` computed `<b6>` in shell while this module computed it in
    Python — the identity rule implemented twice, which is what the module
    docstring argues against for the digest's grammar."""
    code = ledger.main(
        ["--root", str(tmp_path), "fragment-dir", "--branch", "feat/café"]
    )
    assert code == 0
    assert capsys.readouterr().out.strip() == (
        "specs/reviews/angle-ledger/branches/a79d68"
    )


def test_an_unreadable_round_number_is_refused_rather_than_skipped() -> None:
    text = (
        "- **Branch hash:** `abc123`\n"
        "- **Rounds captured:** 1 (`aa`), two (`bb`), 3 (`cc`)\n"
    )
    with pytest.raises(ledger.LedgerError, match="unreadable round number"):
        ledger.parse_digest(text)


def test_a_round_entry_missing_its_fingerprint_is_refused() -> None:
    """The superseded grammar recorded bare round numbers.

    Accepting one would leave a captured round with no fingerprint to compare
    against, which is the substring-era blindness arriving by another route —
    so the entry grammar refuses rather than defaulting the missing half.
    """
    text = "- **Branch hash:** `abc123`\n- **Rounds captured:** 1, 2, 3\n"
    with pytest.raises(ledger.LedgerError, match="unreadable round entry"):
        ledger.parse_digest(text)


def test_a_repeated_round_entry_is_refused_rather_than_silently_dropped() -> None:
    """The dict assignment kept the last entry and lost the first, in silence.

    Measured before the fix: this parsed to `{1: 'bb'}`. `_refuse_on_uncaptured`
    then compares round 1's surviving fragment against `bb` alone — a false
    refusal if the digest actually inlined the `aa` text, and in the mirror case
    a genuinely divergent fragment matching the wrong entry and being deleted.
    """
    text = "- **Branch hash:** `abc123`\n- **Rounds captured:** 1 (`aa`), 1 (`bb`)\n"
    with pytest.raises(ledger.LedgerError, match="round 1 after round 1"):
        ledger.parse_digest(text)


def test_a_descending_round_entry_is_refused() -> None:
    """Three of this module's own error strings and ADR-0072 §8 assert ascending
    order; nothing checked it, and `3 (`aa`), 1 (`bb`)` parsed without complaint."""
    text = "- **Branch hash:** `abc123`\n- **Rounds captured:** 3 (`aa`), 1 (`bb`)\n"
    with pytest.raises(ledger.LedgerError, match="round 1 after round 3"):
        ledger.parse_digest(text)


def test_an_ascending_list_with_gaps_is_still_accepted() -> None:
    """A round briefed and abandoned leaves its number consumed but uncaptured,
    so gaps are ordinary and only repeats and descents are errors."""
    text = "- **Branch hash:** `abc123`\n- **Rounds captured:** 1 (`aa`), 4 (`bb`)\n"
    assert ledger.parse_digest(text) == ("abc123", {1: "aa", 4: "bb"})


# --------------------------------------------------------------------------
# Allocation — the defect this work item closes
# --------------------------------------------------------------------------


def test_an_empty_ledger_allocates_round_one(tmp_path: Path) -> None:
    assert ledger.next_round(tmp_path, "feat/x") == 1


def test_allocation_follows_the_fragments_on_disk(tmp_path: Path) -> None:
    repo = tmp_path / "r"
    for n in (1, 2):
        _write_fragment(repo, "feat/x", n)
    assert ledger.next_round(repo, "feat/x") == 3


def test_the_round_number_survives_a_collapse(tmp_path: Path) -> None:
    """The PR #95 finding, in one test.

    After a collapse the branch's directory is gone, so an allocator reading
    only the disk restarts at 1 and re-issues numbers the digest already holds —
    against the invariant ADR-0072 §8 leans on to make "never recompose a digest"
    safe. Reading the retained digest is what closes it.
    """
    repo = _repo(tmp_path)
    branch = "feat/x"
    _on_branch(repo, branch)
    for n in (1, 2, 3):
        _write_fragment(repo, branch, n)
    _commit_ledger(repo, "rounds")
    ledger.collapse(repo, 96, branch, "main")

    assert ledger.fragment_dir(repo, ledger.branch_hash(branch)).exists() is False
    assert ledger.next_round(repo, branch) == 4


def test_a_digest_from_another_branch_does_not_move_this_branch_s_counter(
    tmp_path: Path,
) -> None:
    repo = _repo(tmp_path)
    _on_branch(repo, "feat/other")
    for n in (1, 2, 3):
        _write_fragment(repo, "feat/other", n)
    _commit_ledger(repo, "rounds")
    ledger.collapse(repo, 96, "feat/other", "main")
    assert ledger.next_round(repo, "feat/x") == 1


def test_allocation_takes_the_higher_of_the_disk_and_the_digest(tmp_path: Path) -> None:
    """The crash window: a digest captured rounds 1 to 3, and fragment 4 is back."""
    repo = _repo(tmp_path)
    branch = "feat/x"
    _on_branch(repo, branch)
    for n in (1, 2, 3):
        _write_fragment(repo, branch, n)
    _commit_ledger(repo, "rounds")
    ledger.collapse(repo, 96, branch, "main")
    _write_fragment(repo, branch, 4)
    assert ledger.next_round(repo, branch) == 5


# --------------------------------------------------------------------------
# Collapse
# --------------------------------------------------------------------------


def test_a_collapse_writes_the_digest_and_removes_the_fragments(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    branch = "feat/x"
    _on_branch(repo, branch)
    for n in (1, 2):
        _write_fragment(repo, branch, n)
    _commit_ledger(repo, "rounds")

    message = ledger.collapse(repo, 96, branch, "main")

    digest = ledger.digest_path(repo, 96)
    assert digest.is_file()
    assert ledger.fragment_dir(repo, ledger.branch_hash(branch)).exists() is False
    assert "collapsed round(s) 1, 2" in message
    text = digest.read_text(encoding="utf-8")
    assert "## Round 1 — feat/x" in text
    assert "## Round 2 — feat/x" in text


def test_the_digest_is_written_with_lf_endings_and_no_bom(tmp_path: Path) -> None:
    """``.gitattributes`` sets ``eol=lf``, and a text-mode write on Windows would
    emit CRLF — the file-corruption hazard this repository has already paid for."""
    repo = _repo(tmp_path)
    _on_branch(repo, "feat/x")
    _write_fragment(repo, "feat/x", 1)
    _commit_ledger(repo, "rounds")
    ledger.collapse(repo, 96, "feat/x", "main")
    raw = ledger.digest_path(repo, 96).read_bytes()
    assert b"\r\n" not in raw
    assert not raw.startswith(b"\xef\xbb\xbf")


def test_a_re_run_after_a_complete_collapse_changes_nothing(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    branch = "feat/x"
    _on_branch(repo, branch)
    _write_fragment(repo, branch, 1)
    _commit_ledger(repo, "rounds")
    ledger.collapse(repo, 96, branch, "main")
    before = ledger.digest_path(repo, 96).read_bytes()

    message = ledger.collapse(repo, 96, branch, "main")

    assert "already collapsed" in message
    assert ledger.digest_path(repo, 96).read_bytes() == before


def test_a_crash_between_the_write_and_the_delete_finishes_the_delete(
    tmp_path: Path,
) -> None:
    """ADR-0072 §8: an existing digest is authoritative and is never recomposed.

    The re-run finishes the deletion and stops. Recomposing from whatever
    fragments happen to remain would overwrite a complete digest with one built
    from a subset of its own inputs — data loss wearing the shape of recovery.
    """
    repo = _repo(tmp_path)
    branch = "feat/x"
    _on_branch(repo, branch)
    for n in (1, 2, 3):
        _write_fragment(repo, branch, n)
    _commit_ledger(repo, "rounds")
    ledger.collapse(repo, 96, branch, "main")
    complete = ledger.digest_path(repo, 96).read_bytes()

    # The crash: the digest landed, one fragment survived the deletion.
    _write_fragment(repo, branch, 2)

    message = ledger.collapse(repo, 96, branch, "main")

    assert "finished an interrupted collapse" in message
    assert ledger.digest_path(repo, 96).read_bytes() == complete
    assert ledger.fragment_dir(repo, ledger.branch_hash(branch)).exists() is False


def test_a_fragment_the_digest_never_captured_stops_the_collapse(
    tmp_path: Path,
) -> None:
    """The invariant ADR-0072 §10 lists as enforced by nothing, now enforced.

    Round 4 was allocated after the digest existed. The documented recovery
    action — finish the deletion and stop — would delete it, and no digest holds
    it. So the run refuses, leaving both the digest and the fragment intact for
    a human to reconcile.
    """
    repo = _repo(tmp_path)
    branch = "feat/x"
    _on_branch(repo, branch)
    for n in (1, 2, 3):
        _write_fragment(repo, branch, n)
    _commit_ledger(repo, "rounds")
    ledger.collapse(repo, 96, branch, "main")
    complete = ledger.digest_path(repo, 96).read_bytes()
    stray = _write_fragment(repo, branch, 4)

    with pytest.raises(ledger.LedgerError, match=r"never captured"):
        ledger.collapse(repo, 96, branch, "main")

    assert stray.is_file()
    assert ledger.digest_path(repo, 96).read_bytes() == complete


def test_a_fragment_amended_after_the_digest_stops_the_collapse(
    tmp_path: Path,
) -> None:
    """The data-loss path an `xhigh` round found, and the reason it was invisible.

    The uncaptured check compared round *numbers*. A fragment whose number the
    digest holds and whose bytes it does not — `/apply-review` amends fragments,
    so this is ordinary — was therefore deleted on the re-entry path, and its
    content existed nowhere afterwards. The crash-recovery test above could not
    see it because it restores the surviving fragment with byte-identical
    content, which is exactly the case a number comparison gets right.
    """
    repo = _repo(tmp_path)
    branch = "feat/x"
    _on_branch(repo, branch)
    for n in (1, 2, 3):
        _write_fragment(repo, branch, n)
    _commit_ledger(repo, "rounds")
    ledger.collapse(repo, 96, branch, "main")
    complete = ledger.digest_path(repo, 96).read_bytes()

    amended = _write_fragment(
        repo, branch, 2, "# Round 2 — feat/x\n\nAMENDED AFTER THE DIGEST\n"
    )
    _commit_ledger(repo, "apply-review amends round 2")

    with pytest.raises(ledger.LedgerError, match="content differs"):
        ledger.collapse(repo, 96, branch, "main")

    assert amended.is_file()
    assert b"AMENDED AFTER THE DIGEST" in amended.read_bytes()
    assert ledger.digest_path(repo, 96).read_bytes() == complete


def test_a_fragment_amended_to_match_another_round_still_stops_the_collapse(
    tmp_path: Path,
) -> None:
    """The collision that defeated the first fix, which was a substring test.

    "Does this fragment's demoted text appear in the digest" is sound forward —
    captured implies present — and unsound in the direction the check uses it.
    Amend round 1 to the text round 2 was captured with, and the substring form
    finds it, reports success, and deletes it: the same data loss the check
    exists to prevent, through a second door. Fingerprinting per round closes
    it, because round 1's entry records round 1's bytes.
    """
    repo = _repo(tmp_path)
    branch = "feat/x"
    _on_branch(repo, branch)
    _write_fragment(repo, branch, 1, "# Round 1 — feat/x\n\nalpha\n")
    _write_fragment(repo, branch, 2, "# Round 2 — feat/x\n\nbeta\n")
    _commit_ledger(repo, "rounds")
    ledger.collapse(repo, 96, branch, "main")
    complete = ledger.digest_path(repo, 96).read_bytes()

    # The crash leaves round 1 behind, and it is then amended to be
    # byte-identical to the text round 2 was captured with.
    first = _write_fragment(repo, branch, 1, "# Round 2 — feat/x\n\nbeta\n")
    _commit_ledger(repo, "amend round 1 into round 2's captured text")

    with pytest.raises(ledger.LedgerError, match="content differs"):
        ledger.collapse(repo, 96, branch, "main")

    assert first.is_file()
    assert ledger.digest_path(repo, 96).read_bytes() == complete


def test_an_uncommitted_edit_to_a_surviving_fragment_stops_the_collapse(
    tmp_path: Path,
) -> None:
    """Content is the discriminator on this path, and `git status` is not.

    The edit here is never committed, so the obvious guard would be the
    dirty-ledger refusal — and that guard is wrong here: a crash leaves the
    collapse's own deletions uncommitted, so the directory is expected to be
    dirty on re-entry and refusing on that blocks every recovery. Comparing
    against what the digest holds catches this one and clears those.
    """
    repo = _repo(tmp_path)
    branch = "feat/x"
    _on_branch(repo, branch)
    _write_fragment(repo, branch, 1)
    _commit_ledger(repo, "round 1")
    ledger.collapse(repo, 96, branch, "main")
    survivor = _write_fragment(repo, branch, 1, "# Round 1 — feat/x\n\nUNCOMMITTED\n")

    with pytest.raises(ledger.LedgerError, match="content differs"):
        ledger.collapse(repo, 96, branch, "main")

    assert survivor.is_file()
    assert b"UNCOMMITTED" in survivor.read_bytes()


def test_a_digest_truncated_after_its_header_does_not_authorize_the_delete(
    tmp_path: Path,
) -> None:
    """The fingerprints live in the header, so the header vouches for itself.

    This is the gap the per-round fingerprint could not close on its own. Cut
    the digest to its header — the `Rounds captured:` line included — and every
    question the re-entry check used to ask is answered "yes": `parse_digest`
    succeeds because both contract lines are present, and each fingerprint
    matches because it was written *with* the header. Nothing re-read the body,
    so every fragment was deleted against a digest holding no rounds at all.

    The body-presence conjunct is what sees it. The substring test is unsound as
    the *sole* check — the test above measures exactly that — and sound as a
    conjunct with the fingerprint, because a complete digest inlines each
    round's text verbatim.
    """
    repo = _repo(tmp_path)
    branch = "feat/x"
    _on_branch(repo, branch)
    for n in (1, 2):
        _write_fragment(repo, branch, n)
    _commit_ledger(repo, "rounds")
    ledger.collapse(repo, 96, branch, "main")

    # The crash: the header reached disk, the bodies did not. The inlined titles
    # are demoted, so the first body line is `## Round 1`, not `# Round 1` —
    # splitting on the undemoted spelling silently truncates nothing and the
    # test then passes for the wrong reason.
    digest = ledger.digest_path(repo, 96)
    text = digest.read_text(encoding="utf-8")
    header, separator, body = text.partition("\n## Round 1")
    assert separator, "the fixture found no inlined body to cut"
    assert body, "the fixture found an empty inlined body"
    assert "Rounds captured:" in header, "the fixture must keep the machine-read half"
    digest.write_bytes(header.encode("utf-8"))

    for n in (1, 2):
        _write_fragment(repo, branch, n)
    _commit_ledger(repo, "restore the fragments the crash left behind")

    with pytest.raises(ledger.LedgerError, match="content differs"):
        ledger.collapse(repo, 96, branch, "main")

    assert _fragments(repo, branch), "the fragments are the only copy left"


def test_an_interrupted_digest_install_leaves_nothing_under_the_digests_name(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A partial digest must never be reachable under the digest's own name.

    **The first version of this test was theatre and a review said so.** It
    asserted only that no `.tmp` file survived a successful collapse, which is a
    proxy for the install shape rather than a check on the property. Measured
    against that version: write straight to the final path, then separately write
    and delete a decoy staging file, and it stayed green with the atomic swap
    gone entirely — no stray `.tmp`, and a crash mid-write leaving exactly the
    partial digest `_write_text` promises is unreachable.

    So interrupt the install instead. `os.replace` raises, and the assertions are
    on the property: the swap was reached with the complete text already staged,
    and the digest's own name holds nothing. A direct `path.write_bytes` fails
    both — it never calls `os.replace`, and it leaves a digest behind.
    """
    repo = _repo(tmp_path)
    branch = "feat/x"
    _on_branch(repo, branch)
    for n in (1, 2):
        _write_fragment(repo, branch, n)
    _commit_ledger(repo, "rounds")

    digest = ledger.digest_path(repo, 96)
    staged: list[bytes] = []

    def exploding_replace(src: object, dst: object) -> None:
        staged.append(Path(str(src)).read_bytes())
        raise OSError(INTERRUPTED)

    monkeypatch.setattr(ledger.os, "replace", exploding_replace)

    # The full sentence, escaped. `match="interrupted"` did no discriminating work
    # at all: pytest derives `tmp_path` from this test's own name, truncated to
    # `test_an_interrupted_digest_ins`, so *any* OSError quoting a path under the
    # repo matched it — measured, a `FileNotFoundError` from a missing parent
    # directory satisfied it while `os.replace` was never reached.
    with pytest.raises(OSError, match=re.escape(INTERRUPTED)):
        ledger.collapse(repo, 96, branch, "main")

    strays = sorted(p.name for p in digest.parent.iterdir())
    assert strays == [], (
        "a failed publication left staging bytes beside the digests: "
        f"{strays}. `/squash-merge` stages this directory with `git add -A`, "
        "so a stray rides into the merge commit rather than merely sitting there"
    )
    assert staged, "the install never reached os.replace — it is not a swap"
    assert b"Rounds captured:" in staged[0], "the swap ran before the text was whole"
    assert b"## Round 2" in staged[0], "the staged digest was missing an inlined round"
    assert not digest.exists(), (
        "a digest exists under its own name after an interrupted install"
    )
    assert _fragments(repo, branch), "the fragments were deleted anyway"


def test_a_setext_fragment_beside_a_digest_reports_content_not_formatting(
    tmp_path: Path,
) -> None:
    """The advice the old message gave destroyed the evidence.

    A digest cannot have captured a setext-bearing fragment — composing one
    refuses — so a setext heading on a fragment found beside an existing digest
    means the fragment was amended after the digest was written. That is the
    `content differs` case. But `demote_headings` raised first, and its message
    reads "Rewrite the heading as ATX": an operator who follows it edits the
    surviving fragment, changing the one copy of the content the digest does not
    hold, and never sees the message saying both artifacts are intact.
    """
    repo = _repo(tmp_path)
    branch = "feat/x"
    _on_branch(repo, branch)
    _write_fragment(repo, branch, 1)
    _commit_ledger(repo, "round 1")
    ledger.collapse(repo, 96, branch, "main")

    survivor = _write_fragment(repo, branch, 1, "Round 1\n=======\n\nbody\n")
    _commit_ledger(repo, "amend round 1 with a setext title")

    with pytest.raises(ledger.LedgerError) as caught:
        ledger.collapse(repo, 96, branch, "main")

    message = str(caught.value)
    assert "content differs" in message
    assert "Rewrite the heading as ATX" not in message, (
        "the recovery path must not tell the operator to edit the surviving copy"
    )
    assert survivor.is_file()


def test_a_fragment_that_is_not_utf8_refuses_instead_of_raising(
    tmp_path: Path,
) -> None:
    """`UnicodeDecodeError` is not a `LedgerError`, so `main` never caught it.

    An editor that saved a fragment as cp1252 — an em dash as the single byte
    0x97 — killed the process with a stack trace naming the codec rather than
    the file. `/review-brief` and `/squash-merge` branch on the exit status, and
    this is the same shape the `.isdecimal()` guard closes for round numbers.
    """
    repo = _repo(tmp_path)
    branch = "feat/x"
    _on_branch(repo, branch)
    path = _write_fragment(repo, branch, 1)
    path.write_bytes("# Round 1\n\nan em dash — here\n".encode("cp1252"))
    _commit_ledger(repo, "round 1")

    with pytest.raises(ledger.LedgerError, match="not valid UTF-8"):
        ledger.collapse(repo, 96, branch, "main")


def test_the_cli_exits_one_on_a_fragment_that_is_not_utf8(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The contract the skills actually read is the exit status, so pin that too.

    A `LedgerError` raised where no caller converts it to an exit code would
    still be a traceback; this is the end-to-end half.
    """
    repo = _repo(tmp_path)
    branch = "feat/x"
    _on_branch(repo, branch)
    path = _write_fragment(repo, branch, 1)
    path.write_bytes("# Round 1\n\nan em dash — here\n".encode("cp1252"))
    _commit_ledger(repo, "round 1")

    code = ledger.main(
        ["--root", str(repo), "collapse", "--pr", "96", "--base", "main"]
    )

    assert code == 1
    assert "not valid UTF-8" in capsys.readouterr().err


def test_the_cli_exits_one_when_the_filesystem_fails(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """`OSError` escaped the same contract the decode guard above closes.

    The module promises exit 1 with the reason on stderr for any failure, and
    every subcommand touches the filesystem — ten functions call something that
    raises `OSError` and none of them wraps it. `main` caught only
    `LedgerError`, so a collapse against an unreadable digest died with a
    `PermissionError` traceback naming an errno instead of a reason. Found by
    Copilot on PR #96, and reproduced against this module before the fix: it
    surfaced from `_read_text`, not from the `_write_text` the finding named,
    which is why the fix is one handler in `main` rather than a wrapper at the
    site.

    The fault is injected at `os.replace` rather than by arranging a real
    permission error, because a test that depends on filesystem ACLs answers
    differently per platform and per CI runner — and this assertion is about
    `main`'s handler, not about which syscall failed.
    """
    repo = _repo(tmp_path)
    branch = "feat/x"
    _on_branch(repo, branch)
    _write_fragment(repo, branch, 1)
    _commit_ledger(repo, "round 1")

    def exploding_replace(src: object, dst: object) -> None:
        raise PermissionError(13, "Permission denied")

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(ledger.os, "replace", exploding_replace)
        code = ledger.main(
            ["--root", str(repo), "collapse", "--pr", "96", "--base", "main"]
        )

    captured = capsys.readouterr()
    assert code == 1, "an OSError must not escape as a traceback"
    assert "filesystem error" in captured.err
    assert "Permission denied" in captured.err
    assert captured.out == "", (
        "the reason belongs on stderr — /review-brief captures stdout into a "
        "shell variable and would name a fragment after it"
    )


def test_the_cli_exits_one_when_reading_an_existing_digest_fails(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The other branch — and the one the original reproduction actually hit.

    `collapse` has two paths that touch the filesystem, and they fail in
    different places: a first collapse writes, while a re-entry *reads* the
    existing digest at `parse_digest(_read_text(digest))`. The test above
    injects at `os.replace`, which only the write path reaches, so a smoke
    round mutated the read path to re-raise its `OSError` as a type `main`
    does not catch — reinstating exactly the pre-fix traceback on the
    digest-exists branch — and the whole suite stayed green.

    That gap mattered here more than it usually would: the failure this fix
    was written for was reproduced on *this* branch, out of `_read_text`, not
    out of the `_write_text` the finding named. One handler covers both, but
    a test that never enters the recovery path cannot show it.
    """
    repo = _repo(tmp_path)
    branch = "feat/x"
    _on_branch(repo, branch)
    _write_fragment(repo, branch, 1)
    _commit_ledger(repo, "round 1")
    ledger.collapse(repo, 96, branch, "main")
    assert ledger.digest_path(repo, 96).is_file(), "fixture must reach re-entry"

    # A second collapse takes the `digest.exists()` branch, whose first act is
    # to read that digest.
    _write_fragment(repo, branch, 1)
    real_read = Path.read_text

    # The parameters are spelled out rather than taken as `*args: object`:
    # pyright --strict cannot forward `object` into `read_text`'s `str | None`
    # parameters, and three errors on one line is the cheapest possible reminder
    # that a passthrough double is still typed code.
    def exploding_read(
        self: Path,
        encoding: str | None = None,
        errors: str | None = None,
        newline: str | None = None,
    ) -> str:
        if self == ledger.digest_path(repo, 96):
            raise PermissionError(13, "Permission denied")
        return real_read(self, encoding=encoding, errors=errors, newline=newline)

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(Path, "read_text", exploding_read)
        code = ledger.main(
            ["--root", str(repo), "collapse", "--pr", "96", "--base", "main"]
        )

    captured = capsys.readouterr()
    assert code == 1, "an OSError on the recovery path must not escape either"
    assert "filesystem error" in captured.err
    assert "Permission denied" in captured.err
    assert captured.out == ""


def test_the_recovery_message_names_what_it_actually_deleted(tmp_path: Path) -> None:
    """It reported the digest's captured count, which is not a measurement.

    With an empty-but-present directory it claimed three fragments removed
    having removed none. `/squash-merge` step 2 tells the operator to read this
    line and decide whether to commit from it, so the count is load-bearing.

    The *label* is load-bearing for the same reason and was wrong for longer.
    This state is not an interrupted collapse — an interrupted collapse leaves
    fragments behind and this branch is reached only when there are none — and
    the skill routes `finished an interrupted collapse` to `git add`/`git
    commit`. Removing an untracked empty directory stages nothing, so the
    operator was sent into a `git commit` that exits 1, in a checklist whose
    rule is to stop at any failing step. `already collapsed` is both accurate
    and the row whose action is "skip the rest of step 2".
    """
    repo = _repo(tmp_path)
    branch = "feat/x"
    _on_branch(repo, branch)
    for n in (1, 2, 3):
        _write_fragment(repo, branch, n)
    _commit_ledger(repo, "rounds")
    ledger.collapse(repo, 96, branch, "main")
    ledger.fragment_dir(repo, ledger.branch_hash(branch)).mkdir(parents=True)

    message = ledger.collapse(repo, 96, branch, "main")

    # The whole message, not a substring of it. "3 " is absent from a message
    # that reinstates the bug as `...removed.(captured3)` — measured — so a
    # substring check leaves the property unpinned, and "no digit anywhere" is
    # unusable because the digest's own path carries digits. Exact equality is
    # the only form that reddens wherever a count is reintroduced.
    assert message == (
        "already collapsed: specs/reviews/angle-ledger/digests/0/pr96.md "
        "exists, and the now-empty fragment directory beside it is removed."
    )


def test_a_digest_recording_another_branch_stops_the_collapse(tmp_path: Path) -> None:
    """A wrong ``--pr``, or a branch renamed after its rounds were written."""
    repo = _repo(tmp_path)
    _on_branch(repo, "feat/other")
    _write_fragment(repo, "feat/other", 1)
    _commit_ledger(repo, "rounds")
    ledger.collapse(repo, 96, "feat/other", "main")

    _write_fragment(repo, "feat/x", 1)
    with pytest.raises(ledger.LedgerError, match="records branch hash"):
        ledger.collapse(repo, 96, "feat/x", "main")


def test_an_uncommitted_fragment_stops_the_collapse(tmp_path: Path) -> None:
    """Allocation writes something; only a commit makes it durable (ADR-0072 §5).

    Inlining an untracked fragment would publish content no review saw and then
    delete the only copy of it.
    """
    repo = _repo(tmp_path)
    branch = "feat/x"
    _on_branch(repo, branch)
    _write_fragment(repo, branch, 1)  # never committed

    with pytest.raises(ledger.LedgerError, match="uncommitted changes"):
        ledger.collapse(repo, 96, branch, "main")

    assert ledger.digest_path(repo, 96).exists() is False


def test_a_modified_fragment_stops_the_collapse(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    branch = "feat/x"
    _on_branch(repo, branch)
    path = _write_fragment(repo, branch, 1)
    _commit_ledger(repo, "rounds")
    path.write_bytes(b"# Round 1 - edited after the commit\n")

    with pytest.raises(ledger.LedgerError, match="uncommitted changes"):
        ledger.collapse(repo, 96, branch, "main")


def test_a_fragment_this_branch_did_not_add_stops_the_collapse(tmp_path: Path) -> None:
    """ADR-0072 §8's provenance cross-check, with the hazard it exists for.

    Six hex characters is an honest bound rather than an absolute one. If two
    branch names ever share a directory, the collapse must not sweep up rounds
    it did not write — so ``<base>...HEAD`` decides, not the listing.
    """
    repo = _repo(tmp_path)
    branch = "feat/x"
    # Round 1 lands on main, before the branch exists.
    _write_fragment(repo, branch, 1)
    _commit_ledger(repo, "a fragment already on the base")
    _on_branch(repo, branch)
    _write_fragment(repo, branch, 2)
    _commit_ledger(repo, "this branch's round")

    with pytest.raises(ledger.LedgerError, match="did not add"):
        ledger.collapse(repo, 96, branch, "main")

    assert ledger.digest_path(repo, 96).exists() is False


def test_a_branch_with_no_rounds_collapses_to_nothing(tmp_path: Path) -> None:
    """Most PRs run no external round; that must not stop a merge."""
    repo = _repo(tmp_path)
    _on_branch(repo, "feat/x")
    message = ledger.collapse(repo, 96, "feat/x", "main")
    assert "nothing to collapse" in message
    assert ledger.digest_path(repo, 96).exists() is False


def test_a_detached_head_refuses_rather_than_hashing_the_word_head(
    tmp_path: Path,
) -> None:
    """Every detached session would otherwise share one ledger directory."""
    repo = _repo(tmp_path)
    _git(repo, "checkout", "-q", "--detach")
    with pytest.raises(ledger.LedgerError, match="detached HEAD"):
        ledger.current_branch(repo)


# --------------------------------------------------------------------------
# The gate on `main`
# --------------------------------------------------------------------------


def test_a_clean_tree_reports_no_uncollapsed_fragments(tmp_path: Path) -> None:
    (tmp_path / "specs" / "reviews").mkdir(parents=True)
    assert ledger.check_collapsed(tmp_path) == []


def test_every_uncollapsed_fragment_is_named(tmp_path: Path) -> None:
    repo = tmp_path / "r"
    _write_fragment(repo, "feat/x", 1)
    _write_fragment(repo, "feat/other", 2)
    found = ledger.check_collapsed(repo)
    assert len(found) == 2
    prefix = "specs/reviews/angle-ledger/branches/"
    assert all(path.startswith(prefix) for path in found)
    assert all("\\" not in path for path in found)


def test_a_digest_alone_is_what_the_gate_wants_to_see(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    branch = "feat/x"
    _on_branch(repo, branch)
    _write_fragment(repo, branch, 1)
    _commit_ledger(repo, "rounds")
    ledger.collapse(repo, 96, branch, "main")
    assert ledger.check_collapsed(repo) == []
    assert ledger.digest_path(repo, 96).is_file()


# --------------------------------------------------------------------------
# The skill quotes this module's outcomes; pin them to each other
# --------------------------------------------------------------------------


def _collapse_return_prefixes() -> list[str]:
    """The literal text each `return` in `collapse` begins with, read from the AST.

    Not a text search over the file. `assert outcome in source` was the previous
    instrument and it answers the wrong question twice: it is satisfied by a
    *comment* that happens to quote the phrase, so rewording a return statement
    while the old wording survives nearby leaves it green; and it can only ask
    about strings someone thought to list, which is how a fifth return message
    was added and named nowhere.

    Every return is either a plain string or an f-string whose first part is a
    literal, so the leading constant is the operator-visible prefix.
    """
    tree = ast.parse(Path(ledger.__file__).read_text(encoding="utf-8"))
    func = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef) and node.name == "collapse"
    )
    prefixes: list[str] = []
    for node in ast.walk(func):
        if not isinstance(node, ast.Return) or node.value is None:
            continue
        value = node.value
        if isinstance(value, ast.Constant):
            assert isinstance(value.value, str), f"non-str return {value.value!r}"
            prefixes.append(value.value)
            continue
        assert isinstance(value, ast.JoinedStr), (
            f"`collapse` returns an unhandled expression shape at line "
            f"{node.lineno}; this oracle can only read literals and f-strings."
        )
        leading = value.values[0]
        interpolated = (
            f"the return at line {node.lineno} opens with an interpolation, so "
            "it has no fixed prefix an operator or the skill's table can match."
        )
        assert isinstance(leading, ast.Constant), interpolated
        assert isinstance(leading.value, str), interpolated
        prefixes.append(leading.value)
    assert prefixes, "found no return statements in `collapse` — the oracle is blind"
    return prefixes


def test_every_read_write_or_delete_in_the_module_goes_through_a_named_helper() -> None:
    """Reads, writes and deletions in `scripts/ledger.py` route through a helper.

    Named for exactly what it watches. An earlier version called itself "every
    file access", and a reviewer landed `path.unlink()` in the module and watched
    it stay green — the name outran the mechanism, which is the same defect as the
    stale count this test was written to replace, arriving one level up. Deletion
    is now watched, and what stays unwatched is listed below rather than implied.

    A raw `path.read_text(...)` raises `UnicodeDecodeError`, which is not a
    `LedgerError`, so `main` does not catch it and the process dies with a
    traceback naming the codec instead of the file — the contract `/review-brief`
    and `/squash-merge` branch on. `_read_text` exists to convert that into a
    refusal. `_write_text` is the mirror: `Path.write_text` opens in text mode and
    would translate every LF to CRLF on Windows, against `.gitattributes eol=lf`
    (CLAUDE.md § PowerShell file encoding), and it is also where the digest's
    atomic install lives. (Named as LF and CRLF rather than written as escapes:
    this sentence carried the escaped forms, and writing it through a heredoc
    turned them into real newlines mid-docstring — the corruption this
    repository's instructions warn about, caught here by a reviewer.)

    **Read/write is deliberately not the axis this test splits on, and that is the
    whole point of its shape.** Three earlier versions tried, and each failed in a
    way the next had to repair:

    1. Watching `read_text` calls only — an aliased `reader = path.read_text` then
       `reader(...)` walked straight past, and so did a raw `open()`.
    2. Watching the bare name `open` as well — now `path.open("w", ...)` was
       refused as a read bypassing the read guard, which is a false accusation
       against a write that has no decode hazard at all.
    3. Parsing the mode to tell reads from writes — `io.open(file, mode, ...)`
       takes mode at index 1 while `path.open(mode, ...)` takes it at 0, so a
       write through `io.open` was accused all over again; and scanning `open` at
       call sites only silently lost the bound builtin `reader = open`, which the
       version before it had caught.

    Every one of those defects came from trying to decide *what kind* of access a
    call is. So this version does not ask. The invariant is that **no read, write
    or deletion happens outside the sanctioned helpers** — which needs no mode
    parsing, and gives an honest message either way: a stray read is pointed at
    `_read_text`, a stray write at `_write_text`, a stray deletion at
    `_remove_dir`. None is accused of being another.

    Deletion is watched because it is this module's whole subject: the collapse
    deletes fragments, and a stray deletion outside `_remove_dir` is precisely
    the data loss every refusal here exists to prevent.

    **The watched set is derived from this module's imports, once, rather than
    from the mutations that caught things.** That distinction is the point. Four
    consecutive rounds extended this test by exactly the one token the last
    mutation had used — `open`, then the mode split, then `unlink`/`rmtree`, then
    `os.remove` — and each time the next round found the next sibling. Reactive
    extension guarantees a next sibling; enumerating the surface does not.
    `scripts/ledger.py`'s file-touching imports are `io`, `os`, `shutil` and
    `pathlib.Path`, and `qualified`/`bare` below are that surface. **A new import
    is the trigger to revisit this list**, and it is the only trigger; a new
    mutation is not.

    **What this oracle cannot see, and why** — each entry a decision, not an
    omission:

    - **`subprocess`.** The module imports it and shells out to `git`, and a
      destructive argv (`git clean -fdx <dir>`) is invisible here by
      construction: the verb lives in a list argument, not in an attribute name,
      so **no addition to the sets below can reach it** — this one is unlike the
      four "next sibling" gaps above, which were finite tokens. Measured: such a
      call inserted into `collapse` left this test green. What makes the module
      safe today is not this test but its two `subprocess.run` sites:
      `current_branch` passes a fixed `symbolic-ref`, and `_git` is a **generic
      runner whose argv comes from its callers** — of which there are two today,
      passing `status --porcelain` and `diff --name-only`. So `_git`'s safety is
      a property of its call sites rather than of itself, and a future caller
      passing a destructive verb is the realistic way this reopens. An
      argv-inspecting check is possible and deliberately not built: it would be
      exactly the kind of clever classification that caused four of this test's
      six rounds of defects.
    - **`replace` and `rename` as *bare* attributes.** `str.replace` occurs twice
      in this module (`_rel`, `check_collapsed`), so watching the bare token fires
      two false positives on the first run — the over-reach an earlier round of
      this same test already paid for. The `os.` forms *are* watched, via
      `qualified`, which is what covers the atomic install. The residual blind
      spot is a bare `path.replace(...)` / `path.rename(...)`.
    - **An aliased import** — `import os as o`, or `from os import remove`.
      `qualified` keys on the literal names `io`/`os`/`shutil`. Inert today (no
      such import exists) and, unlike `subprocess`, closeable by this same
      mechanism if one ever appears; changing how a module is imported counts as
      the new-import trigger above.
    - **`mkdir` and `makedirs`.** Creating a directory destroys nothing, and
      `collapse` legitimately calls `mkdir`.
    - **Metadata reads** — `iterdir`, `glob`, `rglob`, `exists`, `is_file`,
      `is_dir`, `stat`. They read the directory, never a file's contents.

    Scanned at *attribute access* level, called or merely bound, so aliasing a
    watched *attribute* is caught. Read from the AST rather than by grep so a
    mention in a comment or docstring cannot satisfy it.
    """
    # Module-qualified: unambiguous, so tokens that collide with common string
    # methods are safe to watch here even though they are not safe bare.
    qualified = {
        ("io", "open"),
        ("os", "remove"),
        ("os", "removedirs"),
        ("os", "rename"),
        ("os", "renames"),
        ("os", "replace"),
        ("os", "rmdir"),
        ("os", "truncate"),
        ("os", "unlink"),
        ("shutil", "copy"),
        ("shutil", "copy2"),
        ("shutil", "copyfile"),
        ("shutil", "copytree"),
        ("shutil", "move"),
        ("shutil", "rmtree"),
    }
    # Bare attribute names: `Path` methods with no colliding builtin-type method.
    bare = {
        "open",
        "read_bytes",
        "read_text",
        "rmdir",
        "rmtree",
        "unlink",
        "write_bytes",
        "write_text",
    }
    guards = {"_read_text", "_write_text", "_remove_dir"}

    def accesses_in(root: ast.AST) -> set[str]:
        found: set[str] = set()
        for node in ast.walk(root):
            if isinstance(node, ast.Attribute):
                owner = node.value
                if isinstance(owner, ast.Name) and (owner.id, node.attr) in qualified:
                    found.add(f"line {node.lineno}: {owner.id}.{node.attr}")
                elif node.attr in bare:
                    found.add(f"line {node.lineno}: .{node.attr}")
            elif isinstance(node, ast.Name) and node.id == "open":
                # The bare builtin, called or bound. Dropping this is what let
                # `reader = open` through in the version before this one.
                found.add(f"line {node.lineno}: open")
        return found

    tree = ast.parse(Path(ledger.__file__).read_text(encoding="utf-8"))
    helpers = {
        node.name: node
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef) and node.name in guards
    }
    # Not a bare `next(...)`: that raised `StopIteration` with no message when the
    # guard was renamed, so the explanatory assert was unreachable and pytest
    # reported only a line number.
    missing = sorted(guards - set(helpers))
    assert not missing, (
        f"{missing} gone or renamed, so this oracle cannot tell a guarded access "
        "from an unguarded one. Point it at the new helpers."
    )

    allowed: set[str] = set()
    for name, node in helpers.items():
        found = accesses_in(node)
        assert found, f"`{name}` no longer touches a file — this oracle is blind"
        allowed |= found

    offenders = sorted(accesses_in(tree) - allowed)
    assert offenders == [], (
        f"these accesses bypass {sorted(guards)}. Route a read through "
        "`_read_text` (a raw one raises UnicodeDecodeError instead of a "
        "refusal), a write through `_write_text` (a raw one can emit CRLF "
        "against .gitattributes eol=lf, and skips the atomic install), and a "
        f"deletion through `_remove_dir`: {offenders}"
    )


def _skill_outcome_rows() -> dict[str, str]:
    """Each of step 2's table rows: its backticked label mapped to its action cell.

    Scoped to the table, not to the file. `assert outcome in skill` was the
    previous instrument on this side and it is the same defect the AST reader
    above exists to fix, left standing on the other half of the same test:
    measured, deleting the `already collapsed` row outright kept this test green,
    because the phrase also appears in step 2's own explanatory prose and in
    step 5. Every one of the labels occurs somewhere outside the table, so the
    whole-file check could not fail for any of them.

    The **action** cell is returned alongside the label because the label alone
    was not enough either: measured, swapping the "Then" cells between two rows
    left every label intact and this test green — and routing an outcome to the
    wrong action is the defect the whole table exists to prevent, and the one
    that actually shipped.
    """
    skill = (
        Path(ledger.__file__).parent.parent
        / ".claude"
        / "skills"
        / "squash-merge"
        / "SKILL.md"
    ).read_text(encoding="utf-8")
    step2 = skill.partition("\n## 2.")[2].partition("\n## 3.")[0]
    assert step2, "could not locate /squash-merge step 2 — has it been renumbered?"
    rows: dict[str, str] = {}
    for line in step2.split("\n"):
        if not line.startswith("|"):
            continue
        cells = line.split("|")
        label = re.match(r"^`([^`]+)`$", cells[1].strip())
        if label is None:
            continue
        assert len(cells) >= 5, f"outcome row has no action cell: {line!r}"
        name = label.group(1).rstrip(" …")
        # Refuse a repeat rather than let the dict keep the last one. Measured: a
        # contradictory duplicate `already collapsed` row inserted above the real
        # one left this test green, because last-write-wins discarded the bad row
        # before any assertion saw it — and a copy-pasted table row is exactly the
        # edit that produces this. Same defect the digest header had, one artifact
        # over: `parse_digest` dropped a repeated round entry in silence too.
        assert name not in rows, (
            f"step 2's table names {name!r} twice, so an operator reading it top "
            "to bottom and one reading it bottom to top get different actions"
        )
        rows[name] = cells[3].strip()
    assert rows, "step 2's outcome table has no rows an operator could match on"
    return rows


def test_every_collapse_outcome_is_named_in_the_skill_and_every_row_is_real() -> None:
    """`/squash-merge` step 2 tells the operator what to do per outcome.

    It does that by quoting `collapse`'s return messages, which makes the skill
    a second representation of strings this module owns — the drift shape
    ADR-0072 §7 records happening three times to one document, and the reason
    §10 keeps a list of rules "enforced by nothing".

    **Both directions, because only one of them was checked and the other is the
    one that failed.** The previous version asserted that a hand-listed set of
    phrases appeared in each file. `collapse` then grew a return the list did not
    cover — an empty fragment directory beside a complete digest — and it was
    labelled `finished an interrupted collapse`, whose row says "commit and
    push". It stages nothing, so the operator met a `git commit` that exits 1.
    Nothing reddened, because every listed phrase was still present. The reverse
    direction below is what sees that: a return whose prefix no row names fails,
    whether it is new or merely mislabelled.

    **Two measured sets, not a hand-written list in the middle.** Each side is
    read from the artifact that owns it — the returns from `collapse`'s AST, the
    rows from step 2's table — so neither derives from the other and the
    comparison can fail in both directions. A hand-written third list would have
    to be kept in step with both, and is the thing most likely to be the stale
    copy.

    **And the actions, which the label check alone did not cover.** Measured
    against the version that checked labels only: swapping the "Then" cells
    between two rows — commit and push on `already collapsed`, skip the read-back
    after a fresh collapse — left every label intact and this test green. Routing
    an outcome to the wrong action is the defect the table exists to prevent and
    the one that actually shipped, so `actions` below pins it. That list *is*
    hand-written, unavoidably: which outcomes have something to commit is a fact
    about what `collapse` did to the working tree, and no reading of either
    artifact yields it.
    """
    rows = _skill_outcome_rows()
    prefixes = _collapse_return_prefixes()

    for prefix in prefixes:
        matching = [row for row in rows if prefix.startswith(row)]
        assert matching, (
            f"`collapse` can return a message beginning {prefix!r}, which no row "
            "of /squash-merge step 2 names — the operator has no action for it"
        )
        assert len(matching) == 1, (
            f"the message beginning {prefix!r} matches {len(matching)} rows "
            f"({matching}) — an operator reading the table cannot tell which "
            "action applies"
        )
    for row in rows:
        assert any(prefix.startswith(row) for prefix in prefixes), (
            f"/squash-merge step 2 has a row for {row!r}, which is not the prefix "
            "of any message `collapse` can return — a dead row sends the operator "
            "looking for output that never appears"
        )

    # Hand-written, because it is the one fact neither artifact states: whether
    # `collapse` left anything for `git add` to stage. `already collapsed`
    # removes at most an untracked empty directory, and `nothing to collapse`
    # touches nothing at all, so both stage nothing and a `git commit` on either
    # exits 1 mid-checklist. That is the defect this round fixed.
    actions = {
        "nothing to collapse": "skip",
        "already collapsed": "skip",
        "finished an interrupted collapse": "commit and push",
        "collapsed round(s)": "commit and push",
    }
    assert set(actions) == set(rows), (
        "step 2's rows and this test's action oracle disagree about which "
        f"outcomes exist: table {sorted(rows)}, oracle {sorted(actions)}"
    )
    for row, expected in actions.items():
        assert rows[row].startswith(expected), (
            f"step 2 routes {row!r} to {rows[row]!r}, but that outcome stages "
            f"{'nothing' if expected == 'skip' else 'a change'}, so its action "
            f"must begin {expected!r}"
        )


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------


def test_the_gate_exits_nonzero_and_names_the_files(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    repo = tmp_path / "r"
    _write_fragment(repo, "feat/x", 1)
    code = ledger.main(["--root", str(repo), "check-collapsed"])
    out = capsys.readouterr().err
    assert code == 1
    assert "specs/reviews/angle-ledger/branches/" in out
    assert "round-1.md" in out


def test_the_gate_exits_zero_on_a_collapsed_tree(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    (tmp_path / "specs" / "reviews").mkdir(parents=True)
    assert ledger.main(["--root", str(tmp_path), "check-collapsed"]) == 0
    assert "no uncollapsed fragments" in capsys.readouterr().out


def test_next_round_prints_the_number_a_skill_can_read(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    repo = tmp_path / "r"
    _write_fragment(repo, "feat/x", 7)
    code = ledger.main(["--root", str(repo), "next-round", "--branch", "feat/x"])
    assert code == 0
    assert capsys.readouterr().out.strip() == "8"


def test_a_refusal_exits_one_and_prints_the_reason(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    repo = _repo(tmp_path)
    _on_branch(repo, "feat/x")
    _write_fragment(repo, "feat/x", 1)  # uncommitted
    code = ledger.main(
        ["--root", str(repo), "collapse", "--pr", "96", "--branch", "feat/x"]
    )
    assert code == 1
    captured = capsys.readouterr()
    assert "uncommitted changes" in captured.err
    assert captured.out == "", "a refusal on stdout becomes the round number"


@pytest.mark.parametrize("pr", ["0", "-3"])
def test_a_pr_number_must_be_positive(pr: str, tmp_path: Path) -> None:
    """`--root` is pinned even though argparse rejects the value first.

    Without it this test defaults `--root` to the live checkout, so the exact
    mutation it exists to catch — weakening `_positive_int` — turns the test
    body into a real collapse of whatever branch the suite is running on,
    composing a `pr0.md` digest and `rmtree`-ing the developer's own fragment
    directory. Every other git-backed test here builds its own repo for that
    reason; this one had no repo and so looked exempt.
    """
    with pytest.raises(SystemExit):
        ledger.main(["--root", str(tmp_path), "collapse", "--pr", pr])

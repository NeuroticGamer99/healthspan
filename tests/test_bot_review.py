"""Bot-review glue (scripts/bot_review.py): identity, time, shape, counting.

Every test here is a regression for a defect that shipped in the prose version
of this logic and failed *silently* — the reason it became a script. The
values are the real ones observed on PR #27, transcribed; nothing here talks
to the network.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import pytest
from bot_review import (
    BOTS,
    BotReviewError,
    as_page,
    count_note,
    is_findings_review,
    parse_ts,
    run_cmd,
    same_login,
    select_review,
    stated_count,
)

CODERABBIT = BOTS["coderabbit"]
COPILOT = BOTS["copilot"]


def _review(
    review_id: int,
    login: str,
    submitted_at: str,
    body: str = "**Actionable comments posted: 1**",
) -> dict[str, Any]:
    return {
        "id": review_id,
        "user": {"login": login},
        "submitted_at": submitted_at,
        "body": body,
    }


# --------------------------------------------------------------------------
# Time: GitHub stamps UTC, git stamps a local offset
# --------------------------------------------------------------------------


def test_parse_ts_normalizes_utc_and_offset_to_the_same_instant() -> None:
    assert parse_ts("2026-07-16T21:38:39Z") == parse_ts("2026-07-16T17:38:39-04:00")


def test_local_offset_floor_no_longer_admits_a_stale_review() -> None:
    # The bug: `git log --format=%cI` yields 17:38:39-04:00 (= 21:38:39Z), and a
    # jq *string* compare ranks the 18:09:41Z review of a superseded commit as
    # newer than it ("18" > "17"), so the floor admits every stale review.
    floor_local = "2026-07-16T17:38:39-04:00"
    stale = "2026-07-16T18:09:41Z"
    assert stale > floor_local  # the string comparison that shipped
    assert parse_ts(stale) < parse_ts(floor_local)  # the instants, compared honestly


def test_parse_ts_refuses_a_naive_timestamp_rather_than_guessing() -> None:
    with pytest.raises(BotReviewError, match="no timezone"):
        parse_ts("2026-07-16T21:38:39")


def test_parse_ts_rejects_junk() -> None:
    with pytest.raises(BotReviewError, match="cannot parse"):
        parse_ts("not-a-timestamp")


# --------------------------------------------------------------------------
# Shape: a bot's reply is a review with an empty body
# --------------------------------------------------------------------------


def test_reply_review_is_not_a_findings_review() -> None:
    # PR #27 id=4717870418: CodeRabbit's "confirmed — this addresses the
    # finding" ack, which GitHub models as a review with an empty body.
    ack = _review(4717870418, "coderabbitai[bot]", "2026-07-16T21:39:21Z", body="")
    assert is_findings_review(ack, CODERABBIT) is False


def test_findings_review_is_recognized() -> None:
    review = _review(4716736483, "coderabbitai[bot]", "2026-07-16T18:47:40Z")
    assert is_findings_review(review, CODERABBIT) is True


def test_another_bots_review_is_not_ours() -> None:
    review = _review(1, "copilot-pull-request-reviewer[bot]", "2026-07-16T18:47:40Z")
    assert is_findings_review(review, CODERABBIT) is False


def test_select_skips_the_ack_and_takes_the_findings_review() -> None:
    # The exact PR #27 situation: the ack (21:39:21) is newer than the findings
    # review (21:44:46 arrives later), so "newest review" alone picks the ack.
    since = parse_ts("2026-07-16T21:38:39Z")
    ack = _review(4717870418, "coderabbitai[bot]", "2026-07-16T21:39:21Z", body="")
    findings = _review(4717901141, "coderabbitai[bot]", "2026-07-16T21:44:46Z")
    chosen = select_review([ack, findings], CODERABBIT, since)
    assert chosen is not None
    assert chosen["id"] == 4717901141


def test_select_returns_none_when_only_an_ack_has_landed() -> None:
    since = parse_ts("2026-07-16T21:38:39Z")
    ack = _review(4717870418, "coderabbitai[bot]", "2026-07-16T21:39:21Z", body="")
    assert select_review([ack], CODERABBIT, since) is None


def test_select_excludes_reviews_at_or_before_the_floor() -> None:
    since = parse_ts("2026-07-16T21:38:39Z")
    stale = _review(4716736483, "coderabbitai[bot]", "2026-07-16T18:47:40Z")
    assert select_review([stale], CODERABBIT, since) is None


def test_select_takes_the_newest_of_several_findings_reviews() -> None:
    since = parse_ts("2026-07-16T00:00:00Z")
    older = _review(1, "coderabbitai[bot]", "2026-07-16T18:09:41Z")
    newer = _review(2, "coderabbitai[bot]", "2026-07-16T18:47:40Z")
    chosen = select_review([older, newer], CODERABBIT, since)
    assert chosen is not None
    assert chosen["id"] == 2


def test_select_accepts_a_floor_expressed_in_local_time() -> None:
    # The recovery path: --since-commit hands back UTC, but a caller passing a
    # local-offset stamp must still get instant semantics, not string semantics.
    since = parse_ts("2026-07-16T17:38:39-04:00")
    stale = _review(1, "coderabbitai[bot]", "2026-07-16T18:09:41Z")
    fresh = _review(2, "coderabbitai[bot]", "2026-07-16T21:44:46Z")
    chosen = select_review([stale, fresh], CODERABBIT, since)
    assert chosen is not None
    assert chosen["id"] == 2  # not the 18:09:41Z one the string compare preferred


# --------------------------------------------------------------------------
# Identity: Copilot answers to two logins
# --------------------------------------------------------------------------


def test_copilot_review_is_authored_by_the_bot_login() -> None:
    review = _review(
        4716560445,
        "copilot-pull-request-reviewer[bot]",
        "2026-07-16T18:23:24Z",
        body="Copilot reviewed 5 out of 5 changed files and generated 1 comment.",
    )
    assert is_findings_review(review, COPILOT) is True


def test_copilot_is_requestable_under_one_login_and_displayed_under_another() -> None:
    # Requesting the display login `Copilot` returns HTTP 200 and adds no one;
    # the bot login is the one GitHub accepts, and `Copilot` is what it then
    # shows in requested_reviewers. The comment author is deliberately absent
    # from the contract: comments are fetched through the review id, so nothing
    # ever matches Copilot's display login against a comment.
    assert COPILOT.request_login == "copilot-pull-request-reviewer[bot]"
    assert COPILOT.requested_display == "Copilot"
    assert COPILOT.review_login == "copilot-pull-request-reviewer[bot]"
    assert CODERABBIT.request_login is None  # reviews every push on its own
    assert CODERABBIT.requested_display is None


@pytest.mark.parametrize(
    "impostor",
    [
        "not-coderabbitai[bot]",
        "coderabbitai-fan",
        "coderabbit",
        "xcoderabbitai[bot]",
    ],
)
def test_a_lookalike_login_cannot_pass_for_the_bot(impostor: str) -> None:
    # The identity was a case-insensitive *substring* match, so every login here
    # passed for CodeRabbit — enough to select an unrelated account's review, or
    # to satisfy the requested-reviewer check that exists to prove a request
    # took effect. The contract is now whole-login.
    assert impostor.casefold().find("coderabbit") != -1  # the old match hit
    assert same_login(impostor, CODERABBIT.review_login) is False
    review = _review(1, impostor, "2026-07-16T18:47:40Z")
    assert is_findings_review(review, CODERABBIT) is False


def test_login_comparison_is_case_folded_but_whole_string() -> None:
    # GitHub preserves the case you typed but treats logins as unique
    # case-folded, so the real bot under a different case is still the bot ...
    assert same_login("CodeRabbitAI[bot]", "coderabbitai[bot]") is True
    # ... while a superstring is not.
    assert same_login("coderabbitai[bot]2", "coderabbitai[bot]") is False


# --------------------------------------------------------------------------
# Counting: the body's claim is evidence, not truth
# --------------------------------------------------------------------------


def test_stated_count_reads_each_bots_marker() -> None:
    assert stated_count("**Actionable comments posted: 2**", CODERABBIT) == 2
    assert (
        stated_count(
            "Copilot reviewed 5 out of 5 changed files and generated 1 comment.",
            COPILOT,
        )
        == 1
    )


def test_stated_count_is_none_when_the_body_says_nothing() -> None:
    assert stated_count("Some prose with no count in it.", CODERABBIT) is None


def test_matching_count_produces_no_note() -> None:
    assert count_note(1, 1) is None


def test_absent_count_skips_the_cross_check() -> None:
    note = count_note(None, 0)
    assert note is not None
    assert "cross-check skipped" in note


def test_mismatch_blames_neither_side() -> None:
    # PR #27: the body claimed 2 while one comment existed, because CodeRabbit
    # counted before deduplicating two findings on the same lines. The note must
    # send the reader to look without asserting whose fault it is — the earlier
    # wording ("your filter is wrong") sent them hunting for a phantom.
    note = count_note(2, 1)
    assert note is not None
    assert "body claims 2, fetched 1" in note
    assert "scoping" in note
    assert "miscounted" in note


def test_mismatch_note_warns_against_reporting_empty_as_clean() -> None:
    note = count_note(3, 0)
    assert note is not None
    assert "clean review" in note


# --------------------------------------------------------------------------
# Encoding: both bots' bodies are full of emoji
# --------------------------------------------------------------------------


def test_subprocess_output_is_decoded_as_utf8_not_the_locale_codec() -> None:
    # `text=True` alone decodes with the locale codec — cp1252 on Windows — and
    # dies on the emoji CodeRabbit puts in every review ("🐇✅", "📐"). This is
    # CLAUDE.md's Windows-1252 warning reaching subprocess: it cost a live
    # UnicodeDecodeError that no pure test could have caught.
    #
    # The child writes raw UTF-8 bytes rather than print()ing: print() would make
    # the *child* encode to its own cp1252 stdout and die there, testing nothing
    # about the decode this covers.
    emit = (
        'import sys; sys.stdout.buffer.write("\U0001f407✅ \U0001f4d0 café".encode())'
    )
    out = run_cmd([sys.executable, "-c", emit])
    assert "🐇✅" in out
    assert "📐" in out
    assert "café" in out


def test_printing_a_review_body_survives_a_cp1252_console() -> None:
    # The encode half: Python writes stdout with the locale codec, so printing a
    # body full of emoji raised UnicodeEncodeError *after* the API calls had all
    # succeeded. Exercised in a child process, whose stdout is a real
    # TextIOWrapper defaulting to the console codepage — pytest's captured
    # stdout is not, so an in-process check would prove nothing.
    scripts = str(Path(__file__).resolve().parent.parent / "scripts")
    code = (
        f"import sys; sys.path.insert(0, {scripts!r});"
        "import bot_review; bot_review.use_utf8_io();"
        'print("\\U0001f407\\u2705 \\u2014 \\U0001f4d0")'
    )
    out = run_cmd([sys.executable, "-c", code])
    assert "🐇✅" in out
    assert "—" in out  # the em dash that came back as `?` before the fix


# --------------------------------------------------------------------------
# Loud failure: a stalled call and an unexpected payload
# --------------------------------------------------------------------------


def test_a_page_that_is_not_a_list_fails_loudly() -> None:
    # GitHub's error object is a dict. Degrading it to "no results" would make
    # `wait` poll a phantom to its timeout and `fetch` print zero comments as an
    # answer — the silent failure this module exists to remove.
    with pytest.raises(BotReviewError, match="expected a list"):
        as_page({"message": "Not Found"}, "repos/o/r/pulls/1/reviews")


def test_an_empty_page_is_a_real_answer() -> None:
    assert as_page([], "repos/o/r/pulls/1/reviews") == []


def test_a_stalled_command_is_bounded_rather_than_hanging() -> None:
    # `wait` only checks its deadline between calls, so an unbounded subprocess
    # makes --timeout unenforceable: one stalled `gh api` hangs the poll forever.
    import bot_review

    original = bot_review.COMMAND_TIMEOUT
    bot_review.COMMAND_TIMEOUT = 1
    try:
        with pytest.raises(BotReviewError, match="did not return within"):
            run_cmd([sys.executable, "-c", "import time; time.sleep(30)"])
    finally:
        bot_review.COMMAND_TIMEOUT = original

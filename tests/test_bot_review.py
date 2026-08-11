"""Bot-review glue (scripts/bot_review.py): identity, time, shape, counting.

Every test here is a regression for a defect that shipped in the prose version
of this logic and failed *silently* — the reason it became a script. The
values are the real ones observed on PR #27, transcribed; nothing here talks
to the network.
"""

from __future__ import annotations

import re
import sys
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from bot_review import (
    BOTS,
    EXIT_CLEAN,
    EXIT_EMPTY_RANGE,
    BotReviewError,
    acknowledged,
    answered_ids,
    as_page,
    comment_ts,
    count_note,
    is_clean_comment,
    is_empty_range_review,
    is_finding_comment,
    is_findings_review,
    is_summary_comment,
    outstanding_findings,
    parse_ts,
    request_event_ids,
    reviewed_sha,
    reviews_by,
    run_cmd,
    same_commit,
    same_login,
    select_clean_comment,
    select_failed_run,
    select_finding_comments,
    select_review,
    select_summary_comment,
    stated_count,
    summary_state,
    unanswered,
)

CODERABBIT = BOTS["coderabbit"]
COPILOT = BOTS["copilot"]
GEMINI = BOTS["gemini"]
GREPTILE = BOTS["greptile"]


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
# Empty range (issue #59): an empty filtered diff posts a review that reads
# identically to a genuinely clean one — `posted 0 inline finding(s)` — unless
# it also carries empty_range_marker. Currently only gemini sets one.
# --------------------------------------------------------------------------

EMPTY_RANGE_BODY = (
    "## Antigravity Gemini review\n\n"
    "<!-- gemini-review: empty-diff-range -->\n\n"
    "Antigravity (Gemini 3 Pro class, `.gemini/styleguide.md` lenses) "
    "reviewed this PR's filtered diff and posted 0 inline finding(s).\n\n"
    "The PR head introduces no changes against main (already merged, or an "
    "empty PR) — nothing to review."
)


def test_is_empty_range_review_recognizes_the_marker() -> None:
    review = _review(1, "github-actions[bot]", "2026-07-24T13:45:42Z", EMPTY_RANGE_BODY)
    assert is_empty_range_review(review, GEMINI) is True


def test_a_genuinely_clean_gemini_review_is_not_an_empty_range_one() -> None:
    review = _review(
        1,
        "github-actions[bot]",
        "2026-07-24T13:45:42Z",
        "posted 0 inline finding(s). No findings — clean per the styleguide lenses.",
    )
    assert is_empty_range_review(review, GEMINI) is False


def test_a_bot_with_no_empty_range_marker_never_reports_one() -> None:
    # Copilot and CodeRabbit leave empty_range_marker unset — this outcome is
    # gemini-specific (issue #61 tracks Copilot's own, different count-regex
    # gap). Even a body that happens to contain gemini's marker text must not
    # spuriously match a bot that never set the field.
    review = _review(
        1,
        "copilot-pull-request-reviewer[bot]",
        "2026-07-24T13:45:42Z",
        EMPTY_RANGE_BODY,
    )
    assert is_empty_range_review(review, COPILOT) is False
    assert is_empty_range_review(review, CODERABBIT) is False


def test_empty_range_exit_code_is_distinct_from_ready_failure_and_clean() -> None:
    assert EXIT_EMPTY_RANGE not in (0, 1, EXIT_CLEAN)


def test_cmd_wait_reports_empty_range_distinct_from_a_ready_review(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    import bot_review

    review = _review(9, "github-actions[bot]", "2026-07-24T13:45:42Z", EMPTY_RANGE_BODY)

    def the_review(repo: str, pr: int) -> list[dict[str, Any]]:
        return [review]

    monkeypatch.setattr(bot_review, "list_reviews", the_review)
    since = parse_ts("2026-07-24T13:38:31Z")
    assert bot_review.cmd_wait("o/r", 60, GEMINI, since, 600) == EXIT_EMPTY_RANGE
    out = capsys.readouterr().out
    assert "nothing to review" in out
    assert "not findings" in out


def test_cmd_fetch_reports_empty_range_distinct_from_findings(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    import bot_review

    review = _review(9, "github-actions[bot]", "2026-07-24T13:45:42Z", EMPTY_RANGE_BODY)

    def the_review(repo: str, pr: int) -> list[dict[str, Any]]:
        return [review]

    def no_comments(repo: str, pr: int, review_id: int) -> list[dict[str, Any]]:
        return []

    monkeypatch.setattr(bot_review, "list_reviews", the_review)
    monkeypatch.setattr(bot_review, "review_comments", no_comments)
    since = parse_ts("2026-07-24T13:38:31Z")
    assert bot_review.cmd_fetch("o/r", 60, GEMINI, since) == EXIT_EMPTY_RANGE
    out = capsys.readouterr().out
    assert "EMPTY RANGE" in out
    # The generic count_note path (mismatch / cross-check-skipped wording)
    # must not also fire for this outcome — it would misreport a 0==0 count
    # match as an ordinary clean result.
    assert "cross-check skipped" not in out
    assert "count mismatch" not in out


# --------------------------------------------------------------------------
# Clean runs: CodeRabbit's no-findings run posts no review object at all
# --------------------------------------------------------------------------

# Transcribed (abridged) from PR #29, 2026-07-17: the first fully clean run
# observed. Its only artifact was this issue comment — the reviews endpoint
# stayed empty, so `wait` polled a finished clean review to its 30-min timeout.
CLEAN_BODY = (
    "<!-- This is an auto-generated comment: summarize by coderabbit.ai -->\n"
    "<!-- review_stack_entry_start -->\n\n"
    "[![Review Change Stack](https://example.invalid/stack.svg)](https://example.invalid)\n\n"
    "<!-- review_stack_entry_end -->\n"
    "No actionable comments were generated in the recent review. 🎉\n\n"
    "<details><summary>Recent review info</summary></details>\n"
)

# The same walkthrough comment as it reads on a findings run: the HTML marker
# is present, the no-findings phrase is not.
FINDINGS_WALKTHROUGH_BODY = (
    "<!-- This is an auto-generated comment: summarize by coderabbit.ai -->\n"
    "<details><summary>📝 Walkthrough</summary>## Walkthrough\n...</details>\n"
)


def _comment(
    comment_id: int,
    login: str,
    created_at: str,
    updated_at: str,
    body: str = CLEAN_BODY,
) -> dict[str, Any]:
    return {
        "id": comment_id,
        "user": {"login": login},
        "created_at": created_at,
        "updated_at": updated_at,
        "body": body,
    }


def test_the_clean_run_summary_is_recognized() -> None:
    # PR #29 comment id 3110584518, transcribed above.
    comment = _comment(
        3110584518, "coderabbitai[bot]", "2026-07-17T19:35:53Z", "2026-07-17T19:35:53Z"
    )
    assert is_clean_comment(comment, CODERABBIT) is True


def test_a_findings_run_walkthrough_is_not_a_clean_summary() -> None:
    # Same author, same auto-generated marker — but no no-findings phrase.
    comment = _comment(
        1,
        "coderabbitai[bot]",
        "2026-07-17T19:35:53Z",
        "2026-07-17T19:35:53Z",
        body=FINDINGS_WALKTHROUGH_BODY,
    )
    assert is_clean_comment(comment, CODERABBIT) is False


def test_the_phrase_quoted_in_prose_is_not_a_clean_summary() -> None:
    # A human (or the bot, in a reply) quoting the phrase lacks the
    # auto-generated-summary HTML marker, which the pattern requires *before*
    # the phrase.
    quoted = 'As CodeRabbit says, "No actionable comments were generated". Ship it.'
    comment = _comment(
        1, "coderabbitai[bot]", "2026-07-17T19:35:53Z", "2026-07-17T19:35:53Z", quoted
    )
    assert is_clean_comment(comment, CODERABBIT) is False


def test_another_authors_clean_looking_comment_is_not_the_bots() -> None:
    comment = _comment(
        1, "not-coderabbitai[bot]", "2026-07-17T19:35:53Z", "2026-07-17T19:35:53Z"
    )
    assert is_clean_comment(comment, CODERABBIT) is False


def test_copilot_has_no_clean_comment_mode() -> None:
    # Copilot's clean run is still a review — "generated no comments" on a first
    # pass, "generated no new comments" on a re-review (issue #61; verified on
    # PR#60, never "generated 0 comments") — which select_review finds by its
    # nonempty body; its spec deliberately opts out of issue-comment scanning.
    assert COPILOT.clean_marker is None
    comment = _comment(
        1,
        "copilot-pull-request-reviewer[bot]",
        "2026-07-17T19:35:53Z",
        "2026-07-17T19:35:53Z",
    )
    assert is_clean_comment(comment, COPILOT) is False


def test_comment_floor_uses_the_edit_time_not_the_creation_time() -> None:
    # CodeRabbit edits its one walkthrough comment in place on every review
    # run, so on any PR past its first review created_at predates every floor.
    # Keying on created_at would make a fresh clean run invisible — the same
    # silent-failure family as the string-compared timestamps.
    edited = _comment(
        1, "coderabbitai[bot]", "2026-07-15T09:00:00Z", "2026-07-17T19:35:53Z"
    )
    assert comment_ts(edited) == parse_ts("2026-07-17T19:35:53Z")
    since = parse_ts("2026-07-17T18:00:00Z")
    chosen = select_clean_comment([edited], CODERABBIT, since)
    assert chosen is not None
    assert chosen["id"] == 1


def test_a_stale_clean_summary_does_not_answer_for_a_new_push() -> None:
    # The clean summary of an *earlier* run, untouched since: its updated_at
    # sits at or before the floor, so it must not report the new push clean.
    stale = _comment(
        1, "coderabbitai[bot]", "2026-07-15T09:00:00Z", "2026-07-17T18:00:00Z"
    )
    since = parse_ts("2026-07-17T18:00:00Z")
    assert select_clean_comment([stale], CODERABBIT, since) is None


def test_select_clean_comment_takes_the_newest() -> None:
    since = parse_ts("2026-07-17T00:00:00Z")
    older = _comment(
        1, "coderabbitai[bot]", "2026-07-17T09:00:00Z", "2026-07-17T09:00:00Z"
    )
    newer = _comment(
        2, "coderabbitai[bot]", "2026-07-17T09:00:00Z", "2026-07-17T19:35:53Z"
    )
    chosen = select_clean_comment([older, newer], CODERABBIT, since)
    assert chosen is not None
    assert chosen["id"] == 2


def test_clean_exit_code_is_distinct_from_ready_and_failure() -> None:
    # 0 would send the caller to fetch-and-triage a review that does not
    # exist; 1 would report a finished clean run as a failure.
    assert EXIT_CLEAN not in (0, 1)


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
    assert CODERABBIT.request_login is None  # not requestable via reviewers
    assert CODERABBIT.requested_display is None


# --------------------------------------------------------------------------
# Asking: CodeRabbit is commanded in-thread, Copilot through reviewers
# --------------------------------------------------------------------------


def test_coderabbit_is_asked_by_trigger_comment_and_copilot_is_not() -> None:
    # With auto_review.enabled: false, nothing reviews a push on its own; the
    # only ask channel CodeRabbit honors is its command comment. Copilot keeps
    # the real request channel and must never fall into the trigger path.
    assert CODERABBIT.trigger_body == "@coderabbitai review"
    assert COPILOT.trigger_body is None


def test_trigger_request_posts_the_comment_and_prints_the_floor_first(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    import bot_review

    calls: list[tuple[str, tuple[str, ...]]] = []

    def fake_gh(path: str, *args: str) -> Any:
        calls.append((path, args))
        return {"id": 5058928383, "body": "@coderabbitai review"}

    monkeypatch.setattr(bot_review, "gh", fake_gh)
    assert bot_review.cmd_request("o/r", 54, CODERABBIT) == 0
    assert calls == [
        ("repos/o/r/issues/54/comments", ("-f", "body=@coderabbitai review"))
    ]
    out = capsys.readouterr().out
    assert "triggered coderabbit via comment 5058928383" in out
    assert "since: " in out
    assert "--bot coderabbit --pr 54" in out


def test_a_mangled_trigger_comment_fails_loudly_rather_than_waiting(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The trigger body starts with `@`, which `gh api` field flags can treat as
    # a read-from-file directive; GitHub renders whatever it received. A comment
    # that does not read exactly as the command never summons the bot, and
    # waiting on it buys the full poll timeout for a review nobody asked for.
    import bot_review

    def mangling_gh(path: str, *args: str) -> Any:
        return {"id": 1, "body": "review"}

    monkeypatch.setattr(bot_review, "gh", mangling_gh)
    with pytest.raises(BotReviewError, match="Do not wait"):
        bot_review.cmd_request("o/r", 54, CODERABBIT)


def _request_event(
    event_id: int,
    login: str,
    created_at: str = "2026-08-10T00:05:18Z",
) -> dict[str, Any]:
    """One timeline `review_requested` entry, transcribed from PR #85."""
    return {
        "id": event_id,
        "event": "review_requested",
        "created_at": created_at,
        "actor": {"login": "NeuroticGamer99"},
        "requested_reviewer": {"login": login},
    }


def _timeline_gh(
    pages: list[list[dict[str, Any]]],
    *,
    before: list[dict[str, Any]] | None = None,
    posts: list[tuple[str, ...]] | None = None,
) -> Any:
    """A `gh` double serving a timeline and accepting the reviewer POST.

    `pages` is what the timeline answers *after* the POST, keyed on the `page=`
    parameter `gh_all` sends; `before` is the single page it answered up to
    then. Reading the page out of the path rather than counting calls is not
    fussiness — a per-call counter makes each *poll* iteration serve the next
    page, so a confirmation reading only page 1 finds the event on its second
    poll and the paging test passes against code with no paging in it.

    Any other endpoint is a failure: the confirmation must read the timeline
    and nothing else, and the one it must not consult (`pulls/<n>`) is
    precisely the one it used to.
    """
    posted = False

    def fake_gh(path: str, *args: str) -> Any:
        nonlocal posted
        if path.endswith("/requested_reviewers"):
            posted = True
            if posts is not None:
                posts.append((path, *args))
            return None
        if "/timeline" in path:
            asked = re.search(r"[?&]page=(\d+)", path)
            page = int(asked.group(1)) if asked else 1
            source = pages if posted else [list(before or [])]
            return list(source[page - 1]) if page <= len(source) else []
        raise AssertionError(
            f"unexpected endpoint {path!r} — this path reads the timeline and "
            "POSTs the request, nothing else. Widen this double if a later "
            "change legitimately adds a call; if the endpoint is `pulls/<n>`, "
            "the requested_reviewers read-back is what came back."
        )

    return fake_gh


def test_reviewer_request_confirms_on_the_timeline_and_prints_the_floor(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # The Copilot branch: POST to requested_reviewers, then prove the ask took
    # by finding a *new* `review_requested` event on the issue timeline. The
    # double refuses the `pulls/<n>` endpoint outright, because consulting it
    # is the defect — that payload answered `requested_reviewers: []` on four
    # consecutive *accepted* requests (PRs #81, #82, #83, #85).
    import bot_review

    posts: list[tuple[str, ...]] = []
    monkeypatch.setattr(
        bot_review,
        "gh",
        _timeline_gh([[_request_event(29195776393, "Copilot")]], posts=posts),
    )
    assert bot_review.cmd_request("o/r", 54, COPILOT) == 0
    out = capsys.readouterr().out
    assert "timeline review_requested event 29195776393 names Copilot" in out
    assert "since: " in out
    assert "--bot copilot --pr 54" in out
    # Order, not just presence: the floor now prints from the caller's
    # `finally`, and "confirmation first, floor last" is the property that
    # refactor's docstring claims. Membership assertions cannot see it.
    assert out.index("timeline review_requested event") < out.index("since: ")
    # The ask itself, not just that some call reached the endpoint: `Copilot`
    # is the login GitHub *displays*, and POSTing it returns HTTP 200 while
    # adding no one. Only the bot login is accepted, and nothing else in this
    # file has ever checked which one was sent.
    # The whole call, not just the login in it. This was narrowed to a
    # membership test on the grounds that a full tuple breaks on any unrelated
    # flag added later, and narrowing turned out to be the worse trade: with
    # `-f` dropped from the call the login became a bare positional argument,
    # `gh api` stopped treating it as a field, and the membership test still
    # passed. `gh()` forwards `*args` straight to the CLI and the suite never
    # reaches a real one, so this assertion is the only thing in it that can
    # see a malformed request for this endpoint.
    assert posts == [
        (
            "repos/o/r/pulls/54/requested_reviewers",
            "-f",
            "reviewers[]=copilot-pull-request-reviewer[bot]",
        )
    ]


def test_an_unconfirmed_ask_prints_the_floor_before_refusing(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # GitHub answers HTTP 200 to a login it does not recognize and silently
    # adds no one, so the confirmation still has to exist. What changed is what
    # a failure means and what it leaves behind: `/copilot-review` tells the
    # operator to pass the floor this command prints and forbids minting one,
    # so a refusal that printed none left them holding an instruction they
    # could not follow — at the one moment they needed it.
    import bot_review

    monkeypatch.setattr(bot_review, "gh", _timeline_gh([[]]))
    monkeypatch.setattr(bot_review, "REQUEST_CONFIRM_TIMEOUT", 0)
    with pytest.raises(BotReviewError, match="unconfirmed") as caught:
        bot_review.cmd_request("o/r", 54, COPILOT)
    # Names *both* logins it searched for. Asserting that each string is
    # present would be vacuous — the message opens with `request_login`
    # regardless, so both appear even when the search is described as
    # narrower than it is. The conjunction is the load-bearing part.
    assert "naming it or 'Copilot'" in str(caught.value)
    out = capsys.readouterr().out
    assert "since: " in out
    assert "--bot copilot --pr 54" in out
    # The old message ended "Do not wait." — advice that was wrong on all four
    # observed refusals, every one of which had a real review already on its
    # way. An operator who obeyed it reported the bot unavailable while it was
    # writing the review.
    assert "Do not wait" not in str(caught.value)


def test_a_poll_failure_after_the_ask_still_leaves_a_floor(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # The POST has returned — the ask took and a review is genuinely coming —
    # and then a confirmation read raises: a transient `gh` failure, an
    # unexpected payload shape, the id-shape refusal. That exception must not
    # carry the floor away with it. This is the one exit that reaches no code
    # of its own, so it printed nothing at all — strictly worse than the
    # refusal this branch was written to fix, which at least prints a floor.
    import bot_review

    reads = [0]

    def fake_gh(path: str, *args: str) -> Any:
        if path.endswith("/requested_reviewers"):
            return None
        if "/timeline" in path:
            reads[0] += 1
            if reads[0] > 1:  # the pre-ask snapshot succeeds; the poll does not
                raise BotReviewError("gh api ... failed: HTTP 502")
            return []
        raise AssertionError(f"unexpected endpoint {path!r}")

    monkeypatch.setattr(bot_review, "gh", fake_gh)
    with pytest.raises(BotReviewError, match="502"):
        bot_review.cmd_request("o/r", 54, COPILOT)
    out = capsys.readouterr().out
    assert "since: " in out
    assert "--bot copilot --pr 54" in out


def test_a_trigger_post_failure_leaves_a_floor_but_a_mangled_one_does_not(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # The third ask channel, and the pair is the point: one rule decides both.
    # A failure *posting* the comment cannot show whether it was created, and a
    # created comment summons the bot whatever this process does next — floor.
    # A post that succeeded and read back mangled proves the trigger never
    # reached the bot — no floor, because there is no review for one to name.
    import bot_review

    def failing_post(path: str, *args: str) -> Any:
        raise BotReviewError("gh api ... failed: HTTP 502")

    monkeypatch.setattr(bot_review, "gh", failing_post)
    with pytest.raises(BotReviewError, match="502"):
        bot_review.cmd_request("o/r", 54, CODERABBIT)
    assert "since: " in capsys.readouterr().out

    # The body the `@`-prefix hazard this guard exists for actually produces:
    # `gh api` reading the value as a filename substitutes unrelated content,
    # it does not truncate. A one-character-short body exercises the same flat
    # inequality and reads as a boundary case the code does not have.
    def mangling_post(path: str, *args: str) -> Any:
        return {"id": 1, "body": "review"}

    monkeypatch.setattr(bot_review, "gh", mangling_post)
    with pytest.raises(BotReviewError, match="did not reach the bot as written"):
        bot_review.cmd_request("o/r", 54, CODERABBIT)
    assert "since: " not in capsys.readouterr().out


def test_an_empty_trigger_response_is_unknown_rather_than_refused(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # `gh()` answers None when the call *succeeded* and printed nothing. That
    # reads back no comment at all, so it cannot show the ask failed — the
    # comment may exist and may have summoned the bot. Folded into the mangled
    # refusal it printed no floor and reported the body as `''`, asserting a
    # read-back that never happened.
    import bot_review

    def empty_response(path: str, *args: str) -> Any:
        return None

    monkeypatch.setattr(bot_review, "gh", empty_response)
    with pytest.raises(BotReviewError, match="response body was empty"):
        bot_review.cmd_request("o/r", 54, CODERABBIT)
    out = capsys.readouterr().out
    assert "since: " in out
    assert "--bot coderabbit --pr 54" in out


def test_a_raw_os_error_from_the_ask_also_leaves_a_floor(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # `run_cmd` converts a non-zero exit and a timeout into BotReviewError, but
    # an OSError raised by `subprocess.run` itself — `gh` gone from PATH, a
    # fork failure — propagates raw. Catching only the converted kind made the
    # floor depend on *how* the call broke, and left these channels narrower
    # than `cmd_request`'s `finally`, which never made that distinction.
    import bot_review

    def vanished_gh(path: str, *args: str) -> Any:
        raise OSError(2, "No such file or directory: 'gh'")

    monkeypatch.setattr(bot_review, "gh", vanished_gh)
    with pytest.raises(OSError, match="No such file or directory"):
        bot_review.cmd_request("o/r", 54, CODERABBIT)
    out = capsys.readouterr().out
    assert "since: " in out
    assert "--bot coderabbit --pr 54" in out


def test_a_dispatch_post_failure_leaves_a_floor(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # The ask itself, on the channel whose *poll* was guarded a round earlier
    # and whose POST was not. A dispatch that errors leaves it unknown whether
    # a run was created, and a created run reviews the PR regardless.
    import bot_review

    def fake_gh(path: str, *args: str) -> Any:
        if path == "repos/o/r":
            return {"default_branch": "main"}
        if path.endswith("/dispatches"):
            raise BotReviewError("gh api ... failed: HTTP 502")
        raise AssertionError(f"unexpected endpoint {path!r}")

    monkeypatch.setattr(bot_review, "gh", fake_gh)

    def no_runs(repo: str, workflow: str) -> list[dict[str, Any]]:
        return []

    monkeypatch.setattr(bot_review, "workflow_runs", no_runs)
    with pytest.raises(BotReviewError, match="502"):
        bot_review.cmd_request("o/r", 56, GEMINI)
    out = capsys.readouterr().out
    assert "since: " in out
    assert "--bot gemini --pr 56" in out


def test_a_rejected_ask_still_leaves_a_floor(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # The POST itself fails — the plan does not carry Copilot, the token lacks
    # permission, the login is refused. `/copilot-review` says to stop on this,
    # and it is still the wrong moment to withhold a floor: a non-zero `gh`
    # exit is the likeliest of the failures here to mean nothing happened, and
    # is still not proof of it. Two ignorable lines against the failure this
    # branch exists to remove.
    import bot_review

    def fake_gh(path: str, *args: str) -> Any:
        if path.endswith("/requested_reviewers"):
            raise BotReviewError("gh api ... failed: HTTP 403 Forbidden")
        if "/timeline" in path:
            return []
        raise AssertionError(f"unexpected endpoint {path!r}")

    monkeypatch.setattr(bot_review, "gh", fake_gh)
    with pytest.raises(BotReviewError, match="403"):
        bot_review.cmd_request("o/r", 54, COPILOT)
    assert "since: " in capsys.readouterr().out


def test_a_dispatch_read_failure_after_the_ask_still_leaves_a_floor(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # The same defect in the sibling ask channel, and it survived the first fix
    # because that one was written to the finding rather than to the shape. The
    # dispatch was accepted and a run may be executing; only our ability to
    # read the runs list failed. Distinct from that path's *refusal*, which
    # deliberately prints no floor — there the read succeeded and found no run.
    import bot_review

    reads = [0]

    def fake_gh(path: str, *args: str) -> Any:
        if path == "repos/o/r":
            return {"default_branch": "main"}
        if path.endswith("/dispatches"):
            return None
        raise AssertionError(f"unexpected endpoint {path!r}")

    def failing_runs(repo: str, workflow: str) -> list[dict[str, Any]]:
        reads[0] += 1
        if reads[0] > 1:  # the pre-dispatch newest-id read succeeds
            raise BotReviewError("gh api ... failed: HTTP 502")
        return []

    monkeypatch.setattr(bot_review, "gh", fake_gh)
    monkeypatch.setattr(bot_review, "workflow_runs", failing_runs)
    with pytest.raises(BotReviewError, match="502"):
        bot_review.cmd_request("o/r", 56, GEMINI)
    out = capsys.readouterr().out
    assert "since: " in out
    assert "--bot gemini --pr 56" in out
    # No run id is known, so none is offered — a `--run` pointing at nothing
    # would make `wait` fail fast on a run that was never identified.
    assert "--run" not in out


def test_the_floor_is_flushed_so_a_merged_capture_keeps_the_order(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The floor goes to stdout and the error to stderr. Under `> file 2>&1`
    # stdout is block-buffered where stderr is not, so an unflushed floor
    # reaches the file *after* the refusal it is documented to sit above —
    # measured. Three sites promise that ordering (the comment in
    # `cmd_request`, the error text, and /copilot-review step 2) and none of
    # them held for a captured log. Asserting the flush is the last thing the
    # floor does is what makes those three true.
    #
    # This is a proxy and the limit is worth stating: the recorder has no
    # buffer, so it observes call order, not the fd interleaving that was
    # actually measured. It catches the regression that matters (the flush
    # removed or moved) and cannot catch a flush that is called and does not
    # reach the OS. The instrument that would is a subprocess writing both
    # streams to one file and asserting byte order — deliberately not written,
    # because it would newly depend on per-platform buffering across three CI
    # legs, one of which cannot be run locally, to catch a failure mode CPython
    # does not exhibit.
    import bot_review

    events: list[str] = []

    class Recorder:
        def write(self, text: str) -> int:
            events.append("write")
            return len(text)

        def flush(self) -> None:
            events.append("flush")

    monkeypatch.setattr(sys, "stdout", Recorder())
    monkeypatch.setattr(bot_review, "gh", _timeline_gh([[]]))
    monkeypatch.setattr(bot_review, "REQUEST_CONFIRM_TIMEOUT", 0)
    with pytest.raises(BotReviewError, match="unconfirmed"):
        bot_review.cmd_request("o/r", 54, COPILOT)
    assert "write" in events
    assert events[-1] == "flush"


def test_a_pre_existing_request_event_does_not_confirm_a_new_ask(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The timeline is append-only, so "an event naming Copilot exists" is
    # satisfied forever by the *first* run's — and re-running `/copilot-review`
    # after a fix commit is routine. Only an id absent from the pre-ask
    # snapshot proves this ask took.
    import bot_review

    stale = [_request_event(29195776393, "Copilot", "2026-08-10T00:05:18Z")]
    monkeypatch.setattr(bot_review, "gh", _timeline_gh([stale], before=stale))
    monkeypatch.setattr(bot_review, "REQUEST_CONFIRM_TIMEOUT", 0)
    with pytest.raises(BotReviewError, match="unconfirmed"):
        bot_review.cmd_request("o/r", 54, COPILOT)


def test_a_request_event_landing_late_is_still_confirmed(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # The event is written by the POST, so one read should find it — but that
    # is an expectation, not a measurement, and the confirmation this replaces
    # failed precisely by trusting an unmeasured assumption about what GitHub
    # populates when. The poll costs nothing on the path that works.
    import bot_review

    empty: list[dict[str, Any]] = []
    # Read 1 is the pre-ask snapshot, read 2 the first confirmation attempt —
    # both empty, so the event lands only on the attempt *after* a sleep.
    reads = iter([empty, empty, [_request_event(29195776393, "Copilot")]])
    slept: list[float] = []

    def fake_gh(path: str, *args: str) -> Any:
        if path.endswith("/requested_reviewers"):
            return None
        if "/timeline" in path:
            return next(reads, empty)
        raise AssertionError(f"unexpected endpoint {path!r}")

    monkeypatch.setattr(bot_review, "gh", fake_gh)
    monkeypatch.setattr(bot_review.time, "sleep", slept.append)
    assert bot_review.cmd_request("o/r", 54, COPILOT) == 0
    # Exactly one wait, at the configured interval: a confirmation that read
    # once would have refused here, and one that slept without re-reading
    # would spend the whole window to reach the same refusal.
    assert slept == [bot_review.REQUEST_POLL_SECONDS]
    assert "event 29195776393" in capsys.readouterr().out


def test_the_poll_keeps_trying_across_the_whole_window(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # The test above proves one sleep is followed by one fresh read, and that
    # is all it proves: a hardcoded "read, sleep, read" satisfies it while
    # honouring neither constant. Landing the event on the fifth attempt is
    # reachable only by a loop bounded by REQUEST_CONFIRM_TIMEOUT.
    import bot_review

    clock = [0.0]
    empty: list[dict[str, Any]] = []
    # One pre-ask snapshot read, four empty attempts, then the event.
    reads = iter([empty] * 5 + [[_request_event(29195776393, "Copilot")]])

    def fake_gh(path: str, *args: str) -> Any:
        if path.endswith("/requested_reviewers"):
            return None
        if "/timeline" in path:
            return next(reads, empty)
        raise AssertionError(f"unexpected endpoint {path!r}")

    def advance(seconds: float) -> None:
        clock[0] += seconds

    monkeypatch.setattr(bot_review, "gh", fake_gh)
    monkeypatch.setattr(bot_review.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(bot_review.time, "sleep", advance)
    assert bot_review.cmd_request("o/r", 54, COPILOT) == 0
    assert "event 29195776393" in capsys.readouterr().out
    assert clock[0] == 4 * bot_review.REQUEST_POLL_SECONDS


def test_the_poll_gives_up_at_the_bound_rather_than_never(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The other side of the same bound. Counting attempts is what distinguishes
    # a deadline from an unbounded retry — the refusal alone cannot, since a
    # loop with no deadline check never reaches one to be observed.
    import bot_review

    clock = [0.0]
    attempts = [0]

    def fake_gh(path: str, *args: str) -> Any:
        if path.endswith("/requested_reviewers"):
            return None
        if "/timeline" in path:
            attempts[0] += 1
            return []
        raise AssertionError(f"unexpected endpoint {path!r}")

    def advance(seconds: float) -> None:
        clock[0] += seconds

    monkeypatch.setattr(bot_review, "gh", fake_gh)
    monkeypatch.setattr(bot_review.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(bot_review.time, "sleep", advance)
    with pytest.raises(BotReviewError, match="unconfirmed"):
        bot_review.cmd_request("o/r", 54, COPILOT)
    # A lower bound, not an exact count. Moving the deadline check from after
    # the read to the loop guard is the same behaviour and changes the count by
    # one, so an exact assertion fails a refactor that broke nothing — while
    # still catching what matters here, since a fixed one- or two-attempt retry
    # falls an order of magnitude short of this.
    intervals = bot_review.REQUEST_CONFIRM_TIMEOUT // bot_review.REQUEST_POLL_SECONDS
    assert attempts[0] >= intervals
    assert clock[0] == bot_review.REQUEST_CONFIRM_TIMEOUT


def test_the_confirmation_always_reads_at_least_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The loop checks the deadline *after* a read rather than as its guard, so
    # an already-elapsed window still buys one look. Guard-first, an ask made
    # as the window closes is refused having never queried the timeline — a
    # refusal about the clock rather than about the ask, and indistinguishable
    # in its output from one that looked. Pinned explicitly because it was
    # otherwise only a side effect of another test's zeroed timeout.
    #
    # The count is exact rather than a floor, unlike the bound assertion in
    # `test_the_poll_gives_up_at_the_bound_rather_than_never`. The difference is
    # what the number means: that one encoded an arithmetic relationship two
    # correct implementations can disagree on, while two here is one snapshot
    # read plus the one confirmation read that succeeds and returns. Nothing
    # correct reads a third time, so the exact count also catches a redundant
    # call that no looser assertion would.
    import bot_review

    attempts = [0]

    def fake_gh(path: str, *args: str) -> Any:
        if path.endswith("/requested_reviewers"):
            return None
        if "/timeline" in path:
            attempts[0] += 1
            return [_request_event(29195776393, "Copilot")] if attempts[0] > 1 else []
        raise AssertionError(f"unexpected endpoint {path!r}")

    monkeypatch.setattr(bot_review, "gh", fake_gh)
    monkeypatch.setattr(bot_review, "REQUEST_CONFIRM_TIMEOUT", 0)
    assert bot_review.cmd_request("o/r", 54, COPILOT) == 0
    assert attempts[0] == 2  # the pre-ask snapshot, then one confirmation read


def test_either_of_the_bots_request_logins_confirms_the_ask() -> None:
    # The identity split that bit the guard itself: the ask POSTs
    # `copilot-pull-request-reviewer[bot]` and the timeline records `Copilot`.
    # Both name one account, so either confirms — pinning it to whichever one
    # GitHub records today is what made the old guard wrong, and re-arming that
    # is the one failure mode this whole change exists to remove.
    assert request_event_ids([_request_event(1, "Copilot")], COPILOT) == {1}
    posted = [_request_event(2, "copilot-pull-request-reviewer[bot]")]
    assert request_event_ids(posted, COPILOT) == {2}
    # Laxity would be a lookalike passing, and it does not: `same_login` is
    # whole-string, so nothing but those two accounts can confirm a Copilot ask.
    assert request_event_ids([_request_event(3, "copilot-fan")], COPILOT) == set()


def test_the_success_line_reports_the_login_the_event_recorded(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # The match accepts either of Copilot's two request-side logins, so naming
    # `requested_display` on the confirmation line was a constant standing in
    # for an observation — true today, and a guess dressed as evidence on the
    # one line whose job is to be evidence.
    #
    # The fixture records a *case variant*, and that is what makes the test
    # discriminating rather than lucky. `same_login` folds case (GitHub logins
    # are case-preserving but unique case-folded), so `COPILOT` matches — while
    # being a distinct string from **both** spec constants. An earlier version
    # used the bot login, which is exactly `request_login`: substituting that
    # constant then passed this test, and the property survived only because a
    # sibling test's fixture happened to spell the other one. Pinned by
    # coincidence is not pinned.
    import bot_review

    recorded = [_request_event(29195776393, "COPILOT")]
    monkeypatch.setattr(bot_review, "gh", _timeline_gh([recorded]))
    assert bot_review.cmd_request("o/r", 54, COPILOT) == 0
    out = capsys.readouterr().out
    assert "names COPILOT" in out
    assert "names Copilot" not in out  # requested_display
    assert "names copilot-pull-request-reviewer[bot]" not in out  # request_login


def test_only_review_requested_events_naming_a_reviewer_are_counted() -> None:
    # A PR timeline carries every kind of entry. Two shapes are worth pinning:
    # another event type that happens to name a reviewer, and a *team* request,
    # which carries `requested_team` and no `requested_reviewer` at all.
    removed = _request_event(4, "Copilot") | {"event": "review_request_removed"}
    team = {
        "id": 5,
        "event": "review_requested",
        "created_at": "2026-08-10T00:05:18Z",
        "actor": {"login": "NeuroticGamer99"},
        "requested_team": {"name": "reviewers"},
    }
    assert request_event_ids([removed, team], COPILOT) == set()


def test_a_request_event_with_no_usable_id_is_refused_rather_than_skipped() -> None:
    # Every `review_requested` entry carries an integer id (only `committed`
    # entries do not — measured on PR #85), so a missing one is an unexpected
    # shape. Skipping it would quietly weaken the confirmation into "some event
    # exists", which is the vacuous form the pre-ask snapshot exists to prevent.
    idless = {
        "event": "review_requested",
        "created_at": "2026-08-10T00:05:18Z",
        "requested_reviewer": {"login": "Copilot"},
    }
    with pytest.raises(BotReviewError, match="no integer id"):
        request_event_ids([idless], COPILOT)


def test_the_id_refusal_is_scoped_to_events_that_would_otherwise_count() -> None:
    # The refusal above rests on the id check sitting *after* the event-type
    # and login filters. Hoisted ahead of them it would fire on every real PR,
    # because `committed` entries carry no id and every timeline has them —
    # turning a guard against an unexpected shape into a guard against normal
    # ones. Nothing else in the suite distinguishes the two orderings.
    committed = {"event": "committed", "message": "[b] a checkpoint"}
    other_bot = {
        "event": "review_requested",
        "created_at": "2026-08-10T00:05:18Z",
        "requested_reviewer": {"login": "some-other-reviewer"},
    }
    assert request_event_ids([committed, other_bot], COPILOT) == set()


def test_the_timeline_is_read_to_its_last_page(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # The timeline is *ascending*, so the event a request has just created is
    # the last entry on the last page. Reading one page deep would confirm the
    # ask on a quiet PR and stop confirming it as the PR filled up — a guard
    # that decays with the length of the review it is guarding.
    import bot_review

    filler = [
        {"id": n, "event": "commented", "created_at": "2026-08-09T00:00:00Z"}
        for n in range(bot_review.PAGE_SIZE)
    ]
    monkeypatch.setattr(
        bot_review,
        "gh",
        _timeline_gh([filler, [_request_event(29195776393, "Copilot")]]),
    )
    # One confirmation attempt, so the event has to be found by *paging* within
    # it. Without this the poll supplies a second attempt, and a single-page
    # read reaches page 2 the slow way — passing the test it was meant to fail.
    monkeypatch.setattr(bot_review, "REQUEST_CONFIRM_TIMEOUT", 0)
    assert bot_review.cmd_request("o/r", 54, COPILOT) == 0
    assert "event 29195776393" in capsys.readouterr().out


def test_a_spec_with_both_ask_channels_cannot_be_built() -> None:
    # cmd_request dispatches on trigger_body first, so a spec setting both
    # would carry request fields that read as active config but never run.
    # The dataclass refuses the combination at construction.
    import bot_review

    with pytest.raises(ValueError, match="mutually exclusive"):
        bot_review.BotSpec(
            key="greedy",
            review_login="greedy[bot]",
            request_login="greedy[bot]",
            requested_display="Greedy",
            count=CODERABBIT.count,
            clean_marker=None,
            trigger_body="@greedy review",
        )


def test_a_spec_with_half_a_request_channel_cannot_be_built() -> None:
    # request_login is what gets POSTed; requested_display is what the
    # read-back verifies. Half a pair would otherwise fail later in
    # cmd_request with the misleading "has neither ask channel" message.
    import bot_review

    with pytest.raises(ValueError, match="pair"):
        bot_review.BotSpec(
            key="half",
            review_login="half[bot]",
            request_login="half[bot]",
            requested_display=None,
            count=CODERABBIT.count,
            clean_marker=None,
            trigger_body=None,
        )


def test_a_spec_with_no_ask_channel_is_refused() -> None:
    import bot_review

    mute = bot_review.BotSpec(
        key="mute",
        review_login="mute[bot]",
        request_login=None,
        requested_display=None,
        count=CODERABBIT.count,
        clean_marker=None,
        trigger_body=None,
    )
    with pytest.raises(BotReviewError, match="cannot be asked"):
        bot_review.cmd_request("o/r", 54, mute)


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
# Dispatch: the Gemini reviewer is a workflow, not a GitHub App
# --------------------------------------------------------------------------


def test_gemini_is_asked_by_dispatch_and_reviews_as_the_actions_bot() -> None:
    # The Antigravity workflow's reviews are authored by github-actions[bot];
    # its only ask channel is dispatching .github/workflows/gemini-review.yml.
    assert GEMINI.dispatch_workflow == "gemini-review.yml"
    assert GEMINI.trigger_body is None
    assert GEMINI.request_login is None
    assert GEMINI.review_login == "github-actions[bot]"
    # Clean run is still a review stating 0 findings (like Copilot), so no
    # clean-comment scanning: wait/fetch exit 0 and triage sees the count.
    assert GEMINI.clean_marker is None


def test_a_spec_combining_dispatch_with_another_channel_cannot_be_built() -> None:
    # cmd_request tries trigger, then dispatch, then requested_reviewers; a
    # spec setting two would carry config that reads as active but never runs.
    import bot_review

    with pytest.raises(ValueError, match="mutually exclusive"):
        bot_review.BotSpec(
            key="greedy",
            review_login="greedy[bot]",
            request_login=None,
            requested_display=None,
            trigger_body="@greedy review",
            count=CODERABBIT.count,
            clean_marker=None,
            dispatch_workflow="greedy.yml",
        )
    with pytest.raises(ValueError, match="mutually exclusive"):
        bot_review.BotSpec(
            key="greedy",
            review_login="greedy[bot]",
            request_login="greedy[bot]",
            requested_display="Greedy",
            trigger_body=None,
            count=CODERABBIT.count,
            clean_marker=None,
            dispatch_workflow="greedy.yml",
        )


def _run(
    run_id: int,
    status: str,
    conclusion: str | None,
    created_at: str,
    title: str = "Gemini review: PR 56",
) -> dict[str, Any]:
    return {
        "id": run_id,
        "status": status,
        "conclusion": conclusion,
        "created_at": created_at,
        "display_title": title,
    }


def test_a_failed_run_after_the_floor_is_selected() -> None:
    # The run that was supposed to post the review died (SDK error, exhausted
    # quota): no review is ever coming, and polling the reviews endpoint would
    # ride the 30-minute timeout to say less than the conclusion already says.
    since = parse_ts("2026-07-23T14:00:00Z")
    failed = _run(101, "completed", "failure", "2026-07-23T14:00:05Z")
    chosen = select_failed_run([failed], since, 56)
    assert chosen is not None
    assert chosen["id"] == 101


def test_an_in_progress_run_has_no_verdict_yet() -> None:
    since = parse_ts("2026-07-23T14:00:00Z")
    running = _run(101, "in_progress", None, "2026-07-23T14:00:05Z")
    assert select_failed_run([running], since, 56) is None


def test_a_successful_run_is_not_a_failure() -> None:
    # Its review is found by the caller's own review check.
    since = parse_ts("2026-07-23T14:00:00Z")
    ok = _run(101, "completed", "success", "2026-07-23T14:00:05Z")
    assert select_failed_run([ok], since, 56) is None


def test_a_stale_failed_run_does_not_answer_for_a_new_ask() -> None:
    # An earlier ask's failure, at or before the floor, must not fail this one.
    since = parse_ts("2026-07-23T14:00:00Z")
    stale = _run(90, "completed", "failure", "2026-07-23T10:00:00Z")
    assert select_failed_run([stale], since, 56) is None


def test_select_failed_run_takes_the_newest_of_several() -> None:
    # Same rule as select_review: recency by instant, not list order.
    since = parse_ts("2026-07-23T14:00:00Z")
    older = _run(101, "completed", "failure", "2026-07-23T14:00:05Z")
    newer = _run(102, "completed", "cancelled", "2026-07-23T14:10:00Z")
    chosen = select_failed_run([newer, older], since, 56)
    assert chosen is not None
    assert chosen["id"] == 102


def test_a_neighbouring_prs_failed_run_does_not_abort_this_wait() -> None:
    # Two PRs dispatch concurrently (the workflow concurrency group is per-PR);
    # PR 41's quota failure after PR 56's floor must not end 56's wait — 56's
    # real review may post minutes later.
    since = parse_ts("2026-07-23T14:00:00Z")
    other = _run(
        101,
        "completed",
        "failure",
        "2026-07-23T14:00:05Z",
        title="Gemini review: PR 41",
    )
    assert select_failed_run([other], since, 56) is None


def test_pr_title_matching_is_whole_token() -> None:
    # "PR 5" must not claim PR 56's run.
    from bot_review import run_is_for_pr

    run = _run(1, "completed", "failure", "2026-07-23T14:00:05Z")
    assert run_is_for_pr(run, 56) is True
    assert run_is_for_pr(run, 5) is False
    assert run_is_for_pr(run, 6) is False


def test_wait_with_a_run_id_fails_fast_on_exactly_that_run(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # The --run path: request confirmed run 101 and printed it; when that run
    # dies, no review is ever coming and wait says so immediately. Polling by
    # id also sidesteps the clock-skew trap: a GitHub-stamped created_at just
    # below the locally-stamped floor would make a timestamp filter miss the
    # very run the dispatch created.
    import bot_review

    def no_reviews(repo: str, pr: int) -> list[dict[str, Any]]:
        return []

    def failed_run(repo: str, run_id: int) -> dict[str, Any]:
        assert run_id == 101
        return _run(101, "completed", "failure", "2026-07-23T13:59:59Z")

    monkeypatch.setattr(bot_review, "list_reviews", no_reviews)
    monkeypatch.setattr(bot_review, "workflow_run", failed_run)
    since = parse_ts("2026-07-23T14:00:00Z")  # skew: run stamped 1s below it
    assert bot_review.cmd_wait("o/r", 56, GEMINI, since, 600, run_id=101) == 1
    err = capsys.readouterr().err
    assert "run 101" in err
    assert "'failure'" in err


def test_wait_without_a_run_id_falls_back_to_this_prs_runs(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # The recovered-floor path (--since-commit, no --run): the fail-fast scans
    # the runs list, but only THIS PR's runs — a neighbouring PR's failure
    # keeps this wait alive, then this PR's own failure ends it.
    import bot_review

    def no_reviews(repo: str, pr: int) -> list[dict[str, Any]]:
        return []

    runs_by_call = iter(
        [
            [
                _run(
                    101,
                    "completed",
                    "failure",
                    "2026-07-23T14:00:05Z",
                    title="Gemini review: PR 41",
                )
            ],
            [_run(102, "completed", "failure", "2026-07-23T14:00:06Z")],
        ]
    )

    def runs(repo: str, wf: str) -> list[dict[str, Any]]:
        return next(runs_by_call)

    def no_sleep(_seconds: float) -> None:
        return None

    monkeypatch.setattr(bot_review, "list_reviews", no_reviews)
    monkeypatch.setattr(bot_review, "workflow_runs", runs)
    monkeypatch.setattr(bot_review.time, "sleep", no_sleep)
    since = parse_ts("2026-07-23T14:00:00Z")
    assert bot_review.cmd_wait("o/r", 56, GEMINI, since, timeout=600) == 1
    assert "run 102" in capsys.readouterr().err


def test_a_posted_review_outranks_a_red_run(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The ordering the fail-fast branch promises: findings first, every
    # iteration. A review that landed before its run turned red (or a red run
    # from an unrelated dispatch) must never eat a real review.
    import bot_review

    review = _review(
        7,
        "github-actions[bot]",
        "2026-07-23T14:05:00Z",
        body="posted 1 inline finding(s).",
    )

    def the_review(repo: str, pr: int) -> list[dict[str, Any]]:
        return [review]

    def failed_run(repo: str, run_id: int) -> dict[str, Any]:
        return _run(101, "completed", "failure", "2026-07-23T14:00:05Z")

    monkeypatch.setattr(bot_review, "list_reviews", the_review)
    monkeypatch.setattr(bot_review, "workflow_run", failed_run)
    since = parse_ts("2026-07-23T14:00:00Z")
    assert bot_review.cmd_wait("o/r", 56, GEMINI, since, 600, run_id=101) == 0


def test_dispatch_request_confirms_a_new_run_and_prints_the_floor(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # The dispatches endpoint answers 204 before any run exists, so the
    # read-back polls for a run id newer than the newest pre-dispatch run —
    # ids, not timestamps, because the floor is stamped by this machine's
    # clock and run creation by GitHub's.
    import bot_review

    calls: list[tuple[str, tuple[str, ...]]] = []
    # before; first poll: another PR's newer run must NOT confirm; then ours.
    pages = iter(
        [
            [_run(100, "in_progress", None, "2026-07-23T13:00:00Z")],
            [
                _run(
                    101,
                    "in_progress",
                    None,
                    "2026-07-23T14:00:02Z",
                    title="Gemini review: PR 41",
                )
            ],
            [_run(102, "in_progress", None, "2026-07-23T14:00:05Z")],
        ]
    )

    def fake_gh(path: str, *args: str) -> Any:
        calls.append((path, args))
        if path == "repos/o/r":
            return {"default_branch": "main"}
        if path.endswith("/dispatches"):
            return None
        assert "/runs?" in path
        return {"workflow_runs": next(pages)}

    monkeypatch.setattr(bot_review, "gh", fake_gh)

    def no_sleep(_seconds: float) -> None:
        return None

    monkeypatch.setattr(bot_review.time, "sleep", no_sleep)
    assert bot_review.cmd_request("o/r", 56, GEMINI) == 0
    dispatch = next(c for c in calls if c[0].endswith("/dispatches"))
    assert dispatch == (
        "repos/o/r/actions/workflows/gemini-review.yml/dispatches",
        ("-f", "ref=main", "-f", "inputs[pr]=56"),
    )
    out = capsys.readouterr().out
    assert "dispatched gemini-review.yml run 102 for PR 56" in out
    assert "since: " in out
    assert "--bot gemini --pr 56" in out
    assert "--run 102" in out  # threads into wait's exact-run fail-fast


def test_the_first_ever_dispatch_of_a_workflow_is_confirmed_too(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # The bootstrap case: a workflow with zero prior runs has no pre-dispatch
    # id to compare against; the appearance of any run is the confirmation.
    import bot_review

    pages: list[dict[str, Any]] = [
        {"workflow_runs": []},
        {"workflow_runs": [_run(5, "in_progress", None, "2026-07-23T14:00:05Z")]},
    ]
    empty_then_one = iter(pages)

    def fake_gh(path: str, *args: str) -> Any:
        if path == "repos/o/r":
            return {"default_branch": "main"}
        if path.endswith("/dispatches"):
            return None
        return next(empty_then_one)

    monkeypatch.setattr(bot_review, "gh", fake_gh)
    assert bot_review.cmd_request("o/r", 56, GEMINI) == 0
    assert "dispatched gemini-review.yml run 5 for PR 56" in capsys.readouterr().out


def test_a_dispatch_that_spawns_no_run_fails_loudly_rather_than_waiting(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # 204 with no run ever appearing (disabled workflow, or the workflow file
    # not on the default branch yet — a dispatch workflow cannot review the PR
    # that introduces it). Waiting after that buys the full poll timeout for a
    # review nobody managed to ask for.
    import bot_review

    def fake_gh(path: str, *args: str) -> Any:
        if path == "repos/o/r":
            return {"default_branch": "main"}
        if path.endswith("/dispatches"):
            return None
        # A NEWER run exists — but it belongs to another PR, so it must not
        # cross-confirm this dispatch (two sessions shipping within seconds).
        return {
            "workflow_runs": [
                _run(
                    101,
                    "in_progress",
                    None,
                    "2026-07-23T14:00:02Z",
                    title="Gemini review: PR 41",
                ),
                _run(
                    100,
                    "in_progress",
                    None,
                    "2026-07-23T13:00:00Z",
                    title="Gemini review: PR 41",
                ),
            ]
        }

    monkeypatch.setattr(bot_review, "gh", fake_gh)
    monkeypatch.setattr(bot_review, "DISPATCH_CONFIRM_TIMEOUT", 0)
    with pytest.raises(BotReviewError, match="Do not wait"):
        bot_review.cmd_request("o/r", 56, GEMINI)
    # And no floor, deliberately — unlike a *read failure* on this same path,
    # which prints one. Here the runs list was read and held no new run, so
    # nothing is executing that a floor could be pointed at; the run is the
    # reviewer. Pinned because the guard that keeps them apart is an `except`
    # where the sibling channel uses a `finally`, and collapsing the two looks
    # like tidying rather than the reversal of a decision that it is.
    assert "since: " not in capsys.readouterr().out


def test_a_runs_payload_without_the_envelope_fails_loudly(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The runs endpoint wraps its list in an envelope object; GitHub's error
    # object is a dict *without* workflow_runs. Degrading that to "no runs"
    # would silently disarm both the dispatch read-back and wait's fail-fast.
    import bot_review

    def error_gh(path: str, *args: str) -> Any:
        return {"message": "Not Found"}

    monkeypatch.setattr(bot_review, "gh", error_gh)
    with pytest.raises(BotReviewError, match="workflow_runs"):
        bot_review.workflow_runs("o/r", "gemini-review.yml")


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
    # The marker .github/scripts/gemini_review_agent.py writes into every
    # review body — including the body-only fallback, which restates 0 inline.
    assert (
        stated_count(
            "reviewed this PR's filtered diff and posted 3 inline finding(s).",
            GEMINI,
        )
        == 3
    )
    assert stated_count("posted 0 inline finding(s).", GEMINI) == 0


def test_copilot_wordless_clean_reviews_count_as_zero() -> None:
    # Copilot never writes "generated 0 comments" (issue #61 — verified across
    # every Copilot review on this repo; 0 occurrences). A clean first pass says
    # "no comments" and a clean re-review says "no new comments" — both wordless,
    # both an explicit assertion of zero findings. They must read as 0, not None:
    # None would make count_note skip the cross-check on exactly the clean
    # re-review a caller is most tempted to wave through. Bodies transcribed from
    # real reviews (the re-review is PR#60's second Copilot review).
    first_pass = (
        "Copilot reviewed 8 out of 8 changed files in this pull request "
        "and generated no comments."
    )
    re_review = (
        "Copilot reviewed 8 out of 8 changed files in this pull request "
        "and generated no new comments."
    )
    assert stated_count(first_pass, COPILOT) == 0
    assert stated_count(re_review, COPILOT) == 0
    # A wordless clean review states 0 and fetches 0 comments -> the cross-check
    # runs and is satisfied, so no spurious "cross-check skipped" note. This is
    # the behavior change: before the fix stated_count returned None here.
    assert count_note(stated_count(re_review, COPILOT), 0) is None


def test_stated_count_distinguishes_wordless_zero_from_no_statement() -> None:
    # The three outcomes must stay distinct: a matched-but-wordless count is an
    # asserted 0 (cross-check lives), while a body that states nothing countable
    # is None (cross-check skipped). Conflating them would either resurrect the
    # #61 gap or silence a genuinely uncountable body's warning.
    assert stated_count("generated no comments.", COPILOT) == 0
    assert stated_count("nothing about counts here", COPILOT) is None
    # "comment" is a prefix of "commentary" — without a word boundary on the
    # count term, "generated no commentary" would read as an asserted 0 and
    # silence the absent-count warning. The `comments?\b` in the regex forbids
    # it (CodeRabbit, PR #63); it states no count, so it must stay None.
    assert stated_count("Copilot generated no commentary on this.", COPILOT) is None
    assert stated_count("generated 2 commentary sections", COPILOT) is None


def test_gemini_review_is_authored_by_the_actions_bot() -> None:
    review = _review(
        1,
        "github-actions[bot]",
        "2026-07-23T14:05:00Z",
        body="## Antigravity Gemini review\n\nposted 2 inline finding(s).",
    )
    assert is_findings_review(review, GEMINI) is True
    assert is_findings_review(review, CODERABBIT) is False


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
    # TextIOWrapper defaulting to the console codepage. The reason given here
    # for the child process used to be that pytest's captured stdout is not a
    # TextIOWrapper — that is **false**, measured: `_pytest.capture.EncodedFile`
    # and `TeeCaptureIO` are both subclasses, which is exactly why calling this
    # from `main()` reconfigured the session's own capture streams. The real
    # reason is narrower and still holds: only a child process has a stdout
    # whose encoding is the console codepage, which is the thing under test.
    scripts = str(Path(__file__).resolve().parent.parent / "scripts")
    code = (
        f"import sys; sys.path.insert(0, {scripts!r});"
        "import bot_review; bot_review.use_utf8_io((sys.stdout, sys.stderr));"
        'print("\\U0001f407\\u2705 \\u2014 \\U0001f4d0")'
    )
    out = run_cmd([sys.executable, "-c", code])
    assert "🐇✅" in out
    assert "—" in out  # the em dash that came back as `?` before the fix


def test_the_utf8_reconfigure_stays_out_of_main_and_fires_from_dunder_main() -> None:
    """`review_worktree.py`'s twin has this pin; `bot_review.py` had none.

    The test above calls `use_utf8_io` directly inside a child, bypassing
    both `main()` and `__main__`, so two regressions were invisible:

    * deleting the `__main__` call silently removes UTF-8 protection from
      every real invocation — the script is reached from six skills
      (coderabbit/copilot/gemini/greptile-review, ship, squash-merge) and
      from `.claude/bot-review-triage.md` — reinstating the
      UnicodeEncodeError-after-the-API-work-succeeded bug while the test
      above still passes;
    * re-adding it *inside* `main()` reinstates the session-wide
      `errors="replace"` downgrade of pytest's capture streams, with ruff,
      pyright and the whole suite green.

    Source-level because that is where the property lives: which call site
    the reconfigure sits at is not observable from a single invocation.
    """
    source = (
        Path(__file__).resolve().parent.parent / "scripts" / "bot_review.py"
    ).read_text(encoding="utf-8")
    main_body = source.split("\ndef main(")[1].split("\nif __name__")[0]
    assert "use_utf8_io" not in main_body, (
        "the reconfigure is back inside main(), where it mutates pytest's "
        "own capture streams for every in-process call"
    )
    dunder_main = source.split("\nif __name__")[1]
    assert "use_utf8_io((sys.stdout, sys.stderr))" in dunder_main, (
        "deleting this leaves every real invocation unprotected"
    )


def test_repo_stats_reconfigure_stays_out_of_main_and_states_its_error_handler() -> (
    None
):
    """The third live copy, and the one that reset the handler to `strict`.

    `tests/test_repo_stats.py` calls `rs.main()` **in-process**, so the
    reconfigure this diff removed from the other two scripts was still live
    here — and worse: it passed `encoding=` with no `errors=`, and
    `TextIOWrapper.reconfigure` resets the handler to `strict` when `errors`
    is omitted. So `rs.main([])` left every later test in that worker writing
    through a stricter stream than the suite started with, including the one
    the POSIX legs tee for the log-canary gate, and a later test emitting a
    lone surrogate raised UnicodeEncodeError with the blame landing on the
    innocent test.

    Both halves are asserted, because fixing only the call site would leave
    the handler implicit and a future edit could drop it again.
    """
    source = (
        Path(__file__).resolve().parent.parent / "scripts" / "repo_stats.py"
    ).read_text(encoding="utf-8")
    main_body = source.split("\ndef main(")[1].split("\nif __name__")[0]
    assert "reconfigure" not in main_body
    assert "_use_utf8_io" not in main_body
    assert 'errors="replace"' in source.split("\ndef _use_utf8_io")[1], (
        "reconfigure without errors= resets the handler to strict"
    )
    assert "_use_utf8_io()" in source.split("\nif __name__")[1]


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


# --------------------------------------------------------------------------
# Greptile: the run is reported through a summary *issue comment*, not through
# the reviews endpoint. Every constant below is transcribed from a live run on
# this repo — PR #67 (findings), #68 and #70 (clean), #69 (findings then clean).
# --------------------------------------------------------------------------

GREPTILE_SHA = "34730887dd1f57a836d0dab8c7c35c8a35c29814"
OTHER_SHA = "bf9173dbd5fe46f065c171ef488507f4c4f7c158"
_RETRIGGER = "https://app.greptile.com/api/retrigger?id=47296691"

# The "Files Needing Attention" lines observed live. #70 ended the clean form
# with a period; #68 continued the sentence with a trailing clause. That
# difference is the whole reason the clean pattern matches a phrase rather than
# a sentence.
ATTENTION_CLEAN = "No files require special attention."
ATTENTION_CLEAN_WITH_CLAUSE = (
    "No files require special attention; the spec files are consistent with each other."
)
ATTENTION_FINDINGS = (
    "specs/adr/0065-catalog-merge-and-removal.md §3 collision table — the "
    "fourth preflight check should appear there."
)


def _greptile_summary(
    attention: str,
    sha: str = GREPTILE_SHA,
    reviews: int = 1,
    fix_prompt: bool = False,
) -> str:
    """A Greptile run summary in the live shape (PR #67/#68/#70)."""
    commit_url = f"https://github.com/neuroticgamer99/healthspan/commit/{sha}"
    prompt = (
        "<details><summary>Prompt To Fix All With AI</summary>\n\n"
        "Fix the following 1 code review issue. Work through them one at a "
        "time, proposing concise fixes.\n\n</details>\n\n"
        if fix_prompt
        else ""
    )
    return (
        "<details><summary><h3>Greptile Summary</h3></summary>\n\n"
        "This PR does a thing.\n</details>\n\n"
        "<h3>Confidence Score: 5/5</h3>\n\nSafe to merge.\n\n"
        f"**Files Needing Attention:** {attention}\n\n"
        "<!-- greptile_other_comments_section -->\n\n"
        f"{prompt}"
        f'<sub>Reviews ({reviews}): Last reviewed commit: ["subject"]'
        f"({commit_url}) | [Re-trigger Greptile]({_RETRIGGER})</sub>\n"
    )


def _pull_comment(
    comment_id: int,
    login: str,
    updated_at: str,
    in_reply_to_id: int | None = None,
    review_id: int | None = None,
) -> dict[str, Any]:
    return {
        "id": comment_id,
        "user": {"login": login},
        "created_at": updated_at,
        "updated_at": updated_at,
        "in_reply_to_id": in_reply_to_id,
        # Which review this comment hangs off, as GitHub reports it. Defaults to
        # None because most fixtures do not care; unmatched_reviews reads it to
        # tell "the filter missed a comment" from "there was no comment".
        "pull_request_review_id": review_id,
        "path": "src/healthspan/fsperm.py",
        "line": 42,
        "body": "P2 — the printed remedy omits /remove:g.",
    }


def _greptile_comment(body: str, fresh: bool = False) -> dict[str, Any]:
    """PR #69's summary comment: created at the first run, edited by the second.

    ``fresh`` stamps it *now* instead. Needed by the tests about the
    summary-before-comments race, since `comments_pending` only calls an
    undercount "still landing" inside a two-minute grace window — a fixture
    stamped in the past is, correctly, no longer pending.
    """
    if fresh:
        now = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
        return _comment(5080225922, "greptile-apps[bot]", now, now, body=body)
    return _comment(
        5080225922,
        "greptile-apps[bot]",
        "2026-07-25T19:21:16Z",
        "2026-07-25T21:09:13Z",
        body=body,
    )


def test_a_greptile_findings_review_is_invisible_to_the_review_path() -> None:
    # The load-bearing fact behind the whole summary_marker path: PR #67's and
    # #69's findings reviews carry state COMMENTED and a body of length ZERO, so
    # the discriminator every other bot relies on rejects them. Polling the
    # reviews endpoint for Greptile finds nothing and times out on a review that
    # completed.
    review = _review(4778163288, "greptile-apps[bot]", "2026-07-25T03:06:12Z", body="")
    assert is_findings_review(review, GREPTILE) is False


@pytest.mark.parametrize(
    "attention", [ATTENTION_CLEAN, ATTENTION_CLEAN_WITH_CLAUSE], ids=["pr70", "pr68"]
)
def test_the_clean_sentinel_is_a_phrase_not_a_sentence(attention: str) -> None:
    # PR #70 wrote "...special attention." and PR #68 wrote "...special
    # attention; the spec files are consistent with each other." A pattern
    # anchored on the period form would have read #68 — a genuinely clean run —
    # as findings, then triaged it against zero comments.
    comment = _greptile_comment(_greptile_summary(attention))
    assert is_clean_comment(comment, GREPTILE) is True


def test_a_findings_summary_is_not_clean_but_is_still_the_summary() -> None:
    comment = _greptile_comment(
        _greptile_summary(ATTENTION_FINDINGS, fix_prompt=True),
    )
    assert is_clean_comment(comment, GREPTILE) is False
    # Recognizing the comment and judging its verdict are separate questions,
    # because a findings run and a clean run both report through this comment.
    assert is_summary_comment(comment, GREPTILE) is True


@pytest.mark.parametrize(
    "marker",
    [
        "<!-- greptile_other_comments_section -->",
        "<!-- greptile_failed_comments -->",
        "<!-- greptile_a_section_not_yet_observed -->",
        "<!--  greptile_other_comments_section  -->",
        "<!--greptile_other_comments_section-->",
    ],
    ids=[
        "other-comments",
        "outside-diff",
        "unobserved",
        "extra-spacing",
        "no-spacing",
    ],
)
def test_the_summary_is_recognized_through_any_section_marker(marker: str) -> None:
    # A summary carrying a "Comments Outside Diff" section emits
    # `greptile_failed_comments` where the observed bodies emitted
    # `greptile_other_comments_section`. Keyed on the latter literal, `wait`
    # burned its full 1800s on a review that had landed in 3.5 minutes and
    # `fetch` then refused with "no greptile summary" while the summary was
    # plainly on the PR (PR #81).
    #
    # The last two ids are the spacing half, and they are not hypothetical
    # either: PR #85 measured this vendor changing its summary template
    # unannounced — the `Files Needing Attention:` line `clean_marker` keys on
    # simply vanished — so the whitespace inside its HTML comments is exactly
    # the kind of presentation detail this whole change refuses to gate on.
    # A one-space pattern rejected both of them.
    #
    # The third id is not an observed shape and is the point of this test:
    # matching the marker *namespace* is what makes a section this repo has
    # never seen — added or renamed by a dashboard toggle, outside any diff —
    # unable to take the summary away again. A pattern listing the two known
    # literals would pass the first two cases and fail this one.
    body = _greptile_summary(ATTENTION_CLEAN).replace(
        "<!-- greptile_other_comments_section -->", marker
    )
    assert is_summary_comment(_greptile_comment(body), GREPTILE) is True


def test_a_foreign_html_comment_is_not_a_greptile_summary() -> None:
    # The namespace pattern is deliberately loose about which section it sees,
    # but it is not loose about whose section it is: another tool's marker in a
    # comment that greptile happened to author must not be read as a summary,
    # or the freshness and count logic downstream reads a body that states
    # neither.
    body = _greptile_summary(ATTENTION_CLEAN).replace(
        "<!-- greptile_other_comments_section -->", "<!-- coderabbitai_section -->"
    )
    assert is_summary_comment(_greptile_comment(body), GREPTILE) is False


@pytest.mark.parametrize(
    "marker",
    [
        "<!-- greptile -->",
        "<!-- greptileai -->",
        "<!-- greptile-other-section -->",
        "<!-- greptile_ -->",
    ],
    ids=["bare", "app-handle", "hyphenated", "separator-only"],
)
def test_the_namespace_still_demands_a_section_name(marker: str) -> None:
    # `<!-- greptile_\w+` widened the marker on purpose, and this is the half of
    # it that is NOT widened: a section *name* is still required, separated by
    # the underscore Greptile writes. Without this the pattern could be weakened
    # all the way to a bare `<!-- greptile` prefix and every other test here
    # would stay green — `search()` is unanchored, so all the positive cases
    # differ only in which greptile-namespaced suffix they carry, and the
    # foreign-marker case above carries no "greptile" substring at all. Neither
    # separates "demands a section name" from "demands nothing".
    #
    # It matters because these are the shapes a loosened prefix would swallow:
    # a bare mention, the App's own `@greptileai` handle, a hyphenated spelling.
    # Any of them appearing in some future non-summary comment the App posts
    # would be selected AS the summary, and the count, freshness and clean
    # verdicts would then all be read off a body that states none of them.
    #
    # Which case earns its place, measured rather than reasoned — an earlier
    # version of this comment asserted an exclusivity it had not run, and three
    # of its four claims were wrong. T means the mutant pattern matches, i.e.
    # that case goes red. **Scoped to this test's own cases**, which is a real
    # limit rather than a hedge: `letters only` also stops the real marker
    # matching at all, so its effects reach well past the four cases below, and
    # a table showing only this column would read as though one case caught it.
    #
    # **How far past is deliberately not characterized**, because four attempts
    # to say it were all wrong, each written to fix the last. `42`, from a run
    # silently scoped by a `-k` filter. Then `44`, when the script recounting it
    # split parametrized ids on whitespace and merged distinct failures. Then
    # "most of the file", which 44 of 201 is not. Then "every test whose fixture
    # carries a Greptile summary" — and twelve of those stay green.
    #
    # That last one is the useful correction, so keep the reason and not the
    # tally: building a fixture through `_greptile_summary` only puts the
    # literal in the body. It does not mean the assertion reaches it *through*
    # `summary_marker` — a test may key on a different field of the same spec
    # (`reviewed_commit`, `count`, `clean_marker`), or build its `SummaryState`
    # by hand and parse no body, or substitute the marker or the author so its
    # spelling never decides the outcome.
    #
    # Nothing is claimed about how the twelve divide among those, or which way
    # any of them asserts. A fifth version said "negative tests stay False" and
    # at least three assert the opposite. The sixth stops enumerating.
    #
    # The table below is measured and scoped to what it shows. That is the
    # whole claim.
    #
    #   mutation (against `<!--\s*greptile_\w+`)  bare  handle  hyphen  sep-only
    #   `<!--\s*greptile\w+`     drop separator     .      T       .       T
    #   `<!--\s*greptile[-_]\w+` widen it           .      .       T       .
    #   `<!--\s*greptile[a-z]+`  letters only       .      T       .       .
    #   `<!--\s*greptile`        full collapse      T      T       T       T
    #   `<!--\s*greptile_\w*`    quantifier soft    .      .       .       T
    #   `<!--\s*greptile_`       quantifier gone    .      .       .       T
    #   `<!-- greptile_\w+`      spacing rigid      .      .       .       .
    #
    # The last row is caught by nothing here on purpose — it is a *positive*
    # regression (a real marker stops matching), so it belongs to the
    # recognition test above, where the `extra-spacing` and `no-spacing` cases
    # both go red on it.
    #
    # So three cases each catch something nothing else does: `app-handle` the
    # letters-only class, `hyphenated` the widened separator, `separator-only`
    # the soft or absent quantifier — that last being the mutation which
    # otherwise survives every test in this file, since no other fixture carries
    # `greptile_` with nothing word-like after it. `bare` uniquely catches
    # nothing: the full collapse is caught by all four independently, none of
    # them needing its help. It stays because it is the likeliest accidental
    # shape a future non-summary comment would carry, which is documentation
    # value rather than detection value — and saying so is the point, because
    # claiming otherwise is what this comment got wrong before.
    body = _greptile_summary(ATTENTION_CLEAN).replace(
        "<!-- greptile_other_comments_section -->", marker
    )
    assert is_summary_comment(_greptile_comment(body), GREPTILE) is False


def test_a_summary_from_another_author_is_not_the_bots() -> None:
    comment = _comment(
        1,
        "not-greptile-apps[bot]",
        "2026-07-25T23:13:06Z",
        "2026-07-25T23:13:06Z",
        body=_greptile_summary(ATTENTION_CLEAN),
    )
    assert is_summary_comment(comment, GREPTILE) is False


def test_a_re_review_is_seen_only_through_the_edit_time() -> None:
    # PR #69: the re-triggered review edited comment 5080225922 in place
    # (created 19:21:16Z, updated 21:09:13Z) and posted nothing else — no new
    # comment, no new review. Keyed on created_at, a floor stamped at the
    # re-trigger sees no answer and times out on a completed review.
    edited = _greptile_comment(_greptile_summary(ATTENTION_CLEAN, reviews=2))
    since = parse_ts("2026-07-25T21:03:00Z")
    chosen = select_summary_comment([edited], GREPTILE, since)
    assert chosen is not None
    assert chosen["id"] == 5080225922


def test_reviewed_sha_is_read_out_of_the_footer() -> None:
    assert reviewed_sha(_greptile_summary(ATTENTION_CLEAN), GREPTILE) == GREPTILE_SHA


def test_reviewed_sha_is_anchored_to_the_footer_phrase() -> None:
    # The narrative and the per-file table routinely link commits, on either
    # side of the footer. The capture is anchored to "Last reviewed commit:"
    # and bounded to that line, so none of them can be read as "the commit
    # reviewed" — an unanchored pattern would take whichever came first.
    stray = "See https://github.com/o/r/commit/" + "a" * 40 + " for context.\n"
    trailing = "Earlier: https://github.com/o/r/commit/" + "b" * 40 + "\n"
    body = stray + _greptile_summary(ATTENTION_CLEAN) + trailing
    assert reviewed_sha(body, GREPTILE) == GREPTILE_SHA


def test_same_commit_prefix_compares_but_refuses_a_short_abbreviation() -> None:
    assert same_commit(GREPTILE_SHA, GREPTILE_SHA[:7]) is True
    assert same_commit(GREPTILE_SHA, OTHER_SHA) is False
    # Answering True on a 3-character "abbreviation" would silently disable the
    # staleness guard for every commit sharing those characters.
    assert same_commit(GREPTILE_SHA, GREPTILE_SHA[:3]) is False


def test_same_commit_ignores_hex_case() -> None:
    # Git and the GitHub UI both render abbreviated SHAs in either case, and a
    # case-sensitive compare would report a fresh review as stale — sending the
    # caller to re-trigger a review that had already run.
    assert same_commit(GREPTILE_SHA, GREPTILE_SHA.upper()) is True


def test_a_bots_reply_is_not_one_of_its_findings() -> None:
    # These come from the pull-level endpoint, so the bot's own replies to a
    # triage thread carry the same author as its findings. Re-triaging an
    # "acknowledged, fixed" reply as a fresh finding is what this prevents.
    reply = _pull_comment(
        2, "greptile-apps[bot]", "2026-07-25T21:09:13Z", in_reply_to_id=1
    )
    finding = _pull_comment(3, "greptile-apps[bot]", "2026-07-25T21:09:13Z")
    assert is_finding_comment(reply, GREPTILE) is False
    assert is_finding_comment(finding, GREPTILE) is True


def test_findings_from_an_earlier_run_are_excluded_by_the_floor() -> None:
    # The review id cannot scope these (a re-review posts under no new review),
    # so the floor is the only scoping there is.
    since = parse_ts("2026-07-25T21:03:00Z")
    stale = _pull_comment(3650858888, "greptile-apps[bot]", "2026-07-25T19:21:20Z")
    fresh = _pull_comment(3650999999, "greptile-apps[bot]", "2026-07-25T21:09:10Z")
    selected = select_finding_comments([stale, fresh], GREPTILE, since)
    assert [c["id"] for c in selected] == [3650999999]


def test_findings_are_returned_oldest_first() -> None:
    # Triage output reads top to bottom, so the order is the order the bot
    # reported in; reversing it silently reorders the verdict table against the
    # thread it must line up with.
    since = parse_ts("2026-07-25T19:00:00Z")
    later = _pull_comment(2, "greptile-apps[bot]", "2026-07-25T21:09:20Z")
    earlier = _pull_comment(1, "greptile-apps[bot]", "2026-07-25T21:09:10Z")
    selected = select_finding_comments([later, earlier], GREPTILE, since)
    assert [c["id"] for c in selected] == [1, 2]


def test_greptile_is_asked_in_thread_with_its_own_handle(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import bot_review

    assert GREPTILE.trigger_body == "@greptileai review"
    assert GREPTILE.request_login is None
    assert GREPTILE.dispatch_workflow is None
    assert GREPTILE.review_login == "greptile-apps[bot]"

    posted: list[tuple[str, ...]] = []

    def fake_gh(path: str, *args: str) -> Any:
        posted.append(args)
        return {"id": 1, "body": "@greptileai review"}

    monkeypatch.setattr(bot_review, "gh", fake_gh)
    assert bot_review.cmd_request("o/r", 71, GREPTILE) == 0
    # The handle is `@greptileai`, not `@greptile`: the wrong string posts a
    # comment that summons nothing, then buys a full poll waiting for it.
    assert posted == [("-f", "body=@greptileai review")]


def _greptile_wait(
    monkeypatch: pytest.MonkeyPatch,
    summary_body: str | None,
    head: str = GREPTILE_SHA,
    timeout: int = 0,
    comments: list[dict[str, Any]] | None = None,
    fresh: bool = False,
) -> int:
    """Drive cmd_wait with one summary comment, or none when body is None."""
    import bot_review

    posted = [] if summary_body is None else [_greptile_comment(summary_body, fresh)]
    review_comments = comments or []

    def the_summary(repo: str, pr: int) -> list[dict[str, Any]]:
        return posted

    def the_head(repo: str, pr: int) -> str:
        return head

    def the_comments(repo: str, pr: int) -> list[dict[str, Any]]:
        return review_comments

    def no_sleep(seconds: float) -> None:
        return None

    monkeypatch.setattr(bot_review, "issue_comments", the_summary)
    monkeypatch.setattr(bot_review, "pr_head_sha", the_head)
    monkeypatch.setattr(bot_review, "pull_comments", the_comments)
    monkeypatch.setattr(bot_review.time, "sleep", no_sleep)
    since = parse_ts("2026-07-25T19:00:00Z")
    return bot_review.cmd_wait("o/r", 69, GREPTILE, since, timeout)


def test_cmd_wait_reports_a_clean_summary_as_clean(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert _greptile_wait(monkeypatch, _greptile_summary(ATTENTION_CLEAN)) == EXIT_CLEAN


def test_cmd_wait_calls_a_countless_findings_summary_ready_not_clean(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # PR #84's shape: not clean, no stated count (the block holding the only
    # countable sentence was toggled off), no comments. `wait` answered this
    # correctly at the time — "ready", not "clean" — which is why the repair
    # for that PR changed `outstanding` and `fetch` and left `wait` alone.
    #
    # That "wait is unchanged" reasoning is true of the code and is not a
    # substitute for a test. Three documents now assert this behavior —
    # `unprovable_summaries`'s docstring, `.greptile/README.md`, and ADR-0067
    # §2 — and a later edit to `undercounted`, `comments_pending` or
    # SummaryState's construction could turn the one correct answer in that
    # incident into a wrong one, silently.
    #
    # What is new here is narrower than "nothing held this": the outward
    # ready-not-clean behavior is already exercised by two to four sibling
    # tests for adjacent shapes. The uncovered slice is the `stated is None`
    # short-circuit in `comments_pending` — measured, mutating its
    # `if not self.undercounted: return False` to `return True` is caught here
    # and by two siblings, but *not* by the PR #72 test, whose `stated=1`
    # takes a different branch. That slice is exactly PR #84's shape.
    assert _greptile_wait(monkeypatch, _greptile_summary(ATTENTION_FINDINGS)) == 0
    out = capsys.readouterr().out
    assert "is ready" in out
    # Not the clean verdict. Reporting this run clean is precisely the failure
    # `outstanding` and `fetch` committed on #84.
    assert "CLEAN:" not in out
    # And it must say there is something to read. "ready — 0 open finding(s)"
    # is true of the comments endpoint and useless to the operator, who then
    # meets the merge gate's refusal with no warning — the sibling of the same
    # incident's `fetch` lie, and what this line said before it was fixed. No
    # number, because the count lives in the block the toggle removed.
    assert "exists only in the summary text" in out
    assert "states no count" in out


def test_cmd_wait_reports_a_findings_summary_as_ready(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    body = _greptile_summary(ATTENTION_FINDINGS, fix_prompt=True)
    finding = _pull_comment(3650858888, "greptile-apps[bot]", "2026-07-25T21:09:10Z")
    assert _greptile_wait(monkeypatch, body, comments=[finding]) == 0


def test_cmd_wait_keeps_polling_until_the_counted_comments_have_landed(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # Greptile posts its summary ~4s before the inline comments it counts (PR
    # #67 03:06:07 -> 03:06:11; PR #69 19:21:16 -> 19:21:20). With a 30s poll,
    # roughly one findings run in seven lands inside that window. Returning
    # there hands fetch a review whose findings do not exist yet, which it then
    # reports as a count mismatch — a real alarm for a non-problem.
    body = _greptile_summary(ATTENTION_FINDINGS, fix_prompt=True)
    assert _greptile_wait(monkeypatch, body, comments=[], fresh=True) == 1
    assert "is ready" not in capsys.readouterr().out


def test_a_clean_summary_over_an_unanswered_finding_is_not_clean(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # The gap that made the clean path the only one checking nothing: a
    # re-review edits the summary to "no files need attention" while an
    # untriaged finding from the first run sits above the floor. Reporting
    # clean there is how a finding nobody read reaches a merge.
    body = _greptile_summary(ATTENTION_CLEAN, reviews=2)
    finding = _pull_comment(3650858888, "greptile-apps[bot]", "2026-07-25T19:21:20Z")
    assert _greptile_wait(monkeypatch, body, comments=[finding]) == 0
    assert "1 open finding(s)" in capsys.readouterr().out


def test_a_clean_summary_over_an_answered_finding_is_clean(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The other half: once the finding carries a threaded reply the triage is
    # done, and the run is as finished as one that found nothing. Otherwise
    # every PR that ever had a finding would block forever.
    body = _greptile_summary(ATTENTION_CLEAN, reviews=2)
    finding = _pull_comment(3650858888, "greptile-apps[bot]", "2026-07-25T19:21:20Z")
    reply = _pull_comment(
        3650871889, "NeuroticGamer99", "2026-07-25T19:29:53Z", in_reply_to_id=3650858888
    )
    assert _greptile_wait(monkeypatch, body, comments=[finding, reply]) == EXIT_CLEAN


def test_a_summary_naming_a_superseded_commit_is_stale_not_ready(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # The failure this exists for, observed live on PR #69: the summary said it
    # last reviewed 72550f1 while the branch was three commits ahead. Its
    # updated_at is newer than any floor minted at PR creation, so by timestamp
    # alone a review of superseded code is indistinguishable from a fresh one.
    body = _greptile_summary(ATTENTION_CLEAN, sha=OTHER_SHA)
    assert _greptile_wait(monkeypatch, body, head=GREPTILE_SHA) == 1
    err = capsys.readouterr().err
    assert "STALE, not missing" in err
    assert OTHER_SHA[:7] in err
    assert GREPTILE_SHA[:7] in err


def test_a_summary_naming_no_commit_is_not_treated_as_stale(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # An unparsed footer is missing evidence, not evidence of staleness.
    # Failing closed there would wedge every wait behind a reworded footer;
    # fetch surfaces the unverified freshness instead.
    body = _greptile_summary(ATTENTION_CLEAN).replace("Last reviewed commit:", "Ref:")
    assert reviewed_sha(body, GREPTILE) is None
    assert _greptile_wait(monkeypatch, body) == EXIT_CLEAN


def _greptile_fetch(
    monkeypatch: pytest.MonkeyPatch,
    summary_body: str | None,
    comments: list[dict[str, Any]],
    head: str = GREPTILE_SHA,
    fresh: bool = False,
) -> int:
    """Drive cmd_fetch with one summary comment, or none when body is None."""
    import bot_review

    posted = [] if summary_body is None else [_greptile_comment(summary_body, fresh)]

    def the_summary(repo: str, pr: int) -> list[dict[str, Any]]:
        return posted

    def the_head(repo: str, pr: int) -> str:
        return head

    def the_comments(repo: str, pr: int) -> list[dict[str, Any]]:
        return comments

    monkeypatch.setattr(bot_review, "issue_comments", the_summary)
    monkeypatch.setattr(bot_review, "pr_head_sha", the_head)
    monkeypatch.setattr(bot_review, "pull_comments", the_comments)
    since = parse_ts("2026-07-25T19:00:00Z")
    return bot_review.cmd_fetch("o/r", 69, GREPTILE, since)


def test_cmd_fetch_prints_findings_read_from_the_comments_endpoint(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    body = _greptile_summary(ATTENTION_FINDINGS, fix_prompt=True)
    finding = _pull_comment(3650858888, "greptile-apps[bot]", "2026-07-25T21:09:10Z")
    assert _greptile_fetch(monkeypatch, body, [finding]) == 0
    out = capsys.readouterr().out
    assert "1 open finding(s)" in out
    assert "src/healthspan/fsperm.py:42" in out
    assert "id=3650858888" in out
    # The body's own count agrees with what was fetched, so no NOTE fires.
    assert "NOTE:" not in out


def test_cmd_fetch_reprints_only_what_is_still_unanswered(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # Re-running fetch after triage must not re-litigate answered findings —
    # otherwise the merge gate can never be satisfied on a PR that had any.
    body = _greptile_summary(ATTENTION_FINDINGS, fix_prompt=True)
    finding = _pull_comment(3650858888, "greptile-apps[bot]", "2026-07-25T21:09:10Z")
    reply = _pull_comment(
        3650871889, "NeuroticGamer99", "2026-07-25T21:29:53Z", in_reply_to_id=3650858888
    )
    assert _greptile_fetch(monkeypatch, body, [finding, reply]) == EXIT_CLEAN
    out = capsys.readouterr().out
    assert "NOTHING OUTSTANDING: all 1 finding(s) have a reply." in out
    assert "id=3650858888" not in out


def test_cmd_fetch_reports_a_clean_summary_as_clean(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # `/squash-merge` calls fetch directly and treats exit 2 as the signal that
    # the Greptile gate is satisfied, so this is the exit code a merge depends
    # on — not merely wait's. A clean run has no findings to print and no
    # review object to read, so fetch must answer from the summary alone.
    assert _greptile_fetch(monkeypatch, _greptile_summary(ATTENTION_CLEAN), []) == (
        EXIT_CLEAN
    )


def test_cmd_fetch_reports_silence_as_silence_rather_than_as_clean(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # No summary at all is the one case that must never collapse into "clean":
    # both mean "nothing to triage", and only one of them means the code was
    # looked at.
    assert _greptile_fetch(monkeypatch, None, []) == 1
    assert "not the same as a clean review" in capsys.readouterr().err


def test_cmd_wait_times_out_plainly_when_the_bot_has_not_reported(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # The other half of the timeout contract: `/greptile-review` step 3 tells
    # the agent to react differently to "no review arrived" than to "STALE, not
    # missing", so the two messages must stay distinguishable. A stale summary
    # seen earlier in the poll is what selects the second message; with nothing
    # seen at all, it must be the first.
    assert _greptile_wait(monkeypatch, None) == 1
    err = capsys.readouterr().err
    assert "no greptile findings review" in err
    assert "STALE" not in err


def test_cmd_fetch_refuses_to_conclude_when_the_count_outruns_the_comments(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # The summary claims one finding; the comments endpoint yields none. Neither
    # "clean" nor "here are the findings" is true, so the honest answer is to
    # refuse — an empty result is a claim, and this one contradicts the body.
    #
    # `fresh=True` is load-bearing, not decoration: this is the *in-grace* case,
    # and the message it must produce ("still landing") is the opposite advice
    # from the post-grace one. A fixture stamped in the past would silently
    # exercise the other branch, and "only 0 were fetched" is common to both —
    # so it would pass either way and pin neither.
    body = _greptile_summary(ATTENTION_FINDINGS, fix_prompt=True)
    assert _greptile_fetch(monkeypatch, body, [], fresh=True) == 1
    err = capsys.readouterr().err
    assert "only 0 were fetched" in err
    assert "usually a review still landing" in err
    assert "no longer in flight" not in err


def test_wait_and_fetch_agree_when_the_two_signals_disagree(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # The invariant whose absence was the bug (Copilot, PR #71): wait reported
    # "ready" (0) on a state fetch then refused to classify (1), so the two
    # commands contradicted each other about the same summary and a caller was
    # sent to triage a run neither could read. Waiting cannot resolve a clean
    # marker sitting beside a stated count — unlike a pending comment — so the
    # wait must end here rather than poll.
    body = _greptile_summary(ATTENTION_CLEAN, fix_prompt=True)
    finding = _pull_comment(3650858888, "greptile-apps[bot]", "2026-07-25T21:09:10Z")
    assert _greptile_wait(monkeypatch, body, comments=[finding]) == 1
    assert "the two signals disagree" in capsys.readouterr().err
    assert _greptile_fetch(monkeypatch, body, [finding]) == 1
    assert "the two signals disagree" in capsys.readouterr().err


def test_a_summary_only_finding_ends_the_wait_instead_of_hanging(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # Transcribed from PR #72, a fifth Greptile shape: a summary stating one
    # finding with NO review object and NO inline comment anywhere — the
    # finding existed only as prose. `comments_pending` modelled the ~4s
    # summary-before-comments race as unbounded, so `wait` polled for comments
    # that were never coming and burned its whole timeout. The comment here is
    # old enough that the grace window has closed.
    body = _greptile_summary(ATTENTION_FINDINGS, fix_prompt=True)
    stale_summary = _comment(
        5085014060,
        "greptile-apps[bot]",
        "2020-01-01T00:00:00Z",
        "2020-01-01T00:00:00Z",
        body=body,
    )
    import bot_review

    def the_summary(repo: str, pr: int) -> list[dict[str, Any]]:
        return [stale_summary]

    def nothing(repo: str, pr: int) -> list[dict[str, Any]]:
        return []

    def the_head(repo: str, pr: int) -> str:
        return GREPTILE_SHA

    def no_sleep(seconds: float) -> None:
        return None

    monkeypatch.setattr(bot_review, "issue_comments", the_summary)
    monkeypatch.setattr(bot_review, "pull_comments", nothing)
    monkeypatch.setattr(bot_review, "pr_head_sha", the_head)
    monkeypatch.setattr(bot_review.time, "sleep", no_sleep)
    since = parse_ts("2019-01-01T00:00:00Z")
    # 0, not a timeout: there is review work, and the wait says where it lives.
    assert bot_review.cmd_wait("o/r", 72, GREPTILE, since, 0) == 0
    assert "1 that exist only in the summary text" in capsys.readouterr().out
    # fetch still refuses to call it clean, naming the real cause — and prints
    # the literal reference, id and all, because this refusal returns before
    # anything else names the summary: it is the one moment the workflow tells
    # a human to write an acknowledgement, and withholding the string there
    # forces the compose-from-memory the reference contract forbids.
    assert bot_review.cmd_fetch("o/r", 72, GREPTILE, since) == 1
    err = capsys.readouterr().err
    assert "exist only in the summary text" in err
    assert "Acknowledges greptile summary 5085014060" in err


def test_a_freshly_posted_undercount_is_still_waited_out(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The counter-case the grace window protects: a summary posted *just now*
    # whose comments have not landed is the real 4-second race, and ending the
    # wait there hands fetch a review whose findings do not exist yet.
    import bot_review

    now = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    fresh = _comment(
        1,
        "greptile-apps[bot]",
        now,
        now,
        body=_greptile_summary(ATTENTION_FINDINGS, fix_prompt=True),
    )
    state = bot_review.SummaryState(
        comment=fresh,
        bot="greptile",
        clean=False,
        stale=False,
        reviewed=GREPTILE_SHA,
        head=GREPTILE_SHA,
        stated=1,
        findings=[],
        open_findings=[],
    )
    assert state.undercounted is True
    assert state.comments_pending is True


def _undercounted_state(age: timedelta) -> Any:
    """A one-stated / zero-matched summary aged `age` behind now."""
    import bot_review

    stamp = (datetime.now(UTC) - age).strftime("%Y-%m-%dT%H:%M:%SZ")
    return bot_review.SummaryState(
        comment=_comment(1, "greptile-apps[bot]", stamp, stamp, body="x"),
        bot="greptile",
        clean=False,
        stale=False,
        reviewed=GREPTILE_SHA,
        head=GREPTILE_SHA,
        stated=1,
        findings=[],
        open_findings=[],
    )


def test_the_grace_window_flips_the_reading_either_side_of_it() -> None:
    # The window is the whole mechanism, so pin both sides of it rather than
    # only the far extremes. `undercounted` is the raw gap and holds either
    # way; only `comments_pending` — the gap *plus* the window — flips.
    #
    # These are just-inside and just-outside, NOT the exact boundary, and the
    # exact boundary is not testable through this fixture: the stamp is built
    # from one `datetime.now(UTC)` and `comments_pending` evaluates against a
    # later one, so an "exactly COMMENT_GRACE" fixture is already past the
    # window by the elapsed delta (~0.4s when measured). Both `<` and `<=`
    # answer False there, so such a test would assert exactly what this one
    # does while claiming to be sharper. Pinning `<` against `<=` would mean
    # injecting the clock into `comments_pending`, which is a production API
    # change for a distinction worth nothing at a two-minute window sized 30x
    # over the observed 4-second race.
    import bot_review

    just_inside = _undercounted_state(bot_review.COMMENT_GRACE - timedelta(seconds=5))
    just_outside = _undercounted_state(bot_review.COMMENT_GRACE + timedelta(seconds=1))
    assert just_inside.undercounted is True
    assert just_outside.undercounted is True
    assert just_inside.comments_pending is True
    assert just_outside.comments_pending is False


def test_a_race_outlasting_the_grace_degrades_safely_never_to_clean(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # The window is sized from two ~4-second observations with a wide margin,
    # but a genuine race could in principle outlast it (rate limiting, an
    # outage). Then the reading is wrong — "structural" for something still
    # resolving. Pin the direction that failure takes: never EXIT_CLEAN. A
    # premature refusal costs a re-run; a premature "clean" buries a finding.
    body = _greptile_summary(ATTENTION_FINDINGS, fix_prompt=True)
    finding = _pull_comment(1, "greptile-apps[bot]", "2026-07-26T01:00:00Z")
    for comments in ([], [finding]):
        assert _greptile_fetch(monkeypatch, body, comments) != EXIT_CLEAN
        capsys.readouterr()


def test_a_pending_comment_is_waited_out_not_failed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The other side of that split: a stated count the comments have not caught
    # up with IS resolvable by waiting, so it must keep polling rather than
    # inherit the conflict's fail-fast. Collapsing the two would undo the
    # summary-before-comments fix.
    body = _greptile_summary(ATTENTION_FINDINGS, fix_prompt=True)
    assert _greptile_wait(monkeypatch, body, comments=[], fresh=True) == 1  # timeout
    assert _greptile_fetch(monkeypatch, body, [], fresh=True) == 1


def test_cmd_fetch_refuses_to_classify_when_the_two_signals_disagree(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # Prose says clean, the fix-prompt block says one finding. Both are
    # drift-prone in different ways — the prose is model-written, the count
    # comes from a configurable block — so neither gets to win by default.
    body = _greptile_summary(ATTENTION_CLEAN, fix_prompt=True)
    assert _greptile_fetch(monkeypatch, body, []) == 1
    assert "the two signals disagree" in capsys.readouterr().err


def test_a_stale_review_is_reported_but_still_triaged(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # Staleness is the ordinary end state of a triaged PR — the fixes a review
    # provokes are commits made after it — so it annotates the output rather
    # than suppressing it. The findings themselves are still real.
    body = _greptile_summary(ATTENTION_FINDINGS, sha=OTHER_SHA, fix_prompt=True)
    finding = _pull_comment(3650858888, "greptile-apps[bot]", "2026-07-25T21:09:10Z")
    assert _greptile_fetch(monkeypatch, body, [finding], head=GREPTILE_SHA) == 0
    out = capsys.readouterr().out
    assert f"looked at {OTHER_SHA[:7]}, not the current head {GREPTILE_SHA[:7]}" in out
    assert "id=3650858888" in out


def test_a_clean_run_does_not_warn_that_its_count_cross_check_was_skipped(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # A clean summary states no count by design and zero findings were fetched,
    # so the two agree and there is nothing to warn about. Emitting the
    # "cross-check skipped" NOTE on every clean run is how a NOTE becomes
    # something people learn to scroll past.
    assert _greptile_fetch(monkeypatch, _greptile_summary(ATTENTION_CLEAN), []) == (
        EXIT_CLEAN
    )
    assert "cross-check skipped" not in capsys.readouterr().out


def test_a_stale_review_whose_findings_are_answered_blocks_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The state every triaged PR reaches by merge time: reviewed at commit A,
    # head is now B because the fixes landed, and every finding has a reply.
    # A gate keyed on freshness would fire here — on 3 of the 4 live PRs — while
    # still saying nothing about the finding nobody answered.
    body = _greptile_summary(ATTENTION_FINDINGS, sha=OTHER_SHA, fix_prompt=True)
    finding = _pull_comment(3650858888, "greptile-apps[bot]", "2026-07-25T21:09:10Z")
    reply = _pull_comment(
        3650871889, "NeuroticGamer99", "2026-07-25T21:29:53Z", in_reply_to_id=3650858888
    )
    assert _greptile_fetch(monkeypatch, body, [finding, reply], head=GREPTILE_SHA) == (
        EXIT_CLEAN
    )


def test_cmd_fetch_flags_a_summary_whose_freshness_it_could_not_verify(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    body = _greptile_summary(ATTENTION_FINDINGS, fix_prompt=True).replace(
        "Last reviewed commit:", "Ref:"
    )
    finding = _pull_comment(3650858888, "greptile-apps[bot]", "2026-07-25T21:09:10Z")
    assert _greptile_fetch(monkeypatch, body, [finding]) == 0
    assert "freshness could not be verified" in capsys.readouterr().out


def test_the_fix_prompt_heading_is_the_only_count_greptile_states() -> None:
    # Its review body is empty and the summary carries no "N findings" line, so
    # the cross-check reads the heading of the fix-prompt block — present only
    # when there are findings (PR #67).
    findings = _greptile_summary(ATTENTION_FINDINGS, fix_prompt=True)
    assert stated_count(findings, GREPTILE) == 1
    # On a clean run there is no such block. An absent count is None, which
    # count_note reports as "cross-check skipped" rather than asserting zero.
    assert stated_count(_greptile_summary(ATTENTION_CLEAN), GREPTILE) is None


def test_only_greptile_uses_the_summary_comment_path() -> None:
    # The other three keep the reviews-endpoint model exactly as it was; a stray
    # summary_marker would silently reroute their detection.
    for spec in (CODERABBIT, COPILOT, GEMINI):
        assert spec.summary_marker is None
        assert spec.reviewed_commit is None


def test_a_summary_marker_without_a_clean_marker_is_refused() -> None:
    import bot_review

    with pytest.raises(ValueError, match="summary_marker requires clean_marker"):
        bot_review.BotSpec(
            key="broken",
            review_login="x[bot]",
            request_login=None,
            requested_display=None,
            trigger_body="@x review",
            count=re.compile(r"(\d+)"),
            clean_marker=None,
            summary_marker=re.compile(r"marker"),
        )


def test_a_reviewed_commit_without_a_summary_marker_is_refused() -> None:
    import bot_review

    with pytest.raises(ValueError, match="reviewed_commit requires summary_marker"):
        bot_review.BotSpec(
            key="broken",
            review_login="x[bot]",
            request_login=None,
            requested_display=None,
            trigger_body="@x review",
            count=re.compile(r"(\d+)"),
            clean_marker=re.compile(r"clean"),
            reviewed_commit=re.compile(r"/commit/([0-9a-f]{7,40})"),
        )


def test_summary_state_reports_none_before_the_bot_has_answered(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import bot_review

    def no_comments(repo: str, pr: int) -> list[dict[str, Any]]:
        return []

    def the_head(repo: str, pr: int) -> str:
        return GREPTILE_SHA

    monkeypatch.setattr(bot_review, "issue_comments", no_comments)
    monkeypatch.setattr(bot_review, "pull_comments", no_comments)
    monkeypatch.setattr(bot_review, "pr_head_sha", the_head)
    since = parse_ts("2026-07-25T19:00:00Z")
    assert summary_state("o/r", 69, GREPTILE, since) is None


def test_a_pull_request_payload_with_no_head_sha_fails_loudly(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The staleness guard is only as good as the head it compares against.
    # Returning "" instead of raising would be worse than useless: same_commit
    # refuses a comparison narrower than seven characters, so an empty head
    # makes *every* review — however fresh — read as stale, and the caller is
    # sent to re-trigger a review that already ran.
    import bot_review

    def headless(path: str, *args: str) -> Any:
        return {"number": 71, "head": {}}

    monkeypatch.setattr(bot_review, "gh", headless)
    with pytest.raises(BotReviewError, match="could not read the head SHA"):
        bot_review.pr_head_sha("o/r", 71)
    # The consequence the raise prevents, stated as the assertion it rests on.
    assert same_commit(GREPTILE_SHA, "") is False


def test_copilots_findings_are_matched_by_its_comment_login_not_its_review_login() -> (
    None
):
    # The silent-zero this field exists to stop (PR #71). Copilot authors its
    # *reviews* as copilot-pull-request-reviewer[bot] and the comments inside
    # them as Copilot. Matching on review_login filters out every one of its
    # findings, and the sweep then reports "0 findings, 0 unanswered" — a merge
    # gate that passes because it failed to look.
    assert COPILOT.comment_login == "Copilot"
    assert COPILOT.commenter == "Copilot"
    assert COPILOT.commenter != COPILOT.review_login
    finding = _pull_comment(3651510728, "Copilot", "2026-07-26T02:04:53Z")
    assert is_finding_comment(finding, COPILOT) is True
    # ...and the review author, which is a different account, is not a finding.
    other = _pull_comment(
        1, "copilot-pull-request-reviewer[bot]", "2026-07-26T02:04:53Z"
    )
    assert is_finding_comment(other, COPILOT) is False


def test_the_other_bots_comment_and_review_logins_coincide() -> None:
    # Only Copilot splits them; declaring the field for everyone would invite a
    # copy-paste that quietly redefines whose comments count.
    for spec in (CODERABBIT, GEMINI, GREPTILE):
        assert spec.comment_login is None
        assert spec.commenter == spec.review_login


def test_a_bot_replying_to_itself_does_not_answer_its_own_finding() -> None:
    # CodeRabbit acks threads routinely and once withdrew its own finding that
    # way (PR #65). Counting that as triage would let a bot clear the merge
    # gate on its own say-so — the unread review the gate exists to stop.
    finding = _pull_comment(1, "coderabbitai[bot]", "2026-07-26T01:00:00Z")
    self_ack = _pull_comment(
        2, "coderabbitai[bot]", "2026-07-26T01:05:00Z", in_reply_to_id=1
    )
    assert unanswered([finding], [finding, self_ack]) == [finding]
    # A reply from anyone else is triage.
    human = _pull_comment(
        3, "NeuroticGamer99", "2026-07-26T01:06:00Z", in_reply_to_id=1
    )
    assert unanswered([finding], [finding, self_ack, human]) == []


def test_a_threaded_reply_marks_its_parent_answered() -> None:
    # Transcribed from PR #69: the owner's reply 3650871889 points at
    # Greptile's finding 3650858888, which is the whole record GitHub keeps of
    # a finding having been triaged.
    finding = _pull_comment(3650858888, "greptile-apps[bot]", "2026-07-25T19:21:20Z")
    reply = _pull_comment(
        3650871889, "NeuroticGamer99", "2026-07-25T19:29:53Z", in_reply_to_id=3650858888
    )
    assert answered_ids([finding, reply]) == {3650858888}
    assert unanswered([finding], [finding, reply]) == []
    # Without the reply the same finding is outstanding.
    assert unanswered([finding], [finding]) == [finding]


def test_a_reply_to_another_bots_finding_does_not_answer_ours() -> None:
    # PR #69 carried CodeRabbit findings and their replies in the same payload;
    # keying on the parent id rather than on mere presence is what keeps one
    # bot's triage from clearing another's.
    ours = _pull_comment(3650858888, "greptile-apps[bot]", "2026-07-25T19:21:20Z")
    theirs = _pull_comment(3651079329, "coderabbitai[bot]", "2026-07-25T21:27:05Z")
    their_reply = _pull_comment(
        3651082022, "NeuroticGamer99", "2026-07-25T21:29:01Z", in_reply_to_id=3651079329
    )
    assert unanswered([ours], [ours, theirs, their_reply]) == [ours]


# --------------------------------------------------------------------------
# `outstanding`: the merge gate, asked of every bot at once. Its whole value is
# failing CLOSED — a gate that answers "nothing outstanding" because it looked
# in the wrong place is worse than no gate, so these test the blocking
# direction as hard as the passing one.
# --------------------------------------------------------------------------


def _outstanding(
    monkeypatch: pytest.MonkeyPatch,
    comments: list[dict[str, Any]],
    reviews: list[dict[str, Any]] | None = None,
    issues: list[dict[str, Any]] | None = None,
) -> int:
    """Drive cmd_outstanding.

    ``issues`` defaults to none posted. It used to default to a clean Greptile
    summary, because Greptile declared ``always_reviews`` and its absence was an
    alarm in its own right — a fixture saying nothing about it would have failed
    every test for a reason the test was not about. `skipReview: "AUTOMATIC"`
    ended that: no live bot is obliged to have posted anything.
    """
    import bot_review

    posted_reviews = reviews or []
    posted_issues = [] if issues is None else issues

    def the_comments(repo: str, pr: int) -> list[dict[str, Any]]:
        return comments

    def the_reviews(repo: str, pr: int) -> list[dict[str, Any]]:
        return posted_reviews

    def the_issues(repo: str, pr: int) -> list[dict[str, Any]]:
        return posted_issues

    monkeypatch.setattr(bot_review, "pull_comments", the_comments)
    monkeypatch.setattr(bot_review, "list_reviews", the_reviews)
    monkeypatch.setattr(bot_review, "issue_comments", the_issues)
    since = parse_ts("2026-07-26T00:00:00Z")
    return bot_review.cmd_outstanding("o/r", 71, since)


def test_outstanding_clears_when_every_finding_has_a_reply(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # PR #71's real shape: three CodeRabbit findings and one Copilot finding,
    # each answered by the owner.
    comments: list[dict[str, Any]] = []
    for n, login in (
        (1, "coderabbitai[bot]"),
        (2, "coderabbitai[bot]"),
        (3, "Copilot"),
    ):
        comments.append(_pull_comment(n, login, "2026-07-26T01:00:00Z"))
        comments.append(
            _pull_comment(
                100 + n, "NeuroticGamer99", "2026-07-26T01:30:00Z", in_reply_to_id=n
            )
        )
    assert _outstanding(monkeypatch, comments) == EXIT_CLEAN
    assert "NOTHING OUTSTANDING" in capsys.readouterr().out


def test_outstanding_findings_buckets_by_bot_as_a_value() -> None:
    # Asserted on the returned structure rather than on rendered text: which
    # bot owns which unanswered finding is the gate's actual answer, and a
    # substring check over the printout would pass on a cross-bot mis-bucketing
    # that happened to print the same words somewhere else.
    since = parse_ts("2026-07-26T00:00:00Z")
    cr = _pull_comment(1, "coderabbitai[bot]", "2026-07-26T01:00:00Z")
    cr_reply = _pull_comment(
        2, "NeuroticGamer99", "2026-07-26T01:30:00Z", in_reply_to_id=1
    )
    cop = _pull_comment(3, "Copilot", "2026-07-26T01:00:00Z")
    report = {b.key: b for b in outstanding_findings([cr, cr_reply, cop], since)}
    assert sorted(report) == ["coderabbit", "copilot", "gemini", "greptile"]
    assert [c["id"] for c in report["coderabbit"].findings] == [1]
    assert report["coderabbit"].open_findings == []
    assert [c["id"] for c in report["copilot"].open_findings] == [3]
    assert report["gemini"].findings == []
    assert report["greptile"].findings == []


def test_a_finding_posted_in_the_same_second_as_the_floor_is_excluded() -> None:
    # The floor is strict (`> since`), and both GitHub stamps and the floor
    # cmd_request mints are second-granular — so a finding landing in the same
    # second is dropped. Pinned here because the gate's value is not missing
    # things: if this ever needs to become `>=`, this test is the place that
    # says so out loud rather than a silent behaviour change.
    since = parse_ts("2026-07-26T01:00:00Z")
    same = _pull_comment(1, "coderabbitai[bot]", "2026-07-26T01:00:00Z")
    later = _pull_comment(2, "coderabbitai[bot]", "2026-07-26T01:00:01Z")
    assert select_finding_comments([same, later], CODERABBIT, since) == [later]


def test_gemini_posts_its_comments_as_the_review_author() -> None:
    # Unlike Copilot's split, this one is grounded in the code path rather than
    # a live payload: no Gemini inline comment exists on this repo to check
    # against. .github/scripts/gemini_review_agent.py posts a review with
    # nested comments through `gh api .../pulls/N/reviews` under
    # `github.token`, so both inherit github-actions[bot]. Recorded as a
    # code-basis assumption, not an observation — if a live Gemini finding ever
    # lands, verify it here.
    assert GEMINI.comment_login is None
    assert GEMINI.commenter == "github-actions[bot]"
    finding = _pull_comment(1, "github-actions[bot]", "2026-07-26T01:00:00Z")
    assert is_finding_comment(finding, GEMINI) is True


def test_outstanding_says_no_findings_rather_than_nothing_outstanding(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # An unreviewed PR and a cleanly-reviewed one are indistinguishable from
    # posted comments alone. Both exit 2 — a PR whose chains were deliberately
    # not spent must not be blocked — but the wording must not let silence read
    # as a verdict, which is the distinction _fetch_summary_bot already makes.
    assert _outstanding(monkeypatch, []) == EXIT_CLEAN
    out = capsys.readouterr().out
    assert "NO FINDINGS POSTED" in out
    assert "NOTHING OUTSTANDING" not in out
    assert "not evidence any bot reviewed it" in out


def test_outstanding_blocks_on_an_unanswered_copilot_finding(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # The case the old gate could not see at all: Copilot's finding is authored
    # `Copilot`, so a sweep keyed on its review login reports zero and merges.
    finding = _pull_comment(3651510728, "Copilot", "2026-07-26T02:04:53Z")
    assert _outstanding(monkeypatch, [finding]) == 0
    out = capsys.readouterr().out
    assert "1 UNANSWERED" in out
    assert "[copilot]" in out
    assert "id=3651510728" in out


def test_outstanding_blocks_when_only_the_bot_answered_itself(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    finding = _pull_comment(1, "coderabbitai[bot]", "2026-07-26T01:00:00Z")
    ack = _pull_comment(
        2, "coderabbitai[bot]", "2026-07-26T01:05:00Z", in_reply_to_id=1
    )
    assert _outstanding(monkeypatch, [finding, ack]) == 0
    assert "1 UNANSWERED" in capsys.readouterr().out


def test_outstanding_counts_every_bot_not_just_the_one_that_reviewed(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    unread = _pull_comment(9, "greptile-apps[bot]", "2026-07-26T01:00:00Z")
    answered = _pull_comment(1, "coderabbitai[bot]", "2026-07-26T01:00:00Z")
    reply = _pull_comment(
        2, "NeuroticGamer99", "2026-07-26T01:30:00Z", in_reply_to_id=1
    )
    assert _outstanding(monkeypatch, [unread, answered, reply]) == 0
    out = capsys.readouterr().out
    # Each bot is reported, so a zero is visibly a zero rather than a silence.
    for key in ("coderabbit", "copilot", "gemini", "greptile"):
        assert key in out
    assert "[greptile]" in out


def test_outstanding_ignores_findings_below_the_floor(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stale = _pull_comment(1, "coderabbitai[bot]", "2025-01-01T00:00:00Z")
    assert _outstanding(monkeypatch, [stale]) == EXIT_CLEAN


def test_outstanding_is_the_only_command_that_needs_no_bot(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    import bot_review

    def nothing(repo: str, pr: int) -> list[dict[str, Any]]:
        return []

    def the_repo() -> str:
        return "o/r"

    monkeypatch.setattr(bot_review, "pull_comments", nothing)
    monkeypatch.setattr(bot_review, "list_reviews", nothing)
    monkeypatch.setattr(bot_review, "issue_comments", nothing)
    monkeypatch.setattr(bot_review, "default_repo", the_repo)
    argv = ["outstanding", "--pr", "71", "--since", "2026-07-26T00:00:00Z"]
    assert bot_review.main(argv) == EXIT_CLEAN
    # The others still must name one, and the refusal says why.
    assert (
        bot_review.main(["fetch", "--pr", "71", "--since", "2026-07-26T00:00:00Z"]) == 1
    )
    assert "needs --bot" in capsys.readouterr().err


def _review_by(login: str, submitted: str, body: str = "") -> dict[str, Any]:
    return {
        "id": 4780620978,
        "user": {"login": login},
        "submitted_at": submitted,
        "body": body,
    }


def test_a_review_whose_comments_matched_none_refuses_to_clear_the_gate(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # The identity-drift alarm. A bot posted a review carrying findings, but the
    # sweep matched zero of its comments — which means the author filter missed,
    # not that the bot found nothing. This is the Copilot failure generalized:
    # the next login rename would otherwise reproduce it silently.
    review = _review_by(
        "coderabbitai[bot]", "2026-07-26T01:00:00Z", "**Actionable comments posted: 2**"
    )
    assert _outstanding(monkeypatch, [], reviews=[review]) == 1
    err = capsys.readouterr().err
    assert "CANNOT CLEAR THE GATE" in err
    assert "0 of its comments matched" in err
    assert "findings this sweep could not read" in err


def test_a_review_stating_zero_findings_is_not_an_alarm(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Copilot's clean run is a review that says it generated no comments, and
    # zero matched comments is then the correct answer, not a missed filter.
    review = _review_by(
        "copilot-pull-request-reviewer[bot]",
        "2026-07-26T01:00:00Z",
        "Copilot reviewed 3 files and generated no comments.",
    )
    assert _outstanding(monkeypatch, [], reviews=[review]) == EXIT_CLEAN


def test_a_countless_review_carrying_no_comment_objects_is_not_an_alarm(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Copilot answered PR #75 with a "Pull request overview" body carrying none
    # of the three `generated ...` shapes issue #61 verified, so stated_count
    # returns None and the text cannot prove the zero. The comment objects can:
    # the review carries none by any author, so no filter could have dropped
    # one. Without this, every clean Copilot review of this shape blocks a merge
    # while reporting a diagnosis -- "the filter is probably wrong" -- that is
    # provably false, because there was nothing to filter.
    review = _review_by(
        "copilot-pull-request-reviewer[bot]",
        "2026-07-26T01:00:00Z",
        "## Pull request overview\n\nAdds an AST gate.\n\n**Changes:**\n- one",
    )
    assert _outstanding(monkeypatch, [], reviews=[review]) == EXIT_CLEAN


def test_a_countless_review_whose_comment_went_unmatched_is_still_an_alarm(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # The identity drift this detector exists for, in the one shape the
    # exclusion above must not swallow: the review states no count AND carries a
    # comment object, but under a login the filter does not match. Here there
    # really was something to miss, so the zero stays unproven and the merge
    # stays blocked. A rename of Copilot's comment login lands exactly here.
    review = _review_by(
        "copilot-pull-request-reviewer[bot]",
        "2026-07-26T01:00:00Z",
        "## Pull request overview\n\nAdds an AST gate.",
    )
    stray = _pull_comment(
        7,
        "copilot-renamed[bot]",
        "2026-07-26T01:05:00Z",
        review_id=int(review["id"]),
    )
    assert _outstanding(monkeypatch, [stray], reviews=[review]) == 1
    assert "0 of its comments matched" in capsys.readouterr().err


def test_a_string_valued_review_id_still_ties_a_comment_to_its_review(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Ids are normalized through int(str(...)) everywhere in this module; this
    # pins why it matters at the one site where the comparison decides whether a
    # merge clears. Unnormalized, a string id on one side and an int on the
    # other makes the membership test false -- so the exclusion applies and the
    # gate opens. Every other failure in this module is closed; this one would
    # be open, which is why it gets a test rather than a convention.
    review = _review_by(
        "copilot-pull-request-reviewer[bot]",
        "2026-07-26T01:00:00Z",
        "## Pull request overview\n\nAdds an AST gate.",
    )
    stray = _pull_comment(7, "copilot-renamed[bot]", "2026-07-26T01:05:00Z")
    stray["pull_request_review_id"] = str(review["id"])
    assert _outstanding(monkeypatch, [stray], reviews=[review]) == 1


def test_a_review_claiming_comments_it_never_posted_is_still_an_alarm(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # The exclusion is scoped to a review that states NO count. One claiming a
    # positive count while carrying no comment objects is the detector working:
    # the comments it names should exist and do not. Keying the exclusion on
    # "no comments" alone would turn this into a silent pass.
    review = _review_by(
        "copilot-pull-request-reviewer[bot]",
        "2026-07-26T01:00:00Z",
        "Copilot reviewed 3 files and generated 2 comments.",
    )
    assert _outstanding(monkeypatch, [], reviews=[review]) == 1
    assert "0 of its comments matched" in capsys.readouterr().err


def test_a_consolidated_multi_finding_comment_is_not_an_alarm(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # CodeRabbit has posted three findings inside ONE comment object (PR #67),
    # so "states 3, matched 1" is a known-benign shape. Keying the alarm on a
    # count mismatch instead of on zero-matched would block merges routinely.
    review = _review_by(
        "coderabbitai[bot]", "2026-07-26T01:00:00Z", "**Actionable comments posted: 3**"
    )
    one = _pull_comment(1, "coderabbitai[bot]", "2026-07-26T01:00:00Z")
    reply = _pull_comment(
        2, "NeuroticGamer99", "2026-07-26T01:30:00Z", in_reply_to_id=1
    )
    assert _outstanding(monkeypatch, [one, reply], reviews=[review]) == EXIT_CLEAN


def test_a_bot_that_reviews_unasked_and_left_nothing_cannot_clear_the_gate(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # A bot that reviews every PR unasked has no legitimate silence. A
    # comments-only sweep counts that silence as zero findings and reads green —
    # making the one bot whose absence is always anomalous the one whose absence
    # looks cleanest.
    #
    # Driven through a SYNTHETIC spec because no live bot declares
    # `always_reviews` any more — `BotSpec.always_reviews` says why it is kept
    # anyway. What belongs here is the consequence: a kept detector with no live
    # subject is exactly the shape that passes by having nothing to check.
    import bot_review

    unasked = replace(GREPTILE, always_reviews=True)
    monkeypatch.setattr(bot_review, "BOTS", {**BOTS, GREPTILE.key: unasked})
    assert _outstanding(monkeypatch, [], issues=[]) == 1
    err = capsys.readouterr().err
    assert "reviews every PR unasked" in err
    assert "Silence here is never a clean verdict" in err


def test_no_live_bot_reviews_unasked_so_silence_alone_blocks_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The converse, and the live posture: every chain here is spent
    # deliberately, so no artifact means the chain was not spent — a legitimate
    # state. Pinned across ALL specs rather than the three that were never
    # automatic, because the day one is set back to True this sweep starts
    # blocking merges and that must be a decision, not a surprise.
    for spec in BOTS.values():
        assert spec.always_reviews is False
    assert _outstanding(monkeypatch, [], issues=[]) == EXIT_CLEAN


def test_copilot_cannot_answer_its_own_finding_under_either_login() -> None:
    # It holds two identities; a single-login exclusion would let an ack under
    # the other one count as third-party triage. Unobserved in practice — it has
    # never been seen to ack — so this errs toward leaving a finding open, which
    # is the safe direction for a merge gate.
    assert COPILOT.logins == ("copilot-pull-request-reviewer[bot]", "Copilot")
    finding = _pull_comment(1, "Copilot", "2026-07-26T01:00:00Z")
    for ack_login in COPILOT.logins:
        ack = _pull_comment(2, ack_login, "2026-07-26T01:05:00Z", in_reply_to_id=1)
        assert unanswered([finding], [finding, ack]) == [finding]
    human = _pull_comment(
        3, "NeuroticGamer99", "2026-07-26T01:06:00Z", in_reply_to_id=1
    )
    assert unanswered([finding], [finding, human]) == []


def test_single_identity_bots_report_one_login() -> None:
    assert CODERABBIT.logins == ("coderabbitai[bot]",)
    assert GREPTILE.logins == ("greptile-apps[bot]",)


def test_a_usage_error_does_not_exit_with_the_clean_code() -> None:
    # argparse exits 2 on any usage error and EXIT_CLEAN is 2, so a gate
    # invocation that never ran — a typo'd --bot, an unquoted --since split into
    # two argv entries, a missing --pr — would hand /squash-merge the same status
    # as "every finding has a reply". Confirmed live against all three shapes.
    import bot_review

    for argv in (
        ["outstanding", "--pr"],
        ["fetch", "--bot", "nosuchbot", "--pr", "71"],
        ["outstanding", "--pr", "71", "--since", "2026-07-26", "00:00:00Z"],
    ):
        with pytest.raises(SystemExit) as exc:
            bot_review.build_parser().parse_args(argv)
        assert exc.value.code == 1
        assert exc.value.code != EXIT_CLEAN


def test_a_bots_empty_bodied_ack_review_is_not_an_unmatched_report(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # GitHub models a bot replying to a thread as a review with an empty body,
    # and CodeRabbit acks routinely (PR #27, three of them; PR #65, a
    # self-withdrawal). Treating that as "a review whose comments I could not
    # match" makes the gate cry wolf on the ordinary end state of a triaged PR.
    ack = _review_by("coderabbitai[bot]", "2026-07-26T01:00:00Z", body="")
    assert _outstanding(monkeypatch, [], reviews=[ack]) == EXIT_CLEAN


def test_greptiles_empty_bodied_findings_review_still_raises_the_alarm(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # The counter-case that makes the ack rule non-trivial: Greptile's findings
    # review is empty-bodied *by design*, so suppressing every empty body would
    # disable this check for the one bot whose findings the reviews endpoint
    # cannot otherwise reveal. `summary_marker` is the discriminator.
    assert GREPTILE.summary_marker is not None
    assert CODERABBIT.summary_marker is None
    review = {
        "id": 4780005491,
        "user": {"login": "greptile-apps[bot]"},
        "submitted_at": "2026-07-26T01:00:00Z",
        "body": "",
    }
    assert _outstanding(monkeypatch, [], reviews=[review]) == 1
    assert "0 of its comments matched" in capsys.readouterr().err


def test_a_pending_review_is_not_a_report() -> None:
    # GitHub reports an unsubmitted (PENDING) review with a null stamp. It is
    # not a report, so reviews_by skips it — deliberately, not incidentally.
    since = parse_ts("2026-07-26T00:00:00Z")
    pending = {
        "id": 555,
        "user": {"login": "coderabbitai[bot]"},
        "submitted_at": None,
        "body": "**Actionable comments posted: 2**",
    }
    submitted = _review_by(
        "coderabbitai[bot]", "2026-07-26T01:00:00Z", "**Actionable comments posted: 2**"
    )
    assert reviews_by([pending], CODERABBIT, since) == []
    assert reviews_by([pending, submitted], CODERABBIT, since) == [submitted]


GEMINI_BODY_ONLY = "\n\n".join(
    [
        "## Antigravity Gemini review",
        "Antigravity reviewed this PR's filtered diff and posted 0 inline finding(s).",
        "3 finding(s) could not be anchored to a diff line and appear here instead:",
        "- `src/a.py:1` — first\n- `src/b.py:2` — second\n- `src/c.py:3` — third",
    ]
)

GEMINI_TRULY_CLEAN = "\n\n".join(
    [
        "## Antigravity Gemini review",
        "Antigravity reviewed this PR's filtered diff and posted 0 inline finding(s).",
        "No findings — clean per the styleguide lenses.",
    ]
)


def test_body_only_findings_refuse_to_clear_the_gate(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # The worst shape the sweep can meet. gemini_review_agent's HTTP-422
    # fallback re-posts every finding into the review BODY with the inline count
    # restated as 0, so the count says "nothing" while the body lists three.
    # They are not comments, so they can never carry a reply and the unanswered
    # sweep can never see them — the gate must say so rather than pass.
    review = _review_by("github-actions[bot]", "2026-07-26T01:00:00Z", GEMINI_BODY_ONLY)
    assert _outstanding(monkeypatch, [], reviews=[review]) == 1
    err = capsys.readouterr().err
    assert "renders 3 finding(s) in its body" in err
    # The refusal prints the exact acknowledgement to post (ADR-0067), so the
    # one exit it has is never composed from memory.
    assert "Acknowledges gemini review 4780620978" in err


def test_body_findings_are_caught_even_when_some_comments_matched(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # The mixed shape, and the ORDINARY one: gemini_review_agent posts anchored
    # findings as real comments and unanchored ones as body bullets in the SAME
    # review. Keying the body check on "matched nothing" — as the first version
    # of this did — misses every mixed review, which is the common case; the
    # all-body-only shape is only the HTTP-422 fallback.
    body = GEMINI_BODY_ONLY.replace("posted 0 inline", "posted 2 inline").replace(
        "3 finding(s) could not", "1 finding(s) could not"
    )
    # Assert the fixture is the mixed shape rather than trusting the test name:
    # a nonzero inline count *and* a body-only remainder in the same review.
    assert stated_count(body, GEMINI) == 2
    assert GEMINI.body_findings is not None
    assert GEMINI.body_findings.search(body) is not None
    review = _review_by("github-actions[bot]", "2026-07-26T01:00:00Z", body)
    anchored = [
        _pull_comment(n, "github-actions[bot]", "2026-07-26T01:00:00Z") for n in (1, 2)
    ]
    replies = [
        _pull_comment(
            10 + n, "NeuroticGamer99", "2026-07-26T01:30:00Z", in_reply_to_id=n
        )
        for n in (1, 2)
    ]
    # Both anchored findings are answered, so the unanswered sweep is satisfied
    # — the third finding is only visible in the body.
    assert _outstanding(monkeypatch, anchored + replies, reviews=[review]) == 1
    assert "renders 1 finding(s) in its body" in capsys.readouterr().err


def test_a_summary_stating_exactly_what_was_matched_does_not_block(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The innocent case for undercounted_summaries: stated == matched must clear,
    # or the check would block every Greptile findings run rather than the
    # undercounted ones.
    summary = _comment(
        5081386528,
        "greptile-apps[bot]",
        "2026-07-26T01:00:00Z",
        "2026-07-26T01:00:00Z",
        body=_greptile_summary(ATTENTION_FINDINGS, fix_prompt=True),
    )
    finding = _pull_comment(1, "greptile-apps[bot]", "2026-07-26T01:00:00Z")
    reply = _pull_comment(
        2, "NeuroticGamer99", "2026-07-26T01:30:00Z", in_reply_to_id=1
    )
    assert _outstanding(monkeypatch, [finding, reply], issues=[summary]) == EXIT_CLEAN


def test_a_review_submitted_before_the_floor_is_not_this_runs_report() -> None:
    since = parse_ts("2026-07-26T00:00:00Z")
    stale = _review_by("coderabbitai[bot]", "2025-01-01T00:00:00Z", "x")
    assert reviews_by([stale], CODERABBIT, since) == []


def test_a_genuinely_clean_gemini_review_still_clears(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The counter-case: 0 inline with no unanchored block is a real clean run,
    # and alarming on it would make every Gemini review block a merge.
    review = _review_by(
        "github-actions[bot]", "2026-07-26T01:00:00Z", GEMINI_TRULY_CLEAN
    )
    assert _outstanding(monkeypatch, [], reviews=[review]) == EXIT_CLEAN


def test_the_body_findings_marker_matches_what_the_agent_writes() -> None:
    # Cross-module contract, mirrored rather than imported (this module is
    # stdlib-only; gemini_review_logic pulls pydantic). Same convention as the
    # count regex and the empty-range marker.
    from gemini_review_logic import Finding, review_body

    rendered = review_body(
        0,
        [
            Finding(
                file="src/a.py",
                line=1,
                severity="major",
                category="correctness",
                description="x",
            ),
            Finding(
                file="src/b.py",
                line=2,
                severity="minor",
                category="style",
                description="y",
            ),
        ],
    )
    assert GEMINI.body_findings is not None
    found = GEMINI.body_findings.search(rendered)
    assert found is not None
    assert found.group(1) == "2"


def test_a_summary_claiming_more_findings_than_were_matched_blocks(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # The check the per-bot `fetch --bot greptile` precondition used to make and
    # the sweep dropped: unmatched_reviews only looks at bots that matched
    # NOTHING, so 3-stated with 1 matched and answered would have cleared the
    # gate with two findings never seen.
    claims_three = _greptile_summary(ATTENTION_FINDINGS, fix_prompt=True).replace(
        "Fix the following 1 code review", "Fix the following 3 code review"
    )
    summary = _comment(
        5081386528,
        "greptile-apps[bot]",
        "2026-07-26T01:00:00Z",
        "2026-07-26T01:00:00Z",
        body=claims_three,
    )
    finding = _pull_comment(1, "greptile-apps[bot]", "2026-07-26T01:00:00Z")
    reply = _pull_comment(
        2, "NeuroticGamer99", "2026-07-26T01:30:00Z", in_reply_to_id=1
    )
    assert _outstanding(monkeypatch, [finding, reply], issues=[summary]) == 1
    err = capsys.readouterr().err
    assert "states 3 finding(s) but the sweep matched 1" in err
    # The refusal prints the exact acknowledgement to post (ADR-0067), so the
    # one exit it has is never composed from memory.
    assert "Acknowledges greptile summary 5081386528" in err


# --------------------------------------------------------------------------
# A summary that is not clean, with no evidence anywhere the sweep can read.
# Measured on PR #84: Greptile stated a finding in its "Files Needing
# Attention" prose, posted no inline comment and no review object, and the one
# countable sentence lived in the "Prompt To Fix All With AI" block — which a
# dashboard toggle, outside this repository, had switched off. So `stated_count`
# was None, the cross-check was skipped, `undercounted_summaries` had no count
# to find an undercount in, `unmatched_reviews` had no review to examine, and
# the gate cleared a PR holding a real unanswered finding.
#
# The rule these pin is structural rather than textual, which is the point: it
# reads only signals no toggle can remove — the clean marker's ABSENCE, zero
# matched comments, and zero review objects.
# --------------------------------------------------------------------------


def _evidence_free_summary(
    attention: str = ATTENTION_FINDINGS, updated_at: str = "2026-07-26T01:00:00Z"
) -> dict[str, Any]:
    """PR #84's shape: a verdict in prose, and nothing countable or repliable.

    Leaving `_greptile_summary`'s `fix_prompt` at its default `False` is what
    makes this the measured body rather than a weaker one: that block holds the
    only sentence Greptile states a number in, and it is precisely what the
    toggle removed, so the summary states no count at all.

    Passing `True` would **break this section**, not merely blur it — measured,
    not reasoned. `unprovable_summaries` fires either way, since its guard is
    only that `bot.findings` is empty and it never reads a count; a stated count
    trips `undercounted_summaries` as well, and that detector does **not**
    consult the clean marker. So the two not-clean tests below would still pass
    on `unprovable_summaries`' own wording, while
    `test_a_clean_summary_with_no_evidence_anywhere_still_clears` would fail
    outright — a stated count it cannot match blocks the gate on a *clean* run,
    flipping that test's exit from EXIT_CLEAN to blocked.

    That test is the load-bearing one here (a clean run posts the summary and
    nothing else, the same evidence-free shape), so the historically faithful
    body is not merely the tidier choice — it is the one this section works at
    all under.
    """
    return _comment(
        5081386528,
        "greptile-apps[bot]",
        "2026-07-26T01:00:00Z",
        updated_at,
        body=_greptile_summary(attention),
    )


def test_a_not_clean_summary_with_no_evidence_anywhere_blocks(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    assert _outstanding(monkeypatch, [], issues=[_evidence_free_summary()]) == 1
    err = capsys.readouterr().err
    assert "does not read clean" in err
    # The refusal prints the exact acknowledgement, like its sibling alarms:
    # this is the moment the workflow asks a human to write one, and a
    # reference composed from memory fails closed with no hint of the typo.
    assert "Acknowledges greptile summary 5081386528" in err


def test_a_clean_summary_with_no_evidence_anywhere_still_clears(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The innocent case, and the one that decides whether this rule is usable:
    # a clean Greptile run posts the summary and NOTHING else — no review, no
    # comments — which is the same evidence-free shape. Only the clean marker
    # separates them, so a rule keyed on "no evidence" rather than on "no
    # evidence AND not clean" would block every clean run on the repo.
    assert (
        _outstanding(monkeypatch, [], issues=[_evidence_free_summary(ATTENTION_CLEAN)])
        == EXIT_CLEAN
    )


def test_a_not_clean_summary_whose_findings_were_matched_and_answered_clears(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The ordinary findings run: not clean, but the findings reached the
    # comments endpoint and carry replies. Nothing here is unprovable, so this
    # rule must stay silent — it fires on missing evidence, not on a verdict.
    #
    # It also pins the freshness comparison's tie, which is why the shared
    # `2026-07-26T01:00:00Z` below is load-bearing rather than incidental: the
    # finding and `_evidence_free_summary`'s default carry the same stamp, so
    # `>=` reads it as the ordinary run and clears while `>` would call it stale
    # and block. GitHub stamps to the second and Greptile posts the summary
    # about four seconds ahead of its comments, so a tie is a rounding artifact,
    # not a signal. Changing either stamp drops that coverage silently — a
    # duplicate test asserting it separately was written and deleted, because a
    # second copy of these exact fixtures proved nothing this one does not.
    finding = _pull_comment(1, "greptile-apps[bot]", "2026-07-26T01:00:00Z")
    reply = _pull_comment(
        2, "NeuroticGamer99", "2026-07-26T01:30:00Z", in_reply_to_id=1
    )
    assert (
        _outstanding(monkeypatch, [finding, reply], issues=[_evidence_free_summary()])
        == EXIT_CLEAN
    )


def test_a_re_edited_summary_is_not_answered_by_the_previous_runs_findings(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # The hole this detector had on arrival, one PR-history step over the shape
    # it was built for. The first run posts an inline finding, which is matched
    # and answered. A re-review then edits the summary IN PLACE with a
    # prose-only, countless finding — posting no new comment and no new review,
    # which is the documented re-review shape. Every detector went silent:
    # this one skipped on `bot.findings`, `unmatched_reviews` skipped on the
    # same condition, and `undercounted_summaries` had no count to compare.
    # Measured before the fix: "NOTHING OUTSTANDING: all 1 finding(s) have a
    # reply", exit EXIT_CLEAN, over an unanswered finding.
    #
    # A reply to the earlier run's comment cannot answer text written after it,
    # which is why the guard is freshness rather than mere existence.
    finding = _pull_comment(
        1, "greptile-apps[bot]", "2026-07-26T01:00:00Z", review_id=4780620978
    )
    reply = _pull_comment(
        2, "NeuroticGamer99", "2026-07-26T01:30:00Z", in_reply_to_id=1
    )
    review = _review_by("greptile-apps[bot]", "2026-07-26T01:00:00Z", body="")
    reviewed_again = _evidence_free_summary(updated_at="2026-07-26T03:00:00Z")
    assert (
        _outstanding(
            monkeypatch, [finding, reply], reviews=[review], issues=[reviewed_again]
        )
        == 1
    )
    err = capsys.readouterr().err
    assert "nothing it posted is newer than that summary" in err
    assert "Acknowledges greptile summary 5081386528" in err
    # The clause must stay true of *this* shape, where a matched comment does
    # exist and is merely older. It once read "no review and no comment this
    # sweep could match" — flatly false here, and contradicted two sentences
    # later by the same message. Nothing pinned it, so the regression could
    # have returned silently on prose whose only job is telling a human the
    # truth about what evidence exists.
    #
    # Both strings appear once in this file, which makes this a weaker pin than
    # the `"does not read clean"` idiom five tests share — there, a wording
    # change reddens five at once and that breadth is itself evidence the phrase
    # is a contract. Here a legitimate rewrite of this one sentence reddens only
    # this test, with nothing to say whether wording or behavior moved. Kept
    # anyway: measured, the positive assertion catches a full revert and the
    # negative catches the false clause returning *alongside* the true one,
    # which the positive alone does not.
    assert "only ones that predate the text now standing" in err
    assert "no review and no comment this sweep could match" not in err


def test_a_review_newer_than_the_summary_is_not_evidence_for_it(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # Only comments count as evidence, and this pins the case that decided it.
    # The freshness rule briefly credited a *review object* newer than the
    # summary too, which looks reasonable and is not: the one shape where that
    # clause acted alone is a re-trigger posting a new review and editing the
    # summary while posting no new comment — and there it cleared the gate over
    # an unanswered summary, resurrecting the bug the freshness rule exists to
    # fix. Everywhere else it merely duplicated the deferral below.
    #
    # A body-less review proves the bot ran. It never proves anything it found
    # can be read, and this detector's whole subject is findings that cannot be.
    finding = _pull_comment(
        1, "greptile-apps[bot]", "2026-07-26T01:00:00Z", review_id=4780620978
    )
    reply = _pull_comment(
        2, "NeuroticGamer99", "2026-07-26T01:30:00Z", in_reply_to_id=1
    )
    first = _review_by("greptile-apps[bot]", "2026-07-26T01:00:00Z", body="")
    # The re-trigger's own review, submitted after the summary it re-wrote.
    retriggered = dict(first, id=901, submitted_at="2026-07-26T03:00:00Z")
    summary = _evidence_free_summary(updated_at="2026-07-26T03:00:00Z")
    assert (
        _outstanding(
            monkeypatch,
            [finding, reply],
            reviews=[first, retriggered],
            issues=[summary],
        )
        == 1
    )
    assert "nothing it posted is newer than that summary" in capsys.readouterr().err


@pytest.mark.parametrize(
    ("login", "submitted"),
    [
        ("NeuroticGamer99", "2026-07-26T01:00:00Z"),
        ("greptile-apps[bot]", "2026-07-25T23:00:00Z"),
    ],
    ids=["another-author", "before-the-floor"],
)
def test_the_deferral_credits_only_this_bots_reviews_since_the_floor(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    login: str,
    submitted: str,
) -> None:
    # The deferral hands this shape to `unmatched_reviews` — but only a review
    # that detector will actually look at counts, which means this bot's own,
    # submitted after the floor. `reviews_by` applies both filters, and dropping
    # it for a bare `reviews` went unnoticed by every other test in this file:
    # a human's review on the PR, or this bot's review from an earlier floor,
    # would then clear a gate `unmatched_reviews` never examines. That is the
    # PR #84 shape once more — a detector going quiet while the run it was
    # asked about stays unaccounted for.
    review = _review_by(login, submitted, body="")
    assert (
        _outstanding(
            monkeypatch, [], reviews=[review], issues=[_evidence_free_summary()]
        )
        == 1
    )
    assert "does not read clean" in capsys.readouterr().err


def test_a_findings_run_whose_summary_precedes_its_comments_clears(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The ordinary run, and the reason the freshness test cannot simply demand
    # that findings postdate the summary by a margin: Greptile posts the summary
    # about four seconds BEFORE the comments it counts (PRs #67 and #69). So on
    # every healthy findings run the evidence is newer, and this must stay
    # silent — a rule keyed the other way would block every reviewed PR.
    summary = _evidence_free_summary(updated_at="2026-07-26T01:00:00Z")
    finding = _pull_comment(1, "greptile-apps[bot]", "2026-07-26T01:00:04Z")
    reply = _pull_comment(
        2, "NeuroticGamer99", "2026-07-26T01:30:00Z", in_reply_to_id=1
    )
    assert _outstanding(monkeypatch, [finding, reply], issues=[summary]) == EXIT_CLEAN


def test_an_acknowledged_evidence_free_summary_clears(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Without this the rule is a permanent block, not a gate: findings that
    # exist only as summary prose have no comment object, so the threaded reply
    # the sweep counts is physically impossible and the ack is the only
    # honest exit (ADR-0067) — the same reasoning that reversed
    # `body_only_findings`.
    ack = _ack(
        "Read the summary; the naming point is answered in 34730887.\n\n"
        "Acknowledges greptile summary 5081386528"
    )
    assert (
        _outstanding(monkeypatch, [], issues=[_evidence_free_summary(), ack])
        == EXIT_CLEAN
    )


def test_an_ack_predating_a_re_edited_summary_does_not_clear(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # A re-review edits the summary in place and posts nothing else, so an ack
    # of the previous text would silently carry over onto findings nobody has
    # read. Same freshness condition `undercounted_summaries` already imposes.
    summary = _evidence_free_summary(updated_at="2026-07-26T03:00:00Z")
    stale_ack = _ack("Acknowledges greptile summary 5081386528")
    assert _outstanding(monkeypatch, [], issues=[summary, stale_ack]) == 1
    assert "does not read clean" in capsys.readouterr().err


def test_a_not_clean_summary_with_a_review_object_is_left_to_its_sibling(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # Complementary by construction: where a review object exists and nothing
    # matched, `unmatched_reviews` already alarms and says the author filter is
    # what to suspect. This rule covers only the case it cannot see — no review
    # at all — so the gate still blocks here, with the sibling's diagnosis
    # rather than a second, vaguer one.
    #
    # The **negative** assertion is what pins the deferral: drop the
    # `reviews_by` guard from `unprovable_summaries` and only that line goes
    # red. The positive one is a liveness check on the sibling — it comes from
    # `unmatched_reviews`, which this change does not touch, so it stays true
    # whether or not the deferral works and would break on a rewording there.
    # Kept anyway, because "the gate still blocks" is only half the claim; the
    # other half is that it blocks with the *better* diagnosis, and nothing but
    # this assertion says which one arrived.
    review = _review_by("greptile-apps[bot]", "2026-07-26T01:00:00Z", body="")
    assert (
        _outstanding(
            monkeypatch, [], reviews=[review], issues=[_evidence_free_summary()]
        )
        == 1
    )
    err = capsys.readouterr().err
    assert "review 4780620978 exists" in err
    assert "does not read clean" not in err


def test_cmd_fetch_refuses_an_evidence_free_summary(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # The same blindness, one command over, and the louder half of it: on PR
    # #84 `fetch` did not merely stay quiet — it printed "NOTHING OUTSTANDING:
    # all 0 finding(s) have a reply" about a summary that stated a finding.
    # A command that reports zero must be able to prove the zero.
    body = _greptile_summary(ATTENTION_FINDINGS)
    assert _greptile_fetch(monkeypatch, body, []) == 1
    out, err = capsys.readouterr()
    assert "NOTHING OUTSTANDING" not in out
    assert "does not read clean" in err


# --------------------------------------------------------------------------
# ADR-0067: a PR-level acknowledgement clears a finding no reply can reach.
# This is the gate's first LOOSENING — every prior change tightened it — so
# the rejection paths are tested as hard as the clearing one: an ack that
# fails any condition must leave the gate exactly as closed as no ack at all.
# --------------------------------------------------------------------------

GREPTILE_SUMMARY_ID = 5081386528
GEMINI_REVIEW_ID = 4780620978  # the id _review_by stamps


def _claims_three_summary(updated_at: str = "2026-07-26T01:00:00Z") -> dict[str, Any]:
    """PR #72's shape, scaled: the summary states 3, comments carry fewer."""
    body = _greptile_summary(ATTENTION_FINDINGS, fix_prompt=True).replace(
        "Fix the following 1 code review", "Fix the following 3 code review"
    )
    return _comment(
        GREPTILE_SUMMARY_ID,
        "greptile-apps[bot]",
        "2026-07-26T01:00:00Z",
        updated_at,
        body=body,
    )


def _ack(
    body: str,
    updated_at: str = "2026-07-26T02:00:00Z",
    login: str = "NeuroticGamer99",
    created_at: str | None = None,
) -> dict[str, Any]:
    """An acknowledgement comment; created_at defaults to the update stamp."""
    return _comment(9001, login, created_at or updated_at, updated_at, body=body)


def test_an_acknowledged_summary_clears_the_gate_a_reply_never_could(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # PRs #72 and #73 blocked here permanently: findings living only in the
    # summary prose have no comment object, so the threaded reply the gate
    # counts is physically impossible and the only exits were an unrecorded
    # override or a metered re-trigger. The mixed shape is the realistic one —
    # one finding threaded and answered, two existing only as prose — and the
    # ack carries the exact reference the alarm prints, on its own line under
    # the prose that actually answers them.
    finding = _pull_comment(1, "greptile-apps[bot]", "2026-07-26T01:00:00Z")
    reply = _pull_comment(
        2, "NeuroticGamer99", "2026-07-26T01:30:00Z", in_reply_to_id=1
    )
    ack = _ack(
        "Both prose findings verified against the code — the first is fixed in "
        "abc1234, the second declined per ADR-0055 §1.\n"
        f"Acknowledges greptile summary {GREPTILE_SUMMARY_ID}"
    )
    assert (
        _outstanding(
            monkeypatch, [finding, reply], issues=[_claims_three_summary(), ack]
        )
        == EXIT_CLEAN
    )
    assert "NOTHING OUTSTANDING" in capsys.readouterr().out


def test_a_bot_authored_ack_leaves_the_gate_closed(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # The same exclusion answered_ids applies via not_by, for the same reason:
    # a bot must never clear the gate on its own say-so, and a DIFFERENT bot's
    # comment is no more a person having read the finding than a self-ack —
    # CodeRabbit auto-replies as a matter of course, and nothing stops that
    # prose containing a matching sentence.
    for login in ("greptile-apps[bot]", "coderabbitai[bot]"):
        ack = _ack(f"Acknowledges greptile summary {GREPTILE_SUMMARY_ID}", login=login)
        assert _outstanding(monkeypatch, [], issues=[_claims_three_summary(), ack]) == 1
        assert "CANNOT CLEAR THE GATE" in capsys.readouterr().err


def test_an_ack_the_artifact_postdates_leaves_the_gate_closed(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # An ack cannot answer prose that had not been written. The artifact stamp
    # is the summary's updated_at, and that is the re-arming mechanism: a
    # Greptile re-review edits the summary IN PLACE, so the edit moves the
    # stamp past every earlier ack and invalidates them — new prose is a new
    # decision. This fixture is exactly that sequence: summary created 01:00,
    # acked 02:00, re-review edit at 03:00.
    ack = _ack(f"Acknowledges greptile summary {GREPTILE_SUMMARY_ID}")
    rereviewed = _claims_three_summary(updated_at="2026-07-26T03:00:00Z")
    assert _outstanding(monkeypatch, [], issues=[rereviewed, ack]) == 1
    assert "CANNOT CLEAR THE GATE" in capsys.readouterr().err
    # Same-second is also not "after" — strict >, the floor convention.
    tied = _ack(
        f"Acknowledges greptile summary {GREPTILE_SUMMARY_ID}",
        updated_at="2026-07-26T01:00:00Z",
    )
    assert _outstanding(monkeypatch, [], issues=[_claims_three_summary(), tied]) == 1
    capsys.readouterr()


@pytest.mark.parametrize(
    "reference",
    [
        f"Acknowledges greptile summary {GREPTILE_SUMMARY_ID + 1}",  # wrong id
        f"Acknowledges greptile review {GREPTILE_SUMMARY_ID}",  # wrong kind
        f"Acknowledges gemini summary {GREPTILE_SUMMARY_ID}",  # wrong bot
        "LGTM — merging.",  # no reference at all
    ],
)
def test_an_ack_naming_the_wrong_artifact_leaves_the_gate_closed(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    reference: str,
) -> None:
    # The id requirement is what stops a passing "LGTM" from clearing a real
    # gap — so a reference that names anything other than THIS artifact, or
    # nothing, must be worth exactly as much.
    ack = _ack(reference)
    assert _outstanding(monkeypatch, [], issues=[_claims_three_summary(), ack]) == 1
    assert "CANNOT CLEAR THE GATE" in capsys.readouterr().err


def test_an_acknowledged_body_only_review_clears_the_gate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The second detector (ADR-0067 covers both): Gemini's body bullets are
    # prose findings by construction, not by accident, so they take the same
    # acknowledgement.
    review = _review_by("github-actions[bot]", "2026-07-26T01:00:00Z", GEMINI_BODY_ONLY)
    ack = _ack(
        "All three unanchored findings verified and answered.\n"
        f"Acknowledges gemini review {GEMINI_REVIEW_ID}"
    )
    assert _outstanding(monkeypatch, [], reviews=[review], issues=[ack]) == EXIT_CLEAN


def test_a_body_only_ack_is_held_to_the_same_three_conditions(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # The conditions live in one shared function, but the wiring is per
    # detector — a body_only_findings that forgot to pass the review's own
    # stamp or id would pass a bot-authored or predated ack while the shared
    # function stayed correct. So each rejection is pinned through THIS
    # detector too.
    review = _review_by("github-actions[bot]", "2026-07-26T01:00:00Z", GEMINI_BODY_ONLY)
    rejected = [
        _ack(f"Acknowledges gemini review {GEMINI_REVIEW_ID}", login="Copilot"),
        _ack(
            f"Acknowledges gemini review {GEMINI_REVIEW_ID}",
            updated_at="2026-07-26T00:30:00Z",
        ),
        _ack(f"Acknowledges gemini review {GEMINI_REVIEW_ID + 1}"),
        _ack(f"Acknowledges gemini summary {GEMINI_REVIEW_ID}"),  # wrong kind
        _ack(f"Acknowledges greptile review {GEMINI_REVIEW_ID}"),  # wrong bot
    ]
    for ack in rejected:
        assert _outstanding(monkeypatch, [], reviews=[review], issues=[ack]) == 1
        assert "Acknowledges gemini review 4780620978" in capsys.readouterr().err


def test_the_ack_reference_is_case_insensitive_but_exact() -> None:
    # Case-insensitive because a human types it; exact in bot, kind, and id
    # because the reference is the entire discriminator between "answered" and
    # "waved through". A longer number containing the right digits is a
    # different id, not a match.
    at = parse_ts("2026-07-26T01:00:00Z")
    assert acknowledged(
        [_ack("acknowledges GREPTILE Summary 555")], GREPTILE, "summary", 555, at
    )
    # The reference lives on its own line under the prose that answers.
    assert acknowledged(
        [_ack("Verified both; see above.\nAcknowledges greptile summary 555.")],
        GREPTILE,
        "summary",
        555,
        at,
    )
    assert not acknowledged(
        [_ack("Acknowledges greptile summary 5550")], GREPTILE, "summary", 555, at
    )
    assert not acknowledged(
        [_ack("Acknowledges greptile summary 55")], GREPTILE, "summary", 555, at
    )
    # One comment may acknowledge several artifacts — one reference per line,
    # so a two-bot triage does not need two comments.
    both = _ack("Acknowledges greptile summary 555.\nAcknowledges gemini review 777.")
    assert acknowledged([both], GREPTILE, "summary", 555, at)
    assert acknowledged([both], GEMINI, "review", 777, at)


def test_an_ack_missing_created_at_falls_back_to_the_edit_stamp() -> None:
    # GitHub has never been seen to omit created_at; if a payload ever does,
    # _created_ts degrades to updated_at — the mirror of comment_ts's own
    # fallback — rather than crashing the gate over a missing field.
    at = parse_ts("2026-07-26T01:00:00Z")
    ack: dict[str, Any] = {
        "id": 9001,
        "user": {"login": "NeuroticGamer99"},
        "updated_at": "2026-07-26T02:00:00Z",
        "body": "Acknowledges greptile summary 555",
    }
    assert acknowledged([ack], GREPTILE, "summary", 555, at)


def test_an_unrelated_comments_garbage_timestamp_cannot_crash_the_gate() -> None:
    # The order of acknowledged()'s checks is load-bearing, not stylistic: the
    # reference is matched before any timestamp is parsed, so a malformed
    # stamp on a comment that never claimed to be an ack (an imported or
    # API-mangled comment) cannot turn the whole merge gate into a parse
    # error. Reordering the checks makes this raise BotReviewError.
    at = parse_ts("2026-07-26T01:00:00Z")
    junk: dict[str, Any] = {
        "id": 1,
        "user": {"login": "NeuroticGamer99"},
        "created_at": "not-a-timestamp",
        "updated_at": None,
        "body": "LGTM — what does this banner mean?",
    }
    valid = _ack("Acknowledges greptile summary 555")
    assert acknowledged([junk, valid], GREPTILE, "summary", 555, at)


def test_quoting_the_gates_own_banner_is_not_an_acknowledgement() -> None:
    # The refusal message prints a valid reference verbatim — that is the
    # copy-paste property the alarm wants — so the commonest non-ack comment
    # containing a reference is someone pasting the banner to ask about the
    # blocker. Mid-line, quoted, and blockquoted occurrences must all fail;
    # only a reference deliberately started at a line's beginning is a
    # decision. This is why ACK_REFERENCE is anchored with ^/MULTILINE.
    at = parse_ts("2026-07-26T01:00:00Z")
    banner_paste = _ack(
        "The merge is blocked with: greptile: its summary states 3 finding(s) "
        "but the sweep matched 1 — [...] carrying its own line "
        "'Acknowledges greptile summary 555' (ADR-0067). What should I do?"
    )
    blockquote = _ack("> Acknowledges greptile summary 555\n\nIs this right?")
    # A hard-wrapped paste can land the reference at a LINE START with the
    # banner's trailing text still attached — a leading anchor alone credited
    # exactly this shape, so the tail anchor is pinned as hard as the head.
    wrapped_paste = _ack(
        "carrying its own line '\nAcknowledges greptile summary 555' "
        "(ADR-0067). What should I do?"
    )
    wrapped_blockquote = _ack(
        "> carrying its own line '\nAcknowledges greptile summary 555'"
    )
    # Separators must not span lines either, or any wrap through the middle
    # of the reference reassembles it.
    token_split = _ack("Acknowledges\ngreptile\nsummary\n555")
    for echo in (
        banner_paste,
        blockquote,
        wrapped_paste,
        wrapped_blockquote,
        token_split,
    ):
        assert not acknowledged([echo], GREPTILE, "summary", 555, at)
    # The same reference owning its line IS credited — line ownership is the
    # only difference between the echo and the decision. A lone trailing
    # period rides along (prose ends that way); the banner's closing quote
    # does not.
    assert acknowledged(
        [_ack("Acknowledges greptile summary 555")], GREPTILE, "summary", 555, at
    )
    assert acknowledged(
        [_ack("Acknowledges greptile summary 555.")], GREPTILE, "summary", 555, at
    )
    assert not acknowledged(
        [_ack("Acknowledges greptile summary 555'")], GREPTILE, "summary", 555, at
    )


def test_editing_a_stale_ack_does_not_revive_it(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # The ack side of the timestamp is created_at — an acknowledgement is a
    # decision made when it was written. Sequence: summary created 01:00,
    # acked 02:00, re-review edits the summary in place at 03:00 (re-arming
    # the gate), then the owner edits the old ack at 04:00 for a typo. On
    # updated_at the 02:00 decision would time-travel past the 03:00 prose it
    # never read; on created_at it stays where it was made.
    edited_stale_ack = _ack(
        f"Acknowledges greptile summary {GREPTILE_SUMMARY_ID}",
        created_at="2026-07-26T02:00:00Z",
        updated_at="2026-07-26T04:00:00Z",
    )
    rereviewed = _claims_three_summary(updated_at="2026-07-26T03:00:00Z")
    assert _outstanding(monkeypatch, [], issues=[rereviewed, edited_stale_ack]) == 1
    assert "CANNOT CLEAR THE GATE" in capsys.readouterr().err


def test_a_summary_ack_clears_the_empty_bodied_review_alarm_too(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # PR #72 one step over: a findings run that posts its empty-bodied review
    # object AND the counting summary, but whose inline comments never land.
    # That trips unmatched_reviews (review exists, zero matched) alongside
    # undercounted_summaries — and if only the latter honored the ack, this
    # shape would rebuild the permanent block one detector to the left. The
    # summary is the artifact a person reads either way, so its ack clears
    # both; a body-less review offers nothing separate to acknowledge.
    empty_review = _review_by("greptile-apps[bot]", "2026-07-26T01:00:00Z", body="")
    assert (
        _outstanding(
            monkeypatch, [], reviews=[empty_review], issues=[_claims_three_summary()]
        )
        == 1
    )
    err = capsys.readouterr().err
    assert "0 of its comments matched" in err
    # The remedy carries the literal reference, like every other ack-clearable
    # alarm — a reference composed from memory and mistyped fails closed with
    # no hint of the typo.
    assert f"Acknowledges greptile summary {GREPTILE_SUMMARY_ID}" in err
    ack = _ack(f"Acknowledges greptile summary {GREPTILE_SUMMARY_ID}")
    assert (
        _outstanding(
            monkeypatch,
            [],
            reviews=[empty_review],
            issues=[_claims_three_summary(), ack],
        )
        == EXIT_CLEAN
    )


def test_the_unmatched_alarm_prints_the_reference_even_when_it_fires_alone(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # A findings summary WITHOUT the optional fix-prompt block states no count,
    # so undercounted_summaries stays silent and unmatched_reviews is the only
    # alarm left to print the reference. If it named the remedy without the
    # literal string, ADR-0067's never-composed-from-memory invariant would
    # break exactly where the triager has no other line to copy from.
    body = _greptile_summary(ATTENTION_FINDINGS, fix_prompt=False)
    summary = _comment(
        GREPTILE_SUMMARY_ID,
        "greptile-apps[bot]",
        "2026-07-26T01:00:00Z",
        "2026-07-26T01:00:00Z",
        body=body,
    )
    empty_review = _review_by("greptile-apps[bot]", "2026-07-26T01:00:00Z", body="")
    assert _outstanding(monkeypatch, [], reviews=[empty_review], issues=[summary]) == 1
    err = capsys.readouterr().err
    assert "0 of its comments matched" in err
    assert f"Acknowledges greptile summary {GREPTILE_SUMMARY_ID}" in err
    ack = _ack(f"Acknowledges greptile summary {GREPTILE_SUMMARY_ID}")
    assert (
        _outstanding(monkeypatch, [], reviews=[empty_review], issues=[summary, ack])
        == EXIT_CLEAN
    )


def test_an_unmatched_review_with_no_summary_yet_offers_no_remedy(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # The third summary state: a summary_marker bot whose review landed but
    # whose summary has NOT — summary is None, distinct from "no marker at
    # all". The remedy line would name an artifact that does not exist, so it
    # must be absent, and with nothing to read there is nothing to ack: the
    # alarm stands.
    empty_review = _review_by("greptile-apps[bot]", "2026-07-26T01:00:00Z", body="")
    assert _outstanding(monkeypatch, [], reviews=[empty_review], issues=[]) == 1
    err = capsys.readouterr().err
    assert "0 of its comments matched" in err
    assert "Acknowledges" not in err


def test_a_non_summary_bots_unmatched_review_takes_no_ack(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # The boundary of the F4 wiring: for every bot without a summary comment,
    # zero-matched means the author FILTER missed real comment objects, and no
    # PR-level comment makes unread comments read. An ack naming the review
    # must be worth nothing there.
    review = _review_by(
        "coderabbitai[bot]", "2026-07-26T01:00:00Z", "**Actionable comments posted: 2**"
    )
    ack = _ack(f"Acknowledges coderabbit review {GEMINI_REVIEW_ID}")
    issues = [
        _comment(
            5081386529,
            "greptile-apps[bot]",
            "2026-07-26T01:00:00Z",
            "2026-07-26T01:00:00Z",
            body=_greptile_summary(ATTENTION_CLEAN),
        ),
        ack,
    ]
    assert _outstanding(monkeypatch, [], reviews=[review], issues=issues) == 1
    err = capsys.readouterr().err
    assert "0 of its comments matched" in err
    # And no remedy reference: there is no summary whose ack could clear it.
    assert "Acknowledges coderabbit" not in err


def test_the_triage_commands_do_not_credit_acknowledgements(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # ADR-0067 §2's other boundary: wait/fetch answer a triage question, not
    # the merge question, so a valid ack changes nothing there — fetch still
    # refuses to classify the summary-only shape. Without this pin, the
    # obvious next "fix" when fetch exits 1 after acking is to wire
    # acknowledged() into summary_state, silently reversing a recorded
    # decision under a green suite.
    import bot_review

    body = _greptile_summary(ATTENTION_FINDINGS, fix_prompt=True)
    stale_summary = _comment(
        GREPTILE_SUMMARY_ID,
        "greptile-apps[bot]",
        "2020-01-01T00:00:00Z",
        "2020-01-01T00:00:00Z",
        body=body,
    )
    valid_ack = _ack(
        f"Acknowledges greptile summary {GREPTILE_SUMMARY_ID}",
        updated_at="2020-01-02T00:00:00Z",
    )
    assert acknowledged(
        [stale_summary, valid_ack],
        GREPTILE,
        "summary",
        GREPTILE_SUMMARY_ID,
        comment_ts(stale_summary),
    )

    def the_issues(repo: str, pr: int) -> list[dict[str, Any]]:
        return [stale_summary, valid_ack]

    def nothing(repo: str, pr: int) -> list[dict[str, Any]]:
        return []

    def the_head(repo: str, pr: int) -> str:
        return GREPTILE_SHA

    monkeypatch.setattr(bot_review, "issue_comments", the_issues)
    monkeypatch.setattr(bot_review, "pull_comments", nothing)
    monkeypatch.setattr(bot_review, "pr_head_sha", the_head)
    since = parse_ts("2019-01-01T00:00:00Z")
    assert bot_review.cmd_fetch("o/r", 72, GREPTILE, since) == 1
    err = capsys.readouterr().err
    assert "exist only in the summary text" in err
    # Printing the reference is not crediting it: the refusal names the exact
    # string to post while the exit stays 1 — the ack already present changes
    # nothing here, because answering the merge question is outstanding's job.
    assert f"Acknowledges greptile summary {GREPTILE_SUMMARY_ID}" in err


def test_a_bot_key_the_ack_reference_cannot_name_is_refused() -> None:
    # The gate prints `Acknowledges <key> ...` as the remedy for unrepliable
    # findings, and ACK_REFERENCE's <bot> group captures only ACK_KEY_PATTERN.
    # A key outside that class would make the printed remedy unmatchable — the
    # bot's unrepliable findings permanently unclearable again, the exact state
    # ADR-0067 removes, rebuilt by a config addition (Copilot, PR #74). Refused
    # at import like every other mis-built spec; the import of BOTS itself is
    # what proves every current key conforms.
    import bot_review

    with pytest.raises(ValueError, match="must match"):
        bot_review.BotSpec(
            key="under_scored",
            review_login="x[bot]",
            request_login=None,
            requested_display=None,
            trigger_body="@x review",
            count=re.compile(r"(\d+)"),
            clean_marker=None,
        )


def test_silence_cannot_be_acknowledged(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # silent_always_reviewers honors no ack for any bot: silence leaves no
    # artifact to read, so there is nothing an acknowledgement could honestly
    # assert. A stray valid-form reference must not stand in for the missing
    # run. Synthetic spec for the same reason as the detector's own test — no
    # live bot reviews unasked since `skipReview: "AUTOMATIC"`.
    import bot_review

    unasked = replace(GREPTILE, always_reviews=True)
    monkeypatch.setattr(bot_review, "BOTS", {**BOTS, GREPTILE.key: unasked})
    stray = _ack(f"Acknowledges greptile summary {GREPTILE_SUMMARY_ID}")
    assert _outstanding(monkeypatch, [], issues=[stray]) == 1
    err = capsys.readouterr().err
    assert "reviews every PR unasked" in err


def test_one_bots_reply_does_not_answer_another_bots_finding() -> None:
    # CodeRabbit auto-replies to findings as a matter of course — three of its
    # own on PR #71 — and nothing stops that landing on another bot's thread.
    # A per-spec exclusion would score it as triage; neither bot is a person
    # who read the finding.
    ours = _pull_comment(3651510728, "Copilot", "2026-07-26T02:04:53Z")
    other_bot = _pull_comment(
        999, "coderabbitai[bot]", "2026-07-26T02:10:00Z", in_reply_to_id=3651510728
    )
    assert unanswered([ours], [ours, other_bot]) == [ours]
    human = _pull_comment(
        1000, "NeuroticGamer99", "2026-07-26T02:11:00Z", in_reply_to_id=3651510728
    )
    assert unanswered([ours], [ours, other_bot, human]) == []


def test_a_comment_login_that_duplicates_the_review_login_is_refused() -> None:
    import bot_review

    with pytest.raises(ValueError, match="duplicates review_login"):
        bot_review.BotSpec(
            key="broken",
            review_login="x[bot]",
            request_login=None,
            requested_display=None,
            trigger_body="@x review",
            count=re.compile(r"(\d+)"),
            clean_marker=None,
            comment_login="X[BOT]",
        )

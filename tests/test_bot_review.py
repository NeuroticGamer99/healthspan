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
    EXIT_CLEAN,
    EXIT_EMPTY_RANGE,
    BotReviewError,
    as_page,
    comment_ts,
    count_note,
    is_clean_comment,
    is_empty_range_review,
    is_findings_review,
    parse_ts,
    run_cmd,
    same_login,
    select_clean_comment,
    select_failed_run,
    select_review,
    stated_count,
)

CODERABBIT = BOTS["coderabbit"]
COPILOT = BOTS["copilot"]
GEMINI = BOTS["gemini"]


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
    # Copilot's clean run is still a review ("generated 0 comments"), which
    # select_review finds; its spec deliberately opts out of comment scanning.
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


def test_reviewer_request_verifies_the_ask_took_and_prints_the_floor(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # The Copilot branch, previously untested: POST to requested_reviewers,
    # then read the PR back to prove GitHub actually added someone — under the
    # *display* login, not the one that was posted.
    import bot_review

    calls: list[str] = []

    def fake_gh(path: str, *args: str) -> Any:
        calls.append(path)
        if path.endswith("/requested_reviewers"):
            return None
        return {"requested_reviewers": [{"login": "Copilot"}]}

    monkeypatch.setattr(bot_review, "gh", fake_gh)
    assert bot_review.cmd_request("o/r", 54, COPILOT) == 0
    assert calls == ["repos/o/r/pulls/54/requested_reviewers", "repos/o/r/pulls/54"]
    out = capsys.readouterr().out
    assert "requested copilot; requested_reviewers now: Copilot" in out
    assert "since: " in out
    assert "--bot copilot --pr 54" in out


def test_a_request_accepted_and_dropped_fails_loudly_rather_than_waiting(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # GitHub answers HTTP 200 to a login it does not recognize and silently
    # adds no one; only the read-back exposes it. Waiting after that buys the
    # full poll timeout for a review nobody managed to request.
    import bot_review

    def fake_gh(path: str, *args: str) -> Any:
        return (
            None
            if path.endswith("/requested_reviewers")
            else {"requested_reviewers": []}
        )

    monkeypatch.setattr(bot_review, "gh", fake_gh)
    with pytest.raises(BotReviewError, match="Do not wait"):
        bot_review.cmd_request("o/r", 54, COPILOT)


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
    monkeypatch: pytest.MonkeyPatch,
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

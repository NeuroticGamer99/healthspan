"""The manual-entry / readback CLI (ADR-0059, Phase 3 WI-4).

Drives the real ``POST /v1/import`` route and the ``read``-scoped GETs through
a FastAPI ``TestClient`` (never just the import engine — the HTTP route has its
own ``extra='forbid'`` allowlist), against a migrated database whose catalog is
the migration 0004/0005 seed. Interactive prompts are fed through
``CliRunner(input=...)``. Synthetic/generic biomarker values only (CLAUDE.md
containment).
"""

import json
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Self

import pytest
import typer
from fastapi import FastAPI
from fastapi.testclient import TestClient
from typer.testing import CliRunner

from healthspan import cli_entry, db, keychain, migrate, token_bootstrap
from healthspan.cli import app as cli_app
from healthspan.cli_entry import (
    _Api,  # pyright: ignore[reportPrivateUsage]
    _biomarker_catalog,  # pyright: ignore[reportPrivateUsage]
    _choice_index,  # pyright: ignore[reportPrivateUsage]
    _record_aliases,  # pyright: ignore[reportPrivateUsage]
    _require_ok,  # pyright: ignore[reportPrivateUsage]
    _validate_draw_utc,  # pyright: ignore[reportPrivateUsage]
    parse_value,
)
from healthspan.config import load_config
from healthspan.kdf import DbKey
from healthspan.locking import InstanceLock
from healthspan.pool import ConnectionPool
from healthspan.service import create_app
from healthspan.service_runtime import ServiceRuntime

runner = CliRunner()
KEY_BYTES = bytes(range(1, 33))


def _key() -> DbKey:
    return DbKey(bytearray(KEY_BYTES))


class _PortalClient(TestClient):
    """A TestClient whose context-manager protocol is a no-op (see test_cli_token)."""

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exc: object) -> None:
        return None


@dataclass
class CliEnv:
    config_path: Path
    app: FastAPI


@pytest.fixture
def cli_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[CliEnv]:
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        'config_version = 1\n\n[database]\npath = "hs.db"\n', encoding="utf-8"
    )
    cfg = load_config(flag=config_path)
    db.provision(cfg.database.path, _key())
    migrate.migrate_database(cfg.database.path, _key())
    setup = db.connect(cfg.database.path, _key())
    try:
        token_bootstrap.bootstrap_default_tokens(setup, lambda _: None)
    finally:
        db.close(setup)
    lock = InstanceLock(cfg.database.path)
    lock.acquire()
    key = _key()
    runtime = ServiceRuntime(
        cfg=cfg,
        key=key,
        lock=lock,
        pool=ConnectionPool(cfg.database.path, key),
        schema_version=3,
    )
    application = create_app(runtime)
    with TestClient(application):

        def portal_client(_cfg: object) -> _PortalClient:
            return _PortalClient(application)

        monkeypatch.setattr(cli_entry, "_build_client", portal_client)
        yield CliEnv(config_path=config_path, app=application)


def _invoke(env: CliEnv, *args: str, stdin: str = "", expect: int = 0) -> str:
    result = runner.invoke(
        cli_app, ["--config", str(env.config_path), *args], input=stdin
    )
    assert result.exit_code == expect, result.output
    return result.output


def _direct_import(env: CliEnv, payload: dict[str, Any]) -> None:
    """POST an import batch straight to the app (for shapes `enter` cannot type).

    ``enter`` never sets a lab's own ``reference_low``/``reference_high``, so the
    lab-native-range render branch is only reachable for results imported by
    another path — exactly what a real ``results`` read must render.
    """
    from healthspan import keychain
    from healthspan.api_import import IMPORT_PATH

    token = keychain.load_token_plaintext("cli-admin")
    client: Any = TestClient(env.app)  # typed Any under pyright strict (WI-1 gotcha)
    response = client.post(
        IMPORT_PATH, json=payload, headers={"Authorization": f"Bearer {token}"}
    )
    assert response.status_code == 200, response.text


# --------------------------------------------------------------------------
# Value parsing — the ADR-0030 fidelity contract
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("<0.1", (0.1, "<", None)),
        ("<=5", (5.0, "<=", None)),
        (">=60", (60.0, ">=", None)),
        (">3", (3.0, ">", None)),
        ("5.2", (5.2, None, None)),
        ("12", (12.0, None, None)),
        ("  <0.1  ", (0.1, "<", None)),
        ("positive", (None, None, "positive")),
        ("inf", (None, None, "inf")),  # non-finite is not a magnitude -> text
        ("150,000", (150000.0, None, None)),  # thousands separators tolerated
        ("1,234.56", (1234.56, None, None)),
        ("<150,000", (150000.0, "<", None)),  # grouped magnitude under a comparator
        ("1,5", (None, None, "1,5")),  # ambiguous comma stays qualitative, not 15
    ],
)
def test_parse_value_table(
    raw: str, expected: tuple[float | None, str | None, str | None]
) -> None:
    assert parse_value(raw) == expected


def test_parse_value_never_drops_the_comparator() -> None:
    # The whole point of the value model: a censored value is not its magnitude.
    value_num, comparator, value_text = parse_value("<0.1")
    assert (value_num, comparator, value_text) == (0.1, "<", None)
    assert comparator is not None  # never a bare 0.1


@pytest.mark.parametrize("raw", ["", "   ", "<abc", ">=", "<inf", "<nan"])
def test_parse_value_rejects_bad_input(raw: str) -> None:
    with pytest.raises(ValueError):  # noqa: PT011 - message asserted at call sites
        parse_value(raw)


@pytest.mark.parametrize(
    "value",
    [
        "2026-01-15",
        "2026-01-15T08:30:00Z",
        "2026-01-15T08:30:00+00:00",
    ],
)
def test_validate_draw_utc_accepts_date_and_utc_timestamp(value: str) -> None:
    _validate_draw_utc(value)  # no raise


@pytest.mark.parametrize(
    "value",
    [
        "2026-1-1",  # unpadded
        "01/15/2026",  # not ISO
        "2026-13-01",  # not a real month
        "20260115",  # dash-less date: fromisoformat accepts, we must not
        "20260115T083000Z",  # basic-format UTC stamp: valid ISO-8601, wrong substr[:10]
        "2026-01-15 08:30:00+00:00",  # space separator, not the required 'T'
        "2026-01-15T08:30:00",  # naive (no UTC designator)
        "2026-01-15T08:30:00+05:00",  # not UTC
        "yesterday",
    ],
)
def test_validate_draw_utc_rejects_malformed_or_non_utc(value: str) -> None:
    with pytest.raises(typer.Exit):
        _validate_draw_utc(value)


# --------------------------------------------------------------------------
# `enter` — the interactive draw-level flow against the real route
# --------------------------------------------------------------------------


def test_enter_happy_path_flags_out_of_range(cli_env: CliEnv) -> None:
    # Total Cholesterol 250 mg/dL vs the seeded nih_medlineplus target (<=200)
    # -> 'above'. Prompts: lab, date, fasting, context, notes, biomarker,
    # value, unit(default), finish, commit.
    output = _invoke(
        cli_env,
        "enter",
        "--framework",
        "nih_medlineplus_lipid_targets",
        stdin="Quest\n2026-05-01\ny\n\n\nTotal Cholesterol\n250\n\n\ny\n",
    )
    assert "Preview (reject)" in output
    assert "Imported batch" in output
    assert "Total Cholesterol" in output
    assert "above" in output
    # The one-sided target (<=200) must never render as a negative-looking "-200".
    assert "-200" not in output


def test_enter_multiple_results_share_one_draw(cli_env: CliEnv) -> None:
    # The point of the draw-level template: enter the draw once, then several
    # results against it. Two results, one draw, one batch.
    output = _invoke(
        cli_env,
        "enter",
        stdin=(
            "Quest\n2026-09-01\n\n\n\n"
            "Total Cholesterol\n190\n\n"
            "LDL Cholesterol\n100\n\n"
            "\ny\n"
        ),
    )
    assert "lab_draws: 1 inserted" in output
    assert "lab_results: 2 inserted" in output
    assert "Total Cholesterol" in output
    assert "LDL Cholesterol" in output


def test_enter_preserves_censored_value(cli_env: CliEnv) -> None:
    entered = _invoke(
        cli_env,
        "enter",
        stdin="Quest\n2026-06-01\nn\n\n\nGlucose\n<50\n\n\ny\n",
    )
    assert "Imported batch" in entered
    # Pin the ADR-0030 triple structurally via --json, NOT a substring of the
    # rendered/echoed output: CliRunner echoes typed stdin back into `output`,
    # so `"<50" in output` would pass even if the comparator were dropped. The
    # stored/served row cannot be faked by stdin echo.
    page = json.loads(_invoke(cli_env, "results", "list", "--json"))
    row = next(r for r in page["items"] if r.get("value_num") == 50.0)
    assert row["comparator"] == "<"  # never a bare 50.0
    assert row["value_text"] is None
    assert row["display"] == "<50"  # censoring survives import -> read


def test_enter_alias_confirm_and_record_then_resolves_next_session(
    cli_env: CliEnv,
) -> None:
    # 'a1c' is not a canonical name; exactly one biomarker matches it
    # (Hemoglobin A1c), so pick 1. Confirm recording the alias.
    first = _invoke(
        cli_env,
        "enter",
        stdin="Quest\n2026-07-01\n\n\n\na1c\n1\ny\n5.5\n\n\ny\n",
    )
    assert "matches no biomarker name" in first
    assert "Hemoglobin A1c" in first
    assert "Imported batch" in first
    # The alias is recorded AFTER the draw commits (ADR-0059 §3).
    assert "recorded 1 new alias" in first

    # Next session, 'a1c' resolves silently — the recorded alias is now in the
    # canonical + alias namespace the CLI consults (a different draw date so it
    # is a fresh insert, not a conflict).
    second = _invoke(
        cli_env,
        "enter",
        stdin="Quest\n2026-08-01\n\n\n\na1c\n5.6\n\n\ny\n",
    )
    assert "matches no biomarker name" not in second
    assert "Imported batch" in second
    assert "Hemoglobin A1c" in second


def test_enter_unknown_name_can_be_skipped(cli_env: CliEnv) -> None:
    # A name with no candidates: search shows none, blank line skips the result,
    # and with no results the flow exits without importing.
    output = _invoke(
        cli_env,
        "enter",
        stdin="Quest\n2026-05-02\n\n\n\nnonexistent-marker\n\n\n",
    )
    assert "no candidates" in output
    assert "No results entered" in output


def test_enter_conflict_under_reject_hints_upsert(cli_env: CliEnv) -> None:
    _invoke(
        cli_env,
        "enter",
        stdin="Quest\n2026-05-01\n\n\n\nTotal Cholesterol\n190\n\n\ny\n",
    )
    # Same (lab, draw_utc, biomarker) with a different value under reject: the
    # dry-run preview rejects with a conflict and points at --on-conflict.
    output = _invoke(
        cli_env,
        "enter",
        stdin="Quest\n2026-05-01\n\n\n\nTotal Cholesterol\n250\n\n\ny\n",
        expect=1,
    )
    assert "conflict" in output
    assert "--on-conflict upsert" in output


def test_enter_upsert_corrects_an_existing_result(cli_env: CliEnv) -> None:
    _invoke(
        cli_env,
        "enter",
        stdin="Quest\n2026-05-01\n\n\n\nTotal Cholesterol\n190\n\n\ny\n",
    )
    output = _invoke(
        cli_env,
        "enter",
        "--on-conflict",
        "upsert",
        stdin="Quest\n2026-05-01\n\n\n\nTotal Cholesterol\n250\n\n\ny\n",
    )
    assert "Imported batch" in output
    assert "corrected" in output


def test_enter_unknown_framework_fails_before_any_prompt(cli_env: CliEnv) -> None:
    # The framework is verified up front, so a typo fails before the lab prompt
    # ever runs (nothing is entered, nothing is committed).
    output = _invoke(cli_env, "enter", "--framework", "bogus", stdin="", expect=1)
    assert "unknown framework" in output
    assert "nih_medlineplus_lipid_targets" in output


def test_enter_missing_token_gives_keyring_guidance(cli_env: CliEnv) -> None:
    output = _invoke(
        cli_env, "enter", "--token-name", "ghost-token", stdin="", expect=1
    )
    assert "ghost-token" in output
    assert "keyring" in output


# --------------------------------------------------------------------------
# Read commands
# --------------------------------------------------------------------------


def test_results_list_renders_and_json(cli_env: CliEnv) -> None:
    _invoke(
        cli_env,
        "enter",
        stdin="Quest\n2026-05-01\n\n\n\nTotal Cholesterol\n190\n\n\ny\n",
    )
    rendered = _invoke(cli_env, "results", "list")
    assert "Total Cholesterol" in rendered

    page = json.loads(_invoke(cli_env, "results", "list", "--json"))
    assert any(row.get("value_num") == 190.0 for row in page["items"])


def test_biomarkers_search(cli_env: CliEnv) -> None:
    output = _invoke(cli_env, "biomarkers", "search", "cholesterol")
    assert "Total Cholesterol" in output
    assert "HDL Cholesterol" in output
    assert "LDL Cholesterol" in output


def test_draws_json_carries_notes(cli_env: CliEnv) -> None:
    # Exercises the _prompt_draw non-blank *notes* branch (context left blank)
    # and draws-list --json. Prompts: lab, date, fasting, context, notes, ...
    _invoke(
        cli_env,
        "enter",
        stdin="Quest\n2026-05-01\n\n\nfasted 12h\nTotal Cholesterol\n190\n\n\ny\n",
    )
    page = json.loads(_invoke(cli_env, "draws", "list", "--json"))
    assert any(draw.get("notes") == "fasted 12h" for draw in page["items"])


def test_results_show_plain_and_secondary_json_branches(cli_env: CliEnv) -> None:
    _invoke(
        cli_env,
        "enter",
        stdin="Quest\n2026-05-01\n\n\n\nTotal Cholesterol\n190\n\n\ny\n",
    )
    page = json.loads(_invoke(cli_env, "results", "list", "--json"))
    result_id = page["items"][0]["id"]
    # results show WITHOUT --framework: the plain (non-comparison) render path.
    shown = _invoke(cli_env, "results", "show", str(result_id))
    assert "Total Cholesterol" in shown

    # The --json branch of each secondary read command (_emit_json call sites).
    assert isinstance(json.loads(_invoke(cli_env, "labs", "list", "--json")), list)
    assert isinstance(
        json.loads(_invoke(cli_env, "frameworks", "list", "--json")), list
    )
    assert isinstance(
        json.loads(_invoke(cli_env, "biomarkers", "search", "Glucose", "--json")), list
    )
    assert "items" in json.loads(_invoke(cli_env, "draws", "list", "--json"))
    assert "items" in json.loads(_invoke(cli_env, "biomarkers", "list", "--json"))


def test_results_show_with_framework_renders_comparison(cli_env: CliEnv) -> None:
    _invoke(
        cli_env,
        "enter",
        stdin="Quest\n2026-05-01\n\n\n\nTotal Cholesterol\n250\n\n\ny\n",
    )
    page = json.loads(_invoke(cli_env, "results", "list", "--json"))
    result_id = page["items"][0]["id"]
    # A separate invocation with empty stdin: nothing here comes from echo.
    output = _invoke(
        cli_env,
        "results",
        "show",
        str(result_id),
        "--framework",
        "nih_medlineplus_lipid_targets",
    )
    assert "above" in output  # _render_comparison flag
    assert "target" in output  # _render_comparison target segment
    assert "nih_medlineplus_lipid_targets" in output  # framework segment


def test_results_list_renders_lab_native_range(cli_env: CliEnv) -> None:
    # `enter` cannot set a lab's own range, so import one directly; the read
    # must render the (lab ref: ...) fallback branch when no framework is asked.
    _direct_import(
        cli_env,
        {
            "source": "manual",
            "conflict_policy": "reject",
            "lab_draws": [{"id": 1, "lab_id": 1, "draw_utc": "2026-04-01"}],
            "lab_results": [
                {
                    "lab_draw_id": 1,
                    "biomarker_name": "Glucose",
                    "value_num": 90,
                    "unit": "mg/dL",
                    "reference_low": 70,
                    "reference_high": 99,
                }
            ],
        },
    )
    output = _invoke(cli_env, "results", "list")
    assert "lab ref: 70-99" in output


def test_results_list_limit_shows_more_hint(cli_env: CliEnv) -> None:
    _invoke(
        cli_env,
        "enter",
        stdin="Quest\n2026-05-01\n\n\n\nTotal Cholesterol\n190\n\n\ny\n",
    )
    _invoke(
        cli_env,
        "enter",
        stdin="Quest\n2026-06-01\n\n\n\nTotal Cholesterol\n195\n\n\ny\n",
    )
    output = _invoke(cli_env, "results", "list", "--limit", "1")
    assert "more rows exist" in output


def test_draws_list_renders_context_and_fasting(cli_env: CliEnv) -> None:
    _invoke(
        cli_env,
        "enter",
        stdin=(
            "Quest\n2026-05-01\ny\nannual physical\n\nTotal Cholesterol\n190\n\n\ny\n"
        ),
    )
    # Separate invocation, empty stdin: draw_utc/context/fasting come from the
    # render, not from echo.
    output = _invoke(cli_env, "draws", "list")
    assert "2026-05-01" in output
    assert "annual physical" in output  # non-blank draw_context branch
    assert "fasting=yes" in output


def test_labs_and_frameworks_list(cli_env: CliEnv) -> None:
    labs = _invoke(cli_env, "labs", "list")
    assert "Quest" in labs
    assert "LabCorp" in labs
    frameworks = _invoke(cli_env, "frameworks", "list")
    assert "nih_medlineplus_lipid_targets" in frameworks
    assert "ada_standards_of_care" in frameworks


def test_biomarkers_list_filtered_by_category(cli_env: CliEnv) -> None:
    output = _invoke(cli_env, "biomarkers", "list", "--category", "metabolic")
    assert "Glucose" in output
    assert "Hemoglobin A1c" in output


def test_one_sided_range_renders_as_bound_not_negative(cli_env: CliEnv) -> None:
    # A one-sided <=200 target must read as "≤200", never "-200" (a negative bound).
    _invoke(
        cli_env,
        "enter",
        stdin="Quest\n2026-05-01\n\n\n\nTotal Cholesterol\n250\n\n\ny\n",
    )
    page = json.loads(_invoke(cli_env, "results", "list", "--json"))
    result_id = page["items"][0]["id"]
    shown = _invoke(
        cli_env,
        "results",
        "show",
        str(result_id),
        "--framework",
        "nih_medlineplus_lipid_targets",
    )
    assert "-200" not in shown
    assert "≤200" in shown  # ≤200


def test_re_entering_a_biomarker_replaces_last_wins(cli_env: CliEnv) -> None:
    # Re-typing a biomarker (the natural typo-correction gesture) must replace the
    # earlier row, not emit a duplicate that 422s the whole batch and loses it all.
    output = _invoke(
        cli_env,
        "enter",
        stdin=(
            "Quest\n2026-05-01\n\n\n\n"
            "Total Cholesterol\n190\n\n"
            "Total Cholesterol\n250\n\n"
            "\ny\n"
        ),
    )
    assert "replacing your earlier entry" in output
    assert "Imported batch" in output
    assert "lab_results: 1 inserted" in output  # one row, not two
    page = json.loads(_invoke(cli_env, "results", "list", "--json"))
    values = [row["value_num"] for row in page["items"]]
    assert values == [250.0]  # last-wins


@pytest.mark.parametrize(
    ("choice", "expected"),
    [
        ("1", 1),
        ("12", 12),
        ("²", None),  # isdigit() True, int() ValueError -> must not crash
        ("①", None),  # circled digit: same trap
        ("abc", None),  # a new search term
        ("", None),
    ],
)
def test_choice_index_rejects_non_decimal_without_crashing(
    choice: str, expected: int | None
) -> None:
    assert _choice_index(choice) == expected


def test_require_ok_guards_a_non_json_200() -> None:
    # A 200 with a non-JSON body is a clean failure, never a NoneType traceback.
    with pytest.raises(typer.Exit):
        _require_ok(200, None, "reject")
    assert _require_ok(200, {"batch_id": 7}, "reject") == {"batch_id": 7}


def _api_for(env: CliEnv) -> _Api:
    """An _Api bound to the test app + cli-admin token (for direct helper calls)."""
    cfg = load_config(flag=env.config_path)
    token = keychain.load_token_plaintext("cli-admin")
    assert token is not None
    return _Api(
        cfg=cfg, token_name="cli-admin", token=token, client=_PortalClient(env.app)
    )


def test_record_aliases_reports_inserted_unchanged_and_skipped(
    cli_env: CliEnv, capsys: pytest.CaptureFixture[str]
) -> None:
    # The ADR-0059 §3 honesty promise: recorded vs already-on-record vs left
    # unchanged (a pre-existing row the skip policy did not repoint).
    api = _api_for(cli_env)
    ids = {
        str(row["canonical_name"]): int(row["id"]) for row in _biomarker_catalog(api)
    }
    glucose, cholesterol = ids["Glucose"], ids["Total Cholesterol"]

    _record_aliases(
        api, [{"biomarker_id": glucose, "alias": "sugar", "source": "manual"}]
    )
    assert "recorded 1 new alias" in capsys.readouterr().out

    # Identical re-record -> rows_unchanged.
    _record_aliases(
        api, [{"biomarker_id": glucose, "alias": "sugar", "source": "manual"}]
    )
    assert "already on record" in capsys.readouterr().out

    # Same normalized alias, different biomarker -> skip keeps the stored row.
    _record_aliases(
        api, [{"biomarker_id": cholesterol, "alias": "sugar", "source": "manual"}]
    )
    assert "left unchanged" in capsys.readouterr().out


def test_alias_resolves_within_the_same_session(cli_env: CliEnv) -> None:
    # After recording 'a1c' during entry, re-typing it later in the SAME session
    # resolves silently (in-session index update), not a second pick dialog.
    output = _invoke(
        cli_env,
        "enter",
        stdin="Quest\n2026-05-01\n\n\n\na1c\n1\ny\n5.5\n\na1c\n5.6\n\n\ny\n",
    )
    assert output.count("matches no biomarker name") == 1  # only the first time
    assert "replacing your earlier entry" in output  # the second 'a1c' resolved
    assert "recorded 1 new alias" in output


def test_enter_rejects_a_malformed_draw_date(cli_env: CliEnv) -> None:
    # A bad date is caught at the interactive surface, before anything commits.
    output = _invoke(cli_env, "enter", stdin="Quest\n2026-1-1\n", expect=1)
    assert "must be YYYY-MM-DD" in output


def test_enter_warns_on_a_comma_decimal_value(cli_env: CliEnv) -> None:
    # "5,2" stays qualitative text (no locale guessing) but is NOT silent — the
    # owner is told they may have meant the number 5.2.
    output = _invoke(
        cli_env,
        "enter",
        stdin="Quest\n2026-05-01\n\n\n\nGlucose\n5,2\n\n\ny\n",
    )
    assert "if you meant a number, use '.' not ','" in output
    assert "Imported batch" in output  # still commits, as a text value


def test_pick_retry_out_of_range_then_new_search(cli_env: CliEnv) -> None:
    # An out-of-range numeric pick re-prompts; a non-numeric choice is a new
    # search — both interactive branches of the pick loop.
    output = _invoke(
        cli_env,
        "enter",
        stdin="Quest\n2026-05-01\n\n\n\na1c\n9\nchol\n\n\n",
    )
    assert "no candidate has that number" in output
    assert "candidates for 'chol'" in output
    assert "No results entered" in output


def test_aborting_the_draw_records_no_alias(cli_env: CliEnv) -> None:
    # Confirm an alias, then decline the commit: the alias must NOT be persisted
    # (it is recorded only after the draw commits).
    first = _invoke(
        cli_env,
        "enter",
        stdin="Quest\n2026-05-01\n\n\n\na1c\n1\ny\n5.5\n\n\nn\n",
    )
    assert "Aborted; nothing written" in first
    assert "recorded" not in first
    # Next session, 'a1c' is still unresolved — proof the alias was not written.
    second = _invoke(
        cli_env,
        "enter",
        stdin="Quest\n2026-05-02\n\n\n\na1c\n\n\n",
    )
    assert "matches no biomarker name" in second


def test_enter_with_lab_id_flag_skips_lab_prompt(cli_env: CliEnv) -> None:
    # --lab-id bypasses the lab-name prompt, so stdin starts at the draw date.
    output = _invoke(
        cli_env,
        "enter",
        "--lab-id",
        "1",
        stdin="2026-05-01\n\n\n\nTotal Cholesterol\n190\n\n\ny\n",
    )
    assert "Imported batch" in output


def test_enter_unknown_lab_id_fails(cli_env: CliEnv) -> None:
    output = _invoke(cli_env, "enter", "--lab-id", "9999", stdin="", expect=1)
    assert "no lab with id 9999" in output


def test_enter_unknown_lab_name_fails(cli_env: CliEnv) -> None:
    output = _invoke(cli_env, "enter", stdin="No Such Lab\n", expect=1)
    assert "no lab named" in output

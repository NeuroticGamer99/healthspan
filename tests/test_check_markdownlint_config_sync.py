"""Unit tests for the markdown-lint config drift gate (ADR-0062, option E).

Two layers: the pure parsers/diff against constructed config strings, and a
check() over the *real* repo configs asserting they currently agree (the gate's
own live contract -- if a future edit desyncs them, this fails here as well as
in CI).
"""

from pathlib import Path

import check_markdownlint_config_sync as sync
import pytest

# --- parse_pymarkdown -----------------------------------------------------


def test_parse_pymarkdown_reads_enabled_and_params() -> None:
    toml = """
[tool.pymarkdown]
mode.strict-config = true
extensions.front-matter.enabled = true
plugins.md013.enabled = false
plugins.md024.siblings_only = true
"""
    rules = sync.parse_pymarkdown(toml)
    assert rules["MD013"] == {"enabled": False, "params": {}}
    assert rules["MD024"] == {"enabled": True, "params": {"siblings_only": True}}
    # The PyMarkdown-only tables carry no rule decision.
    assert set(rules) == {"MD013", "MD024"}


def test_parse_pymarkdown_rejects_unknown_top_level_table() -> None:
    # A new PyMarkdown-only concept must be added to the exclusion list
    # consciously, not silently tolerated.
    with pytest.raises(ValueError, match="unexpected top-level key"):
        sync.parse_pymarkdown("[tool.pymarkdown]\nfuture.setting = 1\n")


def test_parse_pymarkdown_rejects_non_table_plugins() -> None:
    with pytest.raises(ValueError, match="must be a table"):
        sync.parse_pymarkdown('[tool.pymarkdown]\nplugins = "nope"\n')


def test_parse_pymarkdown_rejects_bad_rule_key() -> None:
    with pytest.raises(ValueError, match="unexpected pymarkdown rule key"):
        sync.parse_pymarkdown("[tool.pymarkdown]\nplugins.notarule.enabled = false\n")


def test_parse_pymarkdown_rejects_non_table_rule_settings() -> None:
    with pytest.raises(ValueError, match="must be a table"):
        sync.parse_pymarkdown('[tool.pymarkdown]\nplugins.md013 = "false"\n')


# --- parse_markdownlint ---------------------------------------------------


def test_parse_markdownlint_scalars_and_nested_params() -> None:
    yaml = """
# a comment
default: true

MD013: false   # inline comment ignored
MD024:
  siblings_only: true
"""
    default, rules = sync.parse_markdownlint(yaml)
    assert default is True
    assert rules["MD013"] == {"enabled": False, "params": {}}
    assert rules["MD024"] == {"enabled": True, "params": {"siblings_only": True}}


def test_parse_markdownlint_rejects_named_alias() -> None:
    with pytest.raises(ValueError, match="unexpected markdownlint rule key"):
        sync.parse_markdownlint("default: true\nline-length: false\n")


def test_parse_markdownlint_rejects_line_without_colon() -> None:
    with pytest.raises(ValueError, match="malformed"):
        sync.parse_markdownlint("default: true\nMD013 false\n")


def test_parse_markdownlint_rejects_indented_top_level_key() -> None:
    with pytest.raises(ValueError, match="indented key"):
        sync.parse_markdownlint("  MD013: false\n")


def test_parse_markdownlint_rejects_non_boolean_default() -> None:
    with pytest.raises(ValueError, match="'default' must be a boolean"):
        sync.parse_markdownlint("default: maybe\n")


def test_parse_markdownlint_rejects_non_boolean_rule_scalar() -> None:
    with pytest.raises(ValueError, match="must be boolean"):
        sync.parse_markdownlint("default: true\nMD013: maybe\n")


def test_parse_markdownlint_coerces_scalar_param_types() -> None:
    # Exercises the scalar coercion branches (int, quoted string) through the
    # public parser rather than the private helper.
    _, rules = sync.parse_markdownlint(
        'default: true\nMD013:\n  line_length: 100\n  style: "relaxed"\n'
    )
    assert rules["MD013"]["params"] == {"line_length": 100, "style": "relaxed"}


def test_parse_markdownlint_keeps_inline_comment_out_of_value() -> None:
    # A trailing " #..." comment is stripped; the value is unaffected.
    _, rules = sync.parse_markdownlint("default: true\nMD013: false   # house style\n")
    assert rules["MD013"] == {"enabled": False, "params": {}}


def test_parse_markdownlint_rejects_hash_in_value() -> None:
    # A '#'-bearing scalar (quoted-hash) is unsupported: fail loud, do not
    # silently truncate it to an empty/partial value (F5).
    with pytest.raises(ValueError, match="unsupported '#'"):
        sync.parse_markdownlint("default: true\nMD013:\n  heading_style: '#'\n")


# --- diff_configs ---------------------------------------------------------


def test_diff_configs_agree() -> None:
    pym: dict[str, sync.Decision] = {"MD013": {"enabled": False, "params": {}}}
    mdl: dict[str, sync.Decision] = {"MD013": {"enabled": False, "params": {}}}
    assert sync.diff_configs(pym, True, mdl) == []


def test_diff_configs_rule_disabled_in_only_one() -> None:
    # Disabled in pyproject, absent (enabled-by-default) in markdownlint. The
    # message must attribute each side to the right file, so an argument swap
    # cannot masquerade as a correct report.
    pym: dict[str, sync.Decision] = {"MD032": {"enabled": False, "params": {}}}
    errors = sync.diff_configs(pym, True, {})
    assert len(errors) == 1
    assert "MD032" in errors[0]
    assert "pyproject.toml=disabled" in errors[0]
    assert ".markdownlint.yaml=enabled" in errors[0]


def test_diff_configs_param_mismatch() -> None:
    pym: dict[str, sync.Decision] = {
        "MD024": {"enabled": True, "params": {"siblings_only": True}}
    }
    mdl: dict[str, sync.Decision] = {
        "MD024": {"enabled": True, "params": {"siblings_only": False}}
    }
    errors = sync.diff_configs(pym, True, mdl)
    assert len(errors) == 1
    assert "MD024" in errors[0]
    assert "pyproject.toml=enabled(siblings_only=True)" in errors[0]
    assert ".markdownlint.yaml=enabled(siblings_only=False)" in errors[0]


def test_diff_configs_disabled_both_sides_ignores_stray_param() -> None:
    # A rule disabled on both sides is equal regardless of a stray param: params
    # are meaningless once a rule is off. Otherwise the report would read the
    # uninterpretable "disabled but disabled" (F1).
    pym: dict[str, sync.Decision] = {
        "MD013": {"enabled": False, "params": {"line_length": 100}}
    }
    mdl: dict[str, sync.Decision] = {"MD013": {"enabled": False, "params": {}}}
    assert sync.diff_configs(pym, True, mdl) == []


def test_diff_configs_flags_non_true_default() -> None:
    # A false markdownlint baseline has no PyMarkdown equivalent -> divergence.
    errors = sync.diff_configs({}, False, {})
    assert any("default" in e for e in errors)


def test_diff_configs_absent_rule_baseline_follows_mdl_default() -> None:
    # With default:false, a rule enabled in pyproject but absent from markdownlint
    # is genuinely disabled there -> a real divergence, reported independently of
    # the default-guard message. Under the old always-enabled baseline this rule
    # would have compared equal and been missed (F4).
    pym: dict[str, sync.Decision] = {"MD050": {"enabled": True, "params": {}}}
    errors = sync.diff_configs(pym, False, {})
    assert any("MD050" in e for e in errors)
    assert any("default" in e for e in errors)


# --- check() wiring over synthetic configs --------------------------------


def _point_at(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, pyproject: str, markdownlint: str
) -> None:
    """Redirect check()'s two file globals at throwaway configs under tmp_path."""
    pyproject_file = tmp_path / "pyproject.toml"
    markdownlint_file = tmp_path / ".markdownlint.yaml"
    pyproject_file.write_text(pyproject, encoding="utf-8")
    markdownlint_file.write_text(markdownlint, encoding="utf-8")
    monkeypatch.setattr(sync, "PYPROJECT", pyproject_file)
    monkeypatch.setattr(sync, "MARKDOWNLINT_YAML", markdownlint_file)


def test_check_reports_divergence_with_correct_attribution(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # MD013 disabled in pyproject but enabled (absent) in markdownlint: check()
    # must surface the divergence AND attribute each side to its own file. A
    # swapped-argument bug in check()'s diff_configs call would flip the
    # attribution, so this catches wiring regressions the in-sync live check
    # (below) cannot.
    _point_at(
        monkeypatch,
        tmp_path,
        "[tool.pymarkdown]\nplugins.md013.enabled = false\n",
        "default: true\n",
    )
    errors = sync.check()
    assert len(errors) == 1
    assert "MD013" in errors[0]
    assert "pyproject.toml=disabled" in errors[0]
    assert ".markdownlint.yaml=enabled" in errors[0]


def test_check_passes_when_synthetic_configs_agree(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _point_at(
        monkeypatch,
        tmp_path,
        "[tool.pymarkdown]\nplugins.md013.enabled = false\n",
        "default: true\nMD013: false\n",
    )
    assert sync.check() == []


# --- live contract: the real repo configs agree ---------------------------


def test_repo_configs_are_in_sync() -> None:
    assert sync.check() == []

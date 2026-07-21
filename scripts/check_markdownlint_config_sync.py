#!/usr/bin/env python3
r"""Verify the two markdown-lint configs encode one intent (ADR-0062, option E).

The markdown style gate is driven by *two* config files that must agree:

  - ``pyproject.toml`` ``[tool.pymarkdown]`` -- read by our CI gate (PyMarkdown).
  - ``.markdownlint.yaml`` (repo root) -- read by CodeRabbit's markdownlint.

If a rule is enabled/disabled or parameterized in one but not the other, our
gate goes green while CodeRabbit keeps flagging the rule (or vice versa) -- the
exact recurring AI-reviewer noise ADR-0062 exists to remove. Comment discipline
(a header in each file) is the human guard; this script is the mechanized one,
in the spirit of testing-strategy.md (don't trust to vigilance what a gate can
enforce). It normalizes both files to a canonical ``rule -> decision`` map and
fails if the maps diverge.

Canonical decision: a rule is either *disabled*, or *enabled* with a (possibly
empty) set of parameters. A rule mentioned in neither file is enabled-by-default
in both linters, so only rules named in at least one file are compared.

Two PyMarkdown-only settings have no markdownlint mirror and are excluded by
design (ADR-0062 §3): ``mode.strict-config`` (governs how PyMarkdown reacts to
its *own* malformed config) and ``extensions.front-matter`` (markdownlint parses
front matter natively). They live under the ``mode`` / ``extensions`` tables;
only the ``plugins`` table carries shared rule decisions. Any *other* top-level
key under ``[tool.pymarkdown]`` fails the check -- a new PyMarkdown-only concept
must be added to the exclusion list consciously, not silently tolerated.

Accepted limitations for this corpus (documented so a future widening is
deliberate):
  - ``.markdownlint.yaml`` is hand-parsed (stdlib has no YAML module), handling
    exactly the shapes this mirror uses: top-level ``key: scalar`` and one level
    of indented ``param: scalar``. A value containing ``#`` is unsupported (the
    ``#`` would be read as a comment); the mirror uses none.
  - Rule keys must be numeric ``MDxxx`` on both sides; markdownlint's named
    aliases (``line-length``) are not resolved -- the corpus uses none, and a
    stray alias fails loudly rather than being silently skipped.
  - Parameter *names* must match across both linters (they share the ``MDxxx``
    vocabulary; ``MD024.siblings_only`` is identical in both). A rule whose two
    linters name the same knob differently would need a rename map here.

Exit code 0 when the two configs agree; 1 with one line per divergence otherwise.
Stdlib only; both files are read as UTF-8.
"""

from __future__ import annotations

import re
import sys
import tomllib
from pathlib import Path
from typing import TypedDict, cast

REPO_ROOT = Path(__file__).resolve().parent.parent
PYPROJECT = REPO_ROOT / "pyproject.toml"
MARKDOWNLINT_YAML = REPO_ROOT / ".markdownlint.yaml"

# Top-level tables under [tool.pymarkdown] that are PyMarkdown-only (no
# markdownlint mirror) and so carry no shared rule decision (ADR-0062 §3).
PYMARKDOWN_ONLY_TABLES = frozenset({"mode", "extensions"})
# The table that holds per-rule decisions.
PLUGINS_TABLE = "plugins"

RULE_RE = re.compile(r"^MD\d+$")


class Decision(TypedDict):
    """A rule's canonical state: enabled/disabled plus its parameters.

    A parameter value is whatever scalar the config holds (bool/int/str); it is
    kept as ``object`` because it is only ever compared and repr'd, never
    arithmetic'd.
    """

    enabled: bool
    params: dict[str, object]


def _norm(decision: Decision) -> tuple[bool, tuple[tuple[str, object], ...]]:
    """A hashable, order-independent form of a decision, for equality.

    "Disabled" is terminal: a disabled rule's parameters carry no meaning (the
    rule is off in both linters regardless), so they are dropped here -- matching
    _describe, which renders a disabled rule as just "disabled". Counting params
    for a disabled rule would report a divergence that _describe then renders as
    the uninterpretable "disabled but disabled".
    """
    if not decision["enabled"]:
        return (False, ())
    params = decision["params"]
    return (True, tuple(sorted(params.items(), key=lambda kv: kv[0])))


def _describe(decision: Decision) -> str:
    if not decision["enabled"]:
        return "disabled"
    params = decision["params"]
    if not params:
        return "enabled"
    inner = ", ".join(f"{k}={v!r}" for k, v in sorted(params.items()))
    return f"enabled({inner})"


def parse_pymarkdown(pyproject_toml: str) -> dict[str, Decision]:
    """Rule -> decision from a pyproject.toml's [tool.pymarkdown] block."""
    data = tomllib.loads(pyproject_toml)
    section = cast("dict[str, object]", data.get("tool", {}).get("pymarkdown", {}))
    rules: dict[str, Decision] = {}
    for key, value in section.items():
        if key in PYMARKDOWN_ONLY_TABLES:
            continue  # PyMarkdown-only; no shared decision (ADR-0062 §3)
        if key != PLUGINS_TABLE:
            raise ValueError(
                f"[tool.pymarkdown] has unexpected top-level key {key!r}; if it "
                "is a new PyMarkdown-only setting, add it to PYMARKDOWN_ONLY_TABLES"
            )
        if not isinstance(value, dict):
            raise ValueError(f"[tool.pymarkdown].{key} must be a table")
        plugins = cast("dict[str, object]", value)
        for raw_rule, settings in plugins.items():
            rule = raw_rule.upper()
            if not RULE_RE.match(rule):
                raise ValueError(f"unexpected pymarkdown rule key {raw_rule!r}")
            if not isinstance(settings, dict):
                raise ValueError(f"plugins.{raw_rule} must be a table")
            rule_settings = cast("dict[str, object]", settings)
            enabled = bool(rule_settings.get("enabled", True))
            params = {k: v for k, v in rule_settings.items() if k != "enabled"}
            rules[rule] = {"enabled": enabled, "params": params}
    return rules


def _coerce(scalar: str) -> object:
    """A YAML scalar as bool / int / str (quotes stripped)."""
    if scalar in ("true", "false"):
        return scalar == "true"
    if re.fullmatch(r"-?\d+", scalar):
        return int(scalar)
    if len(scalar) >= 2 and scalar[0] == scalar[-1] and scalar[0] in "\"'":
        return scalar[1:-1]
    return scalar


def _tokenize_yaml(text: str) -> list[tuple[int, str, str]]:
    """(indent, key, value) for each non-blank, non-comment ``key: value`` line."""
    tokens: list[tuple[int, str, str]] = []
    for raw in text.splitlines():
        if not raw.strip() or raw.lstrip().startswith("#"):
            continue
        indent = len(raw) - len(raw.lstrip(" "))
        # Strip a trailing " #..." comment (space then hash). A '#' that survives
        # in the value is a quoted-hash scalar (e.g. heading_style: '#') this
        # parser does not support -- fail loud below rather than truncate it
        # silently, consistent with the module's stance on other bad shapes.
        comment = raw.find(" #")
        content = (raw if comment == -1 else raw[:comment]).strip()
        if not content:
            continue
        if ":" not in content:
            raise ValueError(f"malformed .markdownlint.yaml line: {raw!r}")
        key, _, value = content.partition(":")
        key, value = key.strip(), value.strip()
        if "#" in value:
            raise ValueError(f"unsupported '#' in .markdownlint.yaml value: {raw!r}")
        tokens.append((indent, key, value))
    return tokens


def parse_markdownlint(yaml_text: str) -> tuple[bool, dict[str, Decision]]:
    """(default-enabled, {rule -> decision}) from a .markdownlint.yaml mirror."""
    tokens = _tokenize_yaml(yaml_text)
    default = True
    rules: dict[str, Decision] = {}
    i = 0
    while i < len(tokens):
        indent, key, value = tokens[i]
        if indent != 0:
            raise ValueError(f"unexpected indented key {key!r} at top level")
        if key == "default":
            coerced = _coerce(value)
            if not isinstance(coerced, bool):
                raise ValueError(f"'default' must be a boolean, got {value!r}")
            default = coerced
            i += 1
            continue
        rule = key.upper()
        if not RULE_RE.match(rule):
            raise ValueError(f"unexpected markdownlint rule key {key!r}")
        if value == "":
            params: dict[str, object] = {}
            i += 1
            while i < len(tokens) and tokens[i][0] > indent:
                _, pk, pv = tokens[i]
                params[pk] = _coerce(pv)
                i += 1
            rules[rule] = {"enabled": True, "params": params}
        else:
            coerced = _coerce(value)
            if not isinstance(coerced, bool):
                raise ValueError(f"rule {key!r} scalar must be boolean, got {value!r}")
            rules[rule] = {"enabled": coerced, "params": {}}
            i += 1
    return default, rules


def diff_configs(
    pym_rules: dict[str, Decision],
    mdl_default: bool,
    mdl_rules: dict[str, Decision],
) -> list[str]:
    """Human-readable divergences between the two canonical maps (empty if none)."""
    errors: list[str] = []
    if mdl_default is not True:
        errors.append(
            "'.markdownlint.yaml' 'default' is not true; PyMarkdown has no "
            "disable-all default, so a false baseline silently diverges"
        )
    # An unlisted rule is enabled-by-default in PyMarkdown, and enabled-or-not
    # per `default:` in markdownlint. Deriving each side's absent baseline from
    # its own default keeps the per-rule comparison correct on its own, rather
    # than leaning on the `mdl_default is not True` guard above to mask a wrong
    # (always-enabled) markdownlint baseline.
    pym_absent: Decision = {"enabled": True, "params": {}}
    mdl_absent: Decision = {"enabled": mdl_default, "params": {}}
    for rule in sorted(set(pym_rules) | set(mdl_rules)):
        pym = pym_rules.get(rule, pym_absent)
        mdl = mdl_rules.get(rule, mdl_absent)
        if _norm(pym) != _norm(mdl):
            errors.append(
                f"{rule}: pyproject.toml={_describe(pym)} but "
                f".markdownlint.yaml={_describe(mdl)}"
            )
    return errors


def check() -> list[str]:
    pym_rules = parse_pymarkdown(PYPROJECT.read_text(encoding="utf-8"))
    mdl_default, mdl_rules = parse_markdownlint(
        MARKDOWNLINT_YAML.read_text(encoding="utf-8")
    )
    return diff_configs(pym_rules, mdl_default, mdl_rules)


def main() -> int:
    errors = check()
    if errors:
        print(f"markdown-lint config drift ({len(errors)} divergence(s)):")
        for e in errors:
            print(f"  - {e}")
        print(
            "the two configs must encode one intent (ADR-0062): fix pyproject.toml "
            "[tool.pymarkdown] and .markdownlint.yaml to agree."
        )
        return 1
    print(
        "markdown-lint configs consistent: pyproject.toml and .markdownlint.yaml agree."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())

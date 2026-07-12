#!/usr/bin/env python3
"""Log canary gate: fail CI if any synthetic fixture health value appears in
captured logs.

Mechanizes observability.md's "never log health data values" prohibition, per
specs/testing-strategy.md (CI Gates -> Log canary gate). The canary manifest --
the complete list of synthetic health values in the fixtures -- is derived by
the fixture loader (``tests/fixture_loader.py``) from the parsed typed fixture
records; this script consumes that manifest and scans captured log output for
any hit. Deriving the manifest from parsed records rather than raw fixture text
closes the drift the Phase-0 interim regex derivation risked, which this script
previously carried (open-questions.md, Testing -- resolved by the loader).

Matching is boundary-anchored: a manifest decimal never matches inside a longer
number (``104.73921`` does not hit ``1104.739215``), and a token never matches
as a prefix of, or inside, a longer alphanumeric run.

Usage: scan_log_canary.py LOG_FILE [LOG_FILE ...]

Exit codes: 0 clean; 1 canary hit(s); 2 usage or fixture-tree error. An empty
fixture tree yields an empty manifest and a trivially green gate; the gate has
teeth the moment the first fixture lands.
"""

import re
import sys
from pathlib import Path

_DECIMAL_SHAPE = re.compile(r"\d+\.\d+")


def compile_manifest_pattern(manifest: set[str]) -> re.Pattern[str] | None:
    """One alternation over the manifest, each value boundary-anchored."""
    if not manifest:
        return None
    parts: list[str] = []
    for value in sorted(manifest, key=len, reverse=True):
        escaped = re.escape(value)
        if _DECIMAL_SHAPE.fullmatch(value):
            parts.append(rf"(?<!\d){escaped}(?!\d)")
        else:
            # Reject a match only when the token genuinely CONTINUES into a
            # longer token — another token char (`[A-Za-z0-9_-]`) or a dotted
            # component (`.x`). A trailing sentence period or dash is a
            # boundary, not a continuation, so a leaked token at a sentence end
            # ("...CANARY-foo.") must still hit. A longer real token is caught
            # by its own (longer, tried-first) manifest entry.
            parts.append(
                rf"(?<![A-Za-z0-9_]){escaped}(?![A-Za-z0-9_-])(?!\.[A-Za-z0-9_-])"
            )
    return re.compile("|".join(parts))


def scan(log_paths: list[Path], manifest: set[str]) -> list[tuple[Path, int, str, str]]:
    """Return (file, line number, canary value, line) for every hit."""
    pattern = compile_manifest_pattern(manifest)
    hits: list[tuple[Path, int, str, str]] = []
    if pattern is None:
        return hits
    for log_path in log_paths:
        with log_path.open(encoding="utf-8", errors="replace") as f:
            for lineno, line in enumerate(f, start=1):
                for match in pattern.finditer(line):
                    hits.append((log_path, lineno, match.group(0), line.rstrip("\n")))
    return hits


def main(argv: list[str]) -> int:
    if not argv:
        print("usage: scan_log_canary.py LOG_FILE [LOG_FILE ...]", file=sys.stderr)
        return 2
    log_paths = [Path(a) for a in argv]
    missing = [p for p in log_paths if not p.is_file()]
    if missing:
        print(f"log files not found: {missing}", file=sys.stderr)
        return 2

    # The fixture loader (tests/) owns manifest derivation; import it lazily so
    # that path is only taken when scanning, not merely importing this module.
    tests_dir = Path(__file__).resolve().parent.parent / "tests"
    if str(tests_dir) not in sys.path:
        sys.path.insert(0, str(tests_dir))
    import fixture_loader

    try:
        manifest = fixture_loader.build_manifest()
    except fixture_loader.FixtureError as exc:
        print(f"canary manifest derivation failed: {exc}", file=sys.stderr)
        return 2
    print(
        f"canary manifest: {len(manifest)} value(s) derived from "
        f"{fixture_loader.FIXTURES_DIR}"
    )

    hits = scan(log_paths, manifest)
    if hits:
        print(
            f"FAIL: {len(hits)} canary hit(s) in captured log output:",
            file=sys.stderr,
        )
        for path, lineno, value, line in hits:
            print(f"  {path}:{lineno}: canary {value!r} in: {line}", file=sys.stderr)
        return 1

    print("log canary gate: no fixture health values found in captured logs")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

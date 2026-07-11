#!/usr/bin/env python3
"""Log canary gate: fail CI if any fixture health value appears in captured logs.

Mechanizes observability.md's "never log health data values" prohibition, per
specs/testing-strategy.md (CI Gates -> Log canary gate). The canary manifest is
derived programmatically from the fixture files themselves -- there is no
hand-maintained list to drift out of sync. (Interim derivation: once the
Phase-1 fixture loader exists it owns the manifest -- see specs/
open-questions.md, Testing.)

Manifest derivation (testing-strategy.md, "Canary rule"):
- every ``CANARY-`` marker token embedded in fixture text fields
- every high-entropy decimal literal (>= 6 significant digits) -- the required
  form for synthetic numeric health values. Decimals embedded in larger
  constructs (timestamps like ``08:30:00.123456``, dotted version strings)
  are excluded: they are infrastructure-shaped, not health values, and would
  only produce false-positive gate failures.

Matching is anchored: a manifest decimal never matches inside a longer number
(``104.73921`` does not hit ``1104.739215``), and a token never matches as a
prefix of a longer token.

Fixtures are JSON or SQL text files (testing-strategy.md, Fixture management).
Any other file under tests/fixtures/ fails the gate loudly rather than being
silently skipped -- silent skips would under-derive the manifest.

Usage: scan_log_canary.py LOG_FILE [LOG_FILE ...]

Exit codes: 0 clean; 1 canary hit(s); 2 usage or fixture-tree error. An empty
fixture tree yields an empty manifest and a trivially green gate; the gate
gains teeth the moment the first fixture lands (Phase 1).
"""

import re
import sys
from pathlib import Path

FIXTURES_DIR = Path(__file__).resolve().parent.parent / "tests" / "fixtures"
FIXTURE_SUFFIXES = {".json", ".sql"}

# Dots allowed interior (never leading/trailing), so a sentence-final period
# after a token is not captured but a dotted token survives intact.
CANARY_TOKEN = re.compile(r"CANARY-[A-Za-z0-9_-]+(?:\.[A-Za-z0-9_-]+)*")
# A standalone decimal literal: not preceded by a digit, ':' (time seconds),
# or '.' (version strings); not followed by a digit or a further dotted
# component. ``2026-01-15T08:30:00.123456`` and ``1.2.3`` derive nothing.
HIGH_ENTROPY_DECIMAL = re.compile(r"(?<![\d:.])\d+\.\d+(?![\d.])")

_DECIMAL_SHAPE = re.compile(r"\d+\.\d+")


def _significant_digits(literal: str) -> int:
    # Leading and trailing zeros are both entropy-free: 100.000 has one
    # significant digit and must not enter the manifest.
    return len(literal.replace(".", "").strip("0"))


def build_manifest(fixtures_dir: Path) -> set[str]:
    """Derive the canary manifest from every fixture file.

    Raises ValueError if a non-JSON/SQL file is present -- the derivation
    only understands the spec's fixture formats, and skipping a file
    silently would leave its canary values unguarded.
    """
    manifest: set[str] = set()
    if not fixtures_dir.is_dir():
        return manifest
    unexpected: list[Path] = []
    for path in sorted(fixtures_dir.rglob("*")):
        if not path.is_file():
            continue
        if path.suffix not in FIXTURE_SUFFIXES:
            unexpected.append(path)
            continue
        text = path.read_text(encoding="utf-8")
        manifest.update(CANARY_TOKEN.findall(text))
        manifest.update(
            m for m in HIGH_ENTROPY_DECIMAL.findall(text) if _significant_digits(m) >= 6
        )
    if unexpected:
        names = ", ".join(str(p) for p in unexpected)
        msg = (
            f"unexpected non-fixture file(s) under {fixtures_dir}: {names} "
            f"(fixtures are JSON or SQL; extend FIXTURE_SUFFIXES deliberately)"
        )
        raise ValueError(msg)
    return manifest


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
            parts.append(rf"(?<![A-Za-z0-9_]){escaped}(?![A-Za-z0-9_.-])")
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

    try:
        manifest = build_manifest(FIXTURES_DIR)
    except ValueError as exc:
        print(f"canary manifest derivation failed: {exc}", file=sys.stderr)
        return 2
    print(f"canary manifest: {len(manifest)} value(s) derived from {FIXTURES_DIR}")

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

#!/usr/bin/env python3
"""Log canary gate: fail CI if any fixture health value appears in captured logs.

Mechanizes observability.md's "never log health data values" prohibition, per
specs/testing-strategy.md (CI Gates -> Log canary gate). The canary manifest is
derived programmatically from the fixture files themselves -- there is no
hand-maintained list to drift out of sync.

Manifest derivation (testing-strategy.md, "Canary rule"):
- every ``CANARY-`` marker token embedded in fixture text fields
- every high-entropy decimal literal (>= 6 significant digits) -- the required
  form for synthetic numeric health values, chosen so they cannot collide with
  timestamps, ports, or status codes

Usage: scan_log_canary.py LOG_FILE [LOG_FILE ...]

Exits 1 on any hit, printing the matched canary value and the offending line.
An empty fixture tree yields an empty manifest and a trivially green gate; the
gate gains teeth the moment the first fixture lands (Phase 1).
"""

import re
import sys
from pathlib import Path

FIXTURES_DIR = Path(__file__).resolve().parent.parent / "tests" / "fixtures"

CANARY_TOKEN = re.compile(r"CANARY-[A-Za-z0-9_-]+")
# A decimal with >= 6 significant digits, e.g. 104.73921 -- the canary-rule
# form for numeric health values in fixtures.
HIGH_ENTROPY_DECIMAL = re.compile(r"\d+\.\d+")


def _significant_digits(literal: str) -> int:
    return len(literal.replace(".", "").lstrip("0"))


def build_manifest(fixtures_dir: Path) -> set[str]:
    """Derive the canary manifest from every file under tests/fixtures/."""
    manifest: set[str] = set()
    if not fixtures_dir.is_dir():
        return manifest
    for path in sorted(fixtures_dir.rglob("*")):
        if not path.is_file():
            continue
        text = path.read_text(encoding="utf-8")
        manifest.update(CANARY_TOKEN.findall(text))
        manifest.update(
            m for m in HIGH_ENTROPY_DECIMAL.findall(text) if _significant_digits(m) >= 6
        )
    return manifest


def scan(log_paths: list[Path], manifest: set[str]) -> list[tuple[Path, int, str, str]]:
    """Return (file, line number, canary value, line) for every hit."""
    hits: list[tuple[Path, int, str, str]] = []
    for log_path in log_paths:
        with log_path.open(encoding="utf-8", errors="replace") as f:
            for lineno, line in enumerate(f, start=1):
                for value in manifest:
                    if value in line:
                        hits.append((log_path, lineno, value, line.rstrip("\n")))
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

    manifest = build_manifest(FIXTURES_DIR)
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

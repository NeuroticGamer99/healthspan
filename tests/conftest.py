"""Test-suite bootstrap: make scripts/ importable for CI-gate script tests."""

import sys
from collections.abc import Callable
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))


@pytest.fixture
def write_file() -> Callable[[Path, str], Path]:
    """Write a UTF-8 text file and return its path (shared authoring helper)."""

    def _write(path: Path, content: str) -> Path:
        path.write_text(content, encoding="utf-8")
        return path

    return _write

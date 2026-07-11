"""Test-suite bootstrap: make scripts/ importable for CI-gate script tests."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

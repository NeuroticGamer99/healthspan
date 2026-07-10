"""Phase 0 skeleton test: the package imports and its entry point runs.

Exists so the 3-OS CI test matrix has something real to execute from the
first code PR (specs/development-plan.md, Phase 0). Replaced by real
suites from Phase 1 onward.
"""

import pytest

import healthspan


def test_main_prints_greeting(capsys: pytest.CaptureFixture[str]) -> None:
    healthspan.main()
    assert "healthspan" in capsys.readouterr().out

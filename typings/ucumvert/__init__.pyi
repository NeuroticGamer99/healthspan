"""Minimal local stub for the untyped ``ucumvert`` UCUM→pint engine.

Covers only the surface the internal units module ([`src/healthspan/units.py`])
uses; extend as call sites grow. Parsed quantities are ``pint`` objects the units
module keeps deliberately opaque (``Any``), so no pint type is imported here.
"""

from typing import Any

class InvalidUcumError(Exception): ...

class PintUcumRegistry:
    def __init__(self) -> None: ...
    def from_ucum(self, ucum_code: str) -> Any: ...

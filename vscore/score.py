"""The v1 formula as pure functions (scoring spec v1: Formula, Codebase total, Missing is not zero)."""

from __future__ import annotations

import math


def v_points(w: float | None, d: int | None, alpha: float) -> float | None:
    """V(f) = log2(1 + W) + alpha * log2(1 + D); None if either part is missing, never filled in with 0."""
    if w is None or d is None:
        return None
    return math.log2(1 + w) + alpha * math.log2(1 + d)


def confidence(w: float | None, d: int | None, partial_history: bool = False) -> str:
    measured = (w is not None) + (d is not None)
    if measured == 0:
        return "none"
    if measured == 2 and not partial_history:
        return "full"
    return "partial"


def codebase_total(ws: list[float | None]) -> tuple[float, float]:
    """W_total sums each file's measured W once; V_total = log2(1 + W_total). Dependents are never summed."""
    w_total = sum(w for w in ws if w is not None)
    return w_total, math.log2(1 + w_total)


def r4(x: float | None) -> float | None:
    return None if x is None else round(x, 4)

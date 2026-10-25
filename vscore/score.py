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


def codebase_total(ws: list[float | None], sizes: list[int], reason: str | None = None) -> dict:
    """W_total sums each measured file's W once; V_total = log2(1 + W_total). Dependents are never summed.

    Missing is not zero: with no measured file the total is null, with the reason. Otherwise it carries
    its coverage, and is labeled partial when any eligible file lacks W.
    """
    measured = [(w, size) for w, size in zip(ws, sizes) if w is not None]
    if not measured:
        return {"W": None, "V": None, "reason": reason or "no_files", "partial": None,
                "files_measured": 0, "files_eligible": len(ws), "bytes_measured_share": 0.0}
    w_total = sum(w for w, _ in measured)
    all_bytes = sum(sizes)
    return {
        "W": w_total,
        "V": math.log2(1 + w_total),
        "partial": len(measured) < len(ws),
        "files_measured": len(measured),
        "files_eligible": len(ws),
        "bytes_measured_share": sum(size for _, size in measured) / all_bytes if all_bytes else 1.0,
    }


def r4(x: float | None) -> float | None:
    return None if x is None else round(x, 4)

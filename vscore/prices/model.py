"""Pure pricing functions: default bands, history refinement, and how they combine.

price(n) = min(restore(n), rebuild(n)) + harm(n), in engineering-hours. Committed code restores from
Git in minutes, so its price is mostly the harm done while it is broken; `at_risk` is the price if
no restore were available (a history rewrite, an untested backup), which the gate uses for
irreversible actions.
"""

from __future__ import annotations

import fnmatch
import math
from dataclasses import dataclass, field
from datetime import datetime

from vscore.adapters import files as f
from vscore.adapters.imports import is_code
from vscore.adapters.schema import Table
from vscore.config import Config
from vscore.evidence import UNVERIFIED, VERIFIED, Evidence

BAND_EDGES = (1.0, 10.0, 100.0, 1000.0)
DOC_EXTENSIONS = (".md", ".rst", ".txt", ".adoc")
ASSET_EXTENSIONS = (".svg", ".css", ".scss", ".less", ".html", ".htm", ".jinja", ".j2", ".ico")
CONFIG_EXTENSIONS = (".yml", ".yaml", ".toml", ".json", ".ini", ".cfg", ".conf", ".sh", ".lock")
BUILD_NAMES = ("Makefile", "Dockerfile", "Procfile", "Justfile")
_LOWERABLE_FREELY = {f.CODE, f.TEST}


def band(hours: float) -> int:
    """0: under 1 h, 1: 1-10 h, 2: 10-100 h, 3: 100-1,000 h, 4: over 1,000 h."""
    return sum(hours >= edge for edge in BAND_EDGES)


def default_file_band(cls: str, path: str, d: int | None, cfg: Config) -> int:
    """The new hire's instincts: a band from structure alone (plan: Default prices)."""
    if cls == f.CROWN_JEWEL:
        return cfg.crown_jewel_band
    if cls in (f.SCHEMA, f.SENSITIVE):
        return 3
    if cls == f.TEST or path.endswith(DOC_EXTENSIONS):
        return 0
    if is_code(path):
        return 2 if (d or 0) > 0 else 1
    name = path.rsplit("/", 1)[-1]
    if path.endswith(ASSET_EXTENSIONS):
        return 1
    if path.endswith(CONFIG_EXTENSIONS) or name in BUILD_NAMES or name.startswith("."):
        return 2  # configuration, CI and build files: breaking them stops work
    return 3  # not classified: unknown is expensive


def combine(default: int, history: int | None, cls: str, confident: bool) -> int:
    """History raises a default band freely but lowers it only when confident, and only one band for
    schema, sensitive and unclassified files. Crown jewels stay where the file pins them."""
    if cls == f.CROWN_JEWEL or history is None or history == default:
        return default
    if history > default:
        return history
    if not confident:
        return default
    return history if cls in _LOWERABLE_FREELY else max(history, default - 1)


@dataclass(frozen=True)
class Price:
    node: str
    kind: str
    cls: str
    band: int
    hours: float | None
    low: float | None
    high: float | None
    at_risk_hours: float | None
    at_risk_band: int
    default_band: int
    history_band: int | None
    confidence: str
    parts: dict = field(default_factory=dict)
    evidence: tuple[str, ...] = ()

    def to_dict(self) -> dict:
        r = lambda x: None if x is None else round(x, 4)
        return {
            "node": self.node, "kind": self.kind, "class": self.cls, "band": self.band, "hours": r(self.hours),
            "low": r(self.low), "high": r(self.high), "at_risk_hours": r(self.at_risk_hours),
            "at_risk_band": self.at_risk_band, "default_band": self.default_band, "history_band": self.history_band,
            "confidence": self.confidence, "parts": {k: r(v) for k, v in self.parts.items()},
            "evidence": list(self.evidence),
        }


def verified_work(contributions: list[tuple[str, float]], evidence: Evidence, cfg: Config) -> tuple[float, dict]:
    """W with work from commits a verification source saw but never verified scaled by unverified_weight."""
    total, counts = 0.0, {"verified": 0, "unverified": 0, "unknown": 0}
    for commit, work in contributions:
        status = evidence.verification(commit)
        counts[status] += 1
        total += work * (cfg.unverified_weight if status == UNVERIFIED else 1.0)
    return total, counts


def _spread(hours: float, calibration: dict | None) -> tuple[float, float]:
    factor = math.exp(1.96 * calibration["residual_sd"]) if calibration else 3.0
    return hours / factor, hours * factor


def price_file(path: str, cls: str, w: float | None, d: int | None, confidence: str,
               contributions: list[tuple[str, float]] | None, evidence: Evidence, cfg: Config,
               calibration: dict | None = None) -> Price:
    notes: list[str] = []
    parts: dict[str, float | None] = {}
    rebuild = harm = None
    if w is not None and contributions is not None:
        w_verified, counts = verified_work(contributions, evidence, cfg)
        rebuild = w_verified / cfg.bytes_per_hour
        parts["W_verified"] = w_verified
        notes.append(f"W {w:.0f} bytes from {len(contributions)} commits "
                     f"({counts[VERIFIED]} verified, {counts[UNVERIFIED]} unverified, {counts['unknown']} unknown)")
    usage = 1.0
    if path in evidence.traffic:
        usage = 1 + math.log2(1 + evidence.traffic[path] / cfg.traffic_reference)
        notes.append(f"{evidence.traffic[path]} requests")
    severities = evidence.incidents.get(path, [])
    incident_hours = sum(cfg.incident_hours[min(max(s, 1), 4) - 1] for s in severities)
    if severities:
        notes.append(f"{len(severities)} incidents (severity {', '.join(map(str, severities))})")
    if d is not None:
        harm = d * cfg.harm_hours_per_dependent * usage + incident_hours
        notes.append(f"D {d} direct importers")
    parts.update(rebuild=rebuild, harm=harm, restore=cfg.restore_hours)

    hours = at_risk = None
    calibrated = False
    if rebuild is not None and harm is not None:
        at_risk = rebuild + harm
        if calibration and w is not None:
            hours = calibration_predict(calibration, w, d)
            calibrated = True
        else:
            hours = min(cfg.restore_hours, rebuild) + harm
    default = default_file_band(cls, path, d, cfg)
    history = None if hours is None else band(hours)
    final = combine(default, history, cls, confidence == "full")
    low, high = _spread(hours, calibration if calibrated else None) if hours is not None else (None, None)
    at_risk_band = max(final, band(at_risk)) if at_risk is not None else final
    if calibrated:
        notes.append("hours from calibration")
    return Price(f"file:{path}", "file", cls, final, hours, low, high, at_risk, at_risk_band, default, history,
                 confidence, parts, tuple(notes))


def price_symbol(symbol: dict, file_cls: str, cfg: Config) -> Price:
    """A top-level function, class or constant: its share of the file's surviving work, and the harm of
    breaking the files that import it by name. Restorable from Git like its file."""
    w, d = symbol["W"], symbol["D"]
    rebuild = None if w is None else w / cfg.bytes_per_hour
    harm = d * cfg.harm_hours_per_dependent
    hours = None if rebuild is None else min(cfg.restore_hours, rebuild) + harm
    if file_cls in (f.CROWN_JEWEL, f.SCHEMA, f.SENSITIVE):
        default = default_file_band(file_cls, symbol["path"], d, cfg)
    else:
        default = 2 if d > 0 else (0 if file_cls == f.TEST else 1)
    history = None if hours is None else band(hours)
    final = combine(default, history, file_cls, w is not None)
    at_risk = None if rebuild is None else rebuild + harm
    notes = (f"{symbol['kind']} at lines {symbol['lines'][0]}-{symbol['lines'][1]}",
             f"imported by name in {d} files")
    return Price(symbol["node"], "symbol", file_cls, final, hours, None if hours is None else hours / 3,
                 None if hours is None else hours * 3, at_risk,
                 max(final, band(at_risk)) if at_risk is not None else final, default, history,
                 "full" if w is not None else "partial", {"rebuild": rebuild, "harm": harm, "restore": cfg.restore_hours},
                 notes)


def price_table(table: Table, evidence: Evidence, cfg: Config, now: str) -> Price:
    notes = [f"{table.rows} rows", f"{table.references_in} foreign keys point here"]
    harm = table.references_in * cfg.harm_hours_per_dependent
    if any(fnmatch.fnmatchcase(table.name.lower(), p) for p in cfg.disposable_tables):
        notes.append("disposable by name")
        return Price(table.node, "table", "disposable", 0, 0.1, 0.03, 0.3, 0.1, 0, 0, 0, "full",
                     {"harm": 0.0}, tuple(notes))
    if table.rows == 0:
        rebuild = 0.5  # recreating an empty table's schema
        restore = rebuild
        default = 1
    else:
        rebuild = cfg.table_rebuild_hours
        tested = evidence.restore_tests.get(table.node)
        fresh = tested is not None and _age_days(tested, now) <= cfg.restore_test_max_age_days
        restore = cfg.table_restore_hours if fresh else rebuild
        notes.append(f"restore tested {tested[:10]}" if fresh else "no recent tested restore")
        default = 4
    if table.personal_columns:
        notes.append(f"personal data columns: {', '.join(table.personal_columns)}")
    hours = min(restore, rebuild) + harm
    final = combine(default, band(hours), f.CODE, True)
    if table.personal_columns and table.rows:
        final = max(final, 2)
    at_risk = rebuild + harm
    return Price(table.node, "table", "table", final, hours, hours / 3, hours * 3, at_risk,
                 max(final, band(at_risk)), default, band(hours), "full",
                 {"rebuild": rebuild, "restore": restore, "harm": harm}, tuple(notes))


def _age_days(earlier: str, now: str) -> float:
    parse = lambda s: datetime.fromisoformat(s.replace("Z", "+00:00"))
    return (parse(now) - parse(earlier)).total_seconds() / 86400


def calibration_predict(calibration: dict, w: float, d: int | None) -> float:
    x1, x2 = math.log2(1 + w), math.log2(1 + (d or 0))
    return math.exp(calibration["b0"] + calibration["b1"] * x1 + calibration["b2"] * x2)


def fit_calibration(rows: list[tuple[float, int, float]]) -> dict:
    """Least squares of ln(hours) on log2(1 + W) and log2(1 + D). Needs at least five labeled files."""
    data = [(1.0, math.log2(1 + w), math.log2(1 + d), math.log(max(h, 1e-3))) for w, d, h in rows]
    if len(data) < 5:
        raise ValueError(f"calibration needs at least 5 labeled files, got {len(data)}")
    xtx = [[sum(r[i] * r[j] for r in data) for j in range(3)] for i in range(3)]
    xty = [sum(r[i] * r[3] for r in data) for i in range(3)]
    b = _solve(xtx, xty)
    predictions = [b[0] + b[1] * r[1] + b[2] * r[2] for r in data]
    mean = sum(r[3] for r in data) / len(data)
    ss_res = sum((r[3] - p) ** 2 for r, p in zip(data, predictions))
    ss_tot = sum((r[3] - mean) ** 2 for r in data)
    return {
        "b0": b[0], "b1": b[1], "b2": b[2], "n": len(data),
        "r2": 1 - ss_res / ss_tot if ss_tot else 0.0,
        "residual_sd": math.sqrt(ss_res / max(len(data) - 3, 1)),
    }


def _solve(a: list[list[float]], b: list[float]) -> list[float]:
    """Gaussian elimination with partial pivoting; a tiny ridge keeps a constant column solvable."""
    n = len(b)
    m = [row[:] + [b[i]] for i, row in enumerate(a)]
    for i in range(n):
        m[i][i] += 1e-9
    for col in range(n):
        pivot = max(range(col, n), key=lambda r: abs(m[r][col]))
        m[col], m[pivot] = m[pivot], m[col]
        for r in range(col + 1, n):
            factor = m[r][col] / m[col][col]
            for c in range(col, n + 1):
                m[r][c] -= factor * m[col][c]
    x = [0.0] * n
    for r in range(n - 1, -1, -1):
        x[r] = (m[r][n] - sum(m[r][c] * x[c] for c in range(r + 1, n))) / m[r][r]
    return x

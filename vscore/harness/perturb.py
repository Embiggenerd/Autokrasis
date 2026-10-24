"""Deletes one file at a time in a sandbox copy and measures the loss: tests that stop passing.

Measured loss in hours = restore_hours + failing tests * hours_per_failing_test. It is a crude label,
but it lets phases 1 and 2 check whether prices rank files by the damage their removal causes better
than churn or size do, and gives `vscore calibrate` something to fit.
"""

from __future__ import annotations

import csv
import io
import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
import xml.etree.ElementTree as ET
from dataclasses import asdict, dataclass
from pathlib import Path

from vscore.adapters.files import is_test
from vscore.adapters.imports import is_code
from vscore.baseline import churn, spearman
from vscore.config import Config
from vscore.gitrepo import Repo
from vscore.prices.model import band
from vscore.prices.run import price_repo


@dataclass(frozen=True)
class Case:
    path: str
    failing: int
    measured_hours: float
    measured_band: int
    V: float | None
    W: float | None
    D: int | None
    price: float | None
    band: int
    default_band: int
    churn: int
    size: int


def passing_tests(workdir: Path, timeout: int) -> set[str]:
    """Test ids that pass when pytest runs in `workdir`; none if pytest cannot even start (no report)."""
    report = workdir.parent / "junit.xml"
    report.unlink(missing_ok=True)
    subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", "--continue-on-collection-errors",
         f"--junitxml={report}"],
        cwd=workdir, capture_output=True, timeout=timeout, env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
    )
    if not report.exists():
        return set()
    passed = set()
    for case in ET.parse(report).getroot().iter("testcase"):
        if not any(child.tag in ("failure", "error", "skipped") for child in case):
            passed.add(f"{case.get('classname')}::{case.get('name')}")
    return passed


def perturb(repo_path, rev: str = "HEAD", cfg: Config | None = None, timeout: int = 300,
            calibration: dict | None = None) -> tuple[list[Case], int]:
    """One case per eligible non-test code file, plus the number of tests passing before any deletion."""
    cfg = cfg or Config()
    priced = price_repo(repo_path, rev, cfg, calibration=calibration)
    repo = Repo(repo_path)
    revision = priced.data["prices"]["revision"]
    files = [f for f in priced.scan.data["files"] if is_code(f["path"]) and not is_test(f["path"])]
    churns = churn(repo, revision, [f["path"] for f in files])

    with tempfile.TemporaryDirectory(prefix="vscore-perturb-") as tmp:
        base = Path(tmp) / "base"
        with tarfile.open(fileobj=io.BytesIO(repo.run("archive", "--format=tar", revision))) as archive:
            archive.extractall(base, filter="data")
        baseline = passing_tests(base, timeout)
        cases = []
        for f in files:
            work = Path(tmp) / "case"
            shutil.copytree(base, work)
            target = work / f["path"]
            size = len(target.read_bytes().splitlines())
            target.unlink()
            failing = len(baseline - passing_tests(work, timeout))
            shutil.rmtree(work)
            measured = cfg.restore_hours + failing * cfg.hours_per_failing_test
            price = priced.prices[f"file:{f['path']}"]
            cases.append(Case(f["path"], failing, measured, band(measured), f["V"], f["W"], f["D"], price.hours,
                              price.band, price.default_band, churns[f["path"]], size))
    return cases, len(baseline)


MEASURES = ("V", "W", "D", "price", "band", "default_band", "churn", "size")


def correlations(cases: list[Case]) -> dict[str, float | None]:
    """Spearman correlation of each measure with measured loss, over cases where the measure is known."""
    result = {}
    for measure in MEASURES:
        known = [c for c in cases if getattr(c, measure) is not None]
        result[measure] = spearman([getattr(c, measure) for c in known], [c.measured_hours for c in known])
    return result


def band_metrics(cases: list[Case]) -> dict[str, float | int | None]:
    """Miss rate: share of high-loss files (measured band 3 or 4) priced at band 0 or 1.
    Band accuracy: share of files priced within one band of their measured loss."""
    high = [c for c in cases if c.measured_band >= 3]
    return {
        "high_loss_files": len(high),
        "miss_rate": sum(c.band <= 1 for c in high) / len(high) if high else None,
        "band_accuracy": sum(abs(c.band - c.measured_band) <= 1 for c in cases) / len(cases) if cases else None,
    }


def write_labels(cases: list[Case], path: str | Path) -> None:
    """Writes path, W, D and measured hours, the input `vscore calibrate` fits."""
    with open(path, "w", newline="") as out:
        writer = csv.writer(out)
        writer.writerow(["path", "W", "D", "measured_hours"])
        for c in cases:
            if c.W is not None and c.D is not None:
                writer.writerow([c.path, c.W, c.D, c.measured_hours])


def read_labels(path: str | Path) -> list[tuple[float, int, float]]:
    with open(path, newline="") as src:
        return [(float(r["W"]), int(r["D"]), float(r["measured_hours"])) for r in csv.DictReader(src)]


def case_rows(cases: list[Case]) -> list[dict]:
    return [asdict(c) for c in cases]

"""Records scan outputs for the current formula, so a later formula can be compared against them.

Usage: python scripts/record_baseline.py <output-dir> [<repo-path>:<name> ...]

Always records the fixture app and the five benchmark scenarios (built fresh with fixed dates, so the
outputs are reproducible); extra repositories, such as a Flask clone, are given as path:name pairs.
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tests"))

from fixture_app import build as build_fixture_app  # noqa: E402

from vscore import FORMULA_VERSION  # noqa: E402
from vscore.bench.scenarios import SCENARIOS  # noqa: E402
from vscore.scan import scan  # noqa: E402


def main(argv: list[str]) -> int:
    out = Path(argv[0])
    out.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="vscore-baseline-") as tmp:
        repos = {"fixture-app": Path(tmp) / "fixture-app"}
        build_fixture_app(repos["fixture-app"])
        for name, scenario in SCENARIOS.items():
            repos[f"scenario-{name}"] = Path(tmp) / name
            scenario.build(repos[f"scenario-{name}"])
        for pair in argv[1:]:
            path, name = pair.rsplit(":", 1)
            repos[name] = Path(path)
        for name, path in repos.items():
            (out / f"{name}.json").write_text(scan(path).to_json())
            print(f"{FORMULA_VERSION}: recorded {name}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

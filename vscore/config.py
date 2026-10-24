"""Scan settings. Defaults follow the v1 scoring spec; a TOML file may override them."""

from __future__ import annotations

import hashlib
import json
import tomllib
from dataclasses import asdict, dataclass, fields, replace
from pathlib import Path


@dataclass(frozen=True)
class Config:
    alpha: float = 2.0
    zstd_level: int = 9
    max_file_bytes: int = 1_048_576
    lockfiles: tuple[str, ...] = (
        "poetry.lock",
        "package-lock.json",
        "yarn.lock",
        "Pipfile.lock",
        "pnpm-lock.yaml",
        "uv.lock",
    )
    excluded_dirs: tuple[str, ...] = ("vendor", "node_modules", "dist", "build")
    sensitive_tokens: tuple[str, ...] = ("auth", "payment", "payments", "billing", "user", "users", "pii")
    import_roots: tuple[str, ...] = ("", "src")
    crown_jewels_file: str = "crown_jewels.yml"
    blame_ignore_revs_file: str = ".git-blame-ignore-revs"

    # Pricing (phase 2). Uncalibrated priors until `vscore calibrate` fits them to measured loss.
    bytes_per_hour: float = 500.0  # surviving, verified compressed bytes one engineer-hour rebuilds
    harm_hours_per_dependent: float = 2.0
    restore_hours: float = 0.25  # restoring a committed file from Git
    unverified_weight: float = 0.5  # work from commits a CI or review source saw but never verified
    incident_hours: tuple[float, ...] = (40.0, 16.0, 4.0, 1.0)  # per linked incident, severity 1 to 4
    traffic_reference: float = 1000.0  # requests that double a file's harm
    crown_jewel_band: int = 4
    table_rebuild_hours: float = 5000.0  # production rows cannot be rebuilt; a nominal band-4 figure
    table_restore_hours: float = 2.0
    hours_per_failing_test: float = 1.0  # measured-loss label for the sandbox check
    restore_test_max_age_days: int = 7
    disposable_tables: tuple[str, ...] = (
        "*cache*", "tmp_*", "*_tmp", "temp_*", "*_temp", "test_*", "*_sessions", "django_session",
    )
    personal_columns: tuple[str, ...] = (
        "email", "phone", "ssn", "address", "birth", "dob", "password", "passport", "iban", "card",
    )

    # Gate (phase 3).
    gate_mode: str = "warn"  # "warn" reports what enforce would do; "enforce" blocks and asks
    default_budget_hours: float = 1.0  # budget when no bug report backs the session
    unverified_budget_cap_hours: float = 10.0

    @classmethod
    def load(cls, path: str | Path | None) -> Config:
        """Reads overrides from the `[vscore]` table of a TOML file; no path means defaults."""
        if path is None:
            return cls()
        table = tomllib.loads(Path(path).read_text()).get("vscore", {})
        known = {f.name: f for f in fields(cls)}
        unknown = sorted(set(table) - set(known))
        if unknown:
            raise ValueError(f"unknown config keys: {', '.join(unknown)}")
        values = {k: tuple(v) if isinstance(v, list) else v for k, v in table.items()}
        return replace(cls(), **values)

    def digest(self) -> str:
        """Hash of every setting, recorded in each scan so results can be traced to their configuration."""
        canonical = json.dumps(asdict(self), sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(canonical.encode()).hexdigest()[:16]

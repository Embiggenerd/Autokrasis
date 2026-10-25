"""One scan: files, classes, dependents and surviving work at a fixed revision, as deterministic JSON."""

from __future__ import annotations

import json
import platform
from dataclasses import dataclass

from compression import zstd

from vscore import FORMULA_VERSION, __version__
from vscore.adapters import files as filesmod
from vscore.adapters.history import ZSTD_MODE, Contribution, History
from vscore.adapters.imports import ImportGraph, build_graph, dependents, is_code
from vscore.adapters.symbols import python_symbols
from vscore.config import Config
from vscore.gitrepo import BlobReader, Repo
from vscore.score import codebase_total, confidence, r4, v_points


@dataclass
class ScanResult:
    data: dict
    contributions: dict[str, list[Contribution] | None]
    importers: dict[str, list[str]]
    graph: ImportGraph | None = None

    def to_json(self) -> str:
        return json.dumps(self.data, sort_keys=True, indent=2, ensure_ascii=False) + "\n"


def symbol_work(symbols: list, parts: list[Contribution] | None) -> list[float | None]:
    """Each symbol's W: every commit's work shared out by where its surviving lines fall. A line inside
    several symbols (such as `a = b = 1`) is split equally among them; lines in no symbol go to none."""
    if parts is None:
        return [None] * len(symbols)
    owners: dict[int, list[int]] = {}
    for index, sym in enumerate(symbols):
        for line in range(sym.start, sym.end + 1):
            owners.setdefault(line, []).append(index)
    work = [0.0] * len(symbols)
    for c in parts:
        if not c.added_lines:
            continue
        for line, weight in c.lines:
            for index in owners.get(line, ()):
                work[index] += c.info * weight / c.added_lines / len(owners[line])
    return work


def scan(repo_path, rev: str = "HEAD", cfg: Config | None = None, allow_shallow: bool = False,
         jobs: int | None = None, only: set[str] | None = None) -> ScanResult:
    """Scores every eligible file at `rev`. `only` limits history extraction (and the file list) to those paths."""
    cfg = cfg or Config()
    repo = Repo(repo_path)
    revision = repo.resolve(rev)
    shallow = repo.is_shallow()
    entries = repo.ls_tree(revision)
    by_path = {e.path: e for e in entries}

    with BlobReader(repo) as blobs:
        jewels_entry = by_path.get(cfg.crown_jewels_file)
        jewels = filesmod.parse_crown_jewels(blobs.read(jewels_entry.sha).decode()) if jewels_entry else ()
        attrs = repo.attributes(revision, [e.path for e in entries if e.type == "blob"], filesmod.GITATTRIBUTES)

        code_sources: dict[str, bytes] = {}

        def is_binary(entry) -> bool:
            data = blobs.read(entry.sha)
            if is_code(entry.path):
                code_sources[entry.path] = data
            return b"\0" in data[:8000]

        eligible, excluded = filesmod.select(entries, attrs, jewels, cfg, is_binary)
        classes = {f.path: f.cls for f in eligible}
        code_sources = {p: s for p, s in code_sources.items() if p in classes}

        graph = build_graph(code_sources, cfg.import_roots)
        tests = {p for p in code_sources if classes[p] == filesmod.TEST}
        d_values = dependents(graph, tests)

        # Work is counted once across the whole codebase, so history is always read for every eligible
        # file; `only` limits the output.
        all_paths = [f.path for f in eligible]
        targets = [p for p in all_paths if only is None or p in only]
        history_skipped = shallow and not allow_shallow
        contributions: dict[str, list[Contribution] | None] = {}
        mechanical: list[str] = []
        if not history_skipped:
            ignore_entry = by_path.get(cfg.blame_ignore_revs_file)
            ignore_revs = blobs.read(ignore_entry.sha) if ignore_entry else None
            history = History(repo, revision, blobs, cfg.zstd_level, ignore_revs, jobs, cfg.copy_detection,
                              cfg.mechanical_min_bytes, cfg.mechanical_words)
            contributions = history.contributions(all_paths)
            mechanical = history.mechanical

    w_values: dict[str, float | None] = {}
    w_reasons: dict[str, str] = {}
    for path in all_paths:
        if history_skipped:
            w_values[path], w_reasons[path] = None, "shallow_clone"
        elif contributions[path] is None:
            w_values[path], w_reasons[path] = None, "no_history"
        else:
            w_values[path] = sum(c.work for c in contributions[path])

    records = []
    for path in targets:
        reasons = {"W": w_reasons[path]} if path in w_reasons else {}
        w = w_values[path]
        commits = 0 if w is None else sum(1 for c in contributions[path] if c.work > 0)
        if path not in code_sources:
            d = None
            reasons["D"] = "unsupported_language"
        else:
            d = d_values[path]
            if d is None:
                reasons["D"] = "parse_error"
        records.append({
            "path": path,
            "class": classes[path],
            "W": r4(w),
            "D": d,
            "V": r4(v_points(w, d, cfg.alpha)),
            "reasons": reasons,
            "confidence": confidence(w, d, partial_history=shallow and allow_shallow),
            "commits": commits,
        })

    symbols = []
    for path in targets:
        found = python_symbols(code_sources[path]) if path.endswith(".py") and path in code_sources else None
        parts = None if history_skipped else contributions.get(path)
        for sym, w in zip(found or [], symbol_work(found or [], parts)):
            d = graph.name_dependents(path, sym.name, tests)
            symbols.append({"node": f"sym:{path}::{sym.name}", "path": path, "name": sym.name, "kind": sym.kind,
                            "lines": [sym.start, sym.end], "W": r4(w), "D": d, "V": r4(v_points(w, d, cfg.alpha))})

    sizes = {f.path: f.size for f in eligible}
    null_reasons = sorted(set(w_reasons.values()))
    total = codebase_total([w_values[p] for p in all_paths], [sizes[p] for p in all_paths],
                           reason=null_reasons[0] if len(null_reasons) == 1 else "mixed" if null_reasons else None)
    coverage = graph.coverage
    data = {
        "scan": {
            "revision": revision,
            "formula": FORMULA_VERSION,
            "scorer": __version__,
            "config_hash": cfg.digest(),
            "zstd": {"library": zstd.zstd_version, "level": cfg.zstd_level, "mode": ZSTD_MODE},
            "python": platform.python_version(),
            "git": repo.version(),
            "shallow": shallow,
            "allow_shallow": allow_shallow,
            "coverage": {
                "tracked": len(entries),
                "eligible": len(eligible),
                "excluded": len(excluded),
                "code_files": len(code_sources),
                "parse_errors": coverage.parse_errors,
                "imports": coverage.imports,
                "imports_resolved": coverage.resolved,
                "imports_unresolved": coverage.unresolved,
                "imports_via_reexport": coverage.via_reexport,
                "imports_type_only": coverage.type_only,
                "dynamic_imports": coverage.dynamic,
                "mechanical_commits": mechanical,
                "W_null": sum(r["W"] is None for r in records),
                "D_null": sum(r["D"] is None for r in records),
                "V_null": sum(r["V"] is None for r in records),
            },
        },
        "total": {k: r4(v) if isinstance(v, float) else v for k, v in total.items()},
        "files": records,
        "symbols": symbols,
        "excluded": [{"path": p, "reason": reason} for p, reason in excluded],
    }
    importers: dict[str, list[str]] = {}
    for source, edge_targets in sorted(graph.edges.items()):
        for target in edge_targets:
            importers.setdefault(target, []).append(source)
    return ScanResult(data, contributions, importers, graph)

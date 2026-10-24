"""Simple baselines V has to beat (raw churn, file size) and the rank correlation used to compare them."""

from __future__ import annotations

import math

from vscore.gitrepo import Repo, decode_path


def parse_numstat(output: bytes) -> list[tuple[int, int, str | None, str]]:
    """(added, deleted, old path or None, path) per entry of `git log --numstat -z --format=`, newest first."""
    tokens = output.split(b"\0")
    entries = []
    i = 0
    while i < len(tokens):
        token = tokens[i].lstrip(b"\n")
        i += 1
        if not token:
            continue
        added, deleted, path = token.split(b"\t", 2)
        old = None
        if not path:
            old, path = decode_path(tokens[i]), tokens[i + 1]
            i += 2
        to_int = lambda n: 0 if n == b"-" else int(n)
        entries.append((to_int(added), to_int(deleted), old, decode_path(path)))
    return entries


def churn(repo: Repo, rev: str, paths: list[str]) -> dict[str, int]:
    """Lines added plus deleted over all non-merge commits, following renames back from `rev`."""
    output = repo.run("log", "--no-merges", "-M", "--numstat", "-z", "--format=", rev)
    alias = {p: p for p in paths}
    totals = {p: 0 for p in paths}
    for added, deleted, old, path in parse_numstat(output):
        if path in alias:
            totals[alias[path]] += added + deleted
            if old is not None:
                alias[old] = alias[path]
    return totals


def _ranks(values: list[float]) -> list[float]:
    order = sorted(range(len(values)), key=lambda i: values[i])
    ranks = [0.0] * len(values)
    i = 0
    while i < len(order):
        j = i
        while j + 1 < len(order) and values[order[j + 1]] == values[order[i]]:
            j += 1
        for k in range(i, j + 1):
            ranks[order[k]] = (i + j) / 2 + 1
        i = j + 1
    return ranks


def spearman(xs: list[float], ys: list[float]) -> float | None:
    """Spearman rank correlation with average ranks for ties; None when either side has no variation."""
    if len(xs) != len(ys) or len(xs) < 2:
        return None
    rx, ry = _ranks(xs), _ranks(ys)
    mx, my = sum(rx) / len(rx), sum(ry) / len(ry)
    cov = sum((a - mx) * (b - my) for a, b in zip(rx, ry))
    vx = sum((a - mx) ** 2 for a in rx)
    vy = sum((b - my) ** 2 for b in ry)
    if vx == 0 or vy == 0:
        return None
    return cov / math.sqrt(vx * vy)

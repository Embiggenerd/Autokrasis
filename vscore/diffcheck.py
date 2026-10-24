"""Pre-commit and CI check: flag changes that delete or mostly rewrite band-3 and band-4 files."""

from __future__ import annotations

from vscore.gitrepo import Repo, decode_path


def diff_check(repo_path, base: str, prices: dict, head: str | None = None, min_band: int = 3) -> list[dict]:
    """Findings for files priced at `min_band` or above (by the prices at `base`) that the change deletes,
    or from which it removes more than half the lines."""
    repo = Repo(repo_path)
    by_node = {n["node"]: n for n in prices.get("nodes", [])}
    revs = [base] + ([head] if head else [])
    numstat = repo.run("diff", "--numstat", "-M", "-z", *revs)
    removed: dict[str, int] = {}
    tokens = numstat.split(b"\0")
    i = 0
    while i < len(tokens):
        token = tokens[i]
        i += 1
        if not token.strip():
            continue
        added, deleted, path = token.split(b"\t", 2)
        if not path:  # rename: the old path follows
            path = tokens[i]
            i += 2
        removed[decode_path(path)] = 0 if deleted == b"-" else int(deleted)
    status = repo.run("diff", "--name-status", "-M", "-z", *revs).split(b"\0")
    deleted_paths = {decode_path(status[j + 1]) for j in range(0, len(status) - 1) if status[j] == b"D"}

    findings = []
    for path, lines_removed in sorted(removed.items()):
        node = by_node.get(f"file:{path}")
        if node is None or node["band"] < min_band:
            continue
        if path in deleted_paths:
            findings.append({"path": path, "change": "deleted", "band": node["band"], "hours": node["hours"]})
            continue
        total = len(repo.run("show", f"{base}:{path}").splitlines())
        if total and lines_removed / total > 0.5:
            findings.append({"path": path, "change": f"rewrote {lines_removed} of {total} lines", "band": node["band"],
                             "hours": node["hours"]})
    return findings

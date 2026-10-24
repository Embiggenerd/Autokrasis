"""What a proposed agent action would destroy (plan: The gate and enforcement, Pricing an action).

A tool call becomes an Action: a list of Targets, each a node plus the effect the action has on it.
Shell commands are split into simple commands and read with a small model of the commands that
destroy things: rm, git, database clients, migration resets, redirections. SQL is classified
statement by statement. Commands the model does not know are left alone unless they mention a priced
path or database, in which case they are `unresolved` and priced at band 3.
"""

from __future__ import annotations

import os
import re
import shlex
from dataclasses import dataclass, field
from pathlib import PurePosixPath

from vscore.adapters.symbols import removed_symbols

# Effects, from most to least destructive. Reversible effects can be undone without a snapshot.
DESTRUCTIVE = ("drop", "db_reset", "truncate", "bulk_delete", "bulk_update", "drop_column", "rewrite_history",
               "discard_uncommitted", "delete", "rewrite", "overwrite", "unresolved", "partial_delete")
HARMLESS = ("move", "rename_table")

_SEPARATORS = {";", "&&", "||", "|", "&", "\n"}
_SAFE_COMMANDS = {
    "ls", "cat", "head", "tail", "less", "more", "grep", "egrep", "rg", "ag", "echo", "printf", "pwd", "cd", "which",
    "wc", "sort", "uniq", "diff", "stat", "file", "tree", "du", "df", "env", "true", "false", "test", "[", "awk",
    "jq", "pytest", "tsc", "node", "npm", "npx", "yarn", "pnpm", "make", "cargo", "go", "mkdir", "touch", "cp",
    "date", "sleep", "basename", "dirname", "realpath", "readlink", "export", "source", ".", "type", "command",
}
_SQL_CLIENTS = {"sqlite3", "psql", "mysql", "mariadb"}
_INTERPRETERS = {"python", "python3", "node", "ruby", "perl", "deno", "bun", "bash", "sh", "zsh", "uv", "poetry"}
_DB_RESET_PATTERNS = (
    r"manage\.py\s+(flush|reset_db|sqlflush)", r"manage\.py\s+migrate\s+\S+\s+zero", r"alembic\s+downgrade",
    r"flask\s+db\s+downgrade", r"rails\s+db:(drop|reset|schema:load)", r"rake\s+db:(drop|reset)",
    r"prisma\s+migrate\s+reset", r"prisma\s+db\s+push\b.*--(force-reset|accept-data-loss)", r"\bdropdb\b",
    r"sequelize\s+db:drop", r"knex\s+migrate:rollback\s+--all",
)
_TRIVIAL_WHERE = re.compile(r"^\s*(1\s*=\s*1|true|1|'1'\s*=\s*'1')\s*$", re.I)


@dataclass(frozen=True)
class Target:
    node: str
    effect: str
    detail: str = ""
    recursive: bool = False


@dataclass(frozen=True)
class Action:
    tool: str
    summary: str
    targets: tuple[Target, ...] = ()
    notes: tuple[str, ...] = ()


@dataclass
class Context:
    """What the action is checked against: the repository and the priced nodes."""

    repo_root: str
    cwd: str
    tracked: set[str] = field(default_factory=set)
    tables: dict[str, list[str]] = field(default_factory=dict)  # lower-case table name -> table nodes
    databases: dict[str, str] = field(default_factory=dict)  # absolute database file path -> database name
    file_lines: dict[str, int] = field(default_factory=dict)  # tracked path -> line count, for rewrite detection

    def rel(self, path: str) -> str | None:
        """Repository-relative path, or None when it lies outside the repository."""
        rel = os.path.relpath(self.absolute(path), os.path.realpath(self.repo_root))
        return None if rel.startswith("..") else rel.replace(os.sep, "/")

    def absolute(self, path: str) -> str:
        """Absolute path with symlinks resolved (on macOS /var is /private/var), so comparisons hold."""
        return os.path.realpath(os.path.join(self.cwd, os.path.expanduser(path)))

    def tracked_under(self, rel: str) -> list[str]:
        if rel in self.tracked:
            return [rel]
        if any(ch in rel for ch in "*?["):  # shell globs: `*` never crosses a `/`
            return sorted(p for p in self.tracked if PurePosixPath(p).full_match(rel))
        prefix = "" if rel in (".", "") else rel.rstrip("/") + "/"
        return sorted(p for p in self.tracked if p.startswith(prefix))

    def table_nodes(self, name: str, database: str | None = None) -> list[str]:
        bare = re.sub(r'^.*\.', "", name.strip('`"[]')).strip('`"[]').lower()
        nodes = self.tables.get(bare, [])
        if database:
            nodes = [n for n in nodes if n.startswith(f"table:{database}.")] or nodes
        return nodes or [f"table:?.?.{bare}"]

    def all_tables(self, database: str | None = None) -> list[str]:
        nodes = sorted({n for ns in self.tables.values() for n in ns})
        return [n for n in nodes if database is None or n.startswith(f"table:{database}.")]


# ---------------------------------------------------------------- SQL


def split_sql(sql: str) -> list[str]:
    """Statements separated by semicolons outside quotes; comments removed."""
    sql = re.sub(r"--[^\n]*", "", re.sub(r"/\*.*?\*/", "", sql, flags=re.S))
    statements, current, quote = [], [], None
    for ch in sql:
        if quote:
            current.append(ch)
            if ch == quote:
                quote = None
        elif ch in "'\"`":
            quote = ch
            current.append(ch)
        elif ch == ";":
            statements.append("".join(current))
            current = []
        else:
            current.append(ch)
    statements.append("".join(current))
    return [" ".join(s.split()) for s in statements if s.strip()]


_NAME = r'([`"\[]?[\w.]+[`"\]]?)'


def classify_sql(sql: str, ctx: Context, database: str | None = None) -> list[Target]:
    targets: list[Target] = []
    for stmt in split_sql(sql):
        upper = stmt.upper()
        if m := re.match(r"DROP\s+(?:TABLE|VIEW)\s+(?:IF\s+EXISTS\s+)?(.+?)(?:\s+CASCADE|\s+RESTRICT)?$", stmt, re.I):
            for name in m.group(1).split(","):
                targets += [Target(n, "drop", stmt) for n in ctx.table_nodes(name.strip(), database)]
        elif re.match(r"DROP\s+(DATABASE|SCHEMA)\b", upper):
            targets += [Target(n, "db_reset", stmt) for n in ctx.all_tables(database)] or [Target("table:?.?.*", "db_reset", stmt)]
        elif m := re.match(r"TRUNCATE\s+(?:TABLE\s+)?(?:ONLY\s+)?(.+?)(?:\s+(?:CASCADE|RESTART IDENTITY|CONTINUE IDENTITY|RESTRICT))*$", stmt, re.I):
            for name in m.group(1).split(","):
                targets += [Target(n, "truncate", stmt) for n in ctx.table_nodes(name.strip(), database)]
        elif m := re.match(rf"DELETE\s+FROM\s+{_NAME}(?:\s+(?:AS\s+)?\w+)?(?:\s+WHERE\s+(.*))?$", stmt, re.I):
            where = m.group(2)
            effect = "bulk_delete" if where is None or _TRIVIAL_WHERE.match(where) else "partial_delete"
            targets += [Target(n, effect, stmt) for n in ctx.table_nodes(m.group(1), database)]
        elif m := re.match(rf"UPDATE\s+{_NAME}\s+SET\s+(.*)$", stmt, re.I):
            where = re.search(r"\sWHERE\s+(.*)$", m.group(2), re.I)
            if where is None or _TRIVIAL_WHERE.match(where.group(1)):
                targets += [Target(n, "bulk_update", stmt) for n in ctx.table_nodes(m.group(1), database)]
        elif m := re.match(rf"ALTER\s+TABLE\s+(?:IF\s+EXISTS\s+)?(?:ONLY\s+)?{_NAME}\s+(.*)$", stmt, re.I):
            rest = m.group(2).upper()
            if re.search(r"\bDROP\s+(COLUMN\s+)?(?!CONSTRAINT|INDEX|DEFAULT|NOT)", rest):
                targets += [Target(n, "drop_column", stmt) for n in ctx.table_nodes(m.group(1), database)]
            elif rest.startswith("RENAME"):
                targets += [Target(n, "rename_table", stmt) for n in ctx.table_nodes(m.group(1), database)]
    return targets


# ---------------------------------------------------------------- shell


def _tokens(command: str) -> list[str]:
    lexer = shlex.shlex(command, posix=True, punctuation_chars=";&|<>")
    lexer.whitespace_split = True
    lexer.commenters = ""
    try:
        return list(lexer)
    except ValueError:  # unbalanced quotes
        return command.split()


def _simple_commands(tokens: list[str]) -> list[list[str]]:
    commands, current = [], []
    for token in tokens:
        if token in _SEPARATORS or set(token) <= set(";&|") and token:
            if current:
                commands.append(current)
            current = []
        else:
            current.append(token)
    if current:
        commands.append(current)
    return commands


def _strip_prefixes(args: list[str]) -> list[str]:
    while args and (args[0] in ("sudo", "time", "nohup", "exec", "env") or re.match(r"^\w+=", args[0])):
        args = args[1:]
    return args


def _redirections(args: list[str]) -> tuple[list[str], list[tuple[str, str]]]:
    """Removes redirections from args; returns them as (operator, path)."""
    kept, found, i = [], [], 0
    while i < len(args):
        token = args[i]
        if token in (">&", "<&"):  # fd duplication such as 2>&1
            if kept and kept[-1].isdigit():
                kept.pop()
            i += 2
            continue
        if token == "&>":
            token = ">"
        if token in (">", ">>", "<", ">|", "<<", "<<<") or (token.startswith(">") and set(token) <= {">", "|"}):
            if kept and kept[-1].isdigit():
                kept.pop()
            if i + 1 < len(args):
                found.append((token, args[i + 1]))
            i += 2
            continue
        kept.append(token)
        i += 1
    return kept, found


def _file_targets(paths: list[str], ctx: Context, effect: str, recursive: bool = False) -> list[Target]:
    targets = []
    for path in paths:
        absolute = ctx.absolute(path)
        if absolute in ctx.databases:
            db = ctx.databases[absolute]
            targets += [Target(n, "db_reset", f"removes database file {path}") for n in ctx.all_tables(db)]
            continue
        rel = ctx.rel(path)
        if rel is None:
            continue
        targets += [Target(f"file:{p}", effect, path, recursive) for p in ctx.tracked_under(rel)]
    return targets


def _mentions_priced(args: list[str], ctx: Context) -> bool:
    for arg in args:
        if ctx.absolute(arg) in ctx.databases:
            return True
        rel = ctx.rel(arg) if not arg.startswith("-") else None
        if rel and rel not in (".", "") and (rel in ctx.tracked or any(p.startswith(rel + "/") for p in ctx.tracked)):
            return True
    return False


def _git(args: list[str], ctx: Context) -> list[Target]:
    sub = next((a for a in args[1:] if not a.startswith("-")), "")
    rest = args[args.index(sub) + 1 :] if sub in args else []
    flags = {a for a in args if a.startswith("-")}
    if sub == "rm":
        return _file_targets([a for a in rest if not a.startswith("-")], ctx, "delete", "-r" in flags)
    if sub == "push" and (flags & {"-f", "--force", "--force-with-lease", "--mirror"} or any(a.startswith("+") for a in rest)):
        return [Target("git:history", "rewrite_history", " ".join(args))]
    if sub in ("filter-branch", "filter-repo", "rebase") or (sub == "branch" and "-D" in flags):
        return [Target("git:history", "rewrite_history", " ".join(args))]
    if sub == "reset" and "--hard" in flags:
        return [Target("git:worktree", "discard_uncommitted", " ".join(args))]
    if sub == "clean" and any(f.startswith("-") and "f" in f for f in flags):
        return [Target("git:untracked", "discard_uncommitted", " ".join(args))]
    if sub in ("checkout", "restore") and "--staged" not in flags:
        paths = rest[rest.index("--") + 1 :] if "--" in rest else ([a for a in rest if not a.startswith("-")] if sub == "restore" else [])
        if paths:
            return [Target("git:worktree", "discard_uncommitted", " ".join(paths))]
    if sub == "stash" and rest[:1] in (["drop"], ["clear"]):
        return [Target("git:stash", "discard_uncommitted", " ".join(args))]
    return []


def _sql_client(args: list[str], raw: str, ctx: Context) -> tuple[list[Target], list[str]]:
    client = os.path.basename(args[0])
    sql_parts, database, notes = [], None, []
    if client == "sqlite3":
        positional = [a for a in args[1:] if not a.startswith("-")]
        if positional:
            path = ctx.absolute(positional[0])
            database = ctx.databases.get(path, os.path.splitext(os.path.basename(positional[0]))[0])
            sql_parts = positional[1:]
    else:
        for i, a in enumerate(args):
            if a in ("-c", "-e", "--command", "--execute") and i + 1 < len(args):
                sql_parts.append(args[i + 1])
            elif a.startswith(("--command=", "--execute=")):
                sql_parts.append(a.split("=", 1)[1])
            elif a.startswith(("postgres://", "postgresql://")):
                database = a.rstrip("/").rsplit("/", 1)[-1].split("?")[0]
            elif a in ("-d", "--dbname") and i + 1 < len(args):
                database = args[i + 1]
    if "<<" in raw:
        body = raw.split("\n", 1)[1] if "\n" in raw else ""
        sql_parts.append(body)
    if not sql_parts:
        notes.append(f"{client} without inline SQL: statements unknown")
        return [Target(n, "unresolved", "interactive SQL client") for n in ctx.all_tables(database)] or [
            Target("table:?.?.*", "unresolved", "interactive SQL client")], notes
    return classify_sql(";".join(sql_parts), ctx, database), notes


def parse_shell(command: str, ctx: Context) -> tuple[list[Target], list[str]]:
    """Targets of a shell command line, and notes about anything the model could not read."""
    targets: list[Target] = []
    notes: list[str] = []
    for pattern in _DB_RESET_PATTERNS:
        if re.search(pattern, command):
            targets += [Target(n, "db_reset", pattern) for n in ctx.all_tables()] or [
                Target("table:?.?.*", "db_reset", pattern)]
    first_line = command.split("\n", 1)[0]
    for args in _simple_commands(_tokens(first_line)):
        args, redirects = _redirections(_strip_prefixes(args))
        for op, path in redirects:
            if op in (">", ">|") and path != "/dev/null":
                targets += _file_targets([path], ctx, "overwrite")
        if not args:
            continue
        name = os.path.basename(args[0])
        flags = {a for a in args[1:] if a.startswith("-")}
        operands = [a for a in args[1:] if not a.startswith("-")]
        if name in ("bash", "sh", "zsh") and "-c" in args and args.index("-c") + 1 < len(args):
            inner_targets, inner_notes = parse_shell(args[args.index("-c") + 1], ctx)
            targets += inner_targets
            notes += inner_notes
        elif name in ("rm", "unlink", "shred"):
            recursive = any(f in flags for f in ("-r", "-R", "-rf", "-fr", "--recursive")) or any(
                f.startswith("-") and not f.startswith("--") and "r" in f.lower() for f in flags)
            targets += _file_targets(operands, ctx, "delete", recursive)
        elif name == "git":
            targets += _git(args, ctx)
        elif name in _SQL_CLIENTS:
            sql_targets, sql_notes = _sql_client(args, command, ctx)
            targets += sql_targets
            notes += sql_notes
        elif name == "mv" and len(operands) >= 2:
            *sources, dest = operands
            dest_rel = ctx.rel(dest)
            if dest_rel in ctx.tracked:
                targets += _file_targets([dest], ctx, "overwrite")
            targets += [Target(t.node, "move", t.detail) for t in _file_targets(sources, ctx, "move")]
        elif name == "truncate" or (name == "dd" and any(a.startswith("of=") for a in args)):
            paths = operands if name == "truncate" else [a[3:] for a in args if a.startswith("of=")]
            targets += _file_targets([p for p in paths if not p.isdigit()], ctx, "overwrite")
        elif name == "find" and ("-delete" in args or "-exec" in args):
            roots = [a for a in args[1:] if not a.startswith("-")][:1] or ["."]
            targets += [Target(t.node, "unresolved", "find with -delete or -exec", True)
                        for t in _file_targets(roots, ctx, "unresolved")]
        elif name in ("python", "python3") and "-c" in args:
            code = args[args.index("-c") + 1] if args.index("-c") + 1 < len(args) else ""
            if re.search(r"\b(DROP|TRUNCATE|DELETE\s+FROM|UPDATE\s+\w+\s+SET)\b", code, re.I):
                targets += classify_sql(" ; ".join(re.findall(r"['\"]([^'\"]*(?:DROP|TRUNCATE|DELETE|UPDATE)[^'\"]*)['\"]", code, re.I)), ctx)
            if re.search(r"\b(os\.remove|os\.unlink|shutil\.rmtree|unlink\()", code):
                notes.append("python -c removes files; paths unknown")
                targets.append(Target("file:?", "unresolved", "python -c file removal"))
        elif name == "sed" and any(f.startswith("-i") for f in flags):
            continue  # in-place edits are ordinary changes
        elif name in _INTERPRETERS:
            continue  # running a repository script; what it does inside cannot be read from the command line
        elif name not in _SAFE_COMMANDS and _mentions_priced(args[1:], ctx):
            notes.append(f"unknown command {name!r} touches priced files")
            targets += [Target(t.node, "unresolved", f"{name} {' '.join(operands)}")
                        for t in _file_targets([a for a in operands if ctx.rel(a)], ctx, "unresolved")]
            for a in args[1:]:
                if ctx.absolute(a) in ctx.databases:
                    targets += [Target(n, "unresolved", f"{name} on database") for n in ctx.all_tables(ctx.databases[ctx.absolute(a)])]
    return targets, notes


# ---------------------------------------------------------------- tool calls


REWRITE_MIN_LINES = 10


def _rewrite_target(rel: str | None, removed_lines: int, ctx: Context) -> list[Target]:
    """A write that removes more than half of a tracked file's lines, and at least ten, is a major rewrite."""
    if rel is None or rel not in ctx.tracked or removed_lines < REWRITE_MIN_LINES:
        return []
    if rel not in ctx.file_lines:
        try:
            with open(os.path.join(ctx.repo_root, rel), "rb") as handle:
                ctx.file_lines[rel] = handle.read().count(b"\n")
        except OSError:
            ctx.file_lines[rel] = 0
    total = ctx.file_lines[rel]
    if total and removed_lines / total > 0.5:
        return [Target(f"file:{rel}", "rewrite", f"removes {removed_lines} of {total} lines")]
    return []


def from_tool_call(tool_name: str, tool_input: dict, ctx: Context) -> Action:
    """Reads a Claude Code tool call (the PreToolUse hook payload) as an Action."""
    if tool_name == "Bash":
        command = tool_input.get("command", "")
        targets, notes = parse_shell(command, ctx)
        return Action(tool_name, command[:200], tuple(targets), tuple(notes))
    if tool_name in ("Write", "Edit", "MultiEdit", "NotebookEdit"):
        path = tool_input.get("file_path") or tool_input.get("notebook_path") or ""
        rel = ctx.rel(path)
        try:
            old_text = open(ctx.absolute(path), encoding="utf-8", errors="replace").read()
        except OSError:
            old_text = None
        new_text = _proposed_text(tool_name, tool_input, old_text)
        if tool_name == "Write":
            new_lines = set((new_text or "").splitlines())
            removed = sum(1 for line in (old_text or "").splitlines() if line not in new_lines)
        elif tool_name == "MultiEdit":
            removed = sum(e.get("old_string", "").count("\n") + 1 for e in tool_input.get("edits", []))
        else:
            removed = tool_input.get("old_string", "").count("\n") + 1 if tool_input.get("old_string") else 0
        targets = _rewrite_target(rel, removed, ctx)
        if rel in ctx.tracked and rel.endswith(".py") and old_text is not None and new_text is not None:
            targets += [Target(f"sym:{rel}::{s.name}", "delete", f"{s.kind} removed by the edit")
                        for s in removed_symbols(old_text, new_text)]
        return Action(tool_name, f"{tool_name} {rel or path}", tuple(targets))
    return Action(tool_name, tool_name)


def _proposed_text(tool_name: str, tool_input: dict, old_text: str | None) -> str | None:
    """The file's content after the edit, when it can be known."""
    if tool_name == "Write":
        return tool_input.get("content", "")
    if old_text is None:
        return None
    edits = tool_input.get("edits", []) if tool_name == "MultiEdit" else [tool_input]
    text = old_text
    for edit in edits:
        old, new = edit.get("old_string"), edit.get("new_string", "")
        if old is None:
            return None
        text = text.replace(old, new) if edit.get("replace_all") else text.replace(old, new, 1)
    return text

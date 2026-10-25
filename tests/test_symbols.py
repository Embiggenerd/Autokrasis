from vscore.actions import Context, from_tool_call
from vscore.adapters.imports import build_graph
from vscore.adapters.symbols import python_symbols, removed_symbols
from vscore.scan import scan

SOURCE = '''import functools

LIMIT = 10


@functools.cache
def area(w, h):
    return w * h


class Shape:
    def size(self):
        return 1
'''


def test_symbols_and_their_spans():
    found = {s.name: (s.kind, s.start, s.end) for s in python_symbols(SOURCE)}
    assert found == {"LIMIT": ("variable", 3, 3), "area": ("function", 6, 8), "Shape": ("class", 11, 13)}
    assert python_symbols("def (:") is None


def test_removed_symbols():
    assert [s.name for s in removed_symbols(SOURCE, SOURCE.replace("LIMIT = 10\n", ""))] == ["LIMIT"]
    assert removed_symbols(SOURCE, "def (:") == []


def test_names_imported_from_a_module_count_per_symbol():
    g = build_graph({
        "pkg/__init__.py": b"from .shapes import area\n",
        "pkg/shapes.py": SOURCE.encode(),
        "a.py": b"from pkg.shapes import area, LIMIT\n",
        "b.py": b"from pkg import area\n",  # through the re-export
        "c.py": b"import pkg.shapes\n",  # whole module: no name
    }, ("",))
    assert g.name_dependents("pkg/shapes.py", "area", set()) == 3  # a, b and the package __init__
    assert g.name_dependents("pkg/shapes.py", "LIMIT", set()) == 1
    assert g.name_dependents("pkg/shapes.py", "Shape", set()) == 0


def test_scan_covers_symbols(make_repo):
    repo = make_repo()
    repo.write("shapes.py", SOURCE)
    repo.commit("shapes")
    repo.write("app.py", "from shapes import area\n")
    repo.write("other.py", "from shapes import area\n")
    repo.commit("users")
    symbols = {s["name"]: s for s in scan(repo.path).data["symbols"]}
    assert symbols["area"]["D"] == 2 and symbols["Shape"]["D"] == 0
    assert symbols["area"]["W"] > 0
    file_w = {f["path"]: f["W"] for f in scan(repo.path).data["files"]}["shapes.py"]
    assert sum(s["W"] for s in symbols.values() if s["path"] == "shapes.py") <= file_w + 1e-6


def test_an_edit_that_removes_a_function_targets_it(make_repo):
    repo = make_repo()
    repo.write("shapes.py", SOURCE)
    for name in "abcdefgh":
        repo.write(f"{name}.py", "from shapes import area\n")
    repo.commit("shapes and users")
    edit = {"file_path": str(repo.path / "shapes.py"), "old_string": "@functools.cache\ndef area(w, h):\n    return w * h\n",
            "new_string": ""}
    ctx = Context(str(repo.path), str(repo.path), tracked={"shapes.py"})
    assert [t.node for t in from_tool_call("Edit", edit, ctx).targets] == ["sym:shapes.py::area"]

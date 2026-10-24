from vscore.adapters.imports import build_graph, dependents


def graph(files: dict[str, str]):
    return build_graph({path: text.encode() for path, text in files.items()}, ("", "src"))


def test_edges_run_from_importer_to_longest_existing_module():
    g = graph({"a/__init__.py": "", "a/b.py": "", "main.py": "import a.b.c\n"})
    assert g.edges["main.py"] == {"a/b.py"}
    assert g.edges["a/b.py"] == set()


def test_module_file_is_preferred_over_package_init():
    g = graph({"a/b.py": "", "a/b/__init__.py": "", "main.py": "import a.b\n"})
    assert g.edges["main.py"] == {"a/b.py"}


def test_src_is_an_import_root():
    g = graph({"src/pkg/__init__.py": "", "src/pkg/mod.py": "", "app.py": "import pkg.mod\n"})
    assert g.edges["app.py"] == {"src/pkg/mod.py"}


def test_from_import_tries_submodule_then_falls_back_to_package():
    g = graph({
        "pkg/__init__.py": "VALUE = 1\n",
        "pkg/sub.py": "",
        "a.py": "from pkg import sub\n",
        "b.py": "from pkg import VALUE\n",
        "c.py": "from pkg import *\n",
    })
    assert g.edges["a.py"] == {"pkg/sub.py"}
    assert g.edges["b.py"] == {"pkg/__init__.py"}
    assert g.edges["c.py"] == {"pkg/__init__.py"}


def test_names_follow_the_package_reexport_to_the_defining_file():
    g = graph({
        "pkg/__init__.py": "from .app import App\nfrom .helpers import url_for as link\nfrom .core import *\nVERSION = '1'\n",
        "pkg/app.py": "class App: ...\n",
        "pkg/helpers.py": "def url_for(): ...\n",
        "pkg/core.py": "",
        "a.py": "from pkg import App\n",
        "b.py": "from pkg import link\n",
        "c.py": "from pkg import VERSION\n",  # defined in __init__.py itself
        "d.py": "from pkg import Starred\n",  # only reachable through a star import
        "e.py": "import pkg\n",
    })
    assert g.edges["a.py"] == {"pkg/app.py"}
    assert g.edges["b.py"] == {"pkg/helpers.py"}
    assert g.edges["c.py"] == {"pkg/__init__.py"}
    assert g.edges["d.py"] == {"pkg/__init__.py"}
    assert g.edges["e.py"] == {"pkg/__init__.py"}
    assert g.coverage.via_reexport == 2


def test_reexports_are_followed_through_nested_packages():
    g = graph({
        "pkg/__init__.py": "from .json import dumps\n",
        "pkg/json/__init__.py": "from .provider import dumps\n",
        "pkg/json/provider.py": "def dumps(x): ...\n",
        "a.py": "from pkg import dumps\n",
    })
    assert g.edges["a.py"] == {"pkg/json/provider.py"}


def test_reexport_cycles_stop_at_an_init_file():
    g = graph({
        "a/__init__.py": "from b import thing\n",
        "b/__init__.py": "from a import thing\n",
        "user.py": "from a import thing\n",
    })
    assert g.edges["user.py"] <= {"a/__init__.py", "b/__init__.py"}


def test_relative_imports_resolve_from_the_importing_package():
    g = graph({
        "pkg/__init__.py": "",
        "pkg/a.py": "from . import b\nfrom .c import thing\n",
        "pkg/b.py": "",
        "pkg/c.py": "",
        "pkg/sub/__init__.py": "",
        "pkg/sub/d.py": "from .. import a\nfrom ..b import x\n",
    })
    assert g.edges["pkg/a.py"] == {"pkg/b.py", "pkg/c.py"}
    assert g.edges["pkg/sub/d.py"] == {"pkg/a.py", "pkg/b.py"}


def test_unresolved_and_dynamic_imports_are_counted_without_edges():
    g = graph({
        "a.py": "from __future__ import annotations\nimport os\nimport requests\nimport importlib\n"
                "importlib.import_module('x')\n__import__('y')\n",
    })
    assert g.edges["a.py"] == set()
    cov = g.coverage
    assert (cov.imports, cov.resolved, cov.unresolved, cov.dynamic) == (6, 0, 6, 2)


def test_parse_error_gives_null_d_but_file_can_still_be_imported():
    g = graph({"bad.py": "def (:\n", "a.py": "import bad\n"})
    assert g.parse_errors == {"bad.py"}
    assert g.edges["a.py"] == {"bad.py"}
    d = dependents(g, set())
    assert d["bad.py"] is None
    assert d["a.py"] == 0


def test_dependents_count_direct_importers_and_skip_tests():
    g = graph({
        "core.py": "",
        "mid.py": "import core\n",
        "top.py": "import mid\nimport core\n",
        "x.py": "import y\n",
        "y.py": "import x\n",
        "tests/test_core.py": "import core\n",
    })
    d = dependents(g, {"tests/test_core.py"})
    assert d == {"core.py": 2, "mid.py": 1, "top.py": 0, "x.py": 1, "y.py": 1, "tests/test_core.py": 0}


def test_cycles_do_not_inflate_dependents():
    ring = {f"m{i}.py": f"import m{(i + 1) % 5}\n" for i in range(5)}
    d = dependents(graph({**ring, "hub.py": "import m0\nimport m1\nimport m2\n"}), set())
    assert d["m0.py"] == 2 and d["m3.py"] == 1


def test_type_checking_imports_are_skipped_but_else_branches_run():
    g = graph({
        "a.py": "",
        "b.py": "",
        "c.py": "",
        "user.py": (
            "import typing as t\nfrom typing import TYPE_CHECKING\n"
            "if TYPE_CHECKING:\n    import a\nelse:\n    import b\n"
            "if t.TYPE_CHECKING:\n    from c import Thing\n"
        ),
    })
    assert g.edges["user.py"] == {"b.py"}
    assert g.coverage.type_only == 2

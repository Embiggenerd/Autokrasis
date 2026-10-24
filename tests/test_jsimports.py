from vscore.adapters.imports import build_graph, dependents
from vscore.adapters.jsimports import extract_js


def graph(files):
    return build_graph({p: s.encode() for p, s in files.items()}, ("", "src"))


def test_extract_reads_every_import_form_and_skips_type_only():
    found = extract_js(
        "import React from 'react';\n"
        "import { a, b as c, type T } from './util';\n"
        "import * as ns from './ns';\n"
        "import './styles.css';\n"
        "import type { Props } from './types';\n"
        "export { x } from './x';\n"
        "export * from './all';\n"
        "export type { Y } from './y';\n"
        "const fs = require('fs');\n"
        "const lazy = import('./lazy');\n"
        "const dyn = import(name);\n"
        "// import gone from './commented';\n"
        "/* import also from './gone' */\n"
    )
    assert ("react", "default") in found.requests
    assert ("./util", "a") in found.requests and ("./util", "b") in found.requests
    assert ("./util", "T") not in found.requests
    assert ("./ns", None) in found.requests
    assert ("./styles.css", None) in found.requests
    assert ("./x", "x") in found.requests and ("./all", None) in found.requests
    assert ("fs", None) in found.requests and ("./lazy", None) in found.requests
    assert all(spec not in ("./types", "./y", "./commented", "./gone") for spec, _ in found.requests)
    assert found.type_only == 2
    assert found.dynamic == 1
    assert found.bound == {"x": ("./x", "x")}


def test_relative_specifiers_resolve_with_extensions_and_index_files():
    g = graph({
        "src/app.ts": "import { load } from './data';\nimport { Button } from './ui';\nimport lodash from 'lodash';\n",
        "src/data.ts": "export function load() {}\n",
        "src/ui/index.ts": "export { Button } from './button';\n",
        "src/ui/button.tsx": "export const Button = 1;\n",
        "src/esm.ts": "import { load } from './data.js';\n",
    })
    assert g.edges["src/app.ts"] == {"src/data.ts", "src/ui/button.tsx"}  # follows the index re-export
    assert g.edges["src/esm.ts"] == {"src/data.ts"}
    assert g.coverage.via_reexport == 1


def test_js_tests_do_not_count_as_dependents():
    g = graph({"lib.js": "", "app.js": "require('./lib')\n", "lib.test.js": "require('./lib')\n"})
    assert dependents(g, {"lib.test.js"})["lib.js"] == 1

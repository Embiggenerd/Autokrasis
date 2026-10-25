"""vscore: V points for the files of a Git repository (see docs/plan.md, "Scoring spec (v1)")."""

__version__ = "0.1.0"
# /2: `from pkg import Name` follows the package __init__.py's re-export to the defining file.
# /3: D counts direct importers; `if TYPE_CHECKING:` imports are skipped; tests are classed before sensitive paths.
# /4: JavaScript and TypeScript files get D and V; top-level Python symbols get their own W, D and V.
# /5: each original line counts once (copy detection); merge-introduced lines are credited; totals keep
#     missing values; a line shared by several symbols is split among them.
FORMULA_VERSION = "vscore-formula/5"

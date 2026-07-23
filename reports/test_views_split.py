"""P1-2 god-file split guards (v2.95).

reports/views.py (4,034 lines, 69 classes) became the reports/views/ package.
Two things must stay true forever:

1. **Namespace parity.** The package __init__ reproduces the old module's
   namespace, so reports/urls.py and every external `from reports.views
   import X` — including private helpers like `_repost_to_ledger` (used by
   giving/views.py) — keep working. If someone adds a view to a submodule
   but forgets the __init__ re-export, the URL entry breaks; the parity
   test catches the subtler case where only an external import breaks.

2. **No regrowth.** The split is pointless if one module quietly becomes
   the next god file. A size ceiling fails the build before that happens;
   split the module further or move logic into services, don't raise the
   ceiling casually.
"""
import ast
import pathlib

from django.test import TestCase

PKG = pathlib.Path(__file__).resolve().parent / "views"

# Names external code is known to import from reports.views (grep'd at split
# time). If one goes missing from the package namespace, another app breaks.
EXTERNALLY_IMPORTED = [
    "_repost_to_ledger",        # giving/views.py
    "_balanced_partition",      # giving tests
    "_camp_goal_records",       # cashbook tests
    "FinancialPositionView", "StatementOfCashFlowsView",
    "DevGroupUnassignedView", "AuditLogView",
]

MAX_MODULE_LINES = 1200


class SplitNamespaceParityTests(TestCase):
    def test_every_submodule_def_is_reexported(self):
        """Any top-level class/def in a submodule must be importable from
        reports.views itself — the __init__ must re-export it."""
        import reports.views as v
        missing = []
        for mod in PKG.glob("*.py"):
            if mod.name == "__init__.py":
                continue
            tree = ast.parse(mod.read_text())
            for node in tree.body:
                if isinstance(node, (ast.ClassDef, ast.FunctionDef)):
                    if not hasattr(v, node.name):
                        missing.append(f"{mod.name}:{node.name}")
        self.assertEqual(missing, [],
                         "add these to reports/views/__init__.py: %s" % missing)

    def test_externally_imported_names_present(self):
        import reports.views as v
        for name in EXTERNALLY_IMPORTED:
            self.assertTrue(hasattr(v, name),
                            f"reports.views.{name} vanished — another app imports it")

    def test_urls_import_cleanly(self):
        import reports.urls
        self.assertGreater(len(reports.urls.urlpatterns), 50)


class NoRegrowthTests(TestCase):
    def test_no_module_exceeds_ceiling(self):
        fat = {p.name: len(p.read_text().splitlines())
               for p in PKG.glob("*.py")
               if len(p.read_text().splitlines()) > MAX_MODULE_LINES}
        self.assertEqual(fat, {},
                         f"views module(s) over {MAX_MODULE_LINES} lines — split "
                         f"further or move logic to services: {fat}")

    def test_old_monolith_gone(self):
        self.assertFalse((PKG.parent / "views.py").exists(),
                         "reports/views.py is back — the package must be the "
                         "only reports.views")

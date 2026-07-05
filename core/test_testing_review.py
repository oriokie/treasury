"""Testing strategy review.

A previous review found and fixed a real "test time-bomb" bug class: a model
field defaulting to `date.today()` (a moving target) combined with a test
that hardcodes an absolute date for a *related* object, where the two need to
fall within a certain window of each other. The test passed when written and
silently broke months later as the gap between "today" and the hardcoded
date grew, with no code change at all (pledges.tests — Pledge.start_date vs a
hardcoded contribution date, fixed by pinning start_date explicitly).

This file adds a guardrail: an inventory test that lists every model field in
the codebase with this exact shape (a DateField/DateTimeField whose default is
a callable, i.e. re-evaluated at every call — as opposed to auto_now_add,
which is fixed at creation and can't drift). If a new one is ever added, this
test's snapshot will need a deliberate update, which is the prompt for
whoever adds it to check: does any test rely on this field's default *and*
hardcode a date for something related to it? That's the exact combination
that bites, and it's cheap to check right when the field is introduced.
"""
import ast
import glob
from django.test import TestCase


def _find_movable_date_defaults():
    """Every DateField/DateTimeField across every models.py with a callable
    default (not auto_now_add) — found via a light AST scan, not import, so
    it works even for apps not on INSTALLED_APPS in a given test run."""
    found = []
    for path in sorted(glob.glob("*/models.py")):
        if "/.venv/" in path:
            continue
        try:
            tree = ast.parse(open(path).read(), filename=path)
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            name = getattr(func, "attr", None) or getattr(func, "id", None)
            if name not in ("DateField", "DateTimeField"):
                continue
            has_auto_now_add = any(
                kw.arg == "auto_now_add" for kw in node.keywords)
            has_callable_default = any(
                kw.arg == "default" and isinstance(kw.value, (ast.Name, ast.Attribute))
                for kw in node.keywords)
            if has_callable_default and not has_auto_now_add:
                found.append(path)
    return found


class MovableDateDefaultInventoryTests(TestCase):
    """A snapshot of every model field known to have this shape today. This
    is not a pass/fail correctness check by itself — it's a deliberate
    tripwire: the count changing is the signal to go check the new field's
    tests for the hardcoded-date-vs-moving-default trap."""

    KNOWN_FILES_WITH_MOVABLE_DATE_DEFAULTS = {
        "pledges/models.py",   # Pledge.start_date, PledgeCampaign.start_date,
                               # PledgePayment.date — all default=dt.date.today.
                               # Confirmed (this review) that only pledges.tests
                               # exercises the date-window-sensitive matching
                               # functions, and that file already pins
                               # start_date explicitly wherever a hardcoded
                               # contribution date needs to fall within range.
    }

    def test_no_new_movable_date_default_files_without_review(self):
        files_found = set(_find_movable_date_defaults())
        new_files = files_found - self.KNOWN_FILES_WITH_MOVABLE_DATE_DEFAULTS
        self.assertEqual(new_files, set(),
            f"New model file(s) with a DateField/DateTimeField defaulting to "
            f"a callable (e.g. date.today) found: {new_files}. This is the "
            f"exact shape that caused a real, silent test failure before "
            f"(pledges.tests) — check every test that creates this model "
            f"without pinning the date field explicitly, especially any test "
            f"that also hardcodes an absolute date for something related to "
            f"it. Once reviewed, add the file to KNOWN_FILES_WITH_MOVABLE_"
            f"DATE_DEFAULTS above.")

    def test_pledges_still_has_the_expected_movable_defaults(self):
        # a sanity check that the scanner itself still finds the known case —
        # if this ever goes to zero, the scanner broke, not the codebase
        self.assertIn("pledges/models.py", _find_movable_date_defaults())

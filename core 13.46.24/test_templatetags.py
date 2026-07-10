"""Unit coverage for the template filters used in dashboards and progress bars."""
from django.test import TestCase

from core.templatetags.treasury_extras import get_item, pct_width


class TemplateFilterTests(TestCase):
    def test_get_item_returns_value(self):
        self.assertEqual(get_item({"a": 1}, "a"), 1)

    def test_get_item_missing_key(self):
        self.assertIsNone(get_item({"a": 1}, "b"))

    def test_get_item_on_non_dict(self):
        self.assertIsNone(get_item(None, "x"))
        self.assertIsNone(get_item(5, "x"))

    def test_pct_width_clamps(self):
        self.assertEqual(pct_width(50), 50)
        self.assertEqual(pct_width(150), 100)   # clamped high
        self.assertEqual(pct_width(-10), 0)     # clamped low

    def test_pct_width_invalid_is_zero(self):
        self.assertEqual(pct_width("abc"), 0)
        self.assertEqual(pct_width(None), 0)

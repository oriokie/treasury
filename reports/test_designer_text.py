"""Report designer — custom text (v2.88).

Pins the authoring contract: a designed report can carry standalone headings and
hand-written text blocks; merge fields ({period_start}, {period_end}, {church},
{today}) fill in at render; blank lines become paragraphs in HTML and in the
Word/PDF exports; tabular exports skip text structure; and the designer can
preview the report it is currently building without saving anything.
"""
import datetime as dt
import json

from django.contrib.auth.models import Group, User
from django.test import TestCase

from core.roles import TREASURER


def _sections():
    return [
        {"component": "heading",
         "params": {"text": "Part 1 — Income"}, "layout": {"width": 12}},
        {"component": "commentary", "title": "Introduction",
         "params": {"text": "For {church}.\n\nPeriod {period_start} to "
                            "{period_end}, printed {today}."},
         "layout": {"width": 12}},
        {"component": "info_panel", "title": "Basis",
         "params": {"text": "From the registry."}, "layout": {"width": 12}},
    ]


class DesignerTextTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user("t_dsg", password="x")
        self.user.groups.add(Group.objects.get_or_create(name=TREASURER)[0])
        self.client.force_login(self.user)

    def _save(self, key="txt_report"):
        r = self.client.post("/reports/designer/new/", {
            "title": "Text Report", "key": key, "category": "Custom",
            "permission": "reports", "enabled": "on", "description": "",
            "sections": json.dumps(_sections()), "filters": "[]"}, follow=True)
        self.assertEqual(r.status_code, 200)
        return f"/reports/r/def__{key}/"

    def test_heading_component_designer_safe_and_registered(self):
        from core.reporting.components import component_registry
        self.assertTrue(component_registry.has("heading"))
        self.assertTrue(component_registry.is_designer_safe("heading"))

    def test_text_components_grouped_under_text_category(self):
        from core.reporting.components import component_registry
        cats = component_registry.by_category(designer_safe_only=True)
        keys = {c["key"] for c in cats.get("Text", [])}
        self.assertLessEqual({"heading", "commentary", "info_panel"}, keys)

    def test_heading_renders_bare_with_sec_h(self):
        url = self._save("head_rep")
        h = self.client.get(url).content.decode()
        self.assertIn("Part 1 — Income", h)
        self.assertIn("sec-h", h)

    def test_merge_fields_filled(self):
        url = self._save("merge_rep")
        h = self.client.get(url).content.decode()
        self.assertNotIn("{church}", h)
        self.assertNotIn("{period_start}", h)
        self.assertIn(dt.date.today().strftime("%d %b %Y"), h)   # {today}

    def test_unknown_token_passes_through_unharmed(self):
        from core.reporting.component_library import _fill_placeholders
        class Ctx: start = None; end = None
        out = _fill_placeholders("Keep {this} and {period_end}.", Ctx())
        self.assertIn("{this}", out)
        self.assertNotIn("{period_end}", out)

    def test_paragraphs_in_html_and_word(self):
        url = self._save("para_rep")
        h = self.client.get(url).content.decode()
        self.assertGreaterEqual(h.count("<p>For"), 1)
        from core.reporting.wordml import docx_text
        w = docx_text(self.client.get(url + "?export=docx").content)
        self.assertIn("Part 1", w)
        self.assertIn("Period", w)

    def test_tabular_export_skips_text_structure(self):
        url = self._save("csv_rep")
        csv = self.client.get(url + "?export=csv").content.decode()
        self.assertNotIn("Part 1 — Income", csv)

    def test_preview_without_saving(self):
        secs = _sections()
        secs[1]["params"]["text"] = "UNSAVED DRAFT TEXT."
        r = self.client.post("/reports/designer/preview/", {
            "title": "Draft", "sections": json.dumps(secs), "filters": "[]"})
        h = r.content.decode()
        self.assertEqual(r.status_code, 200)
        self.assertIn("UNSAVED DRAFT TEXT.", h)
        self.assertIn("Unsaved preview", h)
        from reports.models import ReportDefinition
        self.assertFalse(ReportDefinition.objects.filter(title="Draft").exists())

    def test_preview_reports_problems_not_tracebacks(self):
        bad = [{"component": "no_such_component"}]
        r = self.client.post("/reports/designer/preview/", {
            "title": "Bad", "sections": json.dumps(bad), "filters": "[]"})
        self.assertEqual(r.status_code, 200)
        self.assertIn("unknown component", r.content.decode())

    def test_preview_requires_designer_access(self):
        plain = User.objects.create_user("plain_dsg", password="x")
        self.client.force_login(plain)
        r = self.client.post("/reports/designer/preview/", {
            "title": "X", "sections": "[]", "filters": "[]"})
        self.assertIn(r.status_code, (302, 403))

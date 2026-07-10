"""Tests for the Report Designer hardening + refinement pass (v2.39):

1. The production crash — ``AttributeError: 'str' object has no attribute
   'get'`` when a section entry was a bare string instead of an object — is
   reproduced exactly and confirmed fixed: ``validate_definition`` and
   ``compile_definition`` never raise, for any malformed shape of section,
   params, layout or filter.
2. The registry's new ``designer_safe`` / ``params_schema`` metadata: chart,
   appendix and financial_statement are excluded from the designer palette
   (they need Python callables/arrays the JSON wire format can't carry) while
   still working in code-defined reports; narrative/commentary/info_panel/
   insights carry a structured params schema.
3. The DesignerEditView end-to-end: the visual builder's wire format (what the
   hidden JSON fields actually post) round-trips through save -> compile ->
   live render; validation problems surface as individual messages and never
   persist a broken definition; a well-known component that WOULD have needed
   a Python callable is rejected with a clear reason rather than crashing at
   render time.
"""
import json

from django.contrib.auth.models import Group, User
from django.test import TestCase
from django.urls import reverse

from core.reporting import registry
from core.reporting.components import component_registry
from core.roles import AUDITOR, TREASURER
from departments.models import Department


def _staff(username, role=TREASURER):
    u = User.objects.create_user(username, password="x")
    u.groups.add(Group.objects.get_or_create(name=role)[0])
    return u


class ProductionCrashReproTests(TestCase):
    """The exact incident: reports/services/designer.py line 66,
    AttributeError: 'str' object has no attribute 'get'."""

    def test_list_of_bare_strings_never_raises(self):
        from reports.services.designer import validate_definition
        bad = {"key": "x", "title": "X",
              "sections": ["executive_summary", "kpi_cards"], "filters": []}
        problems = validate_definition(bad)          # must not raise
        self.assertEqual(len(problems), 2)
        self.assertIn("expected a component block", problems[0])

    def test_compile_raises_definitionerror_not_attributeerror(self):
        from reports.services.designer import DefinitionError, compile_definition
        bad = {"key": "x", "title": "X",
              "sections": ["executive_summary"], "filters": []}
        with self.assertRaises(DefinitionError):
            compile_definition(bad)

    def test_view_post_never_500s_on_the_repro_payload(self):
        self.client.force_login(_staff("pc_tr"))
        r = self.client.post(reverse("designer_new"), {
            "title": "Repro", "enabled": "on",
            "sections": json.dumps(["executive_summary", "kpi_cards"]),
            "filters": "[]",
        })
        self.assertEqual(r.status_code, 200)
        from reports.models import ReportDefinition
        self.assertFalse(ReportDefinition.objects.filter(title="Repro").exists())


class MalformedShapeTests(TestCase):
    """Every shape validate_definition/compile_definition must survive
    without raising — general hardening beyond the one reported incident."""

    def _assert_safe(self, bad_def):
        from reports.services.designer import (DefinitionError,
                                               compile_definition,
                                               validate_definition)
        problems = validate_definition(bad_def)       # must not raise
        self.assertTrue(problems)
        with self.assertRaises(DefinitionError):       # must not raise anything else
            compile_definition(bad_def)

    def test_sections_not_a_list(self):
        self._assert_safe({"sections": "narrative", "filters": []})

    def test_section_is_a_number(self):
        self._assert_safe({"sections": [42], "filters": []})

    def test_section_is_a_list(self):
        self._assert_safe({"sections": [["narrative"]], "filters": []})

    def test_section_missing_component(self):
        self._assert_safe({"sections": [{"title": "no comp"}], "filters": []})

    def test_params_not_a_dict(self):
        self._assert_safe({"sections": [
            {"component": "narrative", "params": "bad"}], "filters": []})

    def test_layout_not_a_dict(self):
        self._assert_safe({"sections": [
            {"component": "kpi_cards", "layout": "bad"}], "filters": []})

    def test_layout_unknown_field(self):
        self._assert_safe({"sections": [
            {"component": "kpi_cards", "layout": {"bogus": 1}}], "filters": []})

    def test_layout_width_wrong_type(self):
        self._assert_safe({"sections": [
            {"component": "kpi_cards", "layout": {"width": "wide"}}],
            "filters": []})

    def test_layout_width_bool_rejected(self):
        # bool is an int subclass in Python — must not silently pass as a width
        self._assert_safe({"sections": [
            {"component": "kpi_cards", "layout": {"width": True}}],
            "filters": []})

    def test_filters_not_a_list(self):
        self._assert_safe({"sections": [{"component": "kpi_cards"}],
                           "filters": "bad"})

    def test_filter_missing_name(self):
        self._assert_safe({"sections": [{"component": "kpi_cards"}],
                           "filters": [{}]})

    def test_empty_definition_object(self):
        from reports.services.designer import validate_definition
        self.assertTrue(validate_definition({}))

    def test_none_definition_fields_tolerated(self):
        from reports.services.designer import validate_definition
        problems = validate_definition({"sections": None, "filters": None})
        self.assertTrue(problems)   # "no sections" — not a crash

    def test_insights_bad_number_param(self):
        self._assert_safe({"sections": [
            {"component": "insights", "params": {"limit": "abc"}}],
            "filters": []})


class DesignerSafeMetadataTests(TestCase):
    """chart/appendix/financial_statement need Python objects the JSON wire
    format can't carry, so they must be excluded from the designer while
    remaining usable in code-defined reports."""

    def test_unsafe_components_flagged(self):
        for key in ("chart", "appendix", "financial_statement"):
            self.assertFalse(component_registry.is_designer_safe(key), key)

    def test_safe_components_flagged(self):
        for key in ("narrative", "kpi_cards", "executive_summary",
                    "commentary", "info_panel", "insights", "fund_summary"):
            self.assertTrue(component_registry.is_designer_safe(key), key)

    def test_unsafe_excluded_from_designer_palette(self):
        palette = component_registry.by_category(designer_safe_only=True)
        all_keys = {c["key"] for comps in palette.values() for c in comps}
        for key in ("chart", "appendix", "financial_statement"):
            self.assertNotIn(key, all_keys)

    def test_unsafe_present_in_full_catalogue(self):
        # the documentation catalogue (component_catalogue view) is unaffected
        full = component_registry.by_category()
        all_keys = {c["key"] for comps in full.values() for c in comps}
        for key in ("chart", "appendix", "financial_statement"):
            self.assertIn(key, all_keys)

    def test_unsafe_component_rejected_by_validation_even_if_referenced(self):
        from reports.services.designer import validate_definition
        d = {"sections": [{"component": "chart"}], "filters": []}
        problems = validate_definition(d)
        self.assertTrue(any("chart" in p and "designer" in p for p in problems))

    def test_narrative_has_required_params_schema(self):
        schema = component_registry._meta["narrative"]["params_schema"]
        self.assertEqual(schema[0]["name"], "narrative_key")
        self.assertTrue(schema[0]["required"])

    def test_number_param_coerced_on_build(self):
        from reports.services.designer import _build_section
        section = _build_section({"component": "insights",
                                  "params": {"limit": "3", "min_priority": "1"}})
        self.assertEqual(section._limit, 3)
        self.assertEqual(section._min_priority, 1)


class DesignerViewEndToEndTests(TestCase):
    """The visual builder's wire format: save -> compile -> live render."""

    def setUp(self):
        Department.objects.create(name="Building", fund_type="LOCAL")
        self.tr = _staff("e2e_tr")
        self.client.force_login(self.tr)

    def test_new_report_page_renders_palette_without_unsafe_components(self):
        r = self.client.get(reverse("designer_new"))
        self.assertEqual(r.status_code, 200)
        html = r.content.decode()
        self.assertIn("ds-palette-item", html)
        self.assertNotIn('data-key="chart"', html)
        self.assertNotIn('data-key="appendix"', html)
        self.assertIn('data-key="narrative"', html)

    def test_save_compile_and_render_round_trip(self):
        sections = [
            {"component": "executive_summary", "layout": {"width": 12, "order": 10}},
            {"component": "narrative", "title": "Summary",
             "params": {"narrative_key": "executive_summary"},
             "layout": {"width": 6, "order": 20}},
        ]
        filters = [{"name": "consolidated", "label": "Consolidate",
                   "kind": "bool", "default": True}]
        r = self.client.post(reverse("designer_new"), {
            "title": "Builder Report", "key": "builder_report",
            "category": "Custom", "permission": "reports", "enabled": "on",
            "sections": json.dumps(sections), "filters": json.dumps(filters)})
        self.assertEqual(r.status_code, 302)

        from reports.models import ReportDefinition
        d = ReportDefinition.objects.get(key="builder_report")
        self.assertEqual(d.engine_key, "def__builder_report")
        self.assertIsNotNone(registry.get(d.engine_key))

        live = self.client.get(reverse("engine_report", args=[d.engine_key])
                               + "?start=2026-01-01&end=2026-12-31")
        self.assertEqual(live.status_code, 200)
        self.assertIn(b"Summary", live.content)

    def test_each_validation_problem_is_its_own_message(self):
        r = self.client.post(reverse("designer_new"), {
            "title": "Multi Bad", "enabled": "on",
            "sections": json.dumps([{"component": "nonexistent"},
                                    {"component": "chart"}]),
            "filters": json.dumps([{"label": "no name"}]),
        })
        self.assertEqual(r.status_code, 200)
        msgs = [str(m) for m in r.context["messages"]]
        self.assertGreaterEqual(len(msgs), 3)
        self.assertTrue(any("nonexistent" in m for m in msgs))
        self.assertTrue(any("chart" in m for m in msgs))
        self.assertTrue(any("name" in m for m in msgs))

    def test_edit_existing_definition_preserves_sections(self):
        from reports.services.designer import register_definition
        from reports.models import ReportDefinition
        d = ReportDefinition.objects.create(
            key="existing_x", title="Existing", category="Custom",
            sections=[{"component": "kpi_cards",
                      "layout": {"width": 12, "order": 10}}])
        register_definition(d)
        r = self.client.get(reverse("designer_edit", args=["existing_x"]))
        self.assertEqual(r.status_code, 200)
        self.assertIn(b"kpi_cards", r.content)

    def test_auditor_cannot_reach_designer(self):
        self.client.force_login(_staff("e2e_aud", AUDITOR))
        r = self.client.get(reverse("designer_new"))
        self.assertIn(r.status_code, (302, 403))

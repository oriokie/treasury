"""Phase 8 — Report Administration Platform tests.

Covers the Report Designer (compile/validate/render, refuses invalid configs),
component configuration via definitions, scheduling (execution + snapshot +
next-run + period policy), distribution recipients, Report Library + favourites +
usage, Feature Adoption Dashboard, snapshot versioning/compare, branding applied
to renderers, and permissions. Backward compatibility: existing engine reports
still render unchanged.
"""
import datetime as dt
from decimal import Decimal

from django.contrib.auth.models import Group, User
from django.test import TestCase, RequestFactory
from django.urls import reverse

from core.reporting import registry
from core.roles import TREASURER, AUDITOR
from departments.models import Department
from giving.models import Transaction


def _staff(username, role=TREASURER):
    u = User.objects.create_user(username, password="x")
    u.groups.add(Group.objects.get_or_create(name=role)[0])
    return u


class DesignerCompileTests(TestCase):
    def setUp(self):
        Department.objects.create(name="Building", fund_type="LOCAL")
        self.tr = _staff("dz_tr")

    def _def(self, **over):
        from reports.models import ReportDefinition
        base = dict(
            key="custom_a", title="Custom A", category="Custom",
            sections=[
                {"component": "kpi_cards", "layout": {"width": 12, "order": 10}},
                {"component": "narrative",
                 "params": {"narrative_key": "executive_summary"},
                 "title": "Summary", "layout": {"width": 12, "order": 20}},
            ])
        base.update(over)
        return ReportDefinition.objects.create(**base)

    def test_valid_definition_compiles_and_renders(self):
        from reports.services.designer import compile_definition
        d = self._def()
        report = compile_definition(d)
        self.assertEqual(report.key, "def__custom_a")
        self.assertEqual(len(report.sections), 2)
        req = RequestFactory().get("/x?start=2026-01-01&end=2026-12-31")
        req.user = self.tr
        rendered = report.render(req)
        self.assertTrue(rendered.sections)

    def test_unknown_component_refused(self):
        from reports.services.designer import validate_definition, DefinitionError, compile_definition
        d = self._def(key="bad_a",
                      sections=[{"component": "does_not_exist"}])
        self.assertTrue(validate_definition(d))
        with self.assertRaises(DefinitionError):
            compile_definition(d)

    def test_unknown_narrative_refused(self):
        from reports.services.designer import validate_definition
        d = self._def(key="bad_b", sections=[
            {"component": "narrative", "params": {"narrative_key": "nope"}}])
        problems = validate_definition(d)
        self.assertTrue(any("nope" in p for p in problems))

    def test_bad_width_refused(self):
        from reports.services.designer import validate_definition
        d = self._def(key="bad_c", sections=[
            {"component": "kpi_cards", "layout": {"width": 99}}])
        problems = validate_definition(d)
        self.assertTrue(any("width" in p for p in problems))

    def test_disabled_section_skipped(self):
        from reports.services.designer import compile_definition
        d = self._def(key="skip_a", sections=[
            {"component": "kpi_cards", "layout": {"width": 12, "order": 10}},
            {"component": "narrative", "enabled": False,
             "params": {"narrative_key": "cash_position"}},
        ])
        report = compile_definition(d)
        self.assertEqual(len(report.sections), 1)

    def test_register_makes_report_available(self):
        from reports.services.designer import register_definition
        d = self._def(key="live_a")
        register_definition(d)
        self.assertIsNotNone(registry.get("def__live_a"))


class DesignerViewTests(TestCase):
    def setUp(self):
        Department.objects.create(name="Building", fund_type="LOCAL")
        self.tr = _staff("dv_tr")

    def test_designer_list_and_new_render(self):
        self.client.force_login(self.tr)
        self.assertEqual(self.client.get(reverse("designer_list")).status_code, 200)
        self.assertEqual(self.client.get(reverse("designer_new")).status_code, 200)

    def test_create_definition_via_post(self):
        import json
        self.client.force_login(self.tr)
        r = self.client.post(reverse("designer_new"), {
            "title": "My New Report", "category": "Custom",
            "permission": "reports", "enabled": "on",
            "sections": json.dumps([
                {"component": "kpi_cards", "layout": {"width": 12, "order": 10}}]),
            "filters": "[]",
        })
        self.assertEqual(r.status_code, 302)
        from reports.models import ReportDefinition
        d = ReportDefinition.objects.get(title="My New Report")
        self.assertTrue(d.enabled)
        self.assertIsNotNone(registry.get(d.engine_key))

    def test_invalid_definition_not_saved(self):
        import json
        self.client.force_login(self.tr)
        r = self.client.post(reverse("designer_new"), {
            "title": "Broken", "enabled": "on",
            "sections": json.dumps([{"component": "nonexistent"}]),
            "filters": "[]",
        })
        self.assertEqual(r.status_code, 200)   # re-rendered with errors
        from reports.models import ReportDefinition
        self.assertFalse(ReportDefinition.objects.filter(title="Broken").exists())

    def test_auditor_cannot_edit(self):
        self.client.force_login(_staff("dv_aud", AUDITOR))
        r = self.client.get(reverse("designer_list"))
        self.assertIn(r.status_code, (302, 403))


class SchedulingTests(TestCase):
    def setUp(self):
        Department.objects.create(name="Building", fund_type="LOCAL")
        self.tr = _staff("sch_tr")

    def _schedule(self, **over):
        from reports.models import ReportSchedule
        base = dict(name="Monthly pack", report_key="income_statement_v2",
                    frequency="MONTHLY", period_policy="ytd", formats=["csv"],
                    created_by=self.tr)
        base.update(over)
        return ReportSchedule.objects.create(**base)

    def test_execute_creates_snapshot_and_run(self):
        from reports.services.scheduling import execute_schedule
        from reports.models import ScheduleRun
        s = self._schedule()
        run = execute_schedule(s, user=self.tr)
        self.assertEqual(run.status, ScheduleRun.Status.SUCCESS)
        self.assertIsNotNone(run.snapshot)
        self.assertTrue(run.snapshot.finalised)

    def test_next_run_computed(self):
        s = self._schedule()
        s.next_run = s.compute_next_run()
        self.assertIsNotNone(s.next_run)

    def test_manual_frequency_has_no_next_run(self):
        s = self._schedule(frequency="MANUAL")
        self.assertIsNone(s.compute_next_run())

    def test_period_policies(self):
        from reports.services.scheduling import _period_for
        today = dt.date(2026, 7, 15)
        self.assertEqual(_period_for("prev_month", today),
                         (dt.date(2026, 6, 1), dt.date(2026, 6, 30)))
        self.assertEqual(_period_for("ytd", today)[0], dt.date(2026, 1, 1))
        self.assertEqual(_period_for("prev_year", today),
                         (dt.date(2025, 1, 1), dt.date(2025, 12, 31)))

    def test_failed_report_captured_not_raised(self):
        from reports.services.scheduling import execute_schedule
        from reports.models import ScheduleRun
        s = self._schedule(report_key="does_not_exist")
        run = execute_schedule(s, user=self.tr)
        self.assertEqual(run.status, ScheduleRun.Status.FAILED)

    def test_run_due_schedules(self):
        from reports.services.scheduling import run_due_schedules
        from django.utils import timezone
        s = self._schedule()
        s.next_run = timezone.now() - dt.timedelta(minutes=1)
        s.save()
        runs = run_due_schedules()
        self.assertTrue(any(r.schedule_id == s.id for r in runs))

    def test_run_view(self):
        self.client.force_login(self.tr)
        s = self._schedule()
        r = self.client.post(reverse("schedule_run", args=[s.pk]))
        self.assertEqual(r.status_code, 302)


class BrandingTests(TestCase):
    def setUp(self):
        Department.objects.create(name="Building", fund_type="LOCAL")
        self.tr = _staff("br_tr")

    def test_only_one_active_branding(self):
        from reports.models import ReportBranding
        ReportBranding.objects.create(name="A", is_active=True)
        ReportBranding.objects.create(name="B", is_active=True)
        self.assertEqual(ReportBranding.objects.filter(is_active=True).count(), 1)
        self.assertEqual(ReportBranding.active().name, "B")

    def test_branding_applied_to_docx(self):
        from reports.models import ReportBranding
        from core.reporting import renderer_registry
        ReportBranding.objects.create(
            name="KWS", is_active=True, church_name="Test Church",
            certification_statement="Certified.", header_text="DRAFT")
        report = registry.get("income_statement_v2")
        req = RequestFactory().get("/x?start=2026-01-01&end=2026-12-31")
        req.user = self.tr
        rendered = report.render(req)
        from core.reporting.wordml import docx_text
        out = docx_text(renderer_registry.get("docx").render(rendered).content)
        self.assertIn("Test Church", out)
        self.assertIn("Certified.", out)
        self.assertIn("DRAFT", out)

    def test_no_branding_falls_back_to_church(self):
        from core.reporting.renderers import resolve_branding
        b = resolve_branding("Fallback Church")
        self.assertEqual(b["church_name"], "Fallback Church")


class LibraryAndUsageTests(TestCase):
    def setUp(self):
        Department.objects.create(name="Building", fund_type="LOCAL")
        self.tr = _staff("lib_tr")

    def test_library_renders(self):
        self.client.force_login(self.tr)
        r = self.client.get(reverse("report_library"))
        self.assertEqual(r.status_code, 200)

    def test_favourite_toggle(self):
        from reports.models import ReportFavourite
        self.client.force_login(self.tr)
        self.client.post(reverse("toggle_favourite", args=["income_statement_v2"]))
        self.assertTrue(ReportFavourite.objects.filter(
            report_key="income_statement_v2", user=self.tr).exists())
        self.client.post(reverse("toggle_favourite", args=["income_statement_v2"]))
        self.assertFalse(ReportFavourite.objects.filter(
            report_key="income_statement_v2", user=self.tr).exists())

    def test_usage_recorded_on_view(self):
        from reports.models import ReportUsage
        self.client.force_login(self.tr)
        self.client.get(reverse("engine_report", args=["income_statement_v2"])
                        + "?start=2026-01-01&end=2026-12-31")
        self.assertTrue(ReportUsage.objects.filter(
            report_key="income_statement_v2").exists())


class AdoptionDashboardTests(TestCase):
    def setUp(self):
        Department.objects.create(name="Building", fund_type="LOCAL")
        self.tr = _staff("ad_tr")

    def test_dashboard_renders(self):
        self.client.force_login(self.tr)
        r = self.client.get(reverse("adoption_dashboard"))
        self.assertEqual(r.status_code, 200)
        self.assertContains(r, "Registered metrics")


class SnapshotVersioningTests(TestCase):
    def setUp(self):
        Department.objects.create(name="Building", fund_type="LOCAL")
        self.tr = _staff("sv_tr")

    def _snap(self):
        from reports.services.snapshots import create_snapshot
        report = registry.get("income_statement_v2")
        req = RequestFactory().get("/x?start=2026-01-01&end=2026-12-31")
        req.user = self.tr
        return create_snapshot(report.render(req), user=self.tr)

    def test_snapshot_history_renders(self):
        self._snap()
        self.client.force_login(self.tr)
        r = self.client.get(reverse("snapshot_history"))
        self.assertEqual(r.status_code, 200)

    def test_compare_two_snapshots(self):
        a = self._snap()
        b = self._snap()
        self.client.force_login(self.tr)
        r = self.client.get(reverse("snapshot_compare", args=[a.id, b.id]))
        self.assertEqual(r.status_code, 200)


class BackwardCompatibilityTests(TestCase):
    def setUp(self):
        Department.objects.create(name="Building", fund_type="LOCAL")
        self.tr = _staff("bc_tr")

    def test_existing_engine_reports_still_render(self):
        self.client.force_login(self.tr)
        for key in ("income_statement_v2", "cash_flow_v2", "board_report_v2",
                    "trial_balance_v2", "fund_balances_v2", "consistency_audit"):
            r = self.client.get(reverse("engine_report", args=[key])
                                + "?start=2026-01-01&end=2026-12-31")
            self.assertEqual(r.status_code, 200, key)

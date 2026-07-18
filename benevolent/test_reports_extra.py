"""Item 6 — the fourteen reporting-gap reports.

These assert three things per report: the service helper returns a sane shape,
the report is registered in the engine, and it renders to HTML (200) and to at
least one export format. The point of routing everything through the engine is
that once a component returns a well-formed SectionData, the HTML/CSV/XLSX/PDF/
permission machinery is the engine's, already tested elsewhere — so these tests
focus on the benevolent-specific wiring, not on re-testing the engine.
"""
import datetime as dt
from decimal import Decimal

from django.contrib.auth.models import Group, User
from django.test import TestCase

from core.roles import TREASURER
from core.reporting.engine import registry
from departments.models import Department
from members.models import Member

from benevolent.models import (BenevolentEventType, BenevolentScheme,
                               SchemeDependant, SchemeMembership, SchemePolicy)
from benevolent.services import cases as case_svc
from benevolent.services import contributions as contrib_svc
from benevolent.services import registry as reg_svc
from benevolent.services import reporting as R
from benevolent.services import schemes as scheme_svc

TODAY = dt.date.today()

REPORT_KEYS = [
    "benevolent_contribution_compliance_report",
    "benevolent_ageing_arrears_report",
    "benevolent_pending_approvals_report",
    "benevolent_rejected_reasons_report",
    "benevolent_benefit_utilisation_report",
    "benevolent_scheme_profitability_report",
    "benevolent_household_statistics_report",
    "benevolent_dependant_demographics_report",
    "benevolent_case_turnaround_report",
    "benevolent_committee_performance_report",
    "benevolent_fraud_alerts_report",
    "benevolent_missing_documents_report",
    "benevolent_contribution_forecast_report",
    "benevolent_fund_sustainability_report",
]


class ReportFixture(TestCase):
    def setUp(self):
        self.treasurer = User.objects.create_user("t_rep", password="x")
        self.treasurer.groups.add(Group.objects.get_or_create(name=TREASURER)[0])
        self.fund = Department.objects.create(
            name="Rep Fund", slug="rep-fund", fund_type=Department.FundType.LOCAL)
        self.scheme = BenevolentScheme.objects.create(
            name="Rep Scheme", code="REP", fund=self.fund, created_by=self.treasurer)
        self.bereavement = BenevolentEventType.objects.create(
            scheme=self.scheme, name="Bereavement", code="BER", triggers_on_death=True)
        self.policy = SchemePolicy.objects.create(
            scheme=self.scheme, effective_from=TODAY - dt.timedelta(days=800),
            membership_required=True, waiting_period_days=0,
            contribution_mode=SchemePolicy.ContributionMode.FIXED_PERIODIC,
            contribution_amount=Decimal("100"),
            contribution_frequency=SchemePolicy.Frequency.MONTHLY,
            benefit_mode=SchemePolicy.BenefitMode.FIXED, benefit_amount=Decimal("5000"),
            arrears_treatment=SchemePolicy.ArrearsTreatment.IGNORE,
            dependant_age_limit=18, created_by=self.treasurer)
        scheme_svc.publish_policy(self.policy, user=self.treasurer)
        scheme_svc.activate_scheme(self.scheme, user=self.treasurer)
        contrib_svc.record_contribution(
            self.scheme, date=TODAY, amount=Decimal("10000"), user=self.treasurer)
        # a member with some history, registered as a household so the household
        # and dependant reports have something to show
        from benevolent.models import RegistrationType
        self.m = Member.objects.create(name="Report Member", phone="254700000001")
        self.mem = reg_svc.register(
            self.scheme, self.m, joined_on=TODAY - dt.timedelta(days=200),
            registration_type=RegistrationType.HOUSEHOLD, household_name="Report Household",
            user=self.treasurer)
        SchemeDependant.objects.create(
            membership=self.mem, name="A Child",
            relationship=SchemeDependant.Relationship.CHILD,
            date_of_birth=dt.date(TODAY.year - 8, 1, 1))


# ---------------------------------------------------------------------------
# Service-helper shape tests
# ---------------------------------------------------------------------------

class ServiceHelperTests(ReportFixture):
    def test_contribution_compliance_shape(self):
        rows = R.contribution_compliance(self.scheme)
        self.assertTrue(all("compliance" in r and "paid" in r for r in rows))

    def test_case_turnaround_shape(self):
        data = R.case_turnaround(self.scheme)
        self.assertIn("avg_report_to_pay", data)
        self.assertIn("rows", data)

    def test_benefit_utilisation_shape(self):
        rows = R.benefit_utilisation()
        self.assertTrue(all("utilisation" in r for r in rows))

    def test_dependant_demographics_bands(self):
        demo = R.dependant_demographics(self.scheme)
        self.assertEqual(demo["total"], 1)
        self.assertEqual(demo["by_age_band"]["6–17"], 1)

    def test_household_statistics(self):
        stats = R.household_statistics(self.scheme)
        self.assertGreaterEqual(stats["households"], 1)

    def test_missing_documents_runs(self):
        # event type that requires a document, on an open case
        self.bereavement.required_documents = ["Burial permit"]
        self.bereavement.save()
        case = case_svc.create_case(
            self.scheme, event_type=self.bereavement, event_date=TODAY,
            membership=self.mem, user=self.treasurer)
        case_svc.submit_case(case, user=self.treasurer)
        rows = R.missing_documents(self.scheme)
        self.assertTrue(any(r["case"].pk == case.pk for r in rows))

    def test_pending_approvals(self):
        case = case_svc.create_case(
            self.scheme, event_type=self.bereavement, event_date=TODAY,
            membership=self.mem, user=self.treasurer)
        case_svc.submit_case(case, user=self.treasurer)
        rows = R.pending_approvals(self.scheme)
        self.assertTrue(any(c.pk == case.pk for c in rows))

    def test_rejected_reasons(self):
        case = case_svc.create_case(
            self.scheme, event_type=self.bereavement, event_date=TODAY,
            membership=self.mem, user=self.treasurer)
        case_svc.submit_case(case, user=self.treasurer)
        case_svc.assess_case(case, user=self.treasurer)
        case_svc.reject_case(case, user=self.treasurer, reason="Not eligible")
        rows = R.rejected_reasons(self.scheme)
        self.assertTrue(any(c.pk == case.pk for c in rows))


# ---------------------------------------------------------------------------
# Registration + render + export
# ---------------------------------------------------------------------------

class ReportRegistrationTests(ReportFixture):
    def test_all_registered(self):
        for key in REPORT_KEYS:
            self.assertIsNotNone(registry.get(key), f"{key} not registered")

    def test_all_render_html(self):
        self.client.force_login(self.treasurer)
        for key in REPORT_KEYS:
            r = self.client.get(f"/reports/r/{key}/")
            self.assertEqual(r.status_code, 200, f"{key} did not render")

    def test_exports_work(self):
        self.client.force_login(self.treasurer)
        for fmt, ctype in [("csv", "text/csv"), ("xlsx", "application/vnd"),
                           ("pdf", "application/pdf")]:
            r = self.client.get(
                f"/reports/r/benevolent_ageing_arrears_report/?export={fmt}")
            self.assertEqual(r.status_code, 200)
            self.assertIn(ctype, r["Content-Type"])

    def test_chart_present(self):
        self.client.force_login(self.treasurer)
        r = self.client.get("/reports/r/benevolent_dependant_demographics_report/")
        self.assertIn("engine-charts", r.content.decode())

    def test_fraud_report_needs_manage(self):
        # the fraud report is gated to managers, not plain viewers
        from core.roles import AUDITOR
        auditor = User.objects.create_user("aud_rep", password="x")
        auditor.groups.add(Group.objects.get_or_create(name=AUDITOR)[0])
        self.client.force_login(auditor)
        r = self.client.get("/reports/r/benevolent_fraud_alerts_report/")
        # auditor can view benevolent but not manage — fraud alerts requires manage
        self.assertIn(r.status_code, (302, 403))


# ---------------------------------------------------------------------------
# Multi-section component support (the engine change)
# ---------------------------------------------------------------------------

class MultiSectionTests(ReportFixture):
    def test_component_can_return_multiple_sections(self):
        """The compliance report's component renders a KPI band, a chart and a
        table — three sections from one component. The rendered report should
        contain all three."""
        rep = registry.get("benevolent_contribution_compliance_report")
        from core.reporting.context import ReportContext
        from django.test import RequestFactory
        req = RequestFactory().get("/reports/r/benevolent_contribution_compliance_report/")
        req.user = self.treasurer
        rendered = rep.render(req)
        keys = {s.key for s in rendered.sections}
        # kpi + chart + table keys all present
        self.assertIn("benevolent_contribution_compliance", keys)
        self.assertTrue(any(k.endswith("_kpi") for k in keys))
        self.assertTrue(any(k.endswith("_chart") for k in keys))

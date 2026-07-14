"""Phase 8 — Reporting, Analytics & Dashboards.

Grouped around the claims Phase 8 makes:

  1. DATA FUNCTIONS       arrears_analysis/committee_report/household_report/
                         audit_summary compute nothing new — they read the
                         same functions the module's own screens already use.
  2. THE ARREARS METRIC   registered once, and arrears_analysis's own total
                         IS that metric's value — never two numbers.
  3. COMPONENTS           produce well-formed SectionData: exportable
                         primitives only, correct totals, correct drill-down.
  4. REGISTERED REPORTS   render as HTML/CSV/XLSX/PDF, respect the scheme
                         filter, respect permissions.
  5. HISTORICAL ACCURACY  a report never re-evaluates a decided case; it
                         reads what was actually approved.
"""
import datetime as dt
from decimal import Decimal

from django.contrib.auth.models import Group, User
from django.test import TestCase
from django.urls import reverse

from core.roles import ASSISTANT, AUDITOR, TREASURER
from departments.models import Department
from members.models import Member

from benevolent.models import (BenevolentCase, BenevolentEventType, BenevolentScheme,
                               SchemeMembership, SchemePolicy)
from benevolent.services import cases as case_svc
from benevolent.services import committee as committee_svc
from benevolent.services import contributions as contrib_svc
from benevolent.services import registry as reg_svc
from benevolent.services import reporting as report_svc
from benevolent.services import schemes as scheme_svc

TODAY = dt.date.today()


class Phase8Fixture(TestCase):
    def setUp(self):
        self.treasurer = User.objects.create_user("t8", password="x")
        self.treasurer.groups.add(Group.objects.get_or_create(name=TREASURER)[0])
        self.clerk = User.objects.create_user("c8", password="x")
        self.clerk.groups.add(Group.objects.get_or_create(name=ASSISTANT)[0])
        self.auditor = User.objects.create_user("a8", password="x")
        self.auditor.groups.add(Group.objects.get_or_create(name=AUDITOR)[0])

        self.fund = Department.objects.create(
            name="P8 Fund", slug="p8-fund", fund_type=Department.FundType.LOCAL,
            category=Department.Category.MINISTRY)
        self.scheme = BenevolentScheme.objects.create(
            name="P8 Scheme", code="P8", fund=self.fund, created_by=self.treasurer)
        self.bereavement = BenevolentEventType.objects.create(
            scheme=self.scheme, name="Bereavement", code="BER")
        self.policy = SchemePolicy.objects.create(
            scheme=self.scheme, effective_from=TODAY - dt.timedelta(days=500),
            membership_required=True, waiting_period_days=0,
            contribution_mode=SchemePolicy.ContributionMode.FIXED_PERIODIC,
            contribution_amount=Decimal("100"),
            benefit_mode=SchemePolicy.BenefitMode.FIXED, benefit_amount=Decimal("10000"),
            arrears_treatment=SchemePolicy.ArrearsTreatment.DEDUCT,
            created_by=self.treasurer)
        scheme_svc.publish_policy(self.policy, user=self.treasurer)
        scheme_svc.activate_scheme(self.scheme, user=self.treasurer)

        self.mary = Member.objects.create(name="Mary Njoroge", phone="254700111222")
        self.john = Member.objects.create(name="John Mwangi", phone="254700111223")

    def _membership(self, member, days_ago=200):
        return reg_svc.register(self.scheme, member,
                                joined_on=TODAY - dt.timedelta(days=days_ago),
                                user=self.treasurer)

    def _paid_case(self, membership):
        case = BenevolentCase.objects.create(
            scheme=self.scheme, membership=membership, event_type=self.bereavement,
            event_date=TODAY - dt.timedelta(days=5), reported_date=TODAY,
            raised_by=self.clerk)
        case_svc.submit_case(case, user=self.clerk)
        case_svc.assess_case(case, user=self.treasurer)
        case_svc.approve_case(case, amount=Decimal("10000"), user=self.treasurer,
                              allow_self_approval=True)
        payout = case_svc.record_payout(case, amount=Decimal("10000"), user=self.clerk)
        payout.expense.status = "APPROVED"
        payout.expense.approved_by = self.treasurer
        payout.expense.save()
        return case


# ===========================================================================
# 1. DATA FUNCTIONS — compute nothing new
# ===========================================================================

class DataFunctionTests(Phase8Fixture):

    def test_arrears_analysis_lists_only_members_actually_owing(self):
        m1 = self._membership(self.mary, days_ago=200)   # owes several months
        m2 = self._membership(self.john, days_ago=1)
        owed = contrib_svc.arrears_for(m2)
        if owed:                                          # settle it in full
            contrib_svc.record_contribution(
                self.scheme, date=TODAY, amount=owed, membership=m2, user=self.treasurer)
        self.assertEqual(contrib_svc.arrears_for(m2), 0)
        rows = report_svc.arrears_analysis(self.scheme)
        ids = [r["membership"].pk for r in rows]
        self.assertIn(m1.pk, ids)
        self.assertNotIn(m2.pk, ids)

    def test_arrears_analysis_agrees_with_arrears_for(self):
        m = self._membership(self.mary, days_ago=200)
        rows = report_svc.arrears_analysis(self.scheme)
        row = next(r for r in rows if r["membership"].pk == m.pk)
        self.assertEqual(row["owed"], contrib_svc.arrears_for(m))

    def test_arrears_total_is_the_sum_of_arrears_analysis(self):
        self._membership(self.mary, days_ago=200)
        self._membership(self.john, days_ago=150)
        rows = report_svc.arrears_analysis(self.scheme)
        self.assertEqual(report_svc.arrears_total(self.scheme),
                         sum((r["owed"] for r in rows), Decimal(0)))

    def test_arrears_ageing_bands(self):
        m = self._membership(self.mary, days_ago=200)
        rows = report_svc.arrears_analysis(self.scheme)
        row = next(r for r in rows if r["membership"].pk == m.pk)
        self.assertIn(row["band"], ("1 period", "2 periods", "3+ periods"))

    def test_committee_report_lists_seats_and_counts_votes(self):
        self._new_committee_policy()
        m = self._membership(self.mary)
        alice = User.objects.create_user("alice8", password="x")
        committee_svc.add_member(self.scheme, alice, added_by=self.treasurer)
        case = self._assessed_committee_case(m)
        case_svc.record_vote(case, user=alice, decision="APPROVE", amount=Decimal("10000"))

        rows = report_svc.committee_report(self.scheme)
        row = next(r for r in rows if r["user"].pk == alice.pk)
        self.assertEqual(row["votes_cast"], 1)

    def test_household_report_lists_dependants(self):
        from benevolent.models import SchemeDependant
        m = self._membership(self.mary)
        m.registration_type = "HOUSEHOLD"
        m.household_name = "The Njoroge Household"
        m.save()
        reg_svc.add_dependant(m, name="Peter Njoroge",
                              relationship=SchemeDependant.Relationship.SPOUSE,
                              user=self.treasurer)
        rows = report_svc.household_report(self.scheme)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["size"], 2)
        self.assertEqual(rows[0]["dependants"][0].name, "Peter Njoroge")

    def test_audit_summary_matches_the_overrides_screen(self):
        m = self._membership(self.mary)
        ex = reg_svc.grant_exemption(m, kind="HARDSHIP", reason="x", user=self.clerk)
        reg_svc.approve_exemption(ex, user=self.treasurer)
        data = report_svc.audit_summary(self.scheme, TODAY - dt.timedelta(days=1),
                                        TODAY + dt.timedelta(days=1))
        self.assertIn(ex, data["exemptions"])

    def _new_committee_policy(self):
        v2 = scheme_svc.new_version_from(
            self.policy, effective_from=TODAY - dt.timedelta(days=400),
            user=self.treasurer)
        v2.approval_mode = SchemePolicy.ApprovalMode.COMMITTEE
        v2.committee_quorum = 1
        v2.save()
        scheme_svc.publish_policy(v2, user=self.treasurer)

    def _assessed_committee_case(self, membership):
        case = BenevolentCase.objects.create(
            scheme=self.scheme, membership=membership, event_type=self.bereavement,
            event_date=TODAY - dt.timedelta(days=2), reported_date=TODAY,
            raised_by=self.clerk)
        case_svc.submit_case(case, user=self.clerk)
        case_svc.assess_case(case, user=self.treasurer)
        return case


# ===========================================================================
# 2. THE ARREARS METRIC
# ===========================================================================

class ArrearsMetricTests(Phase8Fixture):

    def test_the_metric_is_registered(self):
        from core.metrics import metrics
        self.assertTrue(metrics.has("benevolent_arrears"))

    def test_the_metric_matches_the_report_function_exactly(self):
        from core.metrics import metrics
        self._membership(self.mary, days_ago=200)
        self.assertEqual(metrics.benevolent_arrears(self.scheme, TODAY),
                         report_svc.arrears_total(self.scheme, TODAY))

    def test_the_metric_appears_in_the_catalogue(self):
        from core.metrics import metrics
        m = metrics.get("benevolent_arrears")
        self.assertEqual(m.category, "Benevolent")
        self.assertTrue(m.definition)


# ===========================================================================
# 3. COMPONENTS — exportable primitives, correct totals
# ===========================================================================

class ComponentTests(Phase8Fixture):

    def _ctx(self, **kw):
        from core.reporting.context import ReportContext
        return ReportContext.for_period(start=kw.get("start"), end=kw.get("end", TODAY))

    def test_kpi_component_renders(self):
        from benevolent.report_components import BenevolentKpiComponent
        self._membership(self.mary, days_ago=200)
        data = BenevolentKpiComponent().render(self._ctx(), {})
        self.assertEqual(data.kind, "kpi")
        self.assertTrue(any(r.cells["label"] == "Active members" for r in data.rows))

    def test_scheme_summary_component_totals_match_totals_helper(self):
        from benevolent.report_components import BenevolentSchemeSummaryComponent
        self._membership(self.mary)
        data = BenevolentSchemeSummaryComponent().render(self._ctx(), {})
        self.assertIsNotNone(data.total)
        self.assertEqual(data.total.cells["scheme"], "TOTAL")

    def test_no_cell_holds_a_raw_model_instance(self):
        """The bug this suite caught in development: a raw User object in a
        cell crashes XLSX export. Every component's cells must be exportable
        primitives (str, Decimal, int, date) — checked generically so this
        can never silently regress for any component, not just the one that
        broke."""
        from benevolent.report_components import (
            BenevolentArrearsComponent, BenevolentAuditComponent,
            BenevolentBenefitPaymentsComponent, BenevolentCaseReportComponent,
            BenevolentCommitteeComponent, BenevolentContributionSummaryComponent,
            BenevolentFundBalancesComponent, BenevolentHouseholdComponent,
            BenevolentIncomeExpenditureComponent, BenevolentKpiComponent,
            BenevolentMembershipComponent, BenevolentSchemeSummaryComponent)
        m = self._membership(self.mary, days_ago=200)
        self._paid_case(m)
        alice = User.objects.create_user("alice8b", password="x")
        committee_svc.add_member(self.scheme, alice, added_by=self.treasurer)
        ex = reg_svc.grant_exemption(m, kind="HARDSHIP", reason="x", user=self.clerk)
        reg_svc.approve_exemption(ex, user=self.treasurer)

        components = [
            BenevolentKpiComponent(), BenevolentSchemeSummaryComponent(),
            BenevolentContributionSummaryComponent(), BenevolentMembershipComponent(),
            BenevolentHouseholdComponent(), BenevolentCommitteeComponent(),
            BenevolentCaseReportComponent(), BenevolentFundBalancesComponent(),
            BenevolentIncomeExpenditureComponent(), BenevolentArrearsComponent(),
            BenevolentBenefitPaymentsComponent(), BenevolentAuditComponent(),
        ]
        ctx = self._ctx(start=TODAY - dt.timedelta(days=400))
        allowed = (str, int, float, Decimal, dt.date, dt.datetime, type(None))
        for comp in components:
            data = comp.render(ctx, {})
            for row in list(data.rows) + ([data.total] if data.total else []):
                for k, v in row.cells.items():
                    self.assertIsInstance(
                        v, allowed,
                        f"{comp.key}: cell '{k}' holds a {type(v).__name__}, not an "
                        f"exportable primitive — value was {v!r}")

# ===========================================================================
# 4. REGISTERED REPORTS — HTML / CSV / XLSX / PDF, filters, permissions
# ===========================================================================

class RegisteredReportTests(Phase8Fixture):

    REPORT_KEYS = [
        "benevolent_overview", "benevolent_contributions",
        "benevolent_membership_households", "benevolent_committee_report",
        "benevolent_case_report", "benevolent_financial_statement",
        "benevolent_arrears_report", "benevolent_benefit_payments_report",
        "benevolent_audit_report",
    ]

    def setUp(self):
        super().setUp()
        m = self._membership(self.mary, days_ago=200)
        self._paid_case(m)

    def test_every_report_is_registered(self):
        from core.reporting.engine import registry
        for key in self.REPORT_KEYS:
            self.assertIsNotNone(registry.get(key), key)

    def test_every_report_renders_as_html(self):
        self.client.force_login(self.treasurer)
        for key in self.REPORT_KEYS:
            r = self.client.get(reverse("engine_report", args=[key]))
            self.assertEqual(r.status_code, 200, key)

    def test_every_report_exports_to_csv_xlsx_and_pdf(self):
        self.client.force_login(self.treasurer)
        for key in self.REPORT_KEYS:
            for fmt in ("csv", "xlsx", "pdf"):
                r = self.client.get(reverse("engine_report", args=[key]), {"export": fmt})
                self.assertEqual(r.status_code, 200, f"{key} -> {fmt}")

    def test_the_scheme_filter_narrows_results(self):
        fund2 = Department.objects.create(
            name="P8 Fund 2", slug="p8-fund-2", fund_type=Department.FundType.LOCAL)
        other = BenevolentScheme.objects.create(
            name="Other P8", code="OP8", fund=fund2, created_by=self.treasurer)
        self.client.force_login(self.treasurer)
        r_all = self.client.get(reverse("engine_report", args=["benevolent_overview"]))
        r_scoped = self.client.get(reverse("engine_report", args=["benevolent_overview"]),
                                   {"scheme": "P8"})
        self.assertEqual(r_all.status_code, 200)
        self.assertEqual(r_scoped.status_code, 200)
        self.assertIn(b"P8 Scheme", r_scoped.content)
        self.assertNotIn(b"Other P8", r_scoped.content)

    def test_the_scheme_filter_accepts_a_code_case_insensitively(self):
        from benevolent.report_components import _scheme_filter_value
        self.assertEqual(_scheme_filter_value({"scheme": "p8"}), self.scheme)
        self.assertEqual(_scheme_filter_value({"scheme": "P8"}), self.scheme)
        self.assertIsNone(_scheme_filter_value({"scheme": ""}))
        self.assertIsNone(_scheme_filter_value({"scheme": "NOPE"}))

    def test_view_only_users_can_see_the_ordinary_reports(self):
        self.client.force_login(self.auditor)
        r = self.client.get(reverse("engine_report", args=["benevolent_overview"]))
        self.assertEqual(r.status_code, 200)

    def test_audit_report_requires_manage_rights_not_just_view(self):
        self.client.force_login(self.auditor)
        r = self.client.get(reverse("engine_report", args=["benevolent_audit_report"]))
        self.assertEqual(r.status_code, 403)

    def test_a_user_with_no_staff_role_at_all_is_redirected_not_shown_the_report(self):
        """A complete outsider is blocked at the outer report-access gate
        (ReportAccessMixin), which redirects rather than 403s — the same
        existing behaviour every other report in the system already has.
        The finer-grained "staff, but not permitted THIS report" case is
        covered separately (test_audit_report_requires_manage_rights) and
        correctly 403s, because that check happens one layer in, on the
        Report's own `permission=`."""
        outsider = User.objects.create_user("outsider8", password="x")
        self.client.force_login(outsider)
        r = self.client.get(reverse("engine_report", args=["benevolent_overview"]))
        self.assertEqual(r.status_code, 302)

    def test_the_reports_appear_in_the_report_library(self):
        self.client.force_login(self.treasurer)
        body = self.client.get(reverse("report_library")).content.decode()
        self.assertIn("Benevolent", body)

    def test_case_report_drills_down_to_the_case_detail_page(self):
        m2 = self._membership(self.john, days_ago=50)
        case = BenevolentCase.objects.create(
            scheme=self.scheme, membership=m2, event_type=self.bereavement,
            event_date=TODAY, reported_date=TODAY, raised_by=self.clerk)
        self.client.force_login(self.treasurer)
        body = self.client.get(reverse("engine_report", args=["benevolent_case_report"])
                               ).content.decode()
        self.assertIn(f"/benevolent/cases/{case.pk}/", body)


# ===========================================================================
# 5. HISTORICAL ACCURACY — never re-evaluates a decided case
# ===========================================================================

class HistoricalAccuracyTests(Phase8Fixture):

    def test_the_case_report_shows_the_amount_actually_approved_not_a_re_evaluation(self):
        m = self._membership(self.mary, days_ago=200)
        case = self._paid_case(m)
        # change the LIVE policy after the fact — a naive report re-running
        # eligibility would now show a different figure than what was
        # actually decided
        v2 = scheme_svc.new_version_from(
            self.policy, effective_from=TODAY - dt.timedelta(days=1),
            user=self.treasurer)
        v2.benefit_amount = Decimal("99999")
        v2.save()
        scheme_svc.publish_policy(v2, user=self.treasurer)

        self.client.force_login(self.treasurer)
        body = self.client.get(reverse("engine_report", args=["benevolent_case_report"])
                               ).content.decode()
        self.assertIn("10,000", body.replace("&nbsp;", " "))
        self.assertNotIn("99,999", body)

    def test_the_benefit_payments_report_only_totals_effective_vouchers(self):
        from benevolent.report_components import BenevolentBenefitPaymentsComponent
        m = self._membership(self.mary, days_ago=200)
        case = self._paid_case(m)
        # a second, still-pending payout must show but not add to the total
        case.approved_amount = Decimal("20000")
        case.status = BenevolentCase.Status.PARTLY_PAID
        case.save()
        case_svc.record_payout(case, amount=Decimal("5000"), user=self.clerk)

        from core.reporting.context import ReportContext
        # The window is deliberately widened by a day at each end. `TODAY` is
        # captured when this module is IMPORTED, but the payouts above are dated
        # when they are CREATED — so a long suite that happens to cross midnight
        # dated them into a "tomorrow" the window did not include, leaving zero
        # rows, a None total, and an AttributeError. This test is about which
        # vouchers count towards the total, not about what time of day it runs.
        today = dt.date.today()
        ctx = ReportContext.for_period(start=today - dt.timedelta(days=31),
                                       end=today + dt.timedelta(days=1))
        data = BenevolentBenefitPaymentsComponent().render(ctx, {})
        self.assertIsNotNone(data.total, "no payouts fell inside the report window")
        self.assertEqual(data.total.cells["amount"], Decimal("10000"))   # not 15000
        self.assertEqual(len(data.rows), 2)                              # but both shown

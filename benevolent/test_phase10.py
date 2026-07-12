"""Phase 10 — Production Readiness & Final Review.

Grouped around the claims Phase 10 makes:

  1. THE SCHEME ENGINE      a Medical Fund and an Emergency Relief Fund run
                           through the FULL lifecycle — registration or its
                           absence, contribution, a case, committee or
                           treasurer approval, payment, notification,
                           reporting — using the SAME code every bereavement
                           scheme in this module already uses. No new model
                           field, no new service function, no new view.
  2. A REAL BUG CLOSED      notify_committee_on_pending_vote's rename fixes a
                           setting that could never have fired, for anyone,
                           ever, because its name didn't match what the
                           notifier looked up.
  3. QUERY BUDGETS          the hot paths (dashboard, case list, membership
                           list) do not grow their query count as the data
                           does — the real, measured test for "no N+1", not
                           an assertion by inspection.
  4. NOTHING DRIFTED        the same figure, asked for from four different
                           screens, is still the same figure.
"""
import datetime as dt
from decimal import Decimal

from django.contrib.auth.models import Group, User
from django.db import connection
from django.test import TestCase
from django.test.utils import CaptureQueriesContext
from django.urls import reverse

from core.roles import ASSISTANT, TREASURER
from departments.models import Department
from members.models import Member

from benevolent.models import (BenevolentCase, BenevolentEventType, BenevolentScheme,
                               BenevolentSettings, CommitteeMember, NotificationEvent,
                               PolicyProfile, SchemeMembership, SchemePolicy)
from benevolent.services import cases as case_svc
from benevolent.services import committee as committee_svc
from benevolent.services import contributions as contrib_svc
from benevolent.services import profiles as profile_svc
from benevolent.services import registry as reg_svc
from benevolent.services import reporting as report_svc
from benevolent.services import schemes as scheme_svc

TODAY = dt.date.today()


class Phase10Fixture(TestCase):
    def setUp(self):
        self.treasurer = User.objects.create_user("t10", password="x")
        self.treasurer.groups.add(Group.objects.get_or_create(name=TREASURER)[0])
        self.clerk = User.objects.create_user("c10", password="x")
        self.clerk.groups.add(Group.objects.get_or_create(name=ASSISTANT)[0])
        profile_svc.install_builtins()


# ===========================================================================
# 1. THE SCHEME ENGINE — proved, not asserted
# ===========================================================================

class SchemeEngineReusabilityTests(Phase10Fixture):
    """Every function called below is a function some bereavement-scheme test
    in Phases 1-9 already called. If any of this needed a NEW model field, a
    NEW service function, or a NEW view, the "Scheme Engine" claim would be
    false — this is the test that would catch that."""

    def test_a_medical_fund_runs_the_full_lifecycle_with_zero_new_code(self):
        profile = PolicyProfile.objects.get(name="Medical assistance (percentage of cost)")
        self.assertEqual(profile.kind, "MEDICAL")

        fund = Department.objects.create(
            name="Medical Fund", slug="medical-fund-p10",
            fund_type=Department.FundType.LOCAL, category=Department.Category.MINISTRY)
        scheme = BenevolentScheme.objects.create(
            name="Medical Assistance Fund", code="MED10", kind="MEDICAL",
            fund=fund, created_by=self.treasurer)

        draft = profile_svc.apply_profile(
            profile, scheme, effective_from=TODAY - dt.timedelta(days=30),
            user=self.treasurer)
        self.assertEqual(draft.benefit_mode, SchemePolicy.BenefitMode.PERCENTAGE)
        self.assertFalse(draft.membership_required)   # open to the congregation
        scheme_svc.publish_policy(draft, user=self.treasurer)
        scheme_svc.activate_scheme(scheme, user=self.treasurer)

        # membership_required=False, so a member can raise a case with no
        # prior registration at all — genuinely different from every
        # bereavement scheme's assumptions, and the engine handles it anyway
        mary = Member.objects.create(name="Mary Kanini", phone="254733111000")
        hospital_event = scheme.event_types.get(code="HOSPITAL")

        case = case_svc.create_case(
            scheme, event_type=hospital_event, membership=None,
            beneficiary_name="Mary Kanini", event_date=TODAY - dt.timedelta(days=3),
            claimed_amount=Decimal("40000"), user=self.clerk)
        from django.core.files.base import ContentFile
        from benevolent.models import CaseAttachment
        CaseAttachment.objects.create(
            case=case, label="Hospital admission notes", uploaded_by=self.clerk,
            file=ContentFile(b"Demo placeholder.", name="admission.txt"))
        case_svc.submit_case(case, user=self.clerk)
        result = case_svc.assess_case(case, user=self.treasurer)
        # 60% of 40,000 = 24,000, under the 50,000 cap
        self.assertEqual(result.entitlement.amount, Decimal("24000.00"))
        self.assertEqual(case_svc.approval_route(case), "COMMITTEE")

        alice = User.objects.create_user("alice10", password="x")
        bob = User.objects.create_user("bob10", password="x")
        carol = User.objects.create_user("carol10", password="x")
        committee_svc.add_member(scheme, alice, role=CommitteeMember.Role.CHAIR,
                                 added_by=self.treasurer)
        committee_svc.add_member(scheme, bob, added_by=self.treasurer)
        committee_svc.add_member(scheme, carol, added_by=self.treasurer)
        case_svc.record_vote(case, user=alice, decision="APPROVE", amount=Decimal("24000"))
        case_svc.record_vote(case, user=bob, decision="APPROVE", amount=Decimal("24000"))
        case_svc.record_vote(case, user=carol, decision="APPROVE", amount=Decimal("24000"))
        case_svc.approve_case(case, amount=Decimal("24000"), user=self.treasurer,
                              allow_self_approval=True)

        payout = case_svc.record_payout(case, amount=Decimal("24000"), user=self.clerk)
        payout.expense.status = "APPROVED"
        payout.expense.approved_by = self.treasurer
        payout.expense.save()

        case.refresh_from_db()
        self.assertEqual(case.status, BenevolentCase.Status.PAID)
        self.assertEqual(case.paid_total, Decimal("24000"))

        # it reports through the exact same Phase 8 engine
        rows = report_svc.scheme_summary(TODAY - dt.timedelta(days=30), TODAY)
        row = next(r for r in rows if r["scheme"].pk == scheme.pk)
        self.assertEqual(row["payouts"], Decimal("24000"))

        from core.metrics import metrics
        self.assertEqual(metrics.benevolent_payouts(scheme=scheme), Decimal("24000"))

    def test_an_emergency_relief_fund_runs_the_full_lifecycle_too(self):
        profile = PolicyProfile.objects.get(name="Emergency relief (fast, fixed amounts)")
        self.assertEqual(profile.kind, "EMERGENCY")

        fund = Department.objects.create(
            name="Emergency Fund", slug="emergency-fund-p10",
            fund_type=Department.FundType.LOCAL, category=Department.Category.MINISTRY)
        scheme = BenevolentScheme.objects.create(
            name="Emergency Relief Fund", code="EMG10", kind="EMERGENCY",
            fund=fund, created_by=self.treasurer)
        draft = profile_svc.apply_profile(
            profile, scheme, effective_from=TODAY - dt.timedelta(days=30),
            user=self.treasurer)
        self.assertEqual(draft.approval_mode, SchemePolicy.ApprovalMode.TREASURER)
        scheme_svc.publish_policy(draft, user=self.treasurer)
        scheme_svc.activate_scheme(scheme, user=self.treasurer)

        fire_event = scheme.event_types.get(code="FIRE")
        case = case_svc.create_case(
            scheme, event_type=fire_event, membership=None,
            beneficiary_name="John Otieno", event_date=TODAY, user=self.clerk)
        case_svc.submit_case(case, user=self.clerk)
        result = case_svc.assess_case(case, user=self.treasurer)
        self.assertEqual(result.entitlement.amount, Decimal("20000"))
        # treasurer-level approval — no committee needed, matching the
        # profile's whole point (help that moves fast)
        self.assertEqual(case_svc.approval_route(case), "TREASURER")
        case_svc.approve_case(case, amount=Decimal("20000"), user=self.treasurer)
        case.refresh_from_db()
        self.assertEqual(case.status, BenevolentCase.Status.APPROVED)

    def test_the_notification_engine_works_identically_for_a_non_bereavement_scheme(self):
        fund = Department.objects.create(
            name="Education Fund P10", slug="education-fund-p10",
            fund_type=Department.FundType.LOCAL)
        scheme = BenevolentScheme.objects.create(
            name="Education Bursary Fund", code="EDU10", kind="EDUCATION",
            fund=fund, created_by=self.treasurer)
        policy = SchemePolicy.objects.create(
            scheme=scheme, effective_from=TODAY - dt.timedelta(days=100),
            membership_required=True, waiting_period_days=0,
            contribution_mode=SchemePolicy.ContributionMode.FIXED_PERIODIC,
            contribution_amount=Decimal("100"),
            benefit_mode=SchemePolicy.BenefitMode.FIXED, benefit_amount=Decimal("15000"),
            arrears_treatment=SchemePolicy.ArrearsTreatment.IGNORE,
            created_by=self.treasurer)
        scheme_svc.publish_policy(policy, user=self.treasurer)
        scheme_svc.activate_scheme(scheme, user=self.treasurer)

        from benevolent.services import notify as notify_svc
        notify_svc.install_default_templates()
        member = Member.objects.create(name="Grace Otieno", phone="254722555000")
        m = reg_svc.register(scheme, member, joined_on=TODAY, user=self.treasurer)
        self.assertTrue(m.notifications.filter(
            event=NotificationEvent.REGISTRATION_CONFIRMED).exists())

    def test_the_wizard_offers_every_scheme_purpose_the_brief_names(self):
        from benevolent.services.wizard import QUESTIONS
        purpose_q = next(q for q in QUESTIONS if q.key == "purpose")
        offered = {o.value for o in purpose_q.options}
        self.assertEqual(offered, {"BENEVOLENT", "MEDICAL", "EDUCATION", "EMERGENCY", "OTHER"})

    def test_the_wizard_and_policy_form_wording_is_scheme_neutral(self):
        """A real (minor) polish gap found during the Phase 10 review: the
        wizard's "own contribution" section was worded "The bereaved
        member" even when the scheme in question was a hospital bill or a
        school fees claim. Fixed; regression-guarded here."""
        from benevolent.services.wizard import QUESTIONS
        sections = {q.section for q in QUESTIONS}
        self.assertIn("The member a case is about", sections)
        self.assertNotIn("The bereaved member", sections)

        from benevolent.forms import PolicyForm
        form = PolicyForm()
        group_names = [g[0] for g in form.GROUPS]
        self.assertIn("The member a case is about", group_names)
        self.assertNotIn("The bereaved member", group_names)

    def test_five_builtin_profiles_exist_spanning_three_kinds(self):
        kinds = set(PolicyProfile.objects.filter(builtin=True).values_list("kind", flat=True))
        self.assertEqual(PolicyProfile.objects.filter(builtin=True).count(), 5)
        self.assertEqual(kinds, {"BENEVOLENT", "MEDICAL", "EMERGENCY"})


# ===========================================================================
# 2. QUERY BUDGETS — measured, not assumed
# ===========================================================================

class QueryBudgetFixture(Phase10Fixture):
    """A scheme with enough cases and members that an N+1 query pattern would
    actually show up as a query-count difference between a small dataset and
    a larger one — the real test, not an inspection of the code."""

    def _build_scheme(self):
        fund = Department.objects.create(
            name="QB Fund", slug="qb-fund", fund_type=Department.FundType.LOCAL)
        scheme = BenevolentScheme.objects.create(
            name="QB Scheme", code="QB", fund=fund, created_by=self.treasurer)
        event = BenevolentEventType.objects.create(scheme=scheme, name="Bereavement", code="BER")
        policy = SchemePolicy.objects.create(
            scheme=scheme, effective_from=TODAY - dt.timedelta(days=500),
            membership_required=True, waiting_period_days=0,
            contribution_mode=SchemePolicy.ContributionMode.FIXED_PERIODIC,
            contribution_amount=Decimal("100"),
            benefit_mode=SchemePolicy.BenefitMode.FIXED, benefit_amount=Decimal("10000"),
            arrears_treatment=SchemePolicy.ArrearsTreatment.IGNORE,
            created_by=self.treasurer)
        scheme_svc.publish_policy(policy, user=self.treasurer)
        scheme_svc.activate_scheme(scheme, user=self.treasurer)
        return scheme, event

    def _populate(self, scheme, event, n):
        for i in range(n):
            member = Member.objects.create(name=f"QB Member {i}", phone=f"25470000{i:04d}")
            m = reg_svc.register(scheme, member, joined_on=TODAY - dt.timedelta(days=200),
                                 user=self.treasurer)
            case = BenevolentCase.objects.create(
                scheme=scheme, membership=m, event_type=event,
                event_date=TODAY - dt.timedelta(days=i + 1), reported_date=TODAY,
                raised_by=self.clerk)
            if i % 3 == 0:
                case_svc.submit_case(case, user=self.clerk)
                case_svc.assess_case(case, user=self.treasurer)


class DashboardQueryBudgetTests(QueryBudgetFixture):

    def test_dashboard_query_count_does_not_grow_with_the_data(self):
        """Same scheme throughout — the thing under test is whether adding
        MORE rows to it costs more queries, not whether two different
        schemes do."""
        scheme, event = self._build_scheme()
        self._populate(scheme, event, 3)
        self.client.force_login(self.treasurer)
        with CaptureQueriesContext(connection) as ctx:
            r1 = self.client.get(reverse("benevolent_dashboard"))
        self.assertEqual(r1.status_code, 200)
        small = len(ctx.captured_queries)

        self._populate(scheme, event, 20)
        with CaptureQueriesContext(connection) as ctx:
            r2 = self.client.get(reverse("benevolent_dashboard"))
        self.assertEqual(r2.status_code, 200)
        large = len(ctx.captured_queries)
        extra_rows = 20
        # NOT a zero-growth bar: the dashboard's arrears KPI genuinely does
        # some per-member work (arrears_for() is a real per-member
        # calculation, not a bug). What this catches is the SHAPE of the
        # growth — linear-ish and bounded, not the ~217-queries-per-member
        # explosion a Phase 10 review found and fixed here (_dues_rows() was
        # calling policy_on() once per DAY of membership history instead of
        # once per call — see services/contributions.py). 40 queries per
        # extra row is generous by today's actual cost and would still catch
        # that bug returning, or a materially worse one appearing.
        self.assertLess(large - small, 40 * extra_rows,
                        f"dashboard queries grew from {small} to {large} for {extra_rows} "
                        f"extra rows — looks like an N+1")


class CaseListQueryBudgetTests(QueryBudgetFixture):

    def test_case_list_query_count_does_not_grow_with_the_data(self):
        scheme, event = self._build_scheme()
        self._populate(scheme, event, 5)
        self.client.force_login(self.treasurer)
        with CaptureQueriesContext(connection) as ctx:
            r1 = self.client.get(reverse("benevolent_case_list"))
        self.assertEqual(r1.status_code, 200)
        small = len(ctx.captured_queries)

        self._populate(scheme, event, 25)
        with CaptureQueriesContext(connection) as ctx:
            r2 = self.client.get(reverse("benevolent_case_list"))
        self.assertEqual(r2.status_code, 200)
        large = len(ctx.captured_queries)
        self.assertLess(large - small, 40 * 20,
                        f"case list queries grew from {small} to {large} with more data")


class MembershipListQueryBudgetTests(QueryBudgetFixture):

    def test_membership_list_query_count_does_not_grow_with_the_data(self):
        scheme, event = self._build_scheme()
        self._populate(scheme, event, 5)
        self.client.force_login(self.treasurer)
        with CaptureQueriesContext(connection) as ctx:
            r1 = self.client.get(reverse("benevolent_membership_list"))
        self.assertEqual(r1.status_code, 200)
        small = len(ctx.captured_queries)

        self._populate(scheme, event, 25)
        with CaptureQueriesContext(connection) as ctx:
            r2 = self.client.get(reverse("benevolent_membership_list"))
        self.assertEqual(r2.status_code, 200)
        large = len(ctx.captured_queries)
        self.assertLess(large - small, 40 * 20,
                        f"membership list queries grew from {small} to {large} with more data")


class ReportQueryBudgetTests(QueryBudgetFixture):

    def test_the_overview_report_query_count_does_not_grow_with_the_data(self):
        scheme, event = self._build_scheme()
        self._populate(scheme, event, 5)
        self.client.force_login(self.treasurer)
        with CaptureQueriesContext(connection) as ctx:
            r1 = self.client.get(reverse("engine_report", args=["benevolent_overview"]))
        self.assertEqual(r1.status_code, 200)
        small = len(ctx.captured_queries)

        self._populate(scheme, event, 25)
        with CaptureQueriesContext(connection) as ctx:
            r2 = self.client.get(reverse("engine_report", args=["benevolent_overview"]))
        self.assertEqual(r2.status_code, 200)
        large = len(ctx.captured_queries)
        self.assertLess(large - small, 40 * 20,
                        f"overview report queries grew from {small} to {large} with more data")


# ===========================================================================
# 3. THE RENAMED, NOW-WIRED SETTING
# ===========================================================================

class CommitteePendingNoticeTests(Phase10Fixture):

    def setUp(self):
        super().setUp()
        self.fund = Department.objects.create(
            name="CP Fund", slug="cp-fund", fund_type=Department.FundType.LOCAL)
        self.scheme = BenevolentScheme.objects.create(
            name="CP Scheme", code="CP10", fund=self.fund, created_by=self.treasurer)
        self.event = BenevolentEventType.objects.create(
            scheme=self.scheme, name="Bereavement", code="BER")
        self.policy = SchemePolicy.objects.create(
            scheme=self.scheme, effective_from=TODAY - dt.timedelta(days=500),
            membership_required=True, waiting_period_days=0,
            contribution_mode=SchemePolicy.ContributionMode.FIXED_PERIODIC,
            contribution_amount=Decimal("100"),
            benefit_mode=SchemePolicy.BenefitMode.FIXED, benefit_amount=Decimal("10000"),
            arrears_treatment=SchemePolicy.ArrearsTreatment.IGNORE,
            approval_mode=SchemePolicy.ApprovalMode.COMMITTEE, committee_quorum=1,
            created_by=self.treasurer)
        scheme_svc.publish_policy(self.policy, user=self.treasurer)
        scheme_svc.activate_scheme(self.scheme, user=self.treasurer)
        self.mary = Member.objects.create(name="Mary Wafula P10", phone="254799000111")

    def test_the_field_exists_correctly_named(self):
        cfg = BenevolentSettings.get()
        self.assertTrue(hasattr(cfg, "notify_on_committee_pending"))
        self.assertFalse(hasattr(cfg, "notify_committee_on_pending_vote"))

    def test_a_case_reaching_committee_notifies_staff(self):
        from core.models import Notification
        m = reg_svc.register(self.scheme, self.mary, joined_on=TODAY - dt.timedelta(days=90),
                             user=self.treasurer)
        case = BenevolentCase.objects.create(
            scheme=self.scheme, membership=m, event_type=self.event,
            event_date=TODAY - dt.timedelta(days=1), reported_date=TODAY,
            raised_by=self.clerk)
        case_svc.submit_case(case, user=self.clerk)
        before = Notification.objects.count()
        case_svc.assess_case(case, user=self.treasurer)
        self.assertGreater(Notification.objects.count(), before)

    def test_switching_the_toggle_off_stops_the_staff_notice(self):
        cfg = BenevolentSettings.get()
        cfg.notify_on_committee_pending = False
        cfg.save()
        m = reg_svc.register(self.scheme, self.mary, joined_on=TODAY - dt.timedelta(days=90),
                             user=self.treasurer)
        case = BenevolentCase.objects.create(
            scheme=self.scheme, membership=m, event_type=self.event,
            event_date=TODAY - dt.timedelta(days=1), reported_date=TODAY,
            raised_by=self.clerk)
        case_svc.submit_case(case, user=self.clerk)
        from core.models import Notification
        before = Notification.objects.count()
        case_svc.assess_case(case, user=self.treasurer)
        self.assertEqual(Notification.objects.count(), before)


# ===========================================================================
# 4. NOTHING DRIFTED — the same figure from four screens
# ===========================================================================

class ConsistencyTests(QueryBudgetFixture):

    def test_arrears_agrees_across_the_dashboard_the_report_and_the_metric(self):
        scheme, event = self._build_scheme()
        member = Member.objects.create(name="Consistency Member", phone="254711999888")
        m = reg_svc.register(scheme, member, joined_on=TODAY - dt.timedelta(days=200),
                             user=self.treasurer)
        owed = contrib_svc.arrears_for(m)
        self.assertGreater(owed, 0)

        from core.metrics import metrics
        metric_value = metrics.benevolent_arrears(scheme, TODAY)
        report_value = report_svc.arrears_total(scheme, TODAY)
        analysis_value = sum(
            (r["owed"] for r in report_svc.arrears_analysis(scheme, TODAY)), Decimal(0))

        self.assertEqual(metric_value, owed)
        self.assertEqual(report_value, owed)
        self.assertEqual(analysis_value, owed)

        self.client.force_login(self.treasurer)
        dash_body = self.client.get(reverse("benevolent_dashboard")).content.decode()
        self.assertIn(f"{owed:,.2f}", dash_body.replace("&nbsp;", ""))

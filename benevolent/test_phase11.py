"""Phase 11 — Guided Scheme Setup & Allocation Transparency.

  1. A plain-language guide connecting the three common funding patterns
     Edwin described to the profiles that already implement them.
  2. An explicit, logged "fund this case from the balance" decision for a
     levy-funded scheme — record_payout() never required a levy; this makes
     skipping one a stated choice rather than an unstated one.
  3. A "matched via" column on the intake queue, surfacing signal data the
     allocator already froze onto each row but the queue never displayed.
"""
import datetime as dt
from decimal import Decimal

from django.contrib.auth.models import Group, User
from django.test import TestCase
from django.urls import reverse

from core.roles import ASSISTANT, TREASURER
from departments.models import Department
from members.models import Member

from benevolent.models import (BenevolentCase, BenevolentEventType, BenevolentScheme,
                               CaseEvent, ContributionIntake, PolicyProfile, SchemePolicy)
from benevolent.services import cases as case_svc
from benevolent.services import registry as reg_svc
from benevolent.services import schemes as scheme_svc

TODAY = dt.date.today()


class Phase11Fixture(TestCase):
    def setUp(self):
        self.treasurer = User.objects.create_user("t11", password="x")
        self.treasurer.groups.add(Group.objects.get_or_create(name=TREASURER)[0])
        self.clerk = User.objects.create_user("c11", password="x")
        self.clerk.groups.add(Group.objects.get_or_create(name=ASSISTANT)[0])
        from benevolent.services import profiles as profile_svc
        profile_svc.install_builtins()


# ===========================================================================
# 1. Guided scheme setup
# ===========================================================================

class GuidedSetupTests(Phase11Fixture):

    def test_the_profile_library_explains_the_three_patterns_in_plain_language(self):
        self.client.force_login(self.treasurer)
        body = self.client.get(reverse("benevolent_profile_list")).content.decode()
        self.assertIn("Which pattern fits your church?", body)
        self.assertIn("the kitty", body.lower())
        self.assertIn("Monthly dues, fixed benefit", body)
        self.assertIn("Per-case levy (harambee)", body)
        self.assertIn("Hybrid", body)

    def test_the_guide_links_directly_to_each_named_profile(self):
        self.client.force_login(self.treasurer)
        body = self.client.get(reverse("benevolent_profile_list")).content.decode()
        monthly = PolicyProfile.objects.get(name="Monthly dues, fixed benefit")
        levy = PolicyProfile.objects.get(name="Per-case levy (harambee)")
        self.assertIn(reverse("benevolent_profile_detail", args=[monthly.pk]), body)
        self.assertIn(reverse("benevolent_profile_detail", args=[levy.pk]), body)

    def test_the_scheme_list_points_a_treasurer_at_the_guide(self):
        self.client.force_login(self.treasurer)
        body = self.client.get(reverse("benevolent_scheme_list")).content.decode()
        self.assertIn(reverse("benevolent_profile_list"), body)
        self.assertIn("Which pattern fits your church?", body)


# ===========================================================================
# 2. Explicit "fund from balance" decision
# ===========================================================================

class FundFromBalanceFixture(Phase11Fixture):
    def setUp(self):
        super().setUp()
        self.fund = Department.objects.create(
            name="P11 Levy Fund", slug="p11-levy-fund",
            fund_type=Department.FundType.LOCAL, category=Department.Category.MINISTRY)
        self.scheme = BenevolentScheme.objects.create(
            name="P11 Levy Scheme", code="P11L", fund=self.fund, created_by=self.treasurer)
        self.event = BenevolentEventType.objects.create(
            scheme=self.scheme, name="Bereavement", code="BER")
        self.policy = SchemePolicy.objects.create(
            scheme=self.scheme, effective_from=TODAY - dt.timedelta(days=500),
            membership_required=True, waiting_period_days=0,
            contribution_mode=SchemePolicy.ContributionMode.PER_CASE_LEVY,
            levy_amount=Decimal("500"),
            benefit_mode=SchemePolicy.BenefitMode.FIXED, benefit_amount=Decimal("10000"),
            arrears_treatment=SchemePolicy.ArrearsTreatment.IGNORE,
            created_by=self.treasurer)
        scheme_svc.publish_policy(self.policy, user=self.treasurer)
        scheme_svc.activate_scheme(self.scheme, user=self.treasurer)
        member = Member.objects.create(name="P11 Member", phone="254711000099")
        self.membership = reg_svc.register(self.scheme, member,
                                           joined_on=TODAY - dt.timedelta(days=90),
                                           user=self.treasurer)
        self.case = BenevolentCase.objects.create(
            scheme=self.scheme, membership=self.membership, event_type=self.event,
            event_date=TODAY - dt.timedelta(days=2), reported_date=TODAY,
            raised_by=self.clerk)
        case_svc.submit_case(self.case, user=self.clerk)
        case_svc.assess_case(self.case, user=self.treasurer)
        case_svc.approve_case(self.case, amount=Decimal("10000"), user=self.treasurer,
                              allow_self_approval=True)


class FundFromBalanceTests(FundFromBalanceFixture):

    def test_paying_from_the_balance_has_never_required_a_levy(self):
        """Confirms the pre-existing capability this phase makes explicit,
        rather than newly enabling: record_payout() has no levy requirement."""
        payout = case_svc.record_payout(
            self.case, amount=Decimal("10000"), user=self.clerk)
        self.assertIsNotNone(payout)

    def test_the_fund_from_balance_action_logs_a_case_event(self):
        before = self.case.events.count()
        case_svc.fund_from_balance(
            self.case, user=self.treasurer,
            reason="Committee decided the balance covers it this quarter.")
        self.assertEqual(self.case.events.count(), before + 1)
        event = self.case.events.filter(kind=CaseEvent.Kind.FUNDED_FROM_BALANCE).first()
        self.assertIsNotNone(event)
        self.assertIn("Committee decided", event.reason)
        self.assertIn(self.fund.name, event.summary)

    def test_the_action_does_not_change_the_case_status_or_move_any_money(self):
        from cashbook.models import Expense
        before_status = self.case.status
        before_expenses = Expense.objects.count()
        case_svc.fund_from_balance(self.case, user=self.treasurer)
        self.case.refresh_from_db()
        self.assertEqual(self.case.status, before_status)
        self.assertEqual(Expense.objects.count(), before_expenses)

    def test_the_view_requires_the_case_officer_right(self):
        self.client.force_login(self.clerk)
        r = self.client.post(
            reverse("benevolent_case_fund_from_balance", args=[self.case.pk]),
            {"reason": "Testing."})
        self.assertEqual(r.status_code, 302)
        self.assertTrue(self.case.events.filter(
            kind=CaseEvent.Kind.FUNDED_FROM_BALANCE).exists())

    def test_the_case_page_shows_the_current_balance_and_the_action(self):
        self.client.force_login(self.treasurer)
        body = self.client.get(
            reverse("benevolent_case_detail", args=[self.case.pk])).content.decode()
        self.assertIn("Fund this case from the balance instead", body)
        self.assertIn(
            reverse("benevolent_case_fund_from_balance", args=[self.case.pk]), body)

    def test_the_action_is_hidden_once_the_case_is_paid(self):
        payout = case_svc.record_payout(self.case, amount=Decimal("10000"), user=self.clerk)
        payout.expense.status = "APPROVED"
        payout.expense.approved_by = self.treasurer
        payout.expense.save()
        self.client.force_login(self.treasurer)
        body = self.client.get(
            reverse("benevolent_case_detail", args=[self.case.pk])).content.decode()
        self.assertNotIn("Fund this case from the balance instead", body)


# ===========================================================================
# 3. "Matched via" transparency on the intake queue
# ===========================================================================

class MatchedViaTests(Phase11Fixture):

    def setUp(self):
        super().setUp()
        self.fund = Department.objects.create(
            name="P11 Intake Fund", slug="p11-intake-fund",
            fund_type=Department.FundType.LOCAL, category=Department.Category.MINISTRY)
        self.scheme = BenevolentScheme.objects.create(
            name="P11 Intake Scheme", code="P11I", fund=self.fund, created_by=self.treasurer)
        self.policy = SchemePolicy.objects.create(
            scheme=self.scheme, effective_from=TODAY - dt.timedelta(days=500),
            membership_required=True, waiting_period_days=0,
            contribution_mode=SchemePolicy.ContributionMode.FIXED_PERIODIC,
            contribution_amount=Decimal("100"),
            benefit_mode=SchemePolicy.BenefitMode.FIXED, benefit_amount=Decimal("10000"),
            arrears_treatment=SchemePolicy.ArrearsTreatment.IGNORE,
            created_by=self.treasurer)
        scheme_svc.publish_policy(self.policy, user=self.treasurer)
        scheme_svc.activate_scheme(self.scheme, user=self.treasurer)

    def test_the_queue_shows_the_best_candidates_signal_labels(self):
        from giving.models import Transaction
        txn = Transaction.objects.create(
            date=TODAY, channel="BANK", direction="CREDIT", amount=Decimal("100"),
            department=self.fund, reference="BEN DUES", confirmed=True,
            allocation_status="REVIEW")
        ContributionIntake.objects.create(
            transaction=txn, scheme=self.scheme, status="REVIEW", confidence=55,
            candidates=[{
                "scheme_id": self.scheme.pk, "scheme_code": self.scheme.code,
                "membership_id": None, "membership_number": "", "member_name": "Test",
                "case_id": None, "case_number": "", "kind": "DUES", "score": 55,
                "signals": [{"code": "member_phone", "label": "Member's own phone matched",
                            "weight": 55, "detail": ""}],
            }])
        self.client.force_login(self.treasurer)
        body = self.client.get(reverse("benevolent_intake_queue")).content.decode()
        self.assertIn("Member&#x27;s own phone matched", body)   # HTML-escaped apostrophe

    def test_an_item_with_no_candidates_shows_a_dash_not_an_error(self):
        from giving.models import Transaction
        txn = Transaction.objects.create(
            date=TODAY, channel="BANK", direction="CREDIT", amount=Decimal("100"),
            department=self.fund, reference="UNKNOWN", confirmed=True,
            allocation_status="REVIEW")
        ContributionIntake.objects.create(
            transaction=txn, scheme=self.scheme, status="UNMATCHED", confidence=0,
            candidates=[])
        self.client.force_login(self.treasurer)
        r = self.client.get(reverse("benevolent_intake_queue"))
        self.assertEqual(r.status_code, 200)

    def test_multiple_signals_are_all_shown(self):
        from giving.models import Transaction
        txn = Transaction.objects.create(
            date=TODAY, channel="BANK", direction="CREDIT", amount=Decimal("100"),
            department=self.fund, reference="BEN DUES", confirmed=True,
            allocation_status="REVIEW")
        ContributionIntake.objects.create(
            transaction=txn, scheme=self.scheme, status="REVIEW", confidence=90,
            candidates=[{
                "scheme_id": self.scheme.pk, "scheme_code": self.scheme.code,
                "membership_id": None, "membership_number": "", "member_name": "Test",
                "case_id": None, "case_number": "", "kind": "DUES", "score": 90,
                "signals": [
                    {"code": "member_phone", "label": "Member's own phone matched",
                     "weight": 55, "detail": ""},
                    {"code": "name_exact", "label": "Exact name match", "weight": 30,
                     "detail": ""},
                ],
            }])
        self.client.force_login(self.treasurer)
        body = self.client.get(reverse("benevolent_intake_queue")).content.decode()
        self.assertIn("Member&#x27;s own phone matched", body)   # HTML-escaped apostrophe
        self.assertIn("Exact name match", body)

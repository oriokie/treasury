"""Item 5 — fraud detection (red-flag scanner).

Each test builds one scenario that should (or should NOT) raise a specific
signal, and asserts on the signal's code. The scanner never blocks anything, so
these are pure detection tests: the scenario is constructed, the scan is run, and
the presence/absence of the flag is checked.
"""
import datetime as dt
from decimal import Decimal

from django.contrib.auth.models import Group, User
from django.test import TestCase
from django.urls import reverse

from core.roles import ASSISTANT, TREASURER
from departments.models import Department
from members.models import Member

from benevolent.models import (BenevolentCase, BenevolentEventType,
                               BenevolentScheme, SchemeMembership, SchemePolicy)
from benevolent.services import cases as case_svc
from benevolent.services import contributions as contrib_svc
from benevolent.services import exceptions as exc_svc
from benevolent.services import fraud as fraud_svc
from benevolent.services import registry as reg_svc
from benevolent.services import schemes as scheme_svc

TODAY = dt.date.today()


def _codes(signals):
    return {s.code for s in signals}


class FraudFixture(TestCase):
    def setUp(self):
        self.treasurer = User.objects.create_user("t_fraud", password="x", first_name="Tim", last_name="Treasurer")
        self.treasurer.groups.add(Group.objects.get_or_create(name=TREASURER)[0])
        self.clerk = User.objects.create_user("c_fraud", password="x", first_name="Cleo", last_name="Clerk")
        self.clerk.groups.add(Group.objects.get_or_create(name=ASSISTANT)[0])
        self.fund = Department.objects.create(
            name="Fraud Fund", slug="fraud-fund", fund_type=Department.FundType.LOCAL)
        self.scheme = BenevolentScheme.objects.create(
            name="Fraud Scheme", code="FRD", fund=self.fund, created_by=self.treasurer)
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
            allow_override=True, created_by=self.treasurer)
        scheme_svc.publish_policy(self.policy, user=self.treasurer)
        scheme_svc.activate_scheme(self.scheme, user=self.treasurer)
        contrib_svc.record_contribution(
            self.scheme, date=TODAY, amount=Decimal("50000"), user=self.treasurer)

    def _enrol(self, name, days_ago, phone=""):
        m = Member.objects.create(name=name, phone=phone)
        return reg_svc.register(
            self.scheme, m, joined_on=TODAY - dt.timedelta(days=days_ago),
            user=self.treasurer)

    def _full_case(self, membership, *, event_days_ago=0, approve_amount="5000",
                   raised_by=None, approved_by=None, override_reason="",
                   pay=False, payee_name=""):
        raised_by = raised_by or self.treasurer
        approved_by = approved_by or self.clerk
        case = case_svc.create_case(
            self.scheme, event_type=self.bereavement,
            event_date=TODAY - dt.timedelta(days=event_days_ago),
            membership=membership, user=raised_by)
        case_svc.submit_case(case, user=raised_by)
        case_svc.assess_case(case, user=raised_by)
        case_svc.approve_case(
            case, amount=Decimal(approve_amount), user=approved_by,
            override_reason=override_reason,
            allow_self_approval=(raised_by == approved_by))
        if pay:
            case_svc.record_payout(
                case, amount=Decimal(approve_amount), date=TODAY, user=approved_by,
                payee_name=payee_name or membership.member.name)
        return case


# ---------------------------------------------------------------------------
# Control breaches
# ---------------------------------------------------------------------------

class ControlBreachTests(FraudFixture):
    def test_self_approval_flagged(self):
        mem = self._enrol("Normal Member", 300)
        self._full_case(mem, raised_by=self.treasurer, approved_by=self.treasurer)
        self.assertIn("self_approval", _codes(fraud_svc.scan(scheme=self.scheme)))

    def test_separate_approver_not_flagged(self):
        mem = self._enrol("Clean Member", 300)
        self._full_case(mem, raised_by=self.treasurer, approved_by=self.clerk)
        self.assertNotIn("self_approval", _codes(fraud_svc.scan(scheme=self.scheme)))

    def test_payee_is_actor_flagged(self):
        mem = self._enrol("Payee Member", 300)
        # payout goes to the approver by name
        self._full_case(mem, approved_by=self.clerk, pay=True,
                        payee_name="Cleo Clerk")
        self.assertIn("payee_is_actor", _codes(fraud_svc.scan(scheme=self.scheme)))

    def test_override_approved_flagged(self):
        # a member with no tenure under a policy that requires it -> ineligible,
        # approved with override
        self.policy2 = SchemePolicy.objects.create(
            scheme=self.scheme, effective_from=TODAY - dt.timedelta(days=1),
            membership_required=True, waiting_period_days=0, min_paid_months=6,
            contribution_mode=SchemePolicy.ContributionMode.FIXED_PERIODIC,
            contribution_amount=Decimal("100"),
            contribution_frequency=SchemePolicy.Frequency.MONTHLY,
            benefit_mode=SchemePolicy.BenefitMode.FIXED, benefit_amount=Decimal("5000"),
            arrears_treatment=SchemePolicy.ArrearsTreatment.IGNORE,
            allow_override=True, created_by=self.treasurer)
        scheme_svc.publish_policy(self.policy2, user=self.treasurer)
        mem = self._enrol("Override Member", 5)   # no tenure
        self._full_case(mem, override_reason="Committee agreed hardship")
        self.assertIn("override_approved", _codes(fraud_svc.scan(scheme=self.scheme)))


# ---------------------------------------------------------------------------
# Membership abuse
# ---------------------------------------------------------------------------

class MembershipAbuseTests(FraudFixture):
    def test_rapid_claim_flagged(self):
        mem = self._enrol("Rapid Member", 5)     # joined 5 days ago, no contribs
        case = case_svc.create_case(
            self.scheme, event_type=self.bereavement, event_date=TODAY,
            membership=mem, user=self.treasurer)
        case_svc.submit_case(case, user=self.treasurer)
        case_svc.assess_case(case, user=self.treasurer)
        self.assertIn("rapid_claim", _codes(fraud_svc.scan(scheme=self.scheme)))

    def test_established_member_not_rapid(self):
        mem = self._enrol("Established Member", 400)
        for i in range(6):
            contrib_svc.record_contribution(
                self.scheme, date=TODAY - dt.timedelta(days=350 - i * 30),
                amount=Decimal("100"), user=self.treasurer, membership=mem)
        case = case_svc.create_case(
            self.scheme, event_type=self.bereavement, event_date=TODAY,
            membership=mem, user=self.treasurer)
        case_svc.submit_case(case, user=self.treasurer)
        case_svc.assess_case(case, user=self.treasurer)
        self.assertNotIn("rapid_claim", _codes(fraud_svc.scan(scheme=self.scheme)))

    def test_enrol_claim_leave_flagged(self):
        mem = self._enrol("Fly By Night", 100)
        case = case_svc.create_case(
            self.scheme, event_type=self.bereavement,
            event_date=TODAY - dt.timedelta(days=30),
            membership=mem, user=self.treasurer)
        case_svc.submit_case(case, user=self.treasurer)
        case_svc.assess_case(case, user=self.treasurer)
        reg_svc.withdraw(mem, on=TODAY, user=self.treasurer)
        self.assertIn("enrol_claim_leave", _codes(fraud_svc.scan(scheme=self.scheme)))


# ---------------------------------------------------------------------------
# Identity / collusion
# ---------------------------------------------------------------------------

class IdentityTests(FraudFixture):
    def test_repeat_beneficiary_flagged(self):
        # same beneficiary name across 3 cases under different members
        for i in range(3):
            mem = self._enrol(f"Host Member {i}", 300)
            case = case_svc.create_case(
                self.scheme, event_type=self.bereavement, event_date=TODAY,
                membership=mem, user=self.treasurer,
                beneficiary_name="Shared Beneficiary")
            case_svc.submit_case(case, user=self.treasurer)
            case_svc.assess_case(case, user=self.treasurer)
        self.assertIn("repeat_beneficiary", _codes(fraud_svc.scan(scheme=self.scheme)))

    def test_shared_phone_flagged(self):
        for i in range(4):
            self._enrol(f"Phone Share {i}", 300, phone="254700111222")
        self.assertIn("shared_phone", _codes(fraud_svc.scan(scheme=self.scheme)))

    def test_distinct_phones_not_flagged(self):
        for i in range(4):
            self._enrol(f"Distinct {i}", 300, phone=f"25470011122{i}")
        self.assertNotIn("shared_phone", _codes(fraud_svc.scan(scheme=self.scheme)))


# ---------------------------------------------------------------------------
# Contribution manipulation
# ---------------------------------------------------------------------------

class ContributionManipulationTests(FraudFixture):
    def test_reversal_after_claim_flagged(self):
        mem = self._enrol("Manipulator", 300)
        c = contrib_svc.record_contribution(
            self.scheme, date=TODAY - dt.timedelta(days=10), amount=Decimal("100"),
            user=self.treasurer, membership=mem)
        case = self._full_case(mem)   # approved today
        # reverse the contribution right after the claim
        exc_svc.reverse_contribution(c, user=self.treasurer, reason="pulled back")
        self.assertIn("reversal_after_claim",
                      _codes(fraud_svc.scan(scheme=self.scheme)))

    def test_reversal_cluster_flagged(self):
        mem = self._enrol("Churner", 400)
        contribs = []
        for i in range(5):
            c = contrib_svc.record_contribution(
                self.scheme, date=TODAY - dt.timedelta(days=200 - i * 10),
                amount=Decimal("100"), user=self.clerk, membership=mem,
                period_label=f"2025-{i+1:02d}")
            contribs.append(c)
        for c in contribs:
            exc_svc.reverse_contribution(c, user=self.clerk, reason="cluster")
        self.assertIn("reversal_cluster", _codes(fraud_svc.scan(scheme=self.scheme)))


# ---------------------------------------------------------------------------
# Scanner behaviour / integration
# ---------------------------------------------------------------------------

class ScannerBehaviourTests(FraudFixture):
    def test_clean_scheme_no_signals(self):
        mem = self._enrol("Upstanding", 400)
        for i in range(6):
            contrib_svc.record_contribution(
                self.scheme, date=TODAY - dt.timedelta(days=350 - i * 30),
                amount=Decimal("100"), user=self.treasurer, membership=mem)
        self._full_case(mem, raised_by=self.treasurer, approved_by=self.clerk,
                        pay=True)
        signals = fraud_svc.scan(scheme=self.scheme)
        self.assertEqual(_codes(signals), set())

    def test_signals_sorted_worst_first(self):
        mem = self._enrol("Multi", 5)
        self._full_case(mem, raised_by=self.treasurer, approved_by=self.treasurer)
        signals = fraud_svc.scan(scheme=self.scheme)
        ranks = [s.rank for s in signals]
        self.assertEqual(ranks, sorted(ranks))

    def test_summary_counts(self):
        mem = self._enrol("Summ", 5)
        self._full_case(mem, raised_by=self.treasurer, approved_by=self.treasurer)
        summ = fraud_svc.summary(scheme=self.scheme)
        self.assertEqual(summ["total"], summ["high"] + summ["medium"] + summ["low"])
        self.assertGreaterEqual(summ["high"], 1)   # self-approval is high

    def test_scan_never_raises_on_empty(self):
        empty_fund = Department.objects.create(
            name="Empty", slug="empty-fund", fund_type=Department.FundType.LOCAL)
        empty = BenevolentScheme.objects.create(
            name="Empty Scheme", code="EMP", fund=empty_fund, created_by=self.treasurer)
        self.assertEqual(fraud_svc.scan(scheme=empty), [])

    def test_page_renders_for_auditor(self):
        auditor = User.objects.create_user("aud_fraud", password="x")
        from core.roles import AUDITOR
        auditor.groups.add(Group.objects.get_or_create(name=AUDITOR)[0])
        self.client.force_login(auditor)
        r = self.client.get(reverse("benevolent_fraud_scan"))
        self.assertEqual(r.status_code, 200)

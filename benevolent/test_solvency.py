"""Item 8 — accounting scenarios: fund depletion, negative balance, reserved
commitments, pending approved payouts, outstanding liabilities, and cash
forecasting.

The ledger integration is not re-tested here (it is covered elsewhere and
unchanged); these tests cover the solvency LAYER on top of it — the questions a
committee asks before committing money.
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
                               BenevolentScheme, BenevolentSettings,
                               SchemeMembership, SchemePolicy)
from benevolent.services import cases as case_svc
from benevolent.services import contributions as contrib_svc
from benevolent.services import registry as reg_svc
from benevolent.services import schemes as scheme_svc
from benevolent.services import solvency as sol_svc

TODAY = dt.date.today()


class SolvencyFixture(TestCase):
    def setUp(self):
        self.treasurer = User.objects.create_user("t_sol", password="x")
        self.treasurer.groups.add(Group.objects.get_or_create(name=TREASURER)[0])
        self.clerk = User.objects.create_user("c_sol", password="x")
        self.clerk.groups.add(Group.objects.get_or_create(name=ASSISTANT)[0])
        self.fund = Department.objects.create(
            name="Sol Fund", slug="sol-fund", fund_type=Department.FundType.LOCAL)
        self.scheme = BenevolentScheme.objects.create(
            name="Sol Scheme", code="SOL", fund=self.fund, created_by=self.treasurer)
        self.bereavement = BenevolentEventType.objects.create(
            scheme=self.scheme, name="Bereavement", code="BER", triggers_on_death=True)
        self.policy = SchemePolicy.objects.create(
            scheme=self.scheme, effective_from=TODAY - dt.timedelta(days=400),
            membership_required=True, waiting_period_days=0,
            contribution_mode=SchemePolicy.ContributionMode.FIXED_PERIODIC,
            contribution_amount=Decimal("100"),
            contribution_frequency=SchemePolicy.Frequency.MONTHLY,
            benefit_mode=SchemePolicy.BenefitMode.FIXED, benefit_amount=Decimal("5000"),
            arrears_treatment=SchemePolicy.ArrearsTreatment.IGNORE,
            created_by=self.treasurer)
        scheme_svc.publish_policy(self.policy, user=self.treasurer)
        scheme_svc.activate_scheme(self.scheme, user=self.treasurer)
        self.mem = self._enrol("Sol Jane", 120)

    def _enrol(self, name, days_ago):
        m = Member.objects.create(name=name)
        return reg_svc.register(
            self.scheme, m, joined_on=TODAY - dt.timedelta(days=days_ago),
            user=self.treasurer)

    def _fund(self, amount):
        contrib_svc.record_contribution(
            self.scheme, date=TODAY, amount=Decimal(amount), user=self.treasurer,
            membership=self.mem)

    def _approved_case(self, amount, membership=None):
        membership = membership or self._enrol(f"Bereaved{amount}", 120)
        case = case_svc.create_case(
            self.scheme, event_type=self.bereavement, event_date=TODAY,
            membership=membership, user=self.treasurer)
        case_svc.submit_case(case, user=self.treasurer)
        case_svc.assess_case(case, user=self.treasurer)
        case_svc.approve_case(case, amount=Decimal(amount), user=self.clerk,
                              allow_self_approval=True)
        return case


# ---------------------------------------------------------------------------
# Fund position
# ---------------------------------------------------------------------------

class FundPositionTests(SolvencyFixture):
    def test_healthy_fund_position(self):
        self._fund(10000)
        pos = sol_svc.fund_position(self.scheme)
        self.assertEqual(pos.balance, Decimal("10000"))
        self.assertEqual(pos.available_after_approved, Decimal("10000"))
        self.assertFalse(pos.is_depleted)
        self.assertFalse(pos.is_negative)

    def test_approved_unpaid_reduces_available(self):
        self._fund(10000)
        self._approved_case(5000)   # approved, no voucher paid yet
        pos = sol_svc.fund_position(self.scheme)
        self.assertEqual(pos.balance, Decimal("10000"))     # balance unchanged (no voucher)
        self.assertEqual(pos.approved_unpaid, Decimal("5000"))
        self.assertEqual(pos.available_after_approved, Decimal("5000"))

    def test_depleted_when_approvals_exceed_balance(self):
        self._fund(3000)
        self._approved_case(5000)
        pos = sol_svc.fund_position(self.scheme)
        self.assertTrue(pos.is_depleted)
        self.assertLess(pos.available_after_approved, 0)

    def test_reserved_open_cases(self):
        self._fund(10000)
        # an open (submitted, not approved) case reserves against the balance
        bereaved = self._enrol("Open Case", 120)
        case = case_svc.create_case(
            self.scheme, event_type=self.bereavement, event_date=TODAY,
            membership=bereaved, user=self.treasurer)
        case_svc.submit_case(case, user=self.treasurer)
        pos = sol_svc.fund_position(self.scheme)
        self.assertGreater(pos.reserved_open_cases, 0)
        self.assertLess(pos.available_after_reserved, pos.available_after_committed)

    def test_reserve_setting_off_zeroes_reserve(self):
        cfg = BenevolentSettings.get()
        cfg.reserve_open_cases = False
        cfg.save()
        self._fund(10000)
        bereaved = self._enrol("Open Case 2", 120)
        case = case_svc.create_case(
            self.scheme, event_type=self.bereavement, event_date=TODAY,
            membership=bereaved, user=self.treasurer)
        case_svc.submit_case(case, user=self.treasurer)
        pos = sol_svc.fund_position(self.scheme)
        self.assertEqual(pos.reserved_open_cases, Decimal("0"))


# ---------------------------------------------------------------------------
# Affordability
# ---------------------------------------------------------------------------

class AffordabilityTests(SolvencyFixture):
    def test_affordable_payout_is_ok(self):
        self._fund(10000)
        chk = sol_svc.can_fund_payout(self.scheme, Decimal("3000"))
        self.assertTrue(chk.ok)
        self.assertEqual(chk.level, "ok")

    def test_unaffordable_payout_warns_by_default(self):
        self._fund(2000)
        chk = sol_svc.can_fund_payout(self.scheme, Decimal("5000"))
        self.assertFalse(chk.ok)
        self.assertEqual(chk.level, "warn")
        self.assertEqual(chk.shortfall, Decimal("3000"))

    def test_unaffordable_payout_blocks_when_setting_on(self):
        cfg = BenevolentSettings.get()
        cfg.block_overdrawn_payouts = True
        cfg.save()
        self._fund(2000)
        chk = sol_svc.can_fund_payout(self.scheme, Decimal("5000"))
        self.assertFalse(chk.ok)
        self.assertEqual(chk.level, "block")

    def test_payout_view_blocks_when_setting_on(self):
        cfg = BenevolentSettings.get()
        cfg.block_overdrawn_payouts = True
        cfg.save()
        self._fund(1000)
        case = self._approved_case(5000)
        self.client.force_login(self.treasurer)
        self.client.post(reverse("benevolent_case_payout", args=[case.pk]), {
            "amount": "5000", "date": TODAY.isoformat(), "method": "CASH"},
            follow=True)
        # no voucher should have been raised
        self.assertEqual(case.payouts.count(), 0)

    def test_payout_view_warns_but_allows_by_default(self):
        self._fund(1000)
        case = self._approved_case(5000)
        self.client.force_login(self.treasurer)
        self.client.post(reverse("benevolent_case_payout", args=[case.pk]), {
            "amount": "5000", "date": TODAY.isoformat(), "method": "CASH"},
            follow=True)
        # voucher raised despite the warning
        self.assertEqual(case.payouts.count(), 1)


# ---------------------------------------------------------------------------
# Forecast
# ---------------------------------------------------------------------------

class ForecastTests(SolvencyFixture):
    def test_forecast_projects_forward(self):
        self._fund(5000)
        fc = sol_svc.forecast_scheme(self.scheme, months=6)
        self.assertEqual(len(fc.months), 6)
        # opening of month 1 is the current balance
        self.assertEqual(fc.months[0].opening, Decimal("5000"))

    def test_forecast_detects_running_dry(self):
        # small balance, a big approved commitment lands in month 1
        self._fund(2000)
        self._approved_case(6000)
        fc = sol_svc.forecast_scheme(self.scheme, months=6)
        self.assertTrue(fc.runs_dry)
        self.assertIsNotNone(fc.first_dry_month)

    def test_forecast_months_chain(self):
        self._fund(5000)
        fc = sol_svc.forecast_scheme(self.scheme, months=3)
        # each month's opening equals the previous month's closing
        for a, b in zip(fc.months, fc.months[1:]):
            self.assertEqual(b.opening, a.closing)


# ---------------------------------------------------------------------------
# Metrics registry + views
# ---------------------------------------------------------------------------

class SolvencyIntegrationTests(SolvencyFixture):
    def test_reserved_commitments_metric_registered(self):
        from core.metrics import metrics
        self.assertTrue(metrics.has("benevolent_reserved_commitments"))

    def test_reserved_commitments_via_reporting(self):
        from benevolent.services import reporting
        self._fund(10000)
        bereaved = self._enrol("Metric Case", 120)
        case = case_svc.create_case(
            self.scheme, event_type=self.bereavement, event_date=TODAY,
            membership=bereaved, user=self.treasurer)
        case_svc.submit_case(case, user=self.treasurer)
        total = reporting.reserved_commitments_total(self.scheme)
        self.assertGreater(total, 0)

    def test_position_page_renders(self):
        self._fund(5000)
        self.client.force_login(self.treasurer)
        r = self.client.get(reverse("benevolent_fund_position", args=[self.scheme.pk]))
        self.assertEqual(r.status_code, 200)

    def test_position_page_shows_depletion_warning(self):
        self._fund(1000)
        self._approved_case(5000)
        self.client.force_login(self.treasurer)
        r = self.client.get(reverse("benevolent_fund_position", args=[self.scheme.pk]))
        self.assertContains(r, "cannot cover its approvals")

    def test_settings_not_rule_fields(self):
        # solvency settings are operational, not policy — must not be in RULE_FIELDS
        for f in ["block_overdrawn_payouts", "reserve_open_cases"]:
            self.assertNotIn(f, SchemePolicy.RULE_FIELDS)

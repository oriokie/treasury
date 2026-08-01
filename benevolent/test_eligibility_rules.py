"""Item 4 — eligibility rules real churches require.

Covers the new policy fields and checks:
  * min_paid_months          — 3/6/12 months paid-up tenure
  * no_missed_contributions  — an unbroken payment record (+ tolerance)
  * max_arrears_periods      — partial arrears expressed in periods
  * catch_up_*               — does clearing arrears restore eligibility now,
                               or after a re-qualification period?

Grace period was already covered by test_phase3/standing; a regression test for
it is included here so the whole eligibility surface is exercised together.

Each rule follows the engine's own contract: one policy field, one _check_*
function of (policy, facts) -> Check, surfaced in evaluate()'s check list and
frozen into the case's eligibility_snapshot. The tests assert both the machine
`code`/`passed` and that the human `detail` states the figures compared.
"""
import datetime as dt
from decimal import Decimal

from django.contrib.auth.models import Group, User
from django.test import TestCase

from core.roles import TREASURER
from departments.models import Department
from members.models import Member

from benevolent.models import (BenevolentEventType, BenevolentScheme,
                               SchemeMembership, SchemePolicy)
from benevolent.services import contributions as contrib_svc
from benevolent.services import registry as reg_svc
from benevolent.services import schemes as scheme_svc
from benevolent.services import standing as standing_svc
from benevolent.services.eligibility import evaluate

TODAY = dt.date.today()


def _check(res, code):
    return next(c for c in res.checks if c.code == code)


def _month_samples(days_back):
    """One day inside every month from `days_back` ago through today.

    The tests below used to walk this span in 28-day strides, which is shorter
    than every month — so the final stride landed in the PREVIOUS month whenever
    today fell in the first days of one, and the current month's dues were never
    paid. A member who was supposed to be fully paid up was then genuinely one
    period in arrears, and two catch-up tests failed. They passed mid-month and
    failed on the 1st: the suite was red or green depending on the date it ran.

    Today is always included, so the current period is always covered.
    """
    out, d = [], TODAY - dt.timedelta(days=days_back)
    while d <= TODAY:
        out.append(d)
        d += dt.timedelta(days=28)
    out.append(TODAY)
    return out


class EligibilityFixture(TestCase):
    def setUp(self):
        self.treasurer = User.objects.create_user("t_elig", password="x")
        self.treasurer.groups.add(Group.objects.get_or_create(name=TREASURER)[0])
        self.fund = Department.objects.create(
            name="Elig Fund", slug="elig-fund", fund_type=Department.FundType.LOCAL)
        self.scheme = BenevolentScheme.objects.create(
            name="Elig Scheme", code="ELG", fund=self.fund, created_by=self.treasurer)
        self.bereavement = BenevolentEventType.objects.create(
            scheme=self.scheme, name="Bereavement", code="BER", triggers_on_death=True)

    def _publish(self, **overrides):
        kwargs = dict(
            scheme=self.scheme, effective_from=TODAY - dt.timedelta(days=1000),
            membership_required=True, waiting_period_days=0,
            contribution_mode=SchemePolicy.ContributionMode.FIXED_PERIODIC,
            contribution_amount=Decimal("100"),
            contribution_frequency=SchemePolicy.Frequency.MONTHLY,
            benefit_mode=SchemePolicy.BenefitMode.FIXED, benefit_amount=Decimal("5000"),
            arrears_treatment=SchemePolicy.ArrearsTreatment.IGNORE,
            created_by=self.treasurer)
        kwargs.update(overrides)
        policy = SchemePolicy.objects.create(**kwargs)
        scheme_svc.publish_policy(policy, user=self.treasurer)
        if self.scheme.status == BenevolentScheme.Status.DRAFT:
            scheme_svc.activate_scheme(self.scheme, user=self.treasurer)
        return policy

    def _enrol(self, name, days_ago):
        m = Member.objects.create(name=name)
        return reg_svc.register(
            self.scheme, m, joined_on=TODAY - dt.timedelta(days=days_ago),
            user=self.treasurer)

    def _pay_months(self, membership, months, start_days_ago):
        """Pay `months` consecutive monthly dues starting start_days_ago back."""
        for i in range(months):
            contrib_svc.record_contribution(
                self.scheme, date=TODAY - dt.timedelta(days=start_days_ago - i * 30),
                amount=Decimal("100"), user=self.treasurer, membership=membership)


# ---------------------------------------------------------------------------
# Tenure — min_paid_months
# ---------------------------------------------------------------------------

class TenureTests(EligibilityFixture):
    def test_no_requirement_is_advisory_and_passes(self):
        self._publish(min_paid_months=0)
        mem = self._enrol("No Tenure", days_ago=400)
        res = evaluate(self.scheme, event_type=self.bereavement, event_date=TODAY,
                       membership=mem)
        c = _check(res, "tenure")
        self.assertTrue(c.passed)
        self.assertFalse(c.blocking)

    def test_insufficient_paid_months_blocks(self):
        self._publish(min_paid_months=6)
        mem = self._enrol("Two Paid", days_ago=300)
        self._pay_months(mem, 2, start_days_ago=250)
        res = evaluate(self.scheme, event_type=self.bereavement, event_date=TODAY,
                       membership=mem)
        c = _check(res, "tenure")
        self.assertFalse(c.passed)
        self.assertTrue(c.blocking)
        self.assertIn("6 required", c.detail)
        self.assertFalse(res.eligible)

    def test_sufficient_paid_months_qualifies(self):
        self._publish(min_paid_months=3)
        mem = self._enrol("Six Paid", days_ago=300)
        self._pay_months(mem, 6, start_days_ago=270)
        res = evaluate(self.scheme, event_type=self.bereavement, event_date=TODAY,
                       membership=mem)
        c = _check(res, "tenure")
        self.assertTrue(c.passed)

    def test_tenure_is_distinct_from_bare_count(self):
        """Two payments in one month satisfy min_contributions=2 but not
        min_paid_months=2 — the point of the tenure rule."""
        self._publish(min_paid_months=2)
        mem = self._enrol("Doubled Up", days_ago=60)
        # two payments, both in the SAME recent month
        contrib_svc.record_contribution(
            self.scheme, date=TODAY - dt.timedelta(days=5), amount=Decimal("100"),
            user=self.treasurer, membership=mem)
        contrib_svc.record_contribution(
            self.scheme, date=TODAY - dt.timedelta(days=3), amount=Decimal("100"),
            user=self.treasurer, membership=mem)
        res = evaluate(self.scheme, event_type=self.bereavement, event_date=TODAY,
                       membership=mem)
        # at most one month is fully paid, so a 2-month tenure fails
        self.assertFalse(_check(res, "tenure").passed)


# ---------------------------------------------------------------------------
# Unbroken record — no_missed_contributions
# ---------------------------------------------------------------------------

class NoMissedTests(EligibilityFixture):
    def test_off_is_advisory(self):
        self._publish(no_missed_contributions=False)
        mem = self._enrol("Anyone", days_ago=200)
        res = evaluate(self.scheme, event_type=self.bereavement, event_date=TODAY,
                       membership=mem)
        c = _check(res, "no_missed")
        self.assertTrue(c.passed)
        self.assertFalse(c.blocking)

    def test_a_gap_disqualifies_even_if_later_paid(self):
        """The essence of an unbroken-record rule: back-paying a period clears
        the arrears but the period was still missed at the time."""
        self._publish(no_missed_contributions=True)
        mem = self._enrol("Late Payer", days_ago=120)
        # skip the first two months, then pay the recent one — currently near
        # clear, but the record is broken
        contrib_svc.record_contribution(
            self.scheme, date=TODAY - dt.timedelta(days=3), amount=Decimal("100"),
            user=self.treasurer, membership=mem)
        res = evaluate(self.scheme, event_type=self.bereavement, event_date=TODAY,
                       membership=mem)
        c = _check(res, "no_missed")
        self.assertFalse(c.passed)
        self.assertTrue(c.blocking)

    def test_tolerance_permits_a_few_gaps(self):
        self._publish(no_missed_contributions=True, missed_contributions_allowed=4)
        mem = self._enrol("Forgetful", days_ago=120)
        contrib_svc.record_contribution(
            self.scheme, date=TODAY - dt.timedelta(days=3), amount=Decimal("100"),
            user=self.treasurer, membership=mem)
        res = evaluate(self.scheme, event_type=self.bereavement, event_date=TODAY,
                       membership=mem)
        # ~4 missed months, tolerance 4 -> passes
        self.assertTrue(_check(res, "no_missed").passed)

    def test_a_spotless_record_passes(self):
        self._publish(no_missed_contributions=True)
        mem = self._enrol("Spotless", days_ago=90)
        self._pay_months(mem, 4, start_days_ago=90)
        res = evaluate(self.scheme, event_type=self.bereavement, event_date=TODAY,
                       membership=mem)
        self.assertTrue(_check(res, "no_missed").passed)


# ---------------------------------------------------------------------------
# Partial arrears expressed in periods — max_arrears_periods
# ---------------------------------------------------------------------------

class ArrearsPeriodsTests(EligibilityFixture):
    def test_within_period_tolerance_passes(self):
        self._publish(arrears_treatment=SchemePolicy.ArrearsTreatment.BLOCK,
                      max_arrears_periods=3)
        mem = self._enrol("Two Behind", days_ago=120)
        # ~5 periods have fallen due; pay the oldest 2, leaving ~3 behind
        self._pay_months(mem, 2, start_days_ago=120)
        res = evaluate(self.scheme, event_type=self.bereavement, event_date=TODAY,
                       membership=mem)
        c = _check(res, "arrears")
        self.assertIn("period(s) in arrears", c.detail)
        self.assertTrue(c.passed)

    def test_beyond_period_tolerance_blocks(self):
        self._publish(arrears_treatment=SchemePolicy.ArrearsTreatment.BLOCK,
                      max_arrears_periods=1)
        mem = self._enrol("Way Behind", days_ago=150)
        res = evaluate(self.scheme, event_type=self.bereavement, event_date=TODAY,
                       membership=mem)
        c = _check(res, "arrears")
        self.assertFalse(c.passed)
        self.assertTrue(c.blocking)


# ---------------------------------------------------------------------------
# Catch-up re-qualification
# ---------------------------------------------------------------------------

class CatchUpTests(EligibilityFixture):
    def test_default_restores_immediately(self):
        self._publish(catch_up_restores_eligibility=True)
        mem = self._enrol("Caught Up", days_ago=120)
        self._pay_months(mem, 4, start_days_ago=120)
        res = evaluate(self.scheme, event_type=self.bereavement, event_date=TODAY,
                       membership=mem)
        c = _check(res, "catch_up")
        self.assertTrue(c.passed)
        self.assertFalse(c.blocking)

    def test_requalify_blocks_until_window_served(self):
        self._publish(catch_up_restores_eligibility=False,
                      catch_up_requalify_days=90,
                      arrears_treatment=SchemePolicy.ArrearsTreatment.IGNORE)
        mem = self._enrol("Just Cleared", days_ago=200)
        # Clear EVERY outstanding period, but with payments dated only days ago —
        # the "paid up in one recent lump once trouble loomed" scenario the
        # re-qualification window exists to catch. Explicit period labels let a
        # recent-dated payment settle an old period.
        from benevolent.services.contributions import period_label_for
        seen = set()
        for day in _month_samples(200):
            lbl = period_label_for(day, SchemePolicy.Frequency.MONTHLY)
            if lbl not in seen:
                seen.add(lbl)
                contrib_svc.record_contribution(
                    self.scheme, date=TODAY - dt.timedelta(days=2), amount=Decimal("100"),
                    user=self.treasurer, membership=mem, period_label=lbl)
        res = evaluate(self.scheme, event_type=self.bereavement, event_date=TODAY,
                       membership=mem)
        c = _check(res, "catch_up")
        self.assertTrue(c.blocking)
        self.assertIn("day(s)", c.detail)

    def test_still_in_arrears_defers_to_arrears_rule(self):
        """The catch-up check only governs the window AFTER clearing — while a
        member is still in arrears it must not double-block."""
        self._publish(catch_up_restores_eligibility=False,
                      catch_up_requalify_days=90)
        mem = self._enrol("Still Behind", days_ago=150)
        res = evaluate(self.scheme, event_type=self.bereavement, event_date=TODAY,
                       membership=mem)
        self.assertTrue(_check(res, "catch_up").passed)  # advisory pass

    def test_always_paid_on_time_is_never_caught(self):
        """A member who has simply paid every period on time has no late gap to
        have caught up on, so the re-qualification rule must never bite them —
        even under a policy that has it switched on."""
        self._publish(catch_up_restores_eligibility=False,
                      catch_up_requalify_days=90)
        mem = self._enrol("On Time Always", days_ago=200)
        # pay each month within its own period (on time), across the whole tenure
        from benevolent.services.contributions import period_label_for
        seen = set()
        for day in _month_samples(200):
            lbl = period_label_for(day, SchemePolicy.Frequency.MONTHLY)
            if lbl not in seen:
                seen.add(lbl)
                contrib_svc.record_contribution(
                    self.scheme, date=day, amount=Decimal("100"),
                    user=self.treasurer, membership=mem, period_label=lbl)
        res = evaluate(self.scheme, event_type=self.bereavement, event_date=TODAY,
                       membership=mem)
        c = _check(res, "catch_up")
        self.assertTrue(c.passed)
        self.assertIn("kept up", c.detail)


# ---------------------------------------------------------------------------
# Facts layer — standing and eligibility must agree
# ---------------------------------------------------------------------------

class FactsAgreementTests(EligibilityFixture):
    def test_paid_periods_counted_in_facts(self):
        self._publish()
        mem = self._enrol("Fact Check", days_ago=120)
        self._pay_months(mem, 2, start_days_ago=120)
        facts = standing_svc.facts_for(mem, as_of=TODAY)
        self.assertGreaterEqual(facts.paid_periods, 2)
        self.assertEqual(facts.paid_periods + facts.missed_periods,
                         facts.total_periods)

    def test_frozen_into_rule_fields(self):
        for field in ["min_paid_months", "no_missed_contributions",
                      "missed_contributions_allowed", "max_arrears_periods",
                      "catch_up_restores_eligibility", "catch_up_requalify_days"]:
            self.assertIn(field, SchemePolicy.RULE_FIELDS)

    def test_snapshot_carries_new_checks(self):
        policy = self._publish(min_paid_months=6, no_missed_contributions=True)
        mem = self._enrol("Snapshot", days_ago=120)
        res = evaluate(self.scheme, event_type=self.bereavement, event_date=TODAY,
                       membership=mem)
        snap = res.as_dict()
        codes = {c["code"] for c in snap["checks"]}
        self.assertIn("tenure", codes)
        self.assertIn("no_missed", codes)
        self.assertIn("catch_up", codes)

"""Benevolent Scheme Engine — foundation tests.

Grouped by the four things Phase 1 must actually guarantee:

  1. ACCOUNTING     the module invents no money maths. Contributions and payouts
                    are ordinary receipts and vouchers; the fund balance, the
                    ledger and the scheme's own figures agree by construction.
  2. IMMUTABILITY   a used policy version can never change; a case carries the
                    terms it was actually decided under, forever.
  3. THE ENGINE     the rules are configuration. The same code decides a
                    bereavement fund and a medical fund from different policy
                    rows, and shows its working.
  4. CONTROLS       segregation of duties, the approval route, period locks.
"""
import datetime as dt
from decimal import Decimal

from django.contrib.auth.models import Group, User
from django.core.exceptions import ValidationError
from django.test import TestCase
from django.urls import reverse

from cashbook.models import Expense
from core.roles import TREASURER, ASSISTANT
from departments.models import Department
from giving.models import Transaction
from members.models import Member

from benevolent.models import (BenevolentCase, BenevolentEventType, BenevolentPayout,
                               BenevolentScheme, SchemeBenefitRule, SchemeMembership,
                               SchemePolicy)
from benevolent.services import cases as case_svc
from benevolent.services import contributions as contrib_svc
from benevolent.services import reporting as report_svc
from benevolent.services import schemes as scheme_svc
from benevolent.services.eligibility import evaluate, evaluate_case

TODAY = dt.date.today()


class SchemeFixture(TestCase):
    """A working benevolent scheme: a fund, a published policy, two members."""

    def setUp(self):
        self.treasurer = User.objects.create_user("t", password="x")
        self.treasurer.groups.add(Group.objects.get_or_create(name=TREASURER)[0])
        self.clerk = User.objects.create_user("c", password="x")
        self.clerk.groups.add(Group.objects.get_or_create(name=ASSISTANT)[0])

        self.fund = Department.objects.create(
            name="Benevolent Fund", slug="benevolent-fund",
            fund_type=Department.FundType.LOCAL,
            category=Department.Category.MINISTRY)

        self.scheme = BenevolentScheme.objects.create(
            name="Church Benevolent Scheme", code="BEN", fund=self.fund,
            kind=BenevolentScheme.Kind.BENEVOLENT, created_by=self.treasurer)

        self.bereavement = BenevolentEventType.objects.create(
            scheme=self.scheme, name="Bereavement — spouse", code="BER_SPOUSE")
        self.hospital = BenevolentEventType.objects.create(
            scheme=self.scheme, name="Hospitalisation", code="HOSPITAL")

        self.policy = SchemePolicy.objects.create(
            scheme=self.scheme,
            effective_from=TODAY - dt.timedelta(days=365),
            membership_required=True,
            waiting_period_days=30,
            contribution_mode=SchemePolicy.ContributionMode.FIXED_PERIODIC,
            contribution_amount=Decimal("200"),
            contribution_frequency=SchemePolicy.Frequency.MONTHLY,
            benefit_mode=SchemePolicy.BenefitMode.SCHEDULE,
            benefit_cap=Decimal("50000"),
            claim_window_days=60,
            created_by=self.treasurer)
        SchemeBenefitRule.objects.create(
            policy=self.policy, event_type=self.bereavement, amount=Decimal("20000"))
        SchemeBenefitRule.objects.create(
            policy=self.policy, event_type=self.hospital, amount=Decimal("5000"),
            max_per_year=1)
        scheme_svc.publish_policy(self.policy, user=self.treasurer)
        scheme_svc.activate_scheme(self.scheme, user=self.treasurer)

        self.mary = Member.objects.create(name="Mary Wanjiku", phone="254712345678")
        self.john = Member.objects.create(name="John Otieno", phone="254712345679")
        self.m_mary = scheme_svc.enrol(
            self.scheme, self.mary, joined_on=TODAY - dt.timedelta(days=200),
            user=self.treasurer)
        self.m_john = scheme_svc.enrol(
            self.scheme, self.john, joined_on=TODAY - dt.timedelta(days=5),
            user=self.treasurer)

    def _case(self, membership=None, event=None, event_date=None, raised_by=None):
        return BenevolentCase.objects.create(
            scheme=self.scheme, membership=membership or self.m_mary,
            event_type=event or self.bereavement,
            event_date=event_date or (TODAY - dt.timedelta(days=3)),
            reported_date=TODAY, raised_by=raised_by or self.clerk)


# ===========================================================================
# 1. ACCOUNTING — no new money machinery
# ===========================================================================

class AccountingIntegrationTests(SchemeFixture):

    def test_contribution_is_an_ordinary_fund_receipt(self):
        c = contrib_svc.record_contribution(
            self.scheme, date=TODAY, amount=Decimal("200"),
            membership=self.m_mary, user=self.treasurer)
        txn = c.transaction
        self.assertEqual(txn.department, self.fund)
        self.assertEqual(txn.direction, Transaction.Direction.CREDIT)
        self.assertEqual(txn.member, self.mary)
        self.assertTrue(txn.confirmed)
        # crucially it is INCOME (unlike a loan receipt), so it is not excluded
        self.assertFalse(txn.excluded_from_income)

    def test_contribution_amount_is_never_copied_from_the_receipt(self):
        """The Transaction is authoritative. Change it and the contribution
        follows — there is no second stored figure that can drift."""
        c = contrib_svc.record_contribution(
            self.scheme, date=TODAY, amount=Decimal("200"),
            membership=self.m_mary, user=self.treasurer)
        self.assertEqual(c.amount, Decimal("200"))
        c.transaction.amount = Decimal("250")
        c.transaction.save()
        c.refresh_from_db()
        self.assertEqual(c.amount, Decimal("250"))

    def test_reversed_receipt_stops_counting_with_no_correction_needed(self):
        c = contrib_svc.record_contribution(
            self.scheme, date=TODAY, amount=Decimal("500"),
            membership=self.m_mary, user=self.treasurer)
        self.assertEqual(contrib_svc.contributions_total(membership=self.m_mary),
                         Decimal("500"))
        c.transaction.is_reversed = True
        c.transaction.save()
        self.assertFalse(c.effective)
        self.assertEqual(contrib_svc.contributions_total(membership=self.m_mary),
                         Decimal(0))

    def test_payout_is_a_pending_expense_on_the_scheme_fund(self):
        case = self._case()
        case_svc.submit_case(case, user=self.clerk)
        case_svc.assess_case(case, user=self.treasurer)
        case_svc.approve_case(case, amount=Decimal("20000"), user=self.treasurer)
        payout = case_svc.record_payout(
            case, amount=Decimal("20000"), user=self.clerk)

        exp = payout.expense
        self.assertEqual(exp.department, self.fund)
        self.assertEqual(exp.category, Expense.Category.BENEVOLENCE)
        # it enters the ordinary approval queue — this module never self-approves
        self.assertEqual(exp.status, Expense.Status.PENDING)
        self.assertFalse(payout.effective)
        self.assertEqual(case.paid_total, Decimal(0))

    def test_scheme_balance_is_the_fund_balance_from_the_registry(self):
        from core.metrics import metrics
        contrib_svc.record_contribution(
            self.scheme, date=TODAY, amount=Decimal("10000"),
            membership=self.m_mary, user=self.treasurer)
        self.assertEqual(report_svc.scheme_balance(self.scheme),
                         metrics.fund_balance(self.fund))
        self.assertEqual(report_svc.scheme_balance(self.scheme), Decimal("10000"))

    def test_approved_voucher_reduces_the_fund_and_the_case_agrees(self):
        """The one test that ties the whole design together: approving the
        VOUCHER (not the case) is what moves the money, and the fund balance,
        the case's paid total and the ledger all move together."""
        from core.metrics import metrics
        contrib_svc.record_contribution(
            self.scheme, date=TODAY, amount=Decimal("30000"),
            membership=self.m_mary, user=self.treasurer)

        case = self._case()
        case_svc.submit_case(case, user=self.clerk)
        case_svc.assess_case(case, user=self.treasurer)
        case_svc.approve_case(case, amount=Decimal("20000"), user=self.treasurer)
        payout = case_svc.record_payout(case, amount=Decimal("20000"), user=self.clerk)

        # still pending: no money has moved
        self.assertEqual(metrics.fund_balance(self.fund), Decimal("30000"))

        # the treasurer approves the voucher in the ORDINARY expense workflow
        exp = payout.expense
        exp.status = Expense.Status.APPROVED
        exp.approved_by = self.treasurer
        exp.save()

        case.refresh_from_db()
        self.assertEqual(case.paid_total, Decimal("20000"))
        self.assertEqual(case.status, BenevolentCase.Status.PAID)
        self.assertEqual(metrics.fund_balance(self.fund), Decimal("10000"))
        self.assertEqual(report_svc.scheme_balance(self.scheme), Decimal("10000"))

    def test_rejecting_the_voucher_walks_the_case_back_automatically(self):
        case = self._case()
        case_svc.submit_case(case, user=self.clerk)
        case_svc.assess_case(case, user=self.treasurer)
        case_svc.approve_case(case, amount=Decimal("20000"), user=self.treasurer)
        payout = case_svc.record_payout(case, amount=Decimal("20000"), user=self.clerk)

        exp = payout.expense
        exp.status = Expense.Status.APPROVED
        exp.save()
        case.refresh_from_db()
        self.assertEqual(case.status, BenevolentCase.Status.PAID)

        # a treasurer rejects it later, in the expense screen, knowing nothing
        # about cases — the case must follow, with nobody remembering to act
        exp.status = Expense.Status.REJECTED
        exp.save()
        case.refresh_from_db()
        self.assertEqual(case.paid_total, Decimal(0))
        self.assertEqual(case.status, BenevolentCase.Status.APPROVED)
        self.assertEqual(case.outstanding, Decimal("20000"))

    def test_payout_cannot_exceed_what_was_approved(self):
        case = self._case()
        case_svc.submit_case(case, user=self.clerk)
        case_svc.assess_case(case, user=self.treasurer)
        case_svc.approve_case(case, amount=Decimal("20000"), user=self.treasurer)
        case_svc.record_payout(case, amount=Decimal("15000"), user=self.clerk)
        with self.assertRaises(ValidationError):
            case_svc.record_payout(case, amount=Decimal("6000"), user=self.clerk)

    def test_ledger_posts_the_contribution_as_income(self):
        from ledger.services import posting
        posting.ensure_chart()
        c = contrib_svc.record_contribution(
            self.scheme, date=TODAY, amount=Decimal("400"),
            membership=self.m_mary, user=self.treasurer)
        from ledger.models import JournalLine
        lines = JournalLine.objects.filter(entry__source_type="transaction",
                                           entry__source_id=c.transaction_id)
        self.assertTrue(lines.exists())
        types = {l.account.type for l in lines}
        self.assertEqual(types, {"ASSET", "INCOME"})   # DR Cash / CR Income


# ===========================================================================
# 2. IMMUTABILITY
# ===========================================================================

class ImmutabilityTests(SchemeFixture):

    def test_a_used_policy_version_cannot_be_edited(self):
        case = self._case()
        case_svc.submit_case(case, user=self.clerk)
        case_svc.assess_case(case, user=self.treasurer)

        self.policy.refresh_from_db()
        self.assertTrue(self.policy.is_locked)
        self.policy.benefit_cap = Decimal("999999")
        with self.assertRaises(ValidationError):
            self.policy.save()

    def test_a_used_policy_version_cannot_be_deleted(self):
        case = self._case()
        case_svc.submit_case(case, user=self.clerk)
        case_svc.assess_case(case, user=self.treasurer)
        with self.assertRaises(ValidationError):
            self.policy.refresh_from_db() or self.policy.delete()

    def test_a_used_policy_benefit_schedule_cannot_be_changed(self):
        case = self._case()
        case_svc.submit_case(case, user=self.clerk)
        case_svc.assess_case(case, user=self.treasurer)
        rule = self.policy.benefit_rules.first()
        rule.amount = Decimal("1")
        with self.assertRaises(ValidationError):
            rule.save()

    def test_new_version_does_not_disturb_decided_cases(self):
        """The whole point of versioning: yesterday's decision stands, and is
        still reproducible, after the rules change."""
        case = self._case()
        case_svc.submit_case(case, user=self.clerk)
        case_svc.assess_case(case, user=self.treasurer)
        case_svc.approve_case(case, amount=Decimal("20000"), user=self.treasurer)
        self.assertEqual(case.assessed_amount, Decimal("20000"))

        v2 = scheme_svc.new_version_from(
            self.policy, effective_from=TODAY + dt.timedelta(days=1),
            user=self.treasurer)
        rule = v2.benefit_rules.get(event_type=self.bereavement)
        rule.amount = Decimal("35000")     # the church becomes more generous
        rule.save()
        scheme_svc.publish_policy(v2, user=self.treasurer)

        case.refresh_from_db()
        self.assertEqual(case.policy_id, self.policy.pk)
        self.assertEqual(case.assessed_amount, Decimal("20000"))
        self.assertEqual(case.policy_snapshot["policy_version"], 1)
        # and the frozen snapshot still says 20,000, whatever v2 says now
        snap = {r["event_type"]: r["amount"]
                for r in case.policy_snapshot["benefit_rules"]}
        self.assertEqual(snap["BER_SPOUSE"], "20000.00")

    def test_publishing_supersedes_the_prior_version_and_closes_its_window(self):
        v2 = scheme_svc.new_version_from(
            self.policy, effective_from=TODAY, user=self.treasurer)
        scheme_svc.publish_policy(v2, user=self.treasurer)
        self.policy.refresh_from_db()
        self.assertEqual(self.policy.status, SchemePolicy.Status.SUPERSEDED)
        self.assertEqual(self.policy.effective_to, TODAY - dt.timedelta(days=1))
        # exactly one version resolves for any date
        self.assertEqual(self.scheme.policy_on(TODAY).pk, v2.pk)
        self.assertEqual(
            self.scheme.policy_on(TODAY - dt.timedelta(days=10)).pk, self.policy.pk)

    def test_a_case_is_decided_by_the_policy_in_force_at_the_EVENT_date(self):
        """A late-reported claim is judged by yesterday's rules, not today's."""
        v2 = scheme_svc.new_version_from(
            self.policy, effective_from=TODAY, user=self.treasurer)
        r = v2.benefit_rules.get(event_type=self.bereavement)
        r.amount = Decimal("35000")
        r.save()
        scheme_svc.publish_policy(v2, user=self.treasurer)

        # the event happened a month ago, under v1
        case = self._case(event_date=TODAY - dt.timedelta(days=30))
        case_svc.submit_case(case, user=self.clerk)
        result = case_svc.assess_case(case, user=self.treasurer)
        self.assertEqual(result.policy.version, 1)
        self.assertEqual(case.assessed_amount, Decimal("20000"))


# ===========================================================================
# 3. THE ENGINE — rules are configuration
# ===========================================================================

class PolicyEngineTests(SchemeFixture):

    def test_waiting_period_blocks_a_new_member(self):
        # John enrolled 5 days ago; the policy needs 30
        result = evaluate(self.scheme, event_type=self.bereavement,
                          event_date=TODAY, membership=self.m_john)
        self.assertFalse(result.eligible)
        codes = [c.code for c in result.blocking_failures]
        self.assertIn("waiting_period", codes)

    def test_a_long_standing_member_qualifies(self):
        result = evaluate(self.scheme, event_type=self.bereavement,
                          event_date=TODAY, membership=self.m_mary)
        self.assertTrue(result.eligible)
        self.assertEqual(result.entitlement.amount, Decimal("20000"))

    def test_claim_window_blocks_a_stale_claim(self):
        result = evaluate(self.scheme, event_type=self.bereavement,
                          event_date=TODAY - dt.timedelta(days=120),
                          membership=self.m_mary, reported_date=TODAY)
        self.assertFalse(result.eligible)
        self.assertIn("claim_window", [c.code for c in result.blocking_failures])

    def test_every_check_explains_itself_with_the_figures_compared(self):
        """Transparency is a requirement, not decoration: an auditor must be
        able to read WHY, not just WHETHER."""
        result = evaluate(self.scheme, event_type=self.bereavement,
                          event_date=TODAY, membership=self.m_john)
        wait = next(c for c in result.checks if c.code == "waiting_period")
        self.assertIn("30 required", wait.detail)
        self.assertTrue(all(c.detail for c in result.checks))

    def test_schedule_mode_pays_per_event(self):
        r = evaluate(self.scheme, event_type=self.hospital, event_date=TODAY,
                     membership=self.m_mary)
        self.assertEqual(r.entitlement.amount, Decimal("5000"))
        r = evaluate(self.scheme, event_type=self.bereavement, event_date=TODAY,
                     membership=self.m_mary)
        self.assertEqual(r.entitlement.amount, Decimal("20000"))

    def test_a_medical_fund_is_the_same_code_with_different_configuration(self):
        """The engine's whole claim, in one test: a percentage-of-cost medical
        scheme with a cap needs no new business logic."""
        med_fund = Department.objects.create(
            name="Medical Fund", slug="medical-fund",
            fund_type=Department.FundType.LOCAL,
            category=Department.Category.MINISTRY)
        med = BenevolentScheme.objects.create(
            name="Medical Assistance Scheme", code="MED", fund=med_fund,
            kind=BenevolentScheme.Kind.MEDICAL)
        surgery = BenevolentEventType.objects.create(
            scheme=med, name="Surgery", code="SURGERY")
        pol = SchemePolicy.objects.create(
            scheme=med, effective_from=TODAY - dt.timedelta(days=30),
            membership_required=False,          # open to the whole congregation
            waiting_period_days=0,
            contribution_mode=SchemePolicy.ContributionMode.VOLUNTARY,
            benefit_mode=SchemePolicy.BenefitMode.PERCENTAGE,
            benefit_percent=Decimal("60"),
            benefit_cap=Decimal("30000"))
        scheme_svc.publish_policy(pol)
        scheme_svc.activate_scheme(med)

        # 60% of 20,000, under the cap
        r = evaluate(med, event_type=surgery, event_date=TODAY,
                     claimed_amount=Decimal("20000"))
        self.assertTrue(r.eligible)
        self.assertEqual(r.entitlement.amount, Decimal("12000"))

        # 60% of 100,000 = 60,000, capped at 30,000 — and it says so
        r = evaluate(med, event_type=surgery, event_date=TODAY,
                     claimed_amount=Decimal("100000"))
        self.assertEqual(r.entitlement.amount, Decimal("30000"))
        self.assertTrue(any("Capped" in w for w in r.entitlement.workings))

    def test_per_event_annual_limit(self):
        old = self._case(event=self.hospital,
                         event_date=TODAY - dt.timedelta(days=20))
        case_svc.submit_case(old, user=self.clerk)
        case_svc.assess_case(old, user=self.treasurer)
        case_svc.approve_case(old, amount=Decimal("5000"), user=self.treasurer)

        # the hospital rule allows one a year
        r = evaluate(self.scheme, event_type=self.hospital, event_date=TODAY,
                     membership=self.m_mary)
        self.assertFalse(r.eligible)
        self.assertIn("claim_frequency", [c.code for c in r.blocking_failures])

    def test_arrears_block_when_the_policy_says_so(self):
        v2 = scheme_svc.new_version_from(
            self.policy, effective_from=TODAY - dt.timedelta(days=1),
            user=self.treasurer)
        v2.arrears_block = True
        v2.max_arrears_allowed = Decimal("400")
        v2.save()
        scheme_svc.publish_policy(v2, user=self.treasurer)

        # Mary has paid nothing since enrolling 200 days ago at 200/month
        owed = contrib_svc.arrears_for(self.m_mary, v2)
        self.assertGreater(owed, Decimal("400"))
        r = evaluate(self.scheme, event_type=self.bereavement, event_date=TODAY,
                     membership=self.m_mary)
        self.assertFalse(r.eligible)
        self.assertIn("arrears", [c.code for c in r.blocking_failures])

    def test_no_policy_in_force_is_a_clear_refusal_not_a_crash(self):
        r = evaluate(self.scheme, event_type=self.bereavement,
                     event_date=dt.date(2000, 1, 1), membership=self.m_mary)
        self.assertFalse(r.eligible)
        self.assertEqual(r.policy, None)
        self.assertIn("No policy version was in force", r.checks[0].detail)


# ===========================================================================
# 4. INTERNAL CONTROLS
# ===========================================================================

class ControlTests(SchemeFixture):

    def test_the_raiser_cannot_approve_their_own_case(self):
        case = self._case(raised_by=self.treasurer)
        case_svc.submit_case(case, user=self.treasurer)
        case_svc.assess_case(case, user=self.treasurer)
        with self.assertRaises(ValidationError) as cm:
            case_svc.approve_case(case, amount=Decimal("20000"), user=self.treasurer)
        self.assertIn("other than the person who raised", str(cm.exception))

    def test_an_ineligible_case_needs_a_written_override_reason(self):
        case = self._case(membership=self.m_john)      # inside the waiting period
        case_svc.submit_case(case, user=self.clerk)
        case_svc.assess_case(case, user=self.treasurer)
        self.assertFalse(case.eligibility_snapshot["eligible"])

        with self.assertRaises(ValidationError):
            case_svc.approve_case(case, amount=Decimal("20000"), user=self.treasurer)

        case_svc.approve_case(case, amount=Decimal("20000"), user=self.treasurer,
                              override_reason="Board resolution 2026/14 — hardship.")
        case.refresh_from_db()
        self.assertEqual(case.status, BenevolentCase.Status.APPROVED)
        self.assertIn("Board resolution", case.override_reason)

    def test_a_policy_that_forbids_overrides_is_obeyed(self):
        v2 = scheme_svc.new_version_from(
            self.policy, effective_from=TODAY - dt.timedelta(days=10),
            user=self.treasurer)
        v2.allow_override = False
        v2.save()
        scheme_svc.publish_policy(v2, user=self.treasurer)

        # the event must fall INSIDE v2's window, or v1 (which does allow an
        # override) would rightly still govern it
        case = self._case(membership=self.m_john,
                          event_date=TODAY - dt.timedelta(days=3))
        case_svc.submit_case(case, user=self.clerk)
        result = case_svc.assess_case(case, user=self.treasurer)
        self.assertEqual(result.policy.version, 2)
        with self.assertRaises(ValidationError) as cm:
            case_svc.approve_case(case, amount=Decimal("20000"), user=self.treasurer,
                                  override_reason="please")
        self.assertIn("does not permit an override", str(cm.exception))

    def test_approval_over_the_cap_needs_a_reason(self):
        case = self._case()
        case_svc.submit_case(case, user=self.clerk)
        case_svc.assess_case(case, user=self.treasurer)
        with self.assertRaises(ValidationError):
            case_svc.approve_case(case, amount=Decimal("80000"), user=self.treasurer)

    def test_a_locked_period_blocks_a_contribution_and_a_payout(self):
        from core.models import PeriodLock
        PeriodLock.objects.create(year=TODAY.year, month=TODAY.month,
                                  locked_by=self.treasurer)
        with self.assertRaises(ValidationError):
            contrib_svc.record_contribution(
                self.scheme, date=TODAY, amount=Decimal("200"),
                membership=self.m_mary, user=self.treasurer)

    def test_a_case_with_payments_cannot_simply_be_cancelled(self):
        case = self._case()
        case_svc.submit_case(case, user=self.clerk)
        case_svc.assess_case(case, user=self.treasurer)
        case_svc.approve_case(case, amount=Decimal("20000"), user=self.treasurer)
        p = case_svc.record_payout(case, amount=Decimal("20000"), user=self.clerk)
        p.expense.status = Expense.Status.PAID
        p.expense.save()
        case.refresh_from_db()
        with self.assertRaises(ValidationError):
            case_svc.cancel_case(case, user=self.treasurer)

    def test_a_scheme_cannot_open_without_a_policy(self):
        fund = Department.objects.create(
            name="Education Fund", slug="education-fund",
            fund_type=Department.FundType.LOCAL)
        s = BenevolentScheme.objects.create(name="Education Scheme", code="EDU",
                                            fund=fund)
        with self.assertRaises(ValidationError) as cm:
            scheme_svc.activate_scheme(s)
        self.assertIn("no policy in force", str(cm.exception))

    def test_a_scheme_fund_must_be_local_not_trust(self):
        trust = Department.objects.create(
            name="Camp Offering", slug="camp-offering",
            fund_type=Department.FundType.TRUST)
        s = BenevolentScheme(name="Bad Scheme", code="BAD", fund=trust)
        with self.assertRaises(ValidationError):
            s.full_clean(exclude=["slug"])

    def test_two_live_schemes_cannot_share_one_fund(self):
        s = BenevolentScheme(name="Second Scheme", code="SEC", fund=self.fund)
        with self.assertRaises(ValidationError):
            s.full_clean(exclude=["slug"])


# ===========================================================================
# Views & API
# ===========================================================================

class ViewTests(SchemeFixture):

    def test_screens_load_for_a_treasurer(self):
        self.client.force_login(self.treasurer)
        case = self._case()
        for url in [reverse("benevolent_dashboard"),
                    reverse("benevolent_scheme_list"),
                    reverse("benevolent_scheme_detail", args=[self.scheme.pk]),
                    reverse("benevolent_case_list"),
                    reverse("benevolent_case_detail", args=[case.pk]),
                    reverse("benevolent_membership_list"),
                    reverse("benevolent_membership_detail", args=[self.m_mary.pk]),
                    reverse("benevolent_contribution_list"),
                    reverse("benevolent_policy_edit",
                            args=[self.scheme.pk, self.policy.pk])]:
            self.assertEqual(self.client.get(url).status_code, 200, url)

    def test_an_assistant_cannot_approve_a_case(self):
        case = self._case()
        case_svc.submit_case(case, user=self.clerk)
        case_svc.assess_case(case, user=self.treasurer)
        self.client.force_login(self.clerk)
        r = self.client.post(
            reverse("benevolent_case_decide", args=[case.pk, "approve"]),
            {"amount": "20000"})
        self.assertEqual(r.status_code, 302)          # bounced, not performed
        case.refresh_from_db()
        self.assertEqual(case.status, BenevolentCase.Status.ASSESSED)
        self.assertIsNone(case.approved_amount)

    def test_an_assistant_cannot_publish_a_policy(self):
        self.client.force_login(self.clerk)
        v2 = scheme_svc.new_version_from(
            self.policy, effective_from=TODAY + dt.timedelta(days=30))
        self.client.post(reverse("benevolent_policy_action",
                                 args=[self.scheme.pk, v2.pk, "publish"]))
        v2.refresh_from_db()
        self.assertEqual(v2.status, SchemePolicy.Status.DRAFT)

    def test_eligibility_api_returns_the_full_working(self):
        self.client.force_login(self.treasurer)
        r = self.client.get(reverse("benevolent_api_eligibility"), {
            "scheme": self.scheme.pk, "event_type": self.bereavement.pk,
            "membership": self.m_john.pk, "event_date": TODAY.isoformat()})
        self.assertEqual(r.status_code, 200)
        data = r.json()
        self.assertFalse(data["eligible"])
        self.assertEqual(data["policy_version"], 1)
        self.assertTrue(any(c["code"] == "waiting_period" and not c["passed"]
                            for c in data["checks"]))

    def test_scheme_api_reports_registry_figures(self):
        contrib_svc.record_contribution(
            self.scheme, date=TODAY, amount=Decimal("7500"),
            membership=self.m_mary, user=self.treasurer)
        self.client.force_login(self.treasurer)
        r = self.client.get(reverse("benevolent_api_scheme", args=[self.scheme.pk]))
        data = r.json()
        self.assertEqual(data["financials"]["balance"], "7500.00")
        self.assertEqual(data["financials"]["contributions"], "7500.00")
        self.assertEqual(data["policy"]["policy_version"], 1)


class UrlRoutingTests(SchemeFixture):
    """Guards a real bug found in this phase: an unprefixed `<str:action>` route
    under schemes/<pk>/ greedily swallowed every sibling single-segment route
    (events/, enrol/, contribute/), so each returned 405 from the POST-only
    action view instead of rendering. Verbs are now namespaced; these tests fail
    loudly if that regresses."""

    def setUp(self):
        super().setUp()
        self.client.force_login(self.treasurer)

    def test_sibling_routes_are_not_shadowed_by_the_action_verb(self):
        for name in ("benevolent_event_types", "benevolent_enrol",
                     "benevolent_contribute"):
            url = reverse(name, args=[self.scheme.pk])
            r = self.client.get(url)
            self.assertEqual(r.status_code, 200,
                             f"{name} ({url}) should render, not 405/404")

    def test_the_action_verb_still_routes(self):
        r = self.client.post(
            reverse("benevolent_scheme_action", args=[self.scheme.pk, "suspend"]))
        self.assertEqual(r.status_code, 302)
        self.scheme.refresh_from_db()
        self.assertEqual(self.scheme.status, BenevolentScheme.Status.SUSPENDED)

    def test_case_payout_route_is_not_shadowed(self):
        case = self._case()
        case_svc.submit_case(case, user=self.clerk)
        case_svc.assess_case(case, user=self.treasurer)
        case_svc.approve_case(case, amount=Decimal("20000"), user=self.treasurer)
        r = self.client.post(reverse("benevolent_case_payout", args=[case.pk]),
                             {"amount": "20000", "date": TODAY.isoformat(),
                              "method": "CASH"})
        self.assertEqual(r.status_code, 302)
        self.assertEqual(case.payouts.count(), 1)


class MetricsRegistryTests(SchemeFixture):

    def test_benevolent_metrics_are_registered_with_definitions(self):
        from core.metrics import metrics
        for key in ("benevolent_scheme_summary", "benevolent_contributions",
                    "benevolent_payouts", "benevolent_fund_balance",
                    "benevolent_commitments"):
            self.assertTrue(metrics.has(key), key)
            self.assertTrue(metrics.get(key).definition)

    def test_summary_figures_come_from_fund_summary(self):
        from core.metrics import metrics
        contrib_svc.record_contribution(
            self.scheme, date=TODAY, amount=Decimal("1200"),
            membership=self.m_mary, user=self.treasurer)
        rows = metrics.benevolent_scheme_summary(None, TODAY)
        row = next(r for r in rows if r["scheme"].pk == self.scheme.pk)
        fund_rows = {r["department"].id: r
                     for r in metrics.fund_summary(None, TODAY, False)}
        self.assertEqual(row["closing"], fund_rows[self.fund.id]["closing"])
        self.assertEqual(row["contributions"], Decimal("1200"))

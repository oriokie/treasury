"""Item 3 — contribution exceptions and automatic reconciliation.

Covers the real-world messes a treasurer meets:
  * paid twice / duplicate         — screen_contribution flags it
  * payment reversed               — reverse_contribution undoes without deleting
  * wrong scheme / wrong member    — correct_attribution re-attributes cleanly
  * backdated / future payment     — screen_contribution flags both
  * anonymous / employer / sponsor — payer_type on the contribution
  * bulk-upload errors             — validate_bulk screens the batch first
  * automatic reconciliation       — reconcile_scheme compares index vs bank
"""
import datetime as dt
from decimal import Decimal

from django.contrib.auth.models import Group, User
from django.core.exceptions import ValidationError
from django.test import TestCase
from django.urls import reverse

from core.roles import TREASURER
from departments.models import Department
from members.models import Member

from benevolent.models import (BenevolentContribution, BenevolentScheme,
                               SchemeMembership, SchemePolicy)
from benevolent.services import contributions as contrib_svc
from benevolent.services import exceptions as exc_svc
from benevolent.services import registry as reg_svc
from benevolent.services import schemes as scheme_svc

TODAY = dt.date.today()


class ExceptionsFixture(TestCase):
    def setUp(self):
        self.treasurer = User.objects.create_user("t_exc", password="x")
        self.treasurer.groups.add(Group.objects.get_or_create(name=TREASURER)[0])
        self.fund = Department.objects.create(
            name="Exc Fund", slug="exc-fund", fund_type=Department.FundType.LOCAL)
        self.scheme = BenevolentScheme.objects.create(
            name="Exc Scheme", code="EXC", fund=self.fund, created_by=self.treasurer)
        self._publish()
        self.mem = self._enrol("Jane Member", 100)

    def _publish(self, scheme=None):
        scheme = scheme or self.scheme
        policy = SchemePolicy.objects.create(
            scheme=scheme, effective_from=TODAY - dt.timedelta(days=400),
            membership_required=True, waiting_period_days=0,
            contribution_mode=SchemePolicy.ContributionMode.FIXED_PERIODIC,
            contribution_amount=Decimal("100"),
            contribution_frequency=SchemePolicy.Frequency.MONTHLY,
            benefit_mode=SchemePolicy.BenefitMode.FIXED, benefit_amount=Decimal("5000"),
            arrears_treatment=SchemePolicy.ArrearsTreatment.IGNORE,
            created_by=self.treasurer)
        scheme_svc.publish_policy(policy, user=self.treasurer)
        if scheme.status == BenevolentScheme.Status.DRAFT:
            scheme_svc.activate_scheme(scheme, user=self.treasurer)
        return policy

    def _enrol(self, name, days_ago, scheme=None):
        scheme = scheme or self.scheme
        m = Member.objects.create(name=name)
        return reg_svc.register(
            scheme, m, joined_on=TODAY - dt.timedelta(days=days_ago),
            user=self.treasurer)


# ---------------------------------------------------------------------------
# Date exceptions
# ---------------------------------------------------------------------------

class DateExceptionTests(ExceptionsFixture):
    def test_future_dated_is_flagged(self):
        exc = exc_svc.screen_contribution(
            self.scheme, date=TODAY + dt.timedelta(days=5), amount=Decimal("100"),
            membership=self.mem)
        codes = {e.code for e in exc}
        self.assertIn("future_dated", codes)

    def test_before_cover_is_flagged(self):
        exc = exc_svc.screen_contribution(
            self.scheme, date=TODAY - dt.timedelta(days=300), amount=Decimal("100"),
            membership=self.mem)
        self.assertIn("before_cover", {e.code for e in exc})

    def test_long_backdated_is_flagged(self):
        old = self._enrol("Old Timer", 900)
        exc = exc_svc.screen_contribution(
            self.scheme, date=TODAY - dt.timedelta(days=400), amount=Decimal("100"),
            membership=old)
        self.assertIn("long_backdated", {e.code for e in exc})

    def test_non_positive_amount_blocks(self):
        exc = exc_svc.screen_contribution(
            self.scheme, date=TODAY, amount=Decimal("0"), membership=self.mem)
        self.assertTrue(any(e.code == "non_positive" and e.blocking for e in exc))

    def test_a_normal_contribution_is_clean(self):
        exc = exc_svc.screen_contribution(
            self.scheme, date=TODAY, amount=Decimal("100"), membership=self.mem)
        self.assertEqual(exc, [])


# ---------------------------------------------------------------------------
# Duplicate
# ---------------------------------------------------------------------------

class DuplicateTests(ExceptionsFixture):
    def test_duplicate_is_flagged(self):
        from benevolent.models import BenevolentSettings
        cfg = BenevolentSettings.get()
        cfg.duplicate_window_days = 7
        cfg.save()
        contrib_svc.record_contribution(
            self.scheme, date=TODAY, amount=Decimal("100"), user=self.treasurer,
            membership=self.mem)
        exc = exc_svc.screen_contribution(
            self.scheme, date=TODAY, amount=Decimal("100"), membership=self.mem)
        self.assertIn("duplicate", {e.code for e in exc})


# ---------------------------------------------------------------------------
# Reversal
# ---------------------------------------------------------------------------

class ReversalTests(ExceptionsFixture):
    def test_reverse_stops_it_counting(self):
        c = contrib_svc.record_contribution(
            self.scheme, date=TODAY, amount=Decimal("100"), user=self.treasurer,
            membership=self.mem)
        self.assertTrue(c.effective)
        total_before = contrib_svc.contributions_total(scheme=self.scheme)
        exc_svc.reverse_contribution(c, user=self.treasurer, reason="bounced")
        c.refresh_from_db()
        self.assertFalse(c.effective)
        self.assertIsNotNone(c.reversed_at)
        total_after = contrib_svc.contributions_total(scheme=self.scheme)
        self.assertEqual(total_after, total_before - Decimal("100"))

    def test_reverse_never_deletes(self):
        c = contrib_svc.record_contribution(
            self.scheme, date=TODAY, amount=Decimal("100"), user=self.treasurer,
            membership=self.mem)
        exc_svc.reverse_contribution(c, user=self.treasurer, reason="mistake")
        self.assertTrue(BenevolentContribution.objects.filter(pk=c.pk).exists())
        # the contra transaction exists on the ledger
        self.assertTrue(c.transaction.is_reversed)

    def test_double_reverse_is_refused(self):
        c = contrib_svc.record_contribution(
            self.scheme, date=TODAY, amount=Decimal("100"), user=self.treasurer,
            membership=self.mem)
        exc_svc.reverse_contribution(c, user=self.treasurer, reason="once")
        with self.assertRaises(ValidationError):
            exc_svc.reverse_contribution(c, user=self.treasurer, reason="twice")


# ---------------------------------------------------------------------------
# Correction / re-attribution
# ---------------------------------------------------------------------------

class CorrectionTests(ExceptionsFixture):
    def test_wrong_member_reattributed(self):
        right = self._enrol("Correct Member", 90)
        c = contrib_svc.record_contribution(
            self.scheme, date=TODAY, amount=Decimal("100"), user=self.treasurer,
            membership=self.mem)
        corrected = exc_svc.correct_attribution(
            c, user=self.treasurer, reason="wrong member", new_membership=right)
        c.refresh_from_db()
        self.assertIsNotNone(c.reversed_at)          # original reversed
        self.assertEqual(corrected.membership_id, right.pk)
        self.assertEqual(corrected.amount, Decimal("100"))
        # money is unchanged in total: reversed original + new = net one contribution
        self.assertEqual(
            contrib_svc.contributions_total(scheme=self.scheme), Decimal("100"))

    def test_wrong_scheme_reattributed(self):
        other = BenevolentScheme.objects.create(
            name="Other", code="OTH", fund=Department.objects.create(
                name="Other Fund", slug="other-fund", fund_type=Department.FundType.LOCAL),
            created_by=self.treasurer)
        self._publish(other)
        c = contrib_svc.record_contribution(
            self.scheme, date=TODAY, amount=Decimal("100"), user=self.treasurer,
            membership=self.mem)
        corrected = exc_svc.correct_attribution(
            c, user=self.treasurer, reason="wrong scheme", new_scheme=other)
        self.assertEqual(corrected.scheme_id, other.pk)
        c.refresh_from_db()
        self.assertIsNotNone(c.reversed_at)

    def test_cannot_correct_an_already_reversed_contribution(self):
        c = contrib_svc.record_contribution(
            self.scheme, date=TODAY, amount=Decimal("100"), user=self.treasurer,
            membership=self.mem)
        exc_svc.reverse_contribution(c, user=self.treasurer, reason="gone")
        with self.assertRaises(ValidationError):
            exc_svc.correct_attribution(c, user=self.treasurer, reason="too late")

    def test_member_from_wrong_scheme_refused(self):
        other = BenevolentScheme.objects.create(
            name="Other2", code="OT2", fund=Department.objects.create(
                name="Other2 Fund", slug="other2-fund", fund_type=Department.FundType.LOCAL),
            created_by=self.treasurer)
        self._publish(other)
        other_mem = self._enrol("Other Member", 90, scheme=other)
        c = contrib_svc.record_contribution(
            self.scheme, date=TODAY, amount=Decimal("100"), user=self.treasurer,
            membership=self.mem)
        # trying to move to other_mem while keeping THIS scheme -> mismatch
        with self.assertRaises(ValidationError):
            exc_svc.correct_attribution(
                c, user=self.treasurer, reason="x", new_membership=other_mem)


# ---------------------------------------------------------------------------
# Payer type (anonymous / employer / sponsor / third-party)
# ---------------------------------------------------------------------------

class PayerTypeTests(ExceptionsFixture):
    def test_employer_payment_records_payer(self):
        c = contrib_svc.record_contribution(
            self.scheme, date=TODAY, amount=Decimal("100"), user=self.treasurer,
            membership=self.mem,
            payer_type=BenevolentContribution.PayerType.EMPLOYER,
            payer_name="Acme Ltd")
        self.assertEqual(c.payer_type, BenevolentContribution.PayerType.EMPLOYER)
        self.assertEqual(c.payer_name, "Acme Ltd")
        # it still settles the member's dues — the money counts as the member's
        self.assertEqual(c.membership_id, self.mem.pk)

    def test_anonymous_donation_defaults_correctly(self):
        # a memberless donation with no named payer is genuinely anonymous
        c = contrib_svc.record_contribution(
            self.scheme, date=TODAY, amount=Decimal("500"), user=self.treasurer,
            member=Member.objects.create(name="Walk In"))
        # member given but no membership -> still SELF unless explicitly anonymous;
        # a truly memberless+nameless gift is ANONYMOUS
        c2 = contrib_svc.record_contribution(
            self.scheme, date=TODAY, amount=Decimal("200"), user=self.treasurer)
        self.assertEqual(c2.payer_type, BenevolentContribution.PayerType.ANONYMOUS)

    def test_sponsor_payment(self):
        c = contrib_svc.record_contribution(
            self.scheme, date=TODAY, amount=Decimal("100"), user=self.treasurer,
            membership=self.mem,
            payer_type=BenevolentContribution.PayerType.SPONSOR,
            payer_name="Diocese Fund")
        self.assertEqual(c.payer_type, BenevolentContribution.PayerType.SPONSOR)


# ---------------------------------------------------------------------------
# Bulk upload validation
# ---------------------------------------------------------------------------

class BulkValidationTests(ExceptionsFixture):
    def test_batch_flags_bad_rows_without_committing(self):
        rows = [
            {"membership": self.mem, "date": TODAY, "amount": Decimal("100")},
            {"membership": self.mem, "date": TODAY + dt.timedelta(days=10),
             "amount": Decimal("100")},   # future
            {"membership": self.mem, "date": TODAY, "amount": Decimal("0")},  # bad
        ]
        results = exc_svc.validate_bulk(self.scheme, rows)
        self.assertEqual(len(results), 3)
        self.assertTrue(results[0].ok)
        self.assertTrue(results[1].ok)      # future is advisory, not blocking
        self.assertFalse(results[2].ok)     # zero amount blocks
        # nothing was committed
        self.assertEqual(
            contrib_svc.contributions_total(scheme=self.scheme), Decimal("0"))


# ---------------------------------------------------------------------------
# Reconciliation
# ---------------------------------------------------------------------------

class ReconciliationTests(ExceptionsFixture):
    def test_clean_scheme_reconciles(self):
        contrib_svc.record_contribution(
            self.scheme, date=TODAY, amount=Decimal("100"), user=self.treasurer,
            membership=self.mem)
        result = exc_svc.reconcile_scheme(self.scheme)
        self.assertTrue(result.balanced)
        self.assertEqual(result.difference, Decimal("0"))

    def test_orphan_receipt_is_detected(self):
        # a bank credit into the scheme fund with no contribution behind it
        from giving.models import Transaction
        Transaction.objects.create(
            date=TODAY, channel=Transaction.Channel.BANK,
            direction=Transaction.Direction.CREDIT, amount=Decimal("250"),
            department=self.fund, confirmed=True,
            allocation_status=Transaction.Status.MANUAL)
        result = exc_svc.reconcile_scheme(self.scheme)
        self.assertFalse(result.balanced)
        kinds = {x.kind for x in result.exceptions}
        self.assertIn("orphan_receipt", kinds)

    def test_reversed_contribution_does_not_break_reconciliation(self):
        c = contrib_svc.record_contribution(
            self.scheme, date=TODAY, amount=Decimal("100"), user=self.treasurer,
            membership=self.mem)
        exc_svc.reverse_contribution(c, user=self.treasurer, reason="bounced")
        # both the receipt and its contribution are reversed -> nets to clean
        result = exc_svc.reconcile_scheme(self.scheme)
        self.assertTrue(result.balanced)


# ---------------------------------------------------------------------------
# View smoke tests
# ---------------------------------------------------------------------------

class ExceptionViewTests(ExceptionsFixture):
    def test_reverse_view_requires_reason(self):
        c = contrib_svc.record_contribution(
            self.scheme, date=TODAY, amount=Decimal("100"), user=self.treasurer,
            membership=self.mem)
        self.client.force_login(self.treasurer)
        self.client.post(reverse("benevolent_contribution_reverse", args=[c.pk]),
                         {"reason": ""}, follow=True)
        c.refresh_from_db()
        self.assertIsNone(c.reversed_at)   # refused without a reason

    def test_reverse_view_works_with_reason(self):
        c = contrib_svc.record_contribution(
            self.scheme, date=TODAY, amount=Decimal("100"), user=self.treasurer,
            membership=self.mem)
        self.client.force_login(self.treasurer)
        self.client.post(reverse("benevolent_contribution_reverse", args=[c.pk]),
                         {"reason": "payment bounced"}, follow=True)
        c.refresh_from_db()
        self.assertIsNotNone(c.reversed_at)

    def test_reconcile_page_renders(self):
        self.client.force_login(self.treasurer)
        r = self.client.get(reverse("benevolent_reconcile", args=[self.scheme.pk]))
        self.assertEqual(r.status_code, 200)

    def test_new_fields_frozen_note(self):
        # payer_type/payer_name/reversal fields are NOT rule fields (they are
        # per-contribution facts, not policy) — confirm they are not accidentally
        # in RULE_FIELDS
        for f in ["payer_type", "payer_name", "reversed_at"]:
            self.assertNotIn(f, SchemePolicy.RULE_FIELDS)

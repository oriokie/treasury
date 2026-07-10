"""Follow-up fixes to Send to review:
1. Bulk send-to-review must only combine entries that share a genuine
   bank-assigned identifier (core_ref split-suffix pattern, or an exact
   M-Pesa reference) — never two different people's unrelated gifts that
   just happen to share the same free-text reference and date. Added
   Transaction.strict_split_siblings() for this, used only by send-to-review;
   the existing split_siblings() (which still allows the looser reference+
   date fallback) is left untouched since cash-entry deletion genuinely
   needs it (a cash entry has no bank identifier at all).
2. A reversal (contra) entry keeps the same direction and a positive amount
   as its original — by design, since the ledger nets it to zero by not
   posting either side, not by inverting the sign. But the transaction list
   showed both sides as identical positive amounts, giving no visual cue
   they cancel out. Reversal rows now display in parentheses like a debit,
   regardless of their stored direction.
3. Confirmed (with a real browser + Playwright, not just the backend) that
   selecting a single entry and using the bulk action works correctly —
   locked in with an explicit test."""
import datetime as dt
from decimal import Decimal
from django.test import TestCase, Client
from django.contrib.auth.models import User, Group
from departments.models import Department
from giving.models import Transaction


def _tr():
    u = User.objects.create_user("tr_strictgroup", password="x", is_superuser=True)
    u.groups.add(Group.objects.get_or_create(name="Treasurer")[0])
    return u


class StrictSiblingGroupingTests(TestCase):
    """Transaction.strict_split_siblings() itself."""
    def setUp(self):
        self.d1 = Department.objects.create(name="StrictFund1", fund_type="LOCAL",
            category="MINISTRY")
        self.d2 = Department.objects.create(name="StrictFund2", fund_type="LOCAL",
            category="MINISTRY")

    def test_genuine_core_ref_split_siblings_found(self):
        t1 = Transaction.objects.create(date=dt.date(2026, 6, 1), amount=Decimal("100"),
            direction="CREDIT", confirmed=True, channel="BANK", allocation_status="AUTO",
            department=self.d1, core_ref="GENUINE001", reference="offering")
        t2 = Transaction.objects.create(date=dt.date(2026, 6, 1), amount=Decimal("100"),
            direction="CREDIT", confirmed=True, channel="BANK", allocation_status="AUTO",
            department=self.d2, core_ref="GENUINE001-S1", reference="offering")
        self.assertIn(t2, list(t1.strict_split_siblings()))

    def test_genuine_mpesa_ref_split_siblings_found(self):
        t1 = Transaction.objects.create(date=dt.date(2026, 6, 2), amount=Decimal("150"),
            direction="CREDIT", confirmed=True, channel="BANK", allocation_status="AUTO",
            department=self.d1, mpesa_ref="SHAREDMPESA1", reference="tithe")
        t2 = Transaction.objects.create(date=dt.date(2026, 6, 2), amount=Decimal("150"),
            direction="CREDIT", confirmed=True, channel="BANK", allocation_status="AUTO",
            department=self.d2, mpesa_ref="SHAREDMPESA1", reference="tithe")
        self.assertIn(t2, list(t1.strict_split_siblings()))

    def test_same_reference_and_date_alone_is_not_enough(self):
        """The core bug: two unrelated people using the same common
        reference word on the same day must NOT be treated as siblings."""
        t1 = Transaction.objects.create(date=dt.date(2026, 6, 3), amount=Decimal("200"),
            direction="CREDIT", confirmed=True, channel="BANK", allocation_status="AUTO",
            department=self.d1, core_ref="UNRELATED001", reference="tithe")
        t2 = Transaction.objects.create(date=dt.date(2026, 6, 3), amount=Decimal("300"),
            direction="CREDIT", confirmed=True, channel="BANK", allocation_status="AUTO",
            department=self.d2, core_ref="UNRELATED002", reference="tithe")
        self.assertNotIn(t2, list(t1.strict_split_siblings()))
        self.assertNotIn(t1, list(t2.strict_split_siblings()))

    def test_no_identifier_at_all_returns_empty(self):
        t1 = Transaction.objects.create(date=dt.date(2026, 6, 4), amount=Decimal("50"),
            direction="CREDIT", confirmed=True, channel="CASH", allocation_status="AUTO",
            department=self.d1, reference="cash gift")
        self.assertEqual(list(t1.strict_split_siblings()), [])

    def test_loose_split_siblings_still_allows_reference_fallback_unaffected(self):
        """The original split_siblings() (used by cash-entry deletion) must
        be completely unaffected by this fix."""
        t1 = Transaction.objects.create(date=dt.date(2026, 6, 5), amount=Decimal("60"),
            direction="CREDIT", confirmed=True, channel="CASH", allocation_status="AUTO",
            department=self.d1, reference="shared cash ref")
        t2 = Transaction.objects.create(date=dt.date(2026, 6, 5), amount=Decimal("40"),
            direction="CREDIT", confirmed=True, channel="CASH", allocation_status="AUTO",
            department=self.d2, reference="shared cash ref")
        self.assertIn(t2, list(t1.split_siblings()))


class BulkSendToReviewStrictGroupingTests(TestCase):
    def setUp(self):
        self.tr = _tr()
        self.c = Client(); self.c.force_login(self.tr)
        self.d1 = Department.objects.create(name="BulkStrictFund1", fund_type="LOCAL",
            category="MINISTRY")
        self.d2 = Department.objects.create(name="BulkStrictFund2", fund_type="LOCAL",
            category="MINISTRY")

    def test_unrelated_entries_sharing_reference_produce_two_separate_replacements(self):
        t1 = Transaction.objects.create(date=dt.date(2026, 6, 10), amount=Decimal("200"),
            direction="CREDIT", confirmed=True, channel="BANK", allocation_status="AUTO",
            department=self.d1, core_ref="BULKUNREL001", reference="tithe")
        t2 = Transaction.objects.create(date=dt.date(2026, 6, 10), amount=Decimal("300"),
            direction="CREDIT", confirmed=True, channel="BANK", allocation_status="AUTO",
            department=self.d2, core_ref="BULKUNREL002", reference="tithe")
        self.c.post("/transactions/bulk-reverse/", {
            "action": "send_to_review", "ids": [str(t1.id), str(t2.id)], "reason": ""})
        replacements = Transaction.objects.filter(allocation_status="REVIEW",
            reference="tithe", date=dt.date(2026, 6, 10))
        # must produce two SEPARATE entries (200 and 300), never one combined 500
        amounts = sorted(r.amount for r in replacements)
        self.assertEqual(amounts, [Decimal("200"), Decimal("300")])

    def test_genuine_split_still_combines_correctly(self):
        t1 = Transaction.objects.create(date=dt.date(2026, 6, 11), amount=Decimal("120"),
            direction="CREDIT", confirmed=True, channel="BANK", allocation_status="AUTO",
            department=self.d1, core_ref="BULKGENUINE001", reference="combined offering")
        t2 = Transaction.objects.create(date=dt.date(2026, 6, 11), amount=Decimal("80"),
            direction="CREDIT", confirmed=True, channel="BANK", allocation_status="AUTO",
            department=self.d2, core_ref="BULKGENUINE001-S1", reference="combined offering")
        self.c.post("/transactions/bulk-reverse/", {
            "action": "send_to_review", "ids": [str(t1.id), str(t2.id)], "reason": ""})
        replacements = Transaction.objects.filter(allocation_status="REVIEW",
            reference="combined offering")
        self.assertEqual(replacements.count(), 1)
        self.assertEqual(replacements.first().amount, Decimal("200"))

    def test_single_entry_selection_works_via_bulk_endpoint(self):
        t = Transaction.objects.create(date=dt.date(2026, 6, 12), amount=Decimal("175"),
            direction="CREDIT", confirmed=True, channel="BANK", allocation_status="AUTO",
            department=self.d1, reference="single bulk test")
        r = self.c.post("/transactions/bulk-reverse/", {
            "action": "send_to_review", "ids": [str(t.id)], "reason": ""})
        self.assertEqual(r.status_code, 302)
        self.assertIn("/queue/", r.url)
        t.refresh_from_db()
        self.assertTrue(t.is_reversed)
        replacement = Transaction.objects.filter(allocation_status="REVIEW",
            reference="single bulk test").first()
        self.assertIsNotNone(replacement)
        self.assertEqual(replacement.amount, Decimal("175"))


class ReversalNegativeDisplayTests(TestCase):
    def setUp(self):
        self.tr = _tr()
        self.c = Client(); self.c.force_login(self.tr)
        self.d = Department.objects.create(name="NegDisplayFund", fund_type="LOCAL",
            category="MINISTRY")

    def test_reversal_amount_shown_in_parentheses(self):
        t = Transaction.objects.create(date=dt.date(2026, 6, 13), amount=Decimal("500"),
            direction="CREDIT", confirmed=True, channel="BANK", allocation_status="AUTO",
            department=self.d, reference="negdisplaytest")
        t.reverse(self.tr, reason="test")
        b = self.c.get("/transactions/?q=negdisplaytest").content.decode()
        self.assertIn("(500.00)", b)

    def test_original_credit_amount_still_shown_positive(self):
        t = Transaction.objects.create(date=dt.date(2026, 6, 14), amount=Decimal("650"),
            direction="CREDIT", confirmed=True, channel="BANK", allocation_status="AUTO",
            department=self.d, reference="negdisplaytest2")
        t.reverse(self.tr, reason="test")
        b = self.c.get("/transactions/?q=negdisplaytest2").content.decode()
        # the original (now-reversed) row is the one carrying the "reversed"
        # pill (not "reversal") - check ITS specific amount cell, since the
        # page also legitimately shows "(650.00)" on the separate reversal row
        idx = b.find("pill-grey\">reversed</span>")
        self.assertGreater(idx, 0)
        row_start = b.rfind("<tr", 0, idx)
        row = b[row_start:idx]
        self.assertIn("650.00", row)
        self.assertNotIn("(650.00)", row)

    def test_reversal_row_gets_debit_styling_class(self):
        t = Transaction.objects.create(date=dt.date(2026, 6, 15), amount=Decimal("300"),
            direction="CREDIT", confirmed=True, channel="BANK", allocation_status="AUTO",
            department=self.d, reference="negdisplaytest3")
        t.reverse(self.tr, reason="test")
        b = self.c.get("/transactions/?q=negdisplaytest3").content.decode()
        self.assertIn('tx-debit', b)

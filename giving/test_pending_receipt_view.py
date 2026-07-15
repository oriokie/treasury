"""The on-page "Pending receipt" view: sortable, with repeated payer names
highlighted so a treasurer notices "the same person twice" or "one name spelled
two ways" without having to open the Excel export and scan it by eye.

"Same name" is judged via members.models.name_key — order-insensitive — so
"RUTH MOMANYI" and "MOMANYI RUTH" are recognised as the same giver, consistent
with how the rest of the system already matches members.
"""
import datetime as dt
from decimal import Decimal

from django.contrib.auth.models import Group, User
from django.test import TestCase
from django.urls import reverse

from core.roles import TREASURER
from departments.models import Department
from giving.models import Transaction

TODAY = dt.date.today()


class PendingReceiptViewFixture(TestCase):
    def setUp(self):
        self.user = User.objects.create_user("t_pr", password="x")
        self.user.groups.add(Group.objects.get_or_create(name=TREASURER)[0])
        self.trust = Department.objects.create(
            name="Trust Fund", slug="trust-fund",
            fund_type=Department.FundType.TRUST)

    def _credit(self, name, amount, days_ago=0, dept=None, receipted=False):
        self._n = getattr(self, "_n", 0) + 1
        return Transaction.objects.create(
            date=TODAY - dt.timedelta(days=days_ago),
            channel=Transaction.Channel.BANK, direction="CREDIT",
            amount=Decimal(amount), department=dept or self.trust,
            allocation_status=Transaction.Status.MANUAL, confirmed=True,
            payer_name=name, core_ref=f"TESTREF{self._n:05d}",
            processed_via_envelope=receipted)


class PendingReceiptSortTests(PendingReceiptViewFixture):
    def test_default_sort_is_by_name(self):
        self._credit("ZEBRA MWANGI", 100)
        self._credit("ALPHA OTIENO", 200)
        self.client.force_login(self.user)
        resp = self.client.get(reverse("pending_receipt_view"))
        rows = resp.context["rows"]
        names = [r["name"] for r in rows]
        self.assertEqual(names, sorted(names, key=lambda n: n.upper()))

    def test_sort_by_amount(self):
        self._credit("A", 100)
        self._credit("B", 500)
        self._credit("C", 250)
        self.client.force_login(self.user)
        resp = self.client.get(reverse("pending_receipt_view") + "?sort=amount")
        amounts = [r["amount"] for r in resp.context["rows"]]
        self.assertEqual(amounts, sorted(amounts, reverse=True))

    def test_sort_by_date(self):
        self._credit("A", 100, days_ago=5)
        self._credit("B", 200, days_ago=1)
        self.client.force_login(self.user)
        resp = self.client.get(reverse("pending_receipt_view") + "?sort=date")
        dates = [r["date"] for r in resp.context["rows"]]
        self.assertEqual(dates, sorted(dates))


class DuplicateNameTests(PendingReceiptViewFixture):
    def test_repeated_name_is_flagged_on_every_occurrence(self):
        self._credit("JOSEPH NGWATO", 200, days_ago=4)
        self._credit("JOSEPH NGWATO", 250, days_ago=1)
        self._credit("MARY ACHIENG", 300, days_ago=2)
        self.client.force_login(self.user)
        resp = self.client.get(reverse("pending_receipt_view"))
        rows = resp.context["rows"]
        joseph_rows = [r for r in rows if r["name"] == "JOSEPH NGWATO"]
        mary_rows = [r for r in rows if r["name"] == "MARY ACHIENG"]
        self.assertEqual(len(joseph_rows), 2)
        self.assertTrue(all(r["is_duplicate_name"] for r in joseph_rows))
        self.assertEqual(len(mary_rows), 1)
        self.assertFalse(mary_rows[0]["is_duplicate_name"])
        self.assertEqual(resp.context["duplicate_names"], 1)

    def test_order_insensitive_name_match_flags_as_duplicate(self):
        """'RUTH MOMANYI' and 'MOMANYI RUTH' are the same giver, recorded two
        different ways — this must still be caught, using the same name_key
        logic the rest of the system uses for member matching."""
        self._credit("RUTH MOMANYI", 100, days_ago=3)
        self._credit("MOMANYI RUTH", 150, days_ago=1)
        self.client.force_login(self.user)
        resp = self.client.get(reverse("pending_receipt_view"))
        rows = resp.context["rows"]
        self.assertEqual(len(rows), 2)
        self.assertTrue(all(r["is_duplicate_name"] for r in rows))

    def test_no_duplicates_when_all_names_distinct(self):
        self._credit("A ONE", 100)
        self._credit("B TWO", 200)
        self.client.force_login(self.user)
        resp = self.client.get(reverse("pending_receipt_view"))
        self.assertEqual(resp.context["duplicate_names"], 0)
        self.assertFalse(any(r["is_duplicate_name"] for r in resp.context["rows"]))

    def test_receipted_credits_are_excluded(self):
        self._credit("ALREADY DONE", 100, receipted=True)
        self._credit("STILL PENDING", 200, receipted=False)
        self.client.force_login(self.user)
        resp = self.client.get(reverse("pending_receipt_view"))
        names = [r["name"] for r in resp.context["rows"]]
        self.assertNotIn("ALREADY DONE", names)
        self.assertIn("STILL PENDING", names)


class PendingReceiptPageTests(PendingReceiptViewFixture):
    def test_page_renders_and_highlights_duplicate(self):
        self._credit("REPEAT NAME", 100, days_ago=3)
        self._credit("REPEAT NAME", 150, days_ago=1)
        self.client.force_login(self.user)
        resp = self.client.get(reverse("pending_receipt_view"))
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "prv-dupe-row")
        self.assertContains(resp, "repeats")

    def test_old_export_urls_still_work_unchanged(self):
        """The Excel/PDF export URLs and params must keep working exactly as
        before — a saved bookmark and the Telegram bot's /pending route point
        at them, so this view must be purely additive."""
        self._credit("SOMEONE", 100)
        self.client.force_login(self.user)
        r1 = self.client.get(reverse("transaction_list") + "?export=pending-receipt")
        self.assertEqual(r1.status_code, 200)
        r2 = self.client.get(reverse("transaction_list") + "?export=pending-receipt-pdf")
        self.assertEqual(r2.status_code, 200)

    def test_ledger_page_links_to_the_new_view(self):
        self.client.force_login(self.user)
        resp = self.client.get(reverse("transaction_list"))
        self.assertContains(resp, reverse("pending_receipt_view"))

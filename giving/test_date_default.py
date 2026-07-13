"""TransactionListView used to load every transaction ever recorded on a
bare visit — no date bound applied unless the user explicitly filtered.
Fixed to default to the current month, while an explicit, deliberate
"show everything" (the filter form submitted with blank dates) is still
honoured exactly as before.
"""
import datetime as dt
from decimal import Decimal

from django.contrib.auth.models import Group, User
from django.test import TestCase

from core.roles import TREASURER
from giving.models import Transaction


class TransactionListDateDefaultTests(TestCase):
    def setUp(self):
        self.treasurer = User.objects.create_user("tr_txndate", password="x")
        self.treasurer.groups.add(Group.objects.get_or_create(name=TREASURER)[0])
        self.client.force_login(self.treasurer)
        self.today = dt.date.today()

    def _txn(self, date, ref, amount="500"):
        return Transaction.objects.create(
            date=date, channel="CASH", direction="CREDIT", amount=Decimal(amount),
            reference=ref, confirmed=True, allocation_status="MANUAL")

    def test_a_bare_visit_shows_only_this_month(self):
        self._txn(self.today, "THIS-MONTH-1")
        self._txn(self.today.replace(day=1) - dt.timedelta(days=10), "LAST-MONTH-1")
        r = self.client.get("/transactions/")
        self.assertEqual(r.status_code, 200)
        body = r.content.decode()
        self.assertIn("THIS-MONTH-1", body)
        self.assertNotIn("LAST-MONTH-1", body)

    def test_the_date_inputs_are_prefilled_with_the_default(self):
        r = self.client.get("/transactions/")
        body = r.content.decode()
        first_of_month = self.today.replace(day=1).isoformat()
        self.assertIn(f'value="{first_of_month}"', body)

    def test_explicitly_clearing_the_dates_shows_everything(self):
        self._txn(self.today.replace(day=1) - dt.timedelta(days=400), "OLD-1")
        r = self.client.get("/transactions/?date_from=&date_to=")
        self.assertEqual(r.status_code, 200)
        self.assertIn("OLD-1", r.content.decode())

    def test_an_explicit_range_is_respected_exactly_as_before(self):
        self._txn(dt.date(2025, 3, 15), "MARCH-1")
        r = self.client.get("/transactions/?date_from=2025-03-01&date_to=2025-03-31")
        self.assertEqual(r.status_code, 200)
        body = r.content.decode()
        self.assertIn("MARCH-1", body)

    def test_the_banner_only_shows_when_the_default_was_actually_applied(self):
        r1 = self.client.get("/transactions/")
        self.assertIn("this month", r1.content.decode().lower())
        r2 = self.client.get("/transactions/?date_from=2025-03-01&date_to=2025-03-31")
        self.assertNotIn("Showing <strong>this month</strong>", r2.content.decode())

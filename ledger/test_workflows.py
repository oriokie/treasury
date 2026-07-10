"""Coverage for the general-ledger views: the trial balance, the
balanced-only manual journal, the ledger-vs-fund reconciliation, a rebuild,
and the chart of accounts."""
import datetime as dt
from decimal import Decimal

from django.contrib.auth.models import User, Group
from django.test import TestCase
from django.urls import reverse

from core.roles import TREASURER, AUDITOR
from departments.models import Department
from giving.models import Transaction
from ledger.models import Account, JournalEntry
from ledger.services import posting


def _user(name, role):
    u = User.objects.create_user(name, password="x")
    u.groups.add(Group.objects.get_or_create(name=role)[0])
    return u


class LedgerViewTests(TestCase):
    def setUp(self):
        self.treasurer = _user("l_tr", TREASURER)
        self.client.force_login(self.treasurer)
        self.fund = Department.objects.create(name="LCB", fund_type="LOCAL",
            opening_balance=Decimal("1000"))
        Transaction.objects.create(date=dt.date.today(), channel="BANK",
            direction="CREDIT", amount=Decimal("500"), department=self.fund,
            allocation_status="AUTO", confirmed=True, core_ref="L1")
        posting.rebuild()

    def test_trial_balance_renders_and_balances(self):
        r = self.client.get(reverse("trial_balance"))
        self.assertEqual(r.status_code, 200)
        rows, totals = posting.trial_balance()
        self.assertEqual(totals["debit"], totals["credit"])

    def test_chart_of_accounts_and_journal_and_gl(self):
        for name in ["chart_of_accounts", "journal", "general_ledger",
                     "ledger_reconciliation"]:
            self.assertEqual(self.client.get(reverse(name)).status_code, 200, name)

    def test_rebuild_ledger(self):
        r = self.client.post(reverse("ledger_rebuild"))
        self.assertIn(r.status_code, (200, 302))


class ManualJournalTests(TestCase):
    def setUp(self):
        self.treasurer = _user("mj_tr", TREASURER)
        self.client.force_login(self.treasurer)
        Department.objects.create(name="LCB", fund_type="LOCAL", opening_balance=Decimal("100"))
        posting.rebuild()
        self.cash = Account.objects.filter(system_key="CASH").first()
        self.accum = Account.objects.filter(system_key="ACCUM_FUNDS").first()

    def test_balanced_entry_is_posted(self):
        before = JournalEntry.objects.filter(source_type="manual").count()
        r = self.client.post(reverse("manual_journal"), {
            "date": dt.date.today().isoformat(), "memo": "Correcting entry",
            "account": [str(self.cash.pk), str(self.accum.pk)],
            "dept": ["", ""], "debit": ["50", "0"], "credit": ["0", "50"]})
        self.assertIn(r.status_code, (200, 302))
        self.assertEqual(JournalEntry.objects.filter(source_type="manual").count(),
                         before + 1)

    def test_unbalanced_entry_is_rejected(self):
        before = JournalEntry.objects.filter(source_type="manual").count()
        self.client.post(reverse("manual_journal"), {
            "date": dt.date.today().isoformat(), "memo": "Bad entry",
            "account": [str(self.cash.pk), str(self.accum.pk)],
            "dept": ["", ""], "debit": ["50", "0"], "credit": ["0", "30"]})
        # debits 50 != credits 30 -> not posted
        self.assertEqual(JournalEntry.objects.filter(source_type="manual").count(),
                         before)


class LedgerAccessTests(TestCase):
    def test_auditor_cannot_rebuild(self):
        au = _user("l_au", AUDITOR)
        self.client.force_login(au)
        r = self.client.post(reverse("ledger_rebuild"))
        self.assertIn(r.status_code, (302, 403))

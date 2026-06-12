import datetime as dt
from decimal import Decimal
from django.test import TestCase


class DoubleEntryTests(TestCase):
    def setUp(self):
        from django.contrib.auth.models import User
        from departments.models import Department
        from giving.models import Transaction
        from cashbook.models import Expense
        from ledger.services import posting
        self.u = User.objects.create_superuser("le", password="x")
        self.local = Department.objects.create(name="Offerings", fund_type=Department.FundType.LOCAL,
                                               opening_balance=Decimal("1000"))
        self.trust = Department.objects.create(name="Tithe", fund_type=Department.FundType.TRUST)
        Transaction.objects.create(date=dt.date(2026, 5, 5), channel="CASH", direction="CREDIT",
            amount=Decimal("8000"), department=self.local, allocation_status="MANUAL")
        Transaction.objects.create(date=dt.date(2026, 5, 6), channel="BANK", direction="CREDIT",
            amount=Decimal("5000"), department=self.trust, allocation_status="AUTO")
        Expense.objects.create(date=dt.date(2026, 5, 10), department=self.local, description="power",
            amount=Decimal("2000"), category="UTILITIES", status=Expense.Status.PAID, recorded_by=self.u)
        posting.rebuild()

    def test_chart_has_five_types(self):
        from ledger.models import Account
        types = set(Account.objects.values_list("type", flat=True))
        self.assertEqual(types, {"ASSET", "LIABILITY", "EQUITY", "INCOME", "EXPENSE"})

    def test_trial_balance_balances(self):
        from ledger.services import posting
        rows, tot = posting.trial_balance()
        self.assertEqual(tot["debit"], tot["credit"])
        self.assertGreater(tot["debit"], 0)

    def test_every_entry_is_balanced(self):
        from ledger.models import JournalEntry
        for e in JournalEntry.objects.prefetch_related("lines"):
            d = sum(l.debit for l in e.lines.all())
            c = sum(l.credit for l in e.lines.all())
            self.assertEqual(d, c, f"entry {e.pk} unbalanced")

    def test_trust_receipt_credits_liability_not_income(self):
        from ledger.services import posting
        from ledger.models import Account
        rows, _ = posting.trial_balance()
        by_key = {r["account"].system_key: r for r in rows}
        # trust 5000 sits in Trust funds payable (liability), not income
        self.assertEqual(by_key["TRUST_PAYABLE"]["credit"], Decimal("5000"))
        self.assertNotIn("INC_TITHE", by_key)  # no tithe income posted

    def test_expense_debits_expense_credits_cash(self):
        from ledger.services import posting
        rows, _ = posting.trial_balance()
        by_key = {r["account"].system_key: r for r in rows}
        self.assertEqual(by_key["EXP_UTILITIES"]["debit"], Decimal("2000"))
        # cash = opening 1000 + 8000 + 5000 - 2000 = 12000
        self.assertEqual(by_key["CASH"]["debit"], Decimal("12000"))

    def test_live_signal_posts_entry(self):
        from giving.models import Transaction
        from ledger.models import JournalEntry
        n = JournalEntry.objects.filter(source_type="transaction").count()
        Transaction.objects.create(date=dt.date(2026, 5, 20), channel="CASH", direction="CREDIT",
            amount=Decimal("300"), department=self.local, allocation_status="MANUAL")
        self.assertEqual(JournalEntry.objects.filter(source_type="transaction").count(), n + 1)


class LedgerReconciliationTests(TestCase):
    def setUp(self):
        import datetime as dt
        from decimal import Decimal
        from django.contrib.auth.models import User
        from departments.models import Department
        from giving.models import Transaction
        from cashbook.models import Expense, FundTransfer
        from ledger.services import posting
        self.u = User.objects.create_superuser("rec", password="x")
        self.a = Department.objects.create(name="General", fund_type=Department.FundType.LOCAL,
                                           opening_balance=Decimal("5000"))
        self.b = Department.objects.create(name="Building", fund_type=Department.FundType.LOCAL,
                                           opening_balance=Decimal("0"))
        self.trust = Department.objects.create(name="Tithe", fund_type=Department.FundType.TRUST,
                                               opening_balance=Decimal("100"))
        Transaction.objects.create(date=dt.date(2026, 5, 1), channel="CASH", direction="CREDIT",
            amount=Decimal("8000"), department=self.a, allocation_status="MANUAL")
        Transaction.objects.create(date=dt.date(2026, 5, 2), channel="BANK", direction="CREDIT",
            amount=Decimal("4000"), department=self.trust, allocation_status="AUTO")
        # recurrent + capital expense on A
        Expense.objects.create(date=dt.date(2026, 5, 3), department=self.a, description="power",
            amount=Decimal("1000"), category="UTILITIES", status=Expense.Status.PAID, recorded_by=self.u)
        Expense.objects.create(date=dt.date(2026, 5, 4), department=self.a, description="roof",
            amount=Decimal("3000"), category="CONSTRUCTION", expenditure_type="CAPITAL",
            status=Expense.Status.PAID, recorded_by=self.u)
        FundTransfer.objects.create(date=dt.date(2026, 5, 5), source=self.a, destination=self.b,
            amount=Decimal("2000"), recorded_by=self.u)
        posting.rebuild()
        self.client.login(username="rec", password="x")

    def _engine(self):
        from reports.services import balances
        return {r["department"].id: r["closing"] for r in
                balances.department_summary(None, None, consolidated=False)}

    def test_every_fund_ties_to_ledger(self):
        from ledger.services import posting
        from departments.models import Department
        eng = self._engine()
        for d in Department.objects.all():
            self.assertEqual(posting.fund_balance_from_ledger(d), eng[d.id],
                             f"{d.name} GL balance must equal the fund report")

    def test_capital_does_not_break_reconciliation(self):
        # A: 5000 + 8000 - 1000 recurrent - 3000 capital - 2000 transfer out = 7000
        from decimal import Decimal
        from ledger.services import posting
        self.assertEqual(posting.fund_balance_from_ledger(self.a), Decimal("7000"))
        # B received the 2000 transfer
        self.assertEqual(posting.fund_balance_from_ledger(self.b), Decimal("2000"))

    def test_trust_fund_ties_via_liability(self):
        from decimal import Decimal
        from ledger.services import posting
        # trust: 100 opening + 4000 received = 4100 outstanding
        self.assertEqual(posting.fund_balance_from_ledger(self.trust), Decimal("4100"))

    def test_entity_equation_balances(self):
        from ledger.services import posting
        self.assertTrue(posting.accounting_equation()["balanced"])

    def test_reconciliation_page_reports_all_tie(self):
        from django.urls import reverse
        r = self.client.get(reverse("ledger_reconciliation"))
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.context["all_tie"])
        self.assertTrue(r.context["eq"]["balanced"])


class ChartOfAccountsManagementTests(TestCase):
    def setUp(self):
        from django.contrib.auth.models import User
        from ledger.services import posting
        self.u = User.objects.create_superuser("coa", password="x")
        posting.rebuild()  # build the built-in (system) accounts
        self.client.login(username="coa", password="x")

    def test_add_custom_account(self):
        from django.urls import reverse
        from ledger.models import Account
        self.client.post(reverse("account_create"),
            {"code": "4950", "name": "Special project income", "type": "INCOME", "active": "on"})
        self.assertTrue(Account.objects.filter(code="4950").exists())

    def test_system_account_code_and_type_locked(self):
        from django.urls import reverse
        from ledger.models import Account
        cash = Account.objects.get(system_key="CASH")
        self.client.post(reverse("account_edit", args=[cash.id]),
            {"code": "9999", "name": "Renamed cash", "type": "EXPENSE", "active": "on"})
        cash.refresh_from_db()
        self.assertEqual(cash.code, "1000")      # code unchanged
        self.assertEqual(cash.type, "ASSET")     # type unchanged
        self.assertEqual(cash.name, "Renamed cash")  # name editable

    def test_system_account_cannot_be_deleted(self):
        from django.urls import reverse
        from ledger.models import Account
        cash = Account.objects.get(system_key="CASH")
        self.client.post(reverse("account_delete", args=[cash.id]))
        self.assertTrue(Account.objects.filter(system_key="CASH").exists())

    def test_custom_account_deleted_when_unused(self):
        from django.urls import reverse
        from ledger.models import Account
        a = Account.objects.create(code="4951", name="Temp", type="INCOME")
        self.client.post(reverse("account_delete", args=[a.id]))
        self.assertFalse(Account.objects.filter(code="4951").exists())

    def test_trial_balance_includes_inactive_with_postings(self):
        from ledger.services import posting
        _, tot = posting.trial_balance()
        self.assertEqual(tot["debit"], tot["credit"])  # stays balanced regardless of active flag


class FundVarianceTests(TestCase):
    """The variance drill-down identifies entries causing an engine-vs-ledger
    difference for a fund."""

    def setUp(self):
        from django.contrib.auth.models import User, Group
        self.u = User.objects.create_user("fv", password="x")
        g, _ = Group.objects.get_or_create(name="Treasurer")
        self.u.groups.add(g)

    def test_variance_page_loads(self):
        from django.test import Client
        from departments.models import Department
        d = Department.objects.create(name="Youth", fund_type="LOCAL",
                                      category="MINISTRY")
        c = Client(); c.force_login(self.u)
        r = c.get(f"/ledger/reconciliation/fund/{d.id}/")
        self.assertEqual(r.status_code, 200)

    def test_unposted_transaction_flagged(self):
        from departments.models import Department
        from giving.models import Transaction
        from ledger.services.posting import fund_variance_detail
        import datetime as dt
        from decimal import Decimal
        d = Department.objects.create(name="Camp", fund_type="LOCAL",
                                      category="MINISTRY")
        # a confirmed credit with no ledger posting -> should be flagged
        Transaction.objects.create(date=dt.date(2026, 6, 6), channel="BANK",
            direction="CREDIT", amount=Decimal("1000"), allocation_status="MANUAL",
            confirmed=True, department=d, core_ref="VAR1", payer_name="Test")
        issues = fund_variance_detail(d)
        self.assertTrue(any(i["kind"] == "transaction" and i["ref"] == "VAR1"
                            for i in issues))


class VarianceReallocationTests(TestCase):
    """A transaction re-allocated to a different fund after posting is detected
    on BOTH funds, with amounts that sum to the variance, and a rebuild clears it."""

    def test_reallocation_detected_and_rebuild_fixes(self):
        from departments.models import Department
        from giving.models import Transaction
        from ledger.services import posting
        import datetime as dt
        from decimal import Decimal
        posting.ensure_chart()
        a = Department.objects.create(name="RA", fund_type="LOCAL", category="MINISTRY")
        b = Department.objects.create(name="RB", fund_type="LOCAL", category="MINISTRY")
        t = Transaction.objects.create(date=dt.date(2026, 6, 6), channel="BANK",
            direction="CREDIT", amount=Decimal("3950"), allocation_status="MANUAL",
            confirmed=True, department=a, core_ref="RA1")
        posting.post_transaction(t)
        Transaction.objects.filter(pk=t.pk).update(department=b)
        # old fund: ledger over-credits; new fund: ledger missing it
        ia = posting.fund_variance_detail(a)
        ib = posting.fund_variance_detail(b)
        self.assertTrue(ia and ib)
        self.assertEqual(sum(i["amount"] for i in ia), Decimal("-3950"))
        self.assertEqual(sum(i["amount"] for i in ib), Decimal("3950"))
        # rebuild clears both
        posting.rebuild()
        self.assertEqual(posting.fund_variance_detail(a), [])
        self.assertEqual(posting.fund_variance_detail(b), [])

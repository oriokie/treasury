"""Liability-transactions refactor tests: doc_class derivation (built-in,
custom-category flag, refiling on category change), migration equivalence,
Expense Register separation, the Liability Register (sources, filters,
exports, permissions, leader scoping), and — critically — accounting
invariance: this refactor classifies documents, it must not move a single
figure on the ledger or any statement.
"""
import datetime as dt
from decimal import Decimal

from django.contrib.auth.models import Group, User
from django.test import TestCase
from django.urls import reverse

from cashbook.models import Expense, ExpenseCategory, classify_category
from core.roles import ASSISTANT, AUDITOR, LEADER, TREASURER
from departments.models import Department, DepartmentLeadership
from ledger.models import JournalEntry
from ledger.services import posting
from loans.models import Lender, Loan
from loans.services import loans as loan_svc


def _user(name, role):
    u = User.objects.create_user(name, password="x")
    u.groups.add(Group.objects.get_or_create(name=role)[0])
    return u


class DocClassDerivationTests(TestCase):
    def setUp(self):
        self.tr = _user("dc_tr", TREASURER)
        self.local = Department.objects.create(name="Development", fund_type="LOCAL")
        self.trust = Department.objects.create(name="Tithe", fund_type="TRUST")

    def _exp(self, category, **kw):
        return Expense.objects.create(
            date=dt.date.today(), department=kw.pop("dept", self.local),
            description="t", amount=Decimal("100"), category=category,
            recorded_by=self.tr, **kw)

    def test_builtin_mapping(self):
        self.assertEqual(classify_category("REMITTANCE"), Expense.DocClass.LIABILITY)
        self.assertEqual(classify_category("LOAN_REPAYMENT"), Expense.DocClass.LIABILITY)
        self.assertEqual(classify_category("TRANSPORT"), Expense.DocClass.EXPENSE)
        self.assertEqual(classify_category("LOAN_INTEREST"), Expense.DocClass.EXPENSE)

    def test_save_derives_class_on_every_path(self):
        self.assertEqual(self._exp(Expense.Category.REMITTANCE, dept=self.trust).doc_class,
                         Expense.DocClass.LIABILITY)
        self.assertEqual(self._exp(Expense.Category.TRANSPORT).doc_class,
                         Expense.DocClass.EXPENSE)

    def test_custom_liability_category_no_code_change(self):
        ExpenseCategory.objects.create(code="DEPOSIT_REFUND",
                                       label="Deposit refund", is_liability=True)
        e = self._exp("DEPOSIT_REFUND")
        self.assertEqual(e.doc_class, Expense.DocClass.LIABILITY)

    def test_category_edit_refiles_the_voucher(self):
        e = self._exp(Expense.Category.TRANSPORT)
        e.category = Expense.Category.LOAN_REPAYMENT
        e.save(update_fields=["category"])       # the recategorise-tool path
        e.refresh_from_db()
        self.assertEqual(e.doc_class, Expense.DocClass.LIABILITY)

    def test_loan_service_settlements_are_liability_class(self):
        lender = Lender.objects.create(name="L")
        loan = Loan.objects.create(lender=lender, fund=self.local,
                                   loan_date=dt.date.today())
        loan_svc.record_receipt(loan, date=dt.date.today(),
                                amount=Decimal("10000"), user=self.tr)
        lt = loan_svc.record_repayment(loan, date=dt.date.today(),
                                       amount=Decimal("4000"), user=self.tr)
        self.assertEqual(lt.expense.doc_class, Expense.DocClass.LIABILITY)
        lt2 = loan_svc.record_interest(loan, date=dt.date.today(),
                                       amount=Decimal("100"), user=self.tr)
        self.assertEqual(lt2.expense.doc_class, Expense.DocClass.EXPENSE)


class AccountingInvarianceTests(TestCase):
    """The refactor must not change a single accounting entry."""

    def setUp(self):
        posting.ensure_chart()
        self.tr = _user("ai_tr", TREASURER)
        self.local = Department.objects.create(name="Development", fund_type="LOCAL")
        self.trust = Department.objects.create(name="Tithe", fund_type="TRUST")

    def test_liability_class_does_not_change_posting(self):
        remit = Expense.objects.create(
            date=dt.date.today(), department=self.trust, description="remit",
            amount=Decimal("500"), category=Expense.Category.REMITTANCE,
            status=Expense.Status.PAID, recorded_by=self.tr,
            approved_by=self.tr, paid_date=dt.date.today())
        self.assertEqual(remit.doc_class, Expense.DocClass.LIABILITY)
        je = JournalEntry.objects.get(source_type="expense", source_id=remit.pk)
        keys = {(l.account.system_key, l.debit, l.credit) for l in je.lines.all()}
        self.assertIn(("TRUST_PAYABLE", Decimal("500"), Decimal(0)), keys)
        self.assertIn(("CASH", Decimal(0), Decimal("500")), keys)

    def test_ie_and_fund_balance_semantics_unchanged(self):
        from reports.services.balances import expenses_by_department
        lender = Lender.objects.create(name="AI L")
        loan = Loan.objects.create(lender=lender, fund=self.local,
                                   loan_date=dt.date.today())
        loan_svc.record_receipt(loan, date=dt.date.today(),
                                amount=Decimal("10000"), user=self.tr)
        loan_svc.record_repayment(loan, date=dt.date.today(),
                                  amount=Decimal("4000"), user=self.tr)
        # fund cash-out includes the repayment (full view) …
        self.assertEqual(expenses_by_department()[self.local.id], Decimal("4000"))
        # … the operating view excludes it — now via doc_class
        self.assertEqual(expenses_by_department(include_remittance=False)
                         .get(self.local.id, Decimal(0)), Decimal(0))
        # trial balance still balances
        rows, totals = posting.trial_balance()
        self.assertEqual(totals["debit"], totals["credit"])

    def test_custom_liability_category_excluded_from_operating(self):
        from reports.services.balances import expenses_by_department
        ExpenseCategory.objects.create(code="ADV_SETTLE",
                                       label="Advance settlement", is_liability=True)
        Expense.objects.create(
            date=dt.date.today(), department=self.local, description="settle",
            amount=Decimal("300"), category="ADV_SETTLE",
            status=Expense.Status.PAID, recorded_by=self.tr,
            approved_by=self.tr, paid_date=dt.date.today())
        self.assertEqual(expenses_by_department()[self.local.id], Decimal("300"))
        self.assertEqual(expenses_by_department(include_remittance=False)
                         .get(self.local.id, Decimal(0)), Decimal(0))


class ExpenseRegisterSeparationTests(TestCase):
    def setUp(self):
        posting.ensure_chart()
        self.tr = _user("es_tr", TREASURER)
        self.client.force_login(self.tr)
        self.local = Department.objects.create(name="Development", fund_type="LOCAL")
        self.trust = Department.objects.create(name="Tithe", fund_type="TRUST")
        Expense.objects.create(date=dt.date.today(), department=self.local,
            description="OPERATIONAL TRANSPORT ROW", amount=Decimal("200"),
            category=Expense.Category.TRANSPORT, status=Expense.Status.PAID,
            recorded_by=self.tr, approved_by=self.tr)
        Expense.objects.create(date=dt.date.today(), department=self.trust,
            description="TRUST RELEASE ROW", amount=Decimal("900"),
            category=Expense.Category.REMITTANCE, status=Expense.Status.PAID,
            recorded_by=self.tr, approved_by=self.tr)
        lender = Lender.objects.create(name="ES LENDER")
        loan = Loan.objects.create(lender=lender, fund=self.local,
                                   loan_date=dt.date.today())
        loan_svc.record_receipt(loan, date=dt.date.today(),
                                amount=Decimal("10000"), user=self.tr)
        self.loan = loan
        loan_svc.record_repayment(loan, date=dt.date.today(),
                                  amount=Decimal("4000"), user=self.tr)

    def test_expense_register_shows_only_operational(self):
        r = self.client.get(reverse("expense_list"))
        self.assertContains(r, "OPERATIONAL TRANSPORT ROW")
        self.assertNotContains(r, "TRUST RELEASE ROW")
        self.assertNotContains(r, "Loan repayment")

    def test_expense_export_excludes_liabilities(self):
        r = self.client.get(reverse("expense_list") + "?export=csv")
        body = r.content.decode()
        self.assertIn("OPERATIONAL TRANSPORT ROW", body)
        self.assertNotIn("TRUST RELEASE ROW", body)

    def test_conversion_contra_never_in_expense_register(self):
        loan_svc.convert_to_donation(self.loan, date=dt.date.today(),
                                     amount=Decimal("2000"), user=self.tr)
        r = self.client.get(reverse("expense_list"))
        self.assertNotContains(r, "Loan converted")

    def test_badges_split(self):
        Expense.objects.create(date=dt.date.today(), department=self.trust,
            description="pending remit", amount=Decimal("10"),
            category=Expense.Category.REMITTANCE, status=Expense.Status.PENDING,
            recorded_by=self.tr)
        r = self.client.get(reverse("expense_list"))
        self.assertEqual(r.context["liability_badge"], 1)
        self.assertEqual(r.context["expense_badge"], 0)


class LiabilityRegisterTests(TestCase):
    def setUp(self):
        posting.ensure_chart()
        self.tr = _user("lr_tr2", TREASURER)
        self.auditor = _user("lr_au2", AUDITOR)
        self.leader = _user("lr_ld2", LEADER)
        self.plain = User.objects.create_user("lr_plain", password="x")
        self.local = Department.objects.create(name="Development", fund_type="LOCAL")
        self.youth = Department.objects.create(name="Youth", fund_type="LOCAL")
        self.trust = Department.objects.create(name="Tithe", fund_type="TRUST")
        DepartmentLeadership.objects.create(user=self.leader, department=self.local)
        # trust release
        Expense.objects.create(date=dt.date.today(), department=self.trust,
            description="RELEASE TO FIELD", amount=Decimal("900"),
            category=Expense.Category.REMITTANCE, status=Expense.Status.PAID,
            recorded_by=self.tr, approved_by=self.tr)
        # loan lifecycle on Development
        lender = Lender.objects.create(name="REG LENDER")
        self.loan = Loan.objects.create(lender=lender, fund=self.local,
                                        loan_date=dt.date.today())
        loan_svc.record_receipt(self.loan, date=dt.date.today(),
                                amount=Decimal("10000"), user=self.tr)
        loan_svc.record_repayment(self.loan, date=dt.date.today(),
                                  amount=Decimal("4000"), user=self.tr)
        # trust receipt
        from giving.models import Transaction
        Transaction.objects.create(date=dt.date.today(), channel="BANK",
            direction="CREDIT", amount=Decimal("2500"), department=self.trust,
            allocation_status="AUTO", confirmed=True, core_ref="TRUSTR1",
            payer_name="TITHE PAYER")
        # a loan on a fund the leader does NOT lead
        other = Loan.objects.create(lender=lender, fund=self.youth,
                                    loan_date=dt.date.today())
        loan_svc.record_receipt(other, date=dt.date.today(),
                                amount=Decimal("7777"), user=self.tr)
        self.url = reverse("liability_register") + "?period=all"

    def test_register_contains_all_liability_types(self):
        self.client.force_login(self.tr)
        r = self.client.get(self.url)
        self.assertContains(r, "RELEASE TO FIELD")           # trust release
        self.assertContains(r, "Loan receipt")               # borrowing traceable
        self.assertContains(r, "Loan repayment")             # settlement
        self.assertContains(r, self.loan.number)
        self.assertContains(r, "Outstanding loans")          # dashboard header

    def test_trust_receipts_via_type_filter(self):
        self.client.force_login(self.tr)
        r = self.client.get(self.url + "&type=trust_receipts")
        self.assertContains(r, "TITHE PAYER")
        self.assertContains(r, "Trust fund receipt")
        self.assertNotContains(r, "Loan repayment")

    def test_type_and_fund_filters(self):
        self.client.force_login(self.tr)
        r = self.client.get(self.url + "&type=loan")
        self.assertContains(r, "Loan repayment")
        self.assertNotContains(r, "RELEASE TO FIELD")
        r = self.client.get(self.url + f"&fund={self.trust.id}")
        self.assertContains(r, "RELEASE TO FIELD")
        self.assertNotContains(r, self.loan.number)

    def test_export(self):
        self.client.force_login(self.tr)
        r = self.client.get(self.url + "&export=csv")
        body = r.content.decode()
        self.assertIn("RELEASE TO FIELD", body)
        self.assertIn("Loan repayment", body)
        self.assertIn("Increase", body)                       # receipt direction
        self.assertIn("Settle", body)
        r = self.client.get(self.url + "&export=xlsx")
        self.assertIn("spreadsheetml", r["Content-Type"])

    def test_auditor_can_view_plain_user_cannot(self):
        self.client.force_login(self.auditor)
        self.assertEqual(self.client.get(self.url).status_code, 200)
        self.client.force_login(self.plain)
        self.assertEqual(self.client.get(self.url).status_code, 302)

    def test_leader_scoped_to_own_funds(self):
        self.client.force_login(self.leader)
        r = self.client.get(self.url)
        self.assertEqual(r.status_code, 200)
        self.assertContains(r, self.loan.number)              # their fund
        self.assertNotContains(r, "7777")                     # other fund hidden
        self.assertNotContains(r, "RELEASE TO FIELD")         # trust not theirs

    def test_loan_receipt_still_in_receipts_side(self):
        """Traceability requirement: the borrowing stays a bank credit on the
        transactions ledger while also appearing on the liability register."""
        from giving.models import Transaction
        lt = self.loan.transactions.filter(kind="RECEIPT").first()
        self.assertTrue(Transaction.objects.filter(
            pk=lt.receipt_transaction_id).exists())

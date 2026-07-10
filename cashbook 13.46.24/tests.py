from django.test import TestCase

# Create your tests here.


class ExpenseChargeAndDebitTests(TestCase):
    def setUp(self):
        from django.contrib.auth.models import User
        from departments.models import Department
        self.u = User.objects.create_superuser("ex", password="x")
        self.dept = Department.objects.create(name="Choir", fund_type=Department.FundType.LOCAL,
                                              opening_balance=__import__("decimal").Decimal("5000"))
        self.client.login(username="ex", password="x")

    def test_charge_creates_second_expense(self):
        from django.urls import reverse
        from cashbook.models import Expense
        self.client.post(reverse("expense_create"), {
            "date": "2026-05-22", "department": self.dept.id, "description": "Printing",
            "amount": "1500", "category": "OTHER", "claimant": "Jane",
            "method": "MPESA", "voucher_no": "", "charge": "30"})
        self.assertEqual(Expense.objects.count(), 2)
        self.assertTrue(Expense.objects.filter(category="BANK_CHARGE", amount=30).exists())

    def test_debit_resolved_as_bank_charge(self):
        import datetime as dt
        from decimal import Decimal
        from django.urls import reverse
        from giving.models import Transaction
        from cashbook.models import Expense
        t = Transaction.objects.create(
            date=dt.date(2026, 5, 20), channel=Transaction.Channel.BANK,
            direction=Transaction.Direction.DEBIT, amount=Decimal("50"),
            allocation_status=Transaction.Status.REVIEW, raw_narration="LEDGER FEE")
        self.client.post(reverse("debit_resolve", args=[t.id]),
                         {"kind": "bank_charge", "department": self.dept.id})
        self.assertTrue(Expense.objects.filter(
            category="BANK_CHARGE", bank_transaction=t).exists())
        t.refresh_from_db()
        self.assertEqual(t.allocation_status, Transaction.Status.MANUAL)


class FundTransferTests(TestCase):
    def setUp(self):
        import datetime as dt
        from decimal import Decimal
        from django.contrib.auth.models import User, Group
        from departments.models import Department
        self.u = User.objects.create_superuser("tf", password="x")
        self.A = Department.objects.create(name="General", fund_type=Department.FundType.LOCAL,
                                           opening_balance=Decimal("50000"))
        self.B = Department.objects.create(name="Project", fund_type=Department.FundType.LOCAL)
        self.trust = Department.objects.create(name="Tithe", fund_type=Department.FundType.TRUST)
        self.client.login(username="tf", password="x")

    def _closing(self, dept):
        from reports.services import balances
        return {r["department"].id: r["closing"] for r in balances.department_summary()}[dept.id]

    def test_transfer_moves_balance_and_preserves_total(self):
        from cashbook.models import FundTransfer
        from reports.services import balances
        from decimal import Decimal
        import datetime as dt
        before_total = sum(r["closing"] for r in balances.department_summary())
        FundTransfer.objects.create(date=dt.date(2026, 5, 10), source=self.A,
            destination=self.B, amount=Decimal("12000"), recorded_by=self.u)
        self.assertEqual(self._closing(self.A), Decimal("38000"))   # 50000 - 12000
        self.assertEqual(self._closing(self.B), Decimal("12000"))
        after_total = sum(r["closing"] for r in balances.department_summary())
        self.assertEqual(before_total, after_total)                 # church total unchanged

    def test_transfer_not_counted_as_income(self):
        from cashbook.models import FundTransfer
        from reports.services import balances
        from decimal import Decimal
        import datetime as dt
        FundTransfer.objects.create(date=dt.date(2026, 5, 10), source=self.A,
            destination=self.B, amount=Decimal("12000"), recorded_by=self.u)
        # receipts (income) must be unaffected — a transfer is not giving
        self.assertEqual(balances.receipts_by_department().get(self.B.id, Decimal(0)), Decimal(0))

    def test_trust_fund_cannot_transfer(self):
        from cashbook.models import FundTransfer
        from django.core.exceptions import ValidationError
        from decimal import Decimal
        import datetime as dt
        ft = FundTransfer(date=dt.date(2026, 5, 10), source=self.trust,
            destination=self.B, amount=Decimal("100"), recorded_by=self.u)
        with self.assertRaises(ValidationError):
            ft.clean()

    def test_reversal_restores_balances(self):
        from cashbook.models import FundTransfer
        from decimal import Decimal
        import datetime as dt
        t = FundTransfer.objects.create(date=dt.date(2026, 5, 10), source=self.A,
            destination=self.B, amount=Decimal("9000"), recorded_by=self.u)
        t.reverse(self.u)
        t.refresh_from_db()
        self.assertTrue(t.is_reversed)
        self.assertEqual(self._closing(self.A), Decimal("50000"))   # back to opening
        self.assertEqual(self._closing(self.B), Decimal("0"))
        self.assertTrue(FundTransfer.objects.filter(is_reversal=True).exists())  # mirror kept


class RecurringExpenseTests(TestCase):
    def setUp(self):
        import datetime as dt
        from decimal import Decimal
        from django.contrib.auth.models import User
        from departments.models import Department
        from cashbook.models import RecurringExpense
        self.u = User.objects.create_superuser("re", password="x")
        self.fund = Department.objects.create(name="LCB", fund_type=Department.FundType.LOCAL)
        self.weekly = RecurringExpense.objects.create(
            description="Sabbath stipend", department=self.fund, category="ALLOWANCE",
            amount=Decimal("500"), frequency="SABBATH",
            start_date=dt.date(2026, 5, 1), created_by=self.u)
        self.monthly = RecurringExpense.objects.create(
            description="Caretaker allowance", department=self.fund, category="ALLOWANCE",
            amount=Decimal("6000"), frequency="MONTHLY", day_of_month=1,
            start_date=dt.date(2026, 1, 1), created_by=self.u)
        self.client.login(username="re", password="x")

    def test_sabbath_due_dates_are_saturdays(self):
        import datetime as dt
        from cashbook.services import recurring
        dates = recurring.due_dates(self.weekly, dt.date(2026, 5, 31))
        self.assertEqual(len(dates), 5)               # 5 Saturdays in May 2026
        self.assertTrue(all(d.weekday() == 5 for d in dates))

    def test_monthly_due_dates(self):
        import datetime as dt
        from cashbook.services import recurring
        dates = recurring.due_dates(self.monthly, dt.date(2026, 4, 30))
        self.assertEqual(dates, [dt.date(2026, 1, 1), dt.date(2026, 2, 1),
                                 dt.date(2026, 3, 1), dt.date(2026, 4, 1)])

    def test_generate_creates_linked_expenses(self):
        import datetime as dt
        from decimal import Decimal
        from cashbook.models import Expense
        from cashbook.services import recurring
        n = recurring.generate_schedule(self.monthly, upto=dt.date(2026, 3, 15), user=self.u)
        self.assertEqual(n, 3)                         # Jan, Feb, Mar
        e = Expense.objects.filter(recurring=self.monthly).first()
        self.assertEqual(e.amount, Decimal("6000"))
        self.assertEqual(e.expenditure_type, "RECURRENT")  # operating expenditure
        self.assertEqual(e.recurring, self.monthly)

    def test_generation_is_idempotent(self):
        import datetime as dt
        from cashbook.services import recurring
        recurring.generate_schedule(self.monthly, upto=dt.date(2026, 3, 15), user=self.u)
        again = recurring.generate_schedule(self.monthly, upto=dt.date(2026, 3, 15), user=self.u)
        self.assertEqual(again, 0)                     # nothing created twice

    def test_locked_period_is_skipped(self):
        import datetime as dt
        from cashbook.models import Expense
        from core.models import PeriodLock
        from cashbook.services import recurring
        PeriodLock.objects.create(year=2026, month=2, locked_by=self.u)
        recurring.generate_schedule(self.monthly, upto=dt.date(2026, 3, 15), user=self.u)
        self.assertFalse(Expense.objects.filter(recurring=self.monthly, date=dt.date(2026, 2, 1)).exists())
        self.assertTrue(Expense.objects.filter(recurring=self.monthly, date=dt.date(2026, 1, 1)).exists())

    def test_auditor_cannot_create(self):
        from django.contrib.auth.models import User, Group
        from django.urls import reverse
        aud = User.objects.create_user("aud2", password="x")
        aud.groups.add(Group.objects.get_or_create(name="Auditor")[0])
        c = self.client
        c.logout(); c.login(username="aud2", password="x")
        self.assertEqual(c.get(reverse("recurring_create")).status_code, 302)


class PettyCashTests(TestCase):
    def setUp(self):
        from django.contrib.auth.models import User
        from departments.models import Department
        self.u = User.objects.create_superuser("pc", password="x")
        self.fund = Department.objects.create(name="LCB", fund_type=Department.FundType.LOCAL)
        self.client.login(username="pc", password="x")

    def test_topup_increases_float(self):
        import datetime as dt
        from decimal import Decimal
        from django.urls import reverse
        from cashbook.views import _petty_balance_asof
        self.client.post(reverse("petty_cash_topup"),
            {"date": str(dt.date.today()), "amount": "5000", "note": "float"})
        self.assertEqual(_petty_balance_asof(dt.date.today()), Decimal("5000"))

    def test_disbursement_charges_ministry_and_reduces_float(self):
        import datetime as dt
        from decimal import Decimal
        from django.urls import reverse
        from cashbook.views import _petty_balance_asof
        from cashbook.models import Expense
        from reports.services import balances
        self.client.post(reverse("petty_cash_topup"),
            {"date": str(dt.date.today()), "amount": "5000"})
        before = {r["department"].id: r["closing"]
                  for r in balances.department_summary(None, None, consolidated=False)}
        self.client.post(reverse("petty_cash_disburse"),
            {"date": str(dt.date.today()), "description": "Tea", "amount": "650",
             "category": "REFRESHMENTS", "department": self.fund.id})
        # float drops by the disbursement
        self.assertEqual(_petty_balance_asof(dt.date.today()), Decimal("4350"))
        # the ministry fund is charged the cost (so fund balances stay correct)
        after = {r["department"].id: r["closing"]
                 for r in balances.department_summary(None, None, consolidated=False)}
        self.assertEqual(before[self.fund.id] - after[self.fund.id], Decimal("650"))
        self.assertTrue(Expense.objects.filter(paid_from_petty_cash=True,
                        department=self.fund).exists())

    def test_auditor_cannot_post(self):
        from django.contrib.auth.models import User, Group
        from django.urls import reverse
        aud = User.objects.create_user("pcaud", password="x")
        aud.groups.add(Group.objects.get_or_create(name="Auditor")[0])
        self.client.logout(); self.client.login(username="pcaud", password="x")
        self.assertEqual(self.client.get(reverse("petty_cash")).status_code, 200)
        self.assertEqual(self.client.post(reverse("petty_cash_topup"), {}).status_code, 302)


class AccrualOverlayTests(TestCase):
    def setUp(self):
        from django.contrib.auth.models import User
        from departments.models import Department
        self.u = User.objects.create_superuser("ac", password="x")
        self.fund = Department.objects.create(name="LCB", fund_type=Department.FundType.LOCAL)
        self.client.login(username="ac", password="x")

    def test_payable_create_and_settle(self):
        import datetime as dt
        from decimal import Decimal
        from django.urls import reverse
        from cashbook.models import Payable
        from reports.services import balances
        self.client.post(reverse("payable_create"),
            {"date": str(dt.date.today()), "vendor": "ABC", "description": "Chairs",
             "amount": "15000", "department": self.fund.id, "category": "MATERIALS"})
        p = Payable.objects.get(vendor="ABC")
        self.assertFalse(p.settled)
        before = {r["department"].id: r["closing"]
                  for r in balances.department_summary(None, None, consolidated=False)}
        self.client.post(reverse("payable_settle", args=[p.id]))
        p.refresh_from_db()
        self.assertTrue(p.settled)
        self.assertIsNotNone(p.settled_expense_id)
        after = {r["department"].id: r["closing"]
                 for r in balances.department_summary(None, None, consolidated=False)}
        # settling charges the fund (cash basis recognition at payment)
        self.assertEqual(before[self.fund.id] - after[self.fund.id], Decimal("15000"))

    def test_prepayment_unexpired_balance(self):
        import datetime as dt
        from decimal import Decimal
        from cashbook.models import Prepayment
        from django.contrib.auth.models import User
        pre = Prepayment.objects.create(date=dt.date(2026, 1, 1), description="Insurance",
            amount=Decimal("24000"), department=self.fund, months=12,
            start_date=dt.date(2026, 1, 1), recorded_by=self.u)
        # after 3 months, 9/12 unexpired
        self.assertEqual(pre.unexpired(dt.date(2026, 4, 1)), Decimal("18000.00"))
        self.assertEqual(pre.unexpired(dt.date(2027, 1, 1)), Decimal("0.00"))

    def test_sofp_stays_balanced_with_accruals(self):
        import datetime as dt
        from django.urls import reverse
        from cashbook.models import Payable, Accrual
        Payable.objects.create(date=dt.date.today(), vendor="V", description="d",
            amount=5000, department=self.fund, category="MATERIALS", recorded_by=self.u)
        Accrual.objects.create(date=dt.date.today(), description="util",
            amount=3000, department=self.fund, category="UTILITIES", recorded_by=self.u)
        r = self.client.get(reverse("report_financial_position"))
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.context["balanced"])


class AttachmentTests(TestCase):
    def setUp(self):
        from django.contrib.auth.models import User
        from departments.models import Department
        from cashbook.models import Expense
        import datetime as dt
        self.u = User.objects.create_superuser("at", password="x")
        self.fund = Department.objects.create(name="LCB", fund_type=Department.FundType.LOCAL)
        self.exp = Expense.objects.create(date=dt.date.today(), department=self.fund,
            description="Advance", amount=1000, category="OTHER", status="PAID",
            recorded_by=self.u)
        self.client.login(username="at", password="x")

    def test_expense_receipt_upload(self):
        from django.urls import reverse
        from django.core.files.uploadedfile import SimpleUploadedFile
        f = SimpleUploadedFile("receipt.pdf", b"%PDF-1.4 receipt data",
                               content_type="application/pdf")
        self.client.post(reverse("expense_attachment_upload", args=[self.exp.id]),
                         {"label": "Receipt", "file": f})
        self.assertEqual(self.exp.attachments.count(), 1)

    def test_asset_document_upload(self):
        from django.urls import reverse
        from django.core.files.uploadedfile import SimpleUploadedFile
        from assets.models import FixedAsset
        import datetime as dt
        a = FixedAsset.objects.create(name="Van", category="VEHICLE",
            acquired_on=dt.date(2025, 1, 1), cost=1000000)
        f = SimpleUploadedFile("warranty.txt", b"warranty", content_type="text/plain")
        self.client.post(reverse("asset_attachment_upload", args=[a.id]),
                         {"label": "Warranty", "file": f})
        self.assertEqual(a.attachments.count(), 1)


class DisposalTests(TestCase):
    def setUp(self):
        from django.contrib.auth.models import User
        from departments.models import Department
        self.u = User.objects.create_superuser("dp", password="x")
        self.fund = Department.objects.create(name="LCB", fund_type=Department.FundType.LOCAL)
        self.client.login(username="dp", password="x")

    def test_disposal_gain_and_fund_receipt(self):
        import datetime as dt
        from decimal import Decimal
        from django.urls import reverse
        from assets.models import FixedAsset
        from reports.services import balances
        a = FixedAsset.objects.create(name="Old PA", category="EQUIPMENT",
            acquired_on=dt.date.today(), cost=Decimal("10000"), method="NONE", rate=0)
        nbv = a.net_book_value()  # no depreciation -> 10000
        before = {r["department"].id: r["closing"]
                  for r in balances.department_summary(None, None, consolidated=False)}
        self.client.post(reverse("asset_dispose", args=[a.id]),
            {"disposed_on": str(dt.date.today()), "method": "SOLD",
             "proceeds": str(nbv + 2000), "fund": self.fund.id})
        a.refresh_from_db()
        self.assertTrue(a.disposed)
        self.assertEqual(a.disposal_gain_loss, Decimal("2000"))
        self.assertEqual(a.net_book_value(), Decimal("0"))
        after = {r["department"].id: r["closing"]
                 for r in balances.department_summary(None, None, consolidated=False)}
        self.assertEqual(after[self.fund.id] - before.get(self.fund.id, 0), nbv + 2000)


class ApprovalControlsTests(TestCase):
    def setUp(self):
        from django.contrib.auth.models import User, Group
        from departments.models import Department
        from decimal import Decimal
        from core.models import SiteConfig
        self.treas = User.objects.create_user("t1", password="x")
        self.treas2 = User.objects.create_user("t2", password="x")
        g, _ = Group.objects.get_or_create(name="Treasurer")
        self.treas.groups.add(g); self.treas2.groups.add(g)
        self.assist = User.objects.create_user("a1", password="x")
        self.assist.groups.add(Group.objects.get_or_create(name="Assistant")[0])
        self.fund = Department.objects.create(name="Building", fund_type=Department.FundType.LOCAL,
                                              opening_balance=Decimal("100000"))
        cfg = SiteConfig.get()
        cfg.dual_approval_threshold = Decimal("50000")
        cfg.require_expense_approval = True
        cfg.save()

    def _mk(self, amount, status="PENDING"):
        from cashbook.models import Expense
        import datetime as dt
        return Expense.objects.create(date=dt.date.today(), department=self.fund,
            description="x", amount=amount, category="OTHER", status=status,
            approved_by=(self.treas if status != "PENDING" else None), recorded_by=self.treas)

    def test_high_value_needs_two_approvers_before_pay(self):
        from django.urls import reverse
        e = self._mk(60000, status="APPROVED")  # approved by treas
        self.client.login(username="t1", password="x")
        # same treasurer cannot pay a high-value item without a second approval
        self.client.post(reverse("expense_approve", args=[e.id]), {"action": "pay"})
        e.refresh_from_db(); self.assertEqual(e.status, "APPROVED")  # blocked
        # same treasurer cannot be the second approver
        self.client.post(reverse("expense_approve", args=[e.id]), {"action": "second_approve"})
        e.refresh_from_db(); self.assertIsNone(e.second_approved_by)
        # a different treasurer co-approves, then pay succeeds
        self.client.login(username="t2", password="x")
        self.client.post(reverse("expense_approve", args=[e.id]), {"action": "second_approve"})
        e.refresh_from_db(); self.assertEqual(e.second_approved_by, self.treas2)
        self.client.post(reverse("expense_approve", args=[e.id]), {"action": "pay"})
        e.refresh_from_db(); self.assertEqual(e.status, "PAID")

    def test_low_value_pays_with_single_approval(self):
        from django.urls import reverse
        e = self._mk(1000, status="APPROVED")
        self.client.login(username="t1", password="x")
        self.client.post(reverse("expense_approve", args=[e.id]), {"action": "pay"})
        e.refresh_from_db(); self.assertEqual(e.status, "PAID")

    def test_overspend_blocked_for_assistant(self):
        from django.urls import reverse
        from cashbook.models import Expense
        self.client.login(username="a1", password="x")
        self.client.post(reverse("expense_create"), {
            "date": "2026-05-02", "department": self.fund.id, "description": "Too much",
            "amount": "500000", "category": "OTHER", "method": "CASH", "voucher_no": ""})
        self.assertFalse(Expense.objects.filter(description="Too much").exists())

    def test_overspend_treasurer_override(self):
        from django.urls import reverse
        from cashbook.models import Expense
        self.client.login(username="t1", password="x")
        self.client.post(reverse("expense_create"), {
            "date": "2026-05-02", "department": self.fund.id, "description": "Override me",
            "amount": "500000", "category": "OTHER", "method": "CASH", "voucher_no": "",
            "override_balance": "1"})
        self.assertTrue(Expense.objects.filter(description="Override me").exists())

    def test_petty_float_block(self):
        from django.urls import reverse
        from cashbook.models import Expense
        self.client.login(username="t1", password="x")
        # no top-up yet -> float is zero -> disbursement blocked
        self.client.post(reverse("petty_cash_disburse"), {
            "date": "2026-05-02", "description": "Snacks", "amount": "500",
            "category": "REFRESHMENTS", "department": self.fund.id})
        self.assertFalse(Expense.objects.filter(description="Snacks").exists())


class RecurringApprovalTests(TestCase):
    def test_high_value_recurring_not_auto_approved(self):
        import datetime as dt
        from decimal import Decimal
        from django.contrib.auth.models import User
        from departments.models import Department
        from cashbook.models import RecurringExpense, Expense
        from cashbook.services.recurring import generate_schedule
        from core.models import SiteConfig
        owner = User.objects.create_user("owner", password="x")
        caller = User.objects.create_user("caller", password="x")
        cfg = SiteConfig.get()
        cfg.require_expense_approval = False
        cfg.dual_approval_threshold = Decimal("10000")
        cfg.save()
        fund = Department.objects.create(name="Rent", fund_type=Department.FundType.LOCAL)
        sched = RecurringExpense.objects.create(description="Hall rent", department=fund,
            category="OTHER", amount=Decimal("15000"), frequency="MONTHLY", day_of_month=1,
            start_date=dt.date(2026, 1, 1), created_by=owner)
        generate_schedule(sched, upto=dt.date(2026, 3, 1), user=caller)
        gens = Expense.objects.filter(recurring=sched)
        self.assertTrue(gens.exists())
        # high-value -> must stay pending despite require_expense_approval=False
        self.assertTrue(all(g.status == "PENDING" for g in gens))
        # the caller (an assistant) is never recorded as approver
        self.assertTrue(all(g.approved_by_id is None for g in gens))


class TextReceiptAndBudgetTests(TestCase):
    def setUp(self):
        from django.contrib.auth.models import User
        from departments.models import Department
        from cashbook.models import Expense
        import datetime as dt
        self.u = User.objects.create_superuser("tr", password="x")
        self.fund = Department.objects.create(name="LCB", fund_type="LOCAL")
        self.exp = Expense.objects.create(date=dt.date.today(), department=self.fund,
            description="Advance", amount=1000, category="OTHER", status="PAID",
            recorded_by=self.u)
        self.client.login(username="tr", password="x")

    def test_text_receipt_attachment(self):
        from django.urls import reverse
        self.client.post(reverse("expense_attachment_upload", args=[self.exp.id]),
                         {"text": "QGT5XY Confirmed. Ksh1,000 paid to ACME.", "label": "M-Pesa"})
        a = self.exp.attachments.first()
        self.assertIsNotNone(a)
        self.assertFalse(a.file)
        self.assertIn("Confirmed", a.text)

    def test_link_receipt_attachment(self):
        from django.urls import reverse
        self.client.post(reverse("expense_attachment_upload", args=[self.exp.id]),
                         {"link": "https://receipts.example.com/r/123"})
        self.assertEqual(self.exp.attachments.filter(link__contains="example.com").count(), 1)


class BudgetFromBreakdownTests(TestCase):
    def test_budget_amount_is_sum_of_lines(self):
        from decimal import Decimal
        from departments.models import Department, Budget, BudgetLine
        from reports.services.budget import budget_amount
        d = Department.objects.create(name="Music", fund_type="LOCAL")
        b = Budget.objects.create(year=2026, department=d, amount=Decimal("0"))
        BudgetLine.objects.create(budget=b, name="Instruments", amount=Decimal("3000"))
        BudgetLine.objects.create(budget=b, name="Uniforms", amount=Decimal("2000"))
        self.assertEqual(budget_amount(2026, d), Decimal("5000"))


class StaffAdvanceAndCountTests(TestCase):
    def setUp(self):
        from django.contrib.auth.models import User
        from departments.models import Department
        self.u = User.objects.create_superuser("sa", password="x")
        self.fund = Department.objects.create(name="Missions", fund_type="LOCAL")
        self.client.login(username="sa", password="x")

    def test_advance_balance_surplus(self):
        from decimal import Decimal
        from cashbook.models import StaffAdvance, Expense
        import datetime as dt
        adv = StaffAdvance.objects.create(staff_name="Jane", department=self.fund,
            amount=Decimal("10000"), date_issued=dt.date.today(), purpose="Trip",
            issued_by=self.u)
        Expense.objects.create(date=dt.date.today(), department=self.fund,
            description="Fare", amount=Decimal("7000"), category="TRANSPORT",
            status="PAID", recorded_by=self.u, advance=adv)
        self.assertEqual(adv.settled_total, Decimal("7000"))
        self.assertEqual(adv.balance, Decimal("3000"))   # surplus to recover

    def test_recurring_regenerates_after_unlock(self):
        import datetime as dt
        from decimal import Decimal
        from cashbook.models import RecurringExpense, Expense
        from cashbook.services import recurring
        from core.models import PeriodLock
        s = RecurringExpense.objects.create(description="Stipend", department=self.fund,
            amount=Decimal("100"), frequency="MONTHLY", day_of_month=1,
            start_date=dt.date(2026, 1, 1), end_date=dt.date(2026, 3, 31),
            created_by=self.u, active=True)
        PeriodLock.objects.create(year=2026, month=2, locked_by=self.u)
        recurring.generate_schedule(s, upto=dt.date(2026, 3, 15), user=self.u)
        self.assertEqual(Expense.objects.filter(recurring=s).count(), 2)  # Jan + Mar
        PeriodLock.objects.filter(year=2026, month=2).delete()
        recurring.generate_schedule(s, upto=dt.date(2026, 3, 15), user=self.u)
        self.assertEqual(Expense.objects.filter(recurring=s).count(), 3)  # + Feb


class CountSessionTests(TestCase):
    def test_count_discrepancy(self):
        from decimal import Decimal
        from django.contrib.auth.models import User
        from envelopes.models import CountSession
        u = User.objects.create_superuser("cs", password="x")
        cs = CountSession.objects.create(date="2026-05-02", counted_total=Decimal("4500"),
            expected_total=Decimal("4000"), recorded_by=u)
        self.assertEqual(cs.discrepancy, Decimal("500"))
        self.assertTrue(cs.has_discrepancy)


class ManualJournalTests(TestCase):
    def test_manual_entry_survives_rebuild(self):
        from django.contrib.auth.models import User, Group
        from core.roles import TREASURER
        from django.urls import reverse
        from ledger.models import Account, JournalEntry
        from ledger.services import posting
        posting.seed_accounts() if hasattr(posting, "seed_accounts") else None
        u = User.objects.create_user("mj", password="x")
        u.groups.add(Group.objects.get_or_create(name=TREASURER)[0])
        self.client.login(username="mj", password="x")
        accts = list(Account.objects.all()[:2])
        if len(accts) < 2:
            self.skipTest("chart not seeded")
        self.client.post(reverse("manual_journal"), {
            "date": "2026-05-10", "memo": "Adjustment",
            "account": [str(accts[0].id), str(accts[1].id)], "dept": ["", ""],
            "debit": ["100", ""], "credit": ["", "100"]})
        self.assertEqual(JournalEntry.objects.filter(source_type="manual").count(), 1)
        posting.rebuild()
        self.assertEqual(JournalEntry.objects.filter(source_type="manual").count(), 1)


class CategoryAndDevGroupTests(TestCase):
    def test_custom_category_resolution(self):
        from cashbook.models import ExpenseCategory, category_label, category_choices
        ExpenseCategory.objects.create(code="MUSIC", label="Music ministry")
        self.assertEqual(category_label("MUSIC"), "Music ministry")
        self.assertEqual(category_label("TRANSPORT"), "Transport")  # built-in still works
        self.assertIn(("MUSIC", "Music ministry"), category_choices())

    def test_dev_group_member_report(self):
        import datetime as dt
        from decimal import Decimal
        from departments.models import Department, DevelopmentGroup
        from giving.models import Transaction
        from members.models import Member
        from reports.services.balances import dev_group_members
        g = DevelopmentGroup.objects.create(number=1, name="Group 1")
        m = Member.objects.create(name="Ruth M")
        Transaction.objects.create(date=dt.date(2026, 5, 2), channel="BANK", direction="CREDIT",
            amount=Decimal("500"), dev_group=g, member=m, allocation_status="AUTO", confirmed=True)
        Transaction.objects.create(date=dt.date(2026, 5, 9), channel="BANK", direction="CREDIT",
            amount=Decimal("700"), dev_group=g, member=m, allocation_status="AUTO", confirmed=True)
        data = dev_group_members(g)
        self.assertEqual(data["total"], Decimal("1200"))
        self.assertEqual(data["rows"][0]["count"], 2)


class EmailConfigTests(TestCase):
    def test_email_not_configured_returns_clear_error(self):
        from core.services.email import send_email, is_configured
        from core.models import SiteConfig
        cfg = SiteConfig.get(); cfg.email_enabled = False; cfg.save()
        self.assertFalse(is_configured(cfg))
        ok, detail = send_email("Hi", "Body", "x@example.com", cfg)
        self.assertFalse(ok)
        self.assertIn("not enabled", detail.lower())


class ExpenseEditGuardTests(TestCase):
    def setUp(self):
        import datetime as dt
        from decimal import Decimal
        from django.contrib.auth.models import User, Group
        from core.roles import TREASURER, ASSISTANT
        from departments.models import Department
        from cashbook.models import Expense
        self.treasurer = User.objects.create_user("t", password="x")
        self.treasurer.groups.add(Group.objects.get_or_create(name=TREASURER)[0])
        self.assistant = User.objects.create_user("a", password="x")
        self.assistant.groups.add(Group.objects.get_or_create(name=ASSISTANT)[0])
        self.fund = Department.objects.create(name="Building", fund_type="LOCAL")
        self.fund2 = Department.objects.create(name="Youth", fund_type="LOCAL")
        self.exp = Expense.objects.create(date=dt.date(2026, 5, 10), department=self.fund,
            description="Approved spend", amount=Decimal("1000"), category="OTHER",
            status="APPROVED", recorded_by=self.assistant, approved_by=self.treasurer)

    def _post_edit(self, **over):
        from django.urls import reverse
        data = {"date": "2026-05-10", "department": self.fund.id,
                "description": "Approved spend", "amount": "1000", "category": "OTHER",
                "method": "CASH"}
        data.update(over)
        return self.client.post(reverse("expense_edit", args=[self.exp.id]), data)

    def test_assistant_cannot_edit_approved(self):
        self.client.login(username="a", password="x")
        self._post_edit(amount="50000")
        self.exp.refresh_from_db()
        self.assertEqual(self.exp.amount, 1000)            # unchanged
        self.assertEqual(self.exp.status, "APPROVED")

    def test_amount_change_resets_to_pending(self):
        self.client.login(username="t", password="x")
        self._post_edit(amount="50000")
        self.exp.refresh_from_db()
        self.assertEqual(self.exp.amount, 50000)
        self.assertEqual(self.exp.status, "PENDING")        # re-approval required
        self.assertIsNone(self.exp.approved_by_id)

    def test_fund_change_resets_to_pending(self):
        self.client.login(username="t", password="x")
        self._post_edit(department=self.fund2.id)
        self.exp.refresh_from_db()
        self.assertEqual(self.exp.department_id, self.fund2.id)
        self.assertEqual(self.exp.status, "PENDING")

    def test_description_only_change_keeps_approved(self):
        self.client.login(username="t", password="x")
        self._post_edit(description="Reworded")
        self.exp.refresh_from_db()
        self.assertEqual(self.exp.status, "APPROVED")       # no material change

    def test_edit_blocked_when_period_locked(self):
        from core.models import PeriodLock
        PeriodLock.objects.create(year=2026, month=5, locked_by=self.treasurer)
        self.client.login(username="t", password="x")
        self._post_edit(amount="2000")
        self.exp.refresh_from_db()
        self.assertEqual(self.exp.amount, 1000)             # locked — unchanged


class RemittanceNumberingTests(TestCase):
    def test_create_batch_sequences_uniquely(self):
        from django.contrib.auth.models import User
        from cashbook.models import RemittanceBatch
        u = User.objects.create_superuser("rb", password="x")
        b1 = RemittanceBatch.create_batch(created_by=u, status="DRAFT")
        b2 = RemittanceBatch.create_batch(created_by=u, status="DRAFT")
        self.assertNotEqual(b1.batch_number, b2.batch_number)
        self.assertTrue(b2.batch_number.endswith("0002"))

    def test_create_batch_survives_existing_collision(self):
        # if a batch already holds the "next" number, create_batch must not crash
        import datetime as dt
        from django.contrib.auth.models import User
        from cashbook.models import RemittanceBatch
        u = User.objects.create_superuser("rb2", password="x")
        prefix = f"RB-{dt.date.today().year}-"
        RemittanceBatch.objects.create(batch_number=f"{prefix}0001",
                                       created_by=u, status="DRAFT")
        b = RemittanceBatch.create_batch(created_by=u, status="DRAFT")
        self.assertEqual(b.batch_number, f"{prefix}0002")


class AttachmentValidationTests(TestCase):
    def setUp(self):
        import datetime as dt
        from django.contrib.auth.models import User, Group
        from core.roles import ASSISTANT
        from departments.models import Department
        from cashbook.models import Expense
        u = User.objects.create_user("at", password="x")
        u.groups.add(Group.objects.get_or_create(name=ASSISTANT)[0])
        self.fund = Department.objects.create(name="F", fund_type="LOCAL")
        self.exp = Expense.objects.create(date=dt.date.today(), department=self.fund,
            description="x", amount=100, category="OTHER", status="PENDING", recorded_by=u)
        self.client.login(username="at", password="x")

    def test_rejects_executable(self):
        from django.urls import reverse
        from django.core.files.uploadedfile import SimpleUploadedFile
        from cashbook.models import ExpenseAttachment
        bad = SimpleUploadedFile("hack.exe", b"MZ...", content_type="application/octet-stream")
        self.client.post(reverse("expense_attachment_upload", args=[self.exp.id]), {"file": bad})
        self.assertEqual(ExpenseAttachment.objects.count(), 0)

    def test_accepts_pdf(self):
        from django.urls import reverse
        from django.core.files.uploadedfile import SimpleUploadedFile
        from cashbook.models import ExpenseAttachment
        ok = SimpleUploadedFile("receipt.pdf", b"%PDF-1.4 ...", content_type="application/pdf")
        self.client.post(reverse("expense_attachment_upload", args=[self.exp.id]), {"file": ok})
        self.assertEqual(ExpenseAttachment.objects.count(), 1)


class TrustDueDateAlertTests(TestCase):
    def test_overdue_alert_uses_due_day(self):
        import datetime as dt
        from decimal import Decimal
        from django.contrib.auth.models import User
        from departments.models import Department
        from giving.models import Transaction
        from core.models import SiteConfig
        from core.services.health import anomalies
        cfg = SiteConfig.get(); cfg.trust_remit_due_day = 1; cfg.save()  # force "past due"
        Department.objects.create(name="Tithe", fund_type="TRUST")
        tr = Department.objects.get(name="Tithe")
        Transaction.objects.create(date=dt.date(dt.date.today().year, 1, 4),
            channel="ENVELOPE", direction="CREDIT", amount=Decimal("5000"), department=tr,
            allocation_status="AUTO", confirmed=True)
        titles = " ".join(a["title"] for a in anomalies())
        # only assert if we're past day 1 of the month (always true except the 1st)
        if dt.date.today().day > 1:
            self.assertIn("remittance overdue", titles.lower())


class ExpenseImportTests(TestCase):
    """Item 5: bulk import of expenses from a spreadsheet."""

    def setUp(self):
        from django.contrib.auth.models import User, Group
        from core.models import SiteConfig
        u = User.objects.create_user("ei", password="x")
        g, _ = Group.objects.get_or_create(name="Treasurer")
        u.groups.add(g)
        self.client.login(username="ei", password="x")
        from departments.models import Department
        self.fund = Department.objects.create(name="EI Fund", fund_type="LOCAL",
                                              selectable=True, category="MINISTRY")
        cfg = SiteConfig.get(); cfg.require_expense_approval = False; cfg.save()

    def _file(self, rows):
        import io, openpyxl
        from django.core.files.uploadedfile import SimpleUploadedFile
        wb = openpyxl.Workbook(); ws = wb.active; ws.title = "Expenses"
        ws.append(["Date", "Fund", "Description", "Amount", "Category", "Method",
                   "Claimant", "Voucher no"])
        for r in rows:
            ws.append(r)
        buf = io.BytesIO(); wb.save(buf)
        return SimpleUploadedFile("e.xlsx", buf.getvalue(),
            content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")

    def test_template_downloads(self):
        r = self.client.get("/expenses/import/?download=1")
        self.assertEqual(r.status_code, 200)
        self.assertIn("spreadsheet", r["Content-Type"])

    def test_import_creates_expenses(self):
        from cashbook.models import Expense
        f = self._file([
            ["2026-06-06", self.fund.name, "Mic", 4500, "Materials", "Cash", "J M", "V1"],
            ["2026-06-07", "NoSuchFund", "Orphan", 999, "Other", "Cash", "", ""],
        ])
        self.client.post("/expenses/import/", {"file": f})
        self.client.post("/expenses/import/", {"apply": "1"})
        e = Expense.objects.filter(description="Mic").first()
        self.assertIsNotNone(e)
        self.assertEqual(e.category, "MATERIALS")
        self.assertEqual(e.method, "CASH")
        self.assertEqual(e.department_id, self.fund.id)
        # auto-approve since approval not required
        self.assertEqual(e.status, Expense.Status.APPROVED)
        # orphan fund skipped
        self.assertFalse(Expense.objects.filter(description="Orphan").exists())

    def test_pending_when_approval_required(self):
        from cashbook.models import Expense
        from core.models import SiteConfig
        cfg = SiteConfig.get(); cfg.require_expense_approval = True; cfg.save()
        f = self._file([["2026-06-06", self.fund.name, "Pend", 100, "Other", "Cash", "", ""]])
        self.client.post("/expenses/import/", {"file": f})
        self.client.post("/expenses/import/", {"apply": "1"})
        e = Expense.objects.filter(description="Pend").first()
        self.assertEqual(e.status, Expense.Status.PENDING)

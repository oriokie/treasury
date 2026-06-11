from django.test import TestCase

# Create your tests here.
import datetime as dt
from decimal import Decimal
from django.test import TestCase
from departments.models import Department
from giving.models import Transaction
from reports.services import monthly


class MonthlyReportTests(TestCase):
    def setUp(self):
        self.year = dt.date.today().year
        tithe = Department.objects.create(name="Tithe", fund_type=Department.FundType.TRUST)
        lcb = Department.objects.create(name="LCB", fund_type=Department.FundType.LOCAL)
        for dep, amt, mth in [(tithe, "1000", 1), (tithe, "500", 2), (lcb, "300", 1)]:
            Transaction.objects.create(
                date=dt.date(self.year, mth, 10), channel=Transaction.Channel.CASH,
                direction=Transaction.Direction.CREDIT, amount=Decimal(amt),
                department=dep, allocation_status="AUTO")

    def test_collections_summary_totals(self):
        d = monthly.collections_summary(self.year)
        self.assertEqual(d["tot_collections"], Decimal("1800"))
        self.assertEqual(d["tot_trust"], Decimal("1500"))
        self.assertEqual(d["tot_local"], Decimal("300"))

    def test_trust_monthly_only_trust(self):
        d = monthly.trust_monthly(self.year)
        names = [r["dept"].name for r in d["rows"]]
        self.assertIn("Tithe", names)
        self.assertNotIn("LCB", names)
        self.assertEqual(d["grand"], Decimal("1500"))


class RemitAndConsolidationTests(TestCase):
    def setUp(self):
        import datetime as dt
        from decimal import Decimal
        from django.contrib.auth.models import User
        from departments.models import Department
        from giving.models import Transaction
        self.u = User.objects.create_superuser("rm", password="x")
        self.trust = Department.objects.create(name="Tithe", fund_type=Department.FundType.TRUST)
        parent = Department.objects.create(name="LCB", fund_type=Department.FundType.LOCAL)
        self.sub = Department.objects.create(name="SS", parent=parent,
                                             fund_type=Department.FundType.LOCAL)
        Transaction.objects.create(date=dt.date(2026, 5, 10), channel="CASH",
            direction="CREDIT", amount=Decimal("5000"), department=self.trust,
            allocation_status="AUTO")
        Transaction.objects.create(date=dt.date(2026, 5, 10), channel="CASH",
            direction="CREDIT", amount=Decimal("800"), department=self.sub,
            allocation_status="AUTO")
        self.client.login(username="rm", password="x")

    def test_remit_creates_remittance_expense(self):
        from django.urls import reverse
        from cashbook.models import Expense
        self.client.post(reverse("remit_trust"),
                         {"start": "2026-05-01", "end": "2026-05-31"})
        self.assertTrue(Expense.objects.filter(
            category="REMITTANCE", department=self.trust, amount=5000).exists())

    def test_lcb_consolidated_in_summary(self):
        from reports.services import balances
        import datetime as dt
        rows = balances.department_summary(dt.date(2026, 5, 1), dt.date(2026, 5, 31))
        lcb = [r for r in rows if r["department"].name == "LCB"][0]
        self.assertEqual(lcb["receipts"], 800)        # rolled up from sub-account
        self.assertEqual(len(lcb["children"]), 1)


class RemittanceBatchTests(TestCase):
    def setUp(self):
        import datetime as dt
        from decimal import Decimal
        from django.contrib.auth.models import User
        from departments.models import Department
        from giving.models import Transaction
        self.u = User.objects.create_superuser("rb", password="x")
        self.trust = Department.objects.create(name="Tithe", fund_type=Department.FundType.TRUST)
        Transaction.objects.create(date=dt.date(2026, 5, 1), channel="BANK", direction="CREDIT",
            amount=Decimal("1000"), department=self.trust, allocation_status="AUTO")
        from ledger.services import posting
        posting.rebuild()  # build the chart + post the ledger so remittances post too
        self.client.login(username="rb", password="x")

    def test_remittance_clears_outstanding_and_posts_to_ledger(self):
        from django.urls import reverse
        from reports.services import balances
        from cashbook.models import RemittanceBatch, Expense
        from ledger.services import posting
        from ledger.models import JournalEntry
        out0 = sum((r["to_remit"] for r in balances.trust_summary()))
        self.assertEqual(out0, 1000)
        self.client.post(reverse("remittance_batch_create"), {"all": "1"})
        b = RemittanceBatch.objects.latest("id")
        # draft (PENDING): nothing committed yet, still outstanding
        self.assertEqual(sum((r["to_remit"] for r in balances.trust_summary())), 1000)
        # approving commits the remittance — outstanding clears, consistent with the
        # rest of the system (committed expenses reduce fund balances everywhere)
        self.client.post(reverse("remittance_batch_approve", args=[b.id]))
        self.assertEqual(sum((r["to_remit"] for r in balances.trust_summary())), 0)
        # remit records the cheque and keeps it cleared
        self.client.post(reverse("remittance_batch_remit", args=[b.id]), {"cheque_no": "C1"})
        self.assertEqual(sum((r["to_remit"] for r in balances.trust_summary())), 0)
        # the remittance must be posted to the general ledger (the .update() bug)
        pe = b.expenses.first()
        self.assertTrue(JournalEntry.objects.filter(
            source_type="expense", source_id=pe.id).exists())
        self.assertTrue(posting.accounting_equation()["balanced"])


class TrustRestrictionTests(TestCase):
    def test_trust_excluded_from_expense_picker(self):
        from departments.models import Department, expense_departments
        Department.objects.create(name="Camp Meeting", fund_type=Department.FundType.TRUST)
        Department.objects.create(name="Choir", fund_type=Department.FundType.LOCAL)
        names = [d.name for d in expense_departments()]
        self.assertIn("Choir", names)
        self.assertNotIn("Camp Meeting", names)


class BudgetVarianceTests(TestCase):
    def setUp(self):
        import datetime as dt
        from decimal import Decimal
        from django.contrib.auth.models import User
        from departments.models import Department, Budget
        from cashbook.models import Expense
        self.u = User.objects.create_superuser("bg", password="x")
        self.dept = Department.objects.create(name="Youth", fund_type=Department.FundType.LOCAL)
        Budget.objects.create(year=2026, department=self.dept, amount=Decimal("120000"))
        Expense.objects.create(date=dt.date(2026, 5, 10), department=self.dept,
            description="camp", amount=Decimal("5000"), category="OTHER",
            status=Expense.Status.PAID, recorded_by=self.u)

    def test_annual_variance(self):
        from reports.services import budget as bs
        d = bs.budget_vs_actual(2026, "ANNUAL")
        row = [r for r in d["rows"] if r["department"].id == self.dept.id][0]
        self.assertEqual(row["budget"], 120000)
        self.assertEqual(row["actual"], 5000)
        self.assertEqual(row["variance"], 115000)

    def test_quarter_proration(self):
        from reports.services import budget as bs
        d = bs.budget_vs_actual(2026, "QUARTER", quarter=2)
        row = [r for r in d["rows"] if r["department"].id == self.dept.id][0]
        self.assertEqual(row["budget"], 30000)   # 120000 / 4
        self.assertEqual(row["actual"], 5000)    # May is in Q2

    def test_budget_amount_falls_back_to_legacy(self):
        from reports.services import budget as bs
        from departments.models import Department
        from decimal import Decimal
        d = Department.objects.create(name="Choir", fund_type=Department.FundType.LOCAL,
                                      annual_budget=Decimal("9000"))
        self.assertEqual(bs.budget_amount(2030, d), Decimal("9000"))  # no Budget row -> legacy


class ReportingSuiteTests(TestCase):
    def setUp(self):
        import datetime as dt
        from decimal import Decimal
        from django.contrib.auth.models import User
        from departments.models import Department
        from giving.models import Transaction
        from cashbook.models import Expense
        self.u = User.objects.create_superuser("rs", password="x")
        d = Department.objects.create(name="Youth", fund_type=Department.FundType.LOCAL)
        Transaction.objects.create(date=dt.date(2026, 5, 30), channel="CASH", direction="CREDIT",
            amount=Decimal("500"), department=d, allocation_status="MANUAL")
        Expense.objects.create(date=dt.date(2026, 5, 30), department=d, description="x",
            amount=Decimal("100"), category="OTHER", status=Expense.Status.PAID, recorded_by=self.u)
        self.client.login(username="rs", password="x")

    def test_pages_render(self):
        from django.urls import reverse
        for name, q in [("report_daily", "?date=2026-05-30"),
                        ("report_weekly", "?date=2026-05-30"),
                        ("report_cash_flow", "?year=2026"),
                        ("report_board", "?year=2026&month=5"),
                        ("report_pastor", "?year=2026&month=5"),
                        ("report_conference", "?year=2026&month=5"),
                        ("report_index", "")]:
            self.assertEqual(self.client.get(reverse(name) + q).status_code, 200, name)

    def test_excel_and_csv_exports(self):
        from django.urls import reverse
        xls = self.client.get(reverse("report_daily") + "?date=2026-05-30&export=xlsx")
        self.assertEqual(xls.status_code, 200)
        self.assertIn("spreadsheetml", xls["Content-Type"])
        csvr = self.client.get(reverse("report_cash_flow") + "?year=2026&export=csv")
        self.assertEqual(csvr["Content-Type"], "text/csv")


class RecurrentCapitalTests(TestCase):
    def setUp(self):
        import datetime as dt
        from decimal import Decimal
        from django.contrib.auth.models import User
        from departments.models import Department
        from giving.models import Transaction
        from cashbook.models import Expense
        from assets.models import FixedAsset
        self.u = User.objects.create_superuser("rc2", password="x")
        self.fund = Department.objects.create(name="Building", fund_type=Department.FundType.LOCAL)
        Transaction.objects.create(date=dt.date(2026, 5, 5), channel="CASH", direction="CREDIT",
            amount=Decimal("100000"), department=self.fund, allocation_status="MANUAL")
        # recurrent 20,000; capital 70,000
        Expense.objects.create(date=dt.date(2026, 5, 10), department=self.fund,
            description="power", amount=Decimal("20000"), category="UTILITIES",
            expenditure_type="RECURRENT", status=Expense.Status.PAID, recorded_by=self.u)
        self.asset = FixedAsset.objects.create(name="Wing", category="BUILDING",
            acquired_on=dt.date(2026, 5, 1), cost=Decimal("70000"), method="NONE", rate=0)
        Expense.objects.create(date=dt.date(2026, 5, 12), department=self.fund,
            description="construction", amount=Decimal("70000"), category="CONSTRUCTION",
            expenditure_type="CAPITAL", capitalized_asset=self.asset,
            status=Expense.Status.PAID, recorded_by=self.u)
        self.client.login(username="rc2", password="x")

    def test_default_is_recurrent(self):
        from cashbook.models import Expense
        e = Expense.objects.create(date=__import__("datetime").date(2026, 5, 1),
            department=self.fund, description="x", amount=1,
            recorded_by=self.u)
        self.assertEqual(e.expenditure_type, "RECURRENT")

    def test_capital_links_to_asset(self):
        from cashbook.models import Expense
        cap = Expense.objects.get(category="CONSTRUCTION")
        self.assertEqual(cap.capitalized_asset, self.asset)
        self.assertIn(cap, self.asset.source_expenses.all())

    def test_income_statement_splits_recurrent_and_capital(self):
        from django.urls import reverse
        from decimal import Decimal
        r = self.client.get(reverse("report_income_statement") + "?year=2026&month=5")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.context["total_income"], Decimal("100000"))
        self.assertEqual(r.context["total_recurrent"], Decimal("20000"))
        self.assertEqual(r.context["total_capital"], Decimal("70000"))
        # operating = income - recurrent; net = operating - capital
        self.assertEqual(r.context["operating"], Decimal("80000"))
        self.assertEqual(r.context["surplus"], Decimal("10000"))


class NewStatementsTests(TestCase):
    def setUp(self):
        import datetime as dt
        from decimal import Decimal
        from django.contrib.auth.models import User
        from departments.models import Department
        from giving.models import Transaction
        from cashbook.models import Expense
        from ledger.services import posting
        self.u = User.objects.create_superuser("ns", password="x")
        self.local = Department.objects.create(name="LCB", fund_type=Department.FundType.LOCAL)
        self.dev = Department.objects.create(name="Building", fund_type=Department.FundType.LOCAL,
                                             category=Department.Category.DEVELOPMENT)
        self.trust = Department.objects.create(name="Tithe", fund_type=Department.FundType.TRUST)
        Transaction.objects.create(date=dt.date(2026, 5, 2), channel="CASH", direction="CREDIT",
            amount=Decimal("10000"), department=self.local, allocation_status="AUTO")
        Transaction.objects.create(date=dt.date(2026, 5, 2), channel="BANK", direction="CREDIT",
            amount=Decimal("5000"), department=self.trust, allocation_status="AUTO")
        Expense.objects.create(date=dt.date(2026, 5, 10), department=self.local,
            description="Power", amount=Decimal("2000"), category="UTILITIES",
            expenditure_type="RECURRENT", status="PAID", paid_date=dt.date(2026, 5, 10),
            recorded_by=self.u, approved_by=self.u)
        posting.rebuild()
        self.client.login(username="ns", password="x")

    def _p(self, name):
        from django.urls import reverse
        return reverse(name) + "?year=2026&month=5"

    def test_cash_flows_reconciles(self):
        r = self.client.get(self._p("report_cash_flows"))
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.context["ties"])
        # net change = receipts (15000) - operating (2000) = 13000
        self.assertEqual(r.context["net_change"], __import__("decimal").Decimal("13000"))

    def test_changes_in_net_assets_ties_to_position(self):
        from decimal import Decimal
        r = self.client.get(self._p("report_changes_net_assets"))
        self.assertEqual(r.status_code, 200)
        # closing total = local closing (10000-2000) + nbv (0) = 8000
        self.assertEqual(r.context["t_close"], Decimal("8000"))
        # column sum equals total
        self.assertEqual(r.context["un"]["closing"] + r.context["al"]["closing"]
                         + r.context["prop"]["closing"], r.context["t_close"])


class IssueFixesTests(TestCase):
    def setUp(self):
        import datetime as dt
        from decimal import Decimal
        from django.contrib.auth.models import User, Group
        from departments.models import Department
        from giving.models import Transaction
        from cashbook.models import Expense
        self.u = User.objects.create_superuser("if", password="x")
        self.tr = Department.objects.create(name="Tithe", fund_type=Department.FundType.TRUST)
        self.loc = Department.objects.create(name="Youth", fund_type=Department.FundType.LOCAL)
        Transaction.objects.create(date=dt.date(2026, 5, 2), channel="BANK", direction="CREDIT",
            amount=Decimal("10000"), department=self.tr, allocation_status="AUTO", confirmed=True)
        Transaction.objects.create(date=dt.date(2026, 5, 2), channel="CASH", direction="CREDIT",
            amount=Decimal("4000"), department=self.loc, allocation_status="MANUAL", confirmed=True)
        # remit part of the tithe
        Expense.objects.create(date=dt.date(2026, 5, 5), department=self.tr,
            description="Remit tithe", amount=Decimal("6000"), category="REMITTANCE",
            status="PAID", recorded_by=self.u, approved_by=self.u)
        self.client.login(username="if", password="x")

    def test_trust_to_remit_is_net(self):
        from reports.services import balances
        rows = {r["department"].id: r for r in balances.trust_summary(None, None)}
        r = rows[self.tr.id]
        self.assertEqual(r["collected"], 10000)
        self.assertEqual(r["remitted"], 6000)
        self.assertEqual(r["to_remit"], 4000)

    def test_income_expenditure_excludes_trust_and_remittance(self):
        import datetime as dt
        from django.urls import reverse
        url = reverse("report_ie") + "?period=ANNUAL&year=2026"
        r = self.client.get(url)
        self.assertEqual(r.status_code, 200)
        # local income only (4000), remittance excluded from expenditure
        self.assertEqual(r.context["income"], 4000)
        self.assertEqual(r.context["trust_collected"], 10000)
        self.assertEqual(r.context["remittances"], 6000)

    def test_offering_summary_spans_multiple_months(self):
        import datetime as dt
        from decimal import Decimal
        from giving.models import Transaction
        from reports.services import balances
        Transaction.objects.create(date=dt.date(2026, 1, 3), channel="CASH", direction="CREDIT",
            amount=Decimal("100"), department=self.loc, allocation_status="MANUAL", confirmed=True)
        Transaction.objects.create(date=dt.date(2026, 2, 7), channel="CASH", direction="CREDIT",
            amount=Decimal("200"), department=self.loc, allocation_status="MANUAL", confirmed=True)
        data = balances.offering_summary(dt.date(2026, 1, 1), dt.date(2026, 2, 28))
        # two different Sabbaths -> two columns (not merged into "week 1")
        self.assertGreaterEqual(len(data["sabbaths"]), 2)


class SecondApproveSequenceTests(TestCase):
    def setUp(self):
        import datetime as dt
        from django.contrib.auth.models import User, Group
        from core.roles import TREASURER
        from departments.models import Department
        from cashbook.models import Expense
        from core.models import SiteConfig
        g = Group.objects.get_or_create(name=TREASURER)[0]
        self.t1 = User.objects.create_user("t1", password="x"); self.t1.groups.add(g)
        self.t2 = User.objects.create_user("t2", password="x"); self.t2.groups.add(g)
        self.fund = Department.objects.create(name="Building", fund_type="LOCAL")
        cfg = SiteConfig.get(); cfg.dual_approval_threshold = 1000; cfg.save()
        self.exp = Expense.objects.create(date=dt.date.today(), department=self.fund,
            description="Big spend", amount=5000, category="OTHER", status="PENDING",
            recorded_by=self.t1)

    def test_cannot_second_approve_before_first(self):
        from django.urls import reverse
        self.client.login(username="t2", password="x")
        self.client.post(reverse("expense_approve", args=[self.exp.id]),
                         {"action": "second_approve"})
        self.exp.refresh_from_db()
        self.assertIsNone(self.exp.second_approved_by_id)

    def test_second_approve_after_first(self):
        from django.urls import reverse
        self.client.login(username="t1", password="x")
        self.client.post(reverse("expense_approve", args=[self.exp.id]), {"action": "approve"})
        self.client.login(username="t2", password="x")
        self.client.post(reverse("expense_approve", args=[self.exp.id]),
                         {"action": "second_approve"})
        self.exp.refresh_from_db()
        self.assertEqual(self.exp.second_approved_by_id, self.t2.id)


class AdvanceFinancialStatementTests(TestCase):
    def setUp(self):
        import datetime as dt
        from decimal import Decimal
        from django.contrib.auth.models import User
        from departments.models import Department
        from cashbook.models import StaffAdvance, Expense
        self.u = User.objects.create_superuser("afs", password="x")
        self.fund = Department.objects.create(name="Missions", fund_type="LOCAL",
                                              opening_balance=Decimal("100000"))
        self.adv = StaffAdvance.objects.create(staff_name="Pr X", department=self.fund,
            amount=Decimal("15000"), date_issued=dt.date.today(), purpose="Trip",
            issued_by=self.u)

    def test_issuing_advance_is_not_expenditure(self):
        # Income & Expenditure must not treat the advance as expense
        import datetime as dt
        from django.urls import reverse
        self.client.login(username="afs", password="x")
        url = reverse("report_ie") + "?period=ANNUAL&year=%d" % dt.date.today().year
        r = self.client.get(url)
        self.assertEqual(r.context["expense"], 0)

    def test_outstanding_advance_is_receivable_on_sofp(self):
        from django.urls import reverse
        from decimal import Decimal
        self.client.login(username="afs", password="x")
        r = self.client.get(reverse("report_financial_position"))
        ctx = r.context
        self.assertEqual(ctx["advances"], Decimal("15000"))
        # reclassified out of cash, totals unchanged, statement balances
        self.assertEqual(ctx["cash_on_hand"], ctx["cash"] - Decimal("15000"))
        self.assertEqual(ctx["total_assets"], ctx["cash"] + ctx["nbv"] + ctx["prepaid"])
        self.assertTrue(ctx["balanced"])

    def test_settling_expense_reduces_receivable_and_hits_ie(self):
        import datetime as dt
        from decimal import Decimal
        from cashbook.models import Expense
        from cashbook.views import outstanding_advances_total
        Expense.objects.create(date=dt.date.today(), department=self.fund,
            description="Fare", amount=Decimal("7000"), category="TRANSPORT",
            status="PAID", recorded_by=self.u, approved_by=self.u, advance=self.adv)
        self.assertEqual(outstanding_advances_total(dt.date.today()), Decimal("8000"))

    def test_closed_advance_drops_off_receivables(self):
        from decimal import Decimal
        from cashbook.models import StaffAdvance
        from cashbook.views import outstanding_advances_total
        import datetime as dt
        self.adv.status = StaffAdvance.Status.CLOSED
        self.adv.save()
        self.assertEqual(outstanding_advances_total(dt.date.today()), Decimal("0"))


class ReversalConsistencyTests(TestCase):
    def setUp(self):
        import datetime as dt
        from decimal import Decimal
        from django.contrib.auth.models import User
        from departments.models import Department
        from giving.models import Transaction
        self.u = User.objects.create_superuser("rv", password="x")
        self.fund = Department.objects.create(name="Youth", fund_type="LOCAL",
                                              opening_balance=Decimal("0"))
        self.t = Transaction.objects.create(date=dt.date(2026, 5, 2), channel="BANK",
            direction="CREDIT", amount=Decimal("500"), department=self.fund,
            allocation_status="MANUAL", confirmed=True)

    def test_contra_amount_is_positive(self):
        contra = self.t.reverse(self.u, "error")
        self.assertGreater(contra.amount, 0)               # validator-safe
        self.assertTrue(contra.is_reversal)
        self.assertTrue(self.t.is_reversed)

    def test_contra_passes_model_validation(self):
        # full_clean must not raise (the old negative amount violated MinValue)
        contra = self.t.reverse(self.u, "error")
        contra.full_clean()                                 # would raise if < 0.01

    def test_reversed_credit_excluded_from_fund_balance(self):
        from decimal import Decimal
        from reports.services.balances import fund_balance
        before = fund_balance(self.fund, None)
        self.t.reverse(self.u, "error")
        after = fund_balance(self.fund, None)
        self.assertEqual(before - after, Decimal("500"))

    def test_reversed_credit_excluded_from_dashboard_bank(self):
        from core.services import dashboard
        b = lambda: next(c["value"] for c in dashboard.cards() if "Cash & bank" in c["label"])
        before = b()
        self.t.reverse(self.u, "error")
        self.assertEqual(before - b(), 500)

    def test_unconfirmed_credit_excluded_from_dashboard(self):
        import datetime as dt
        from decimal import Decimal
        from giving.models import Transaction
        from core.services import dashboard
        b = lambda: next(c["value"] for c in dashboard.cards() if "Cash & bank" in c["label"])
        before = b()
        Transaction.objects.create(date=dt.date(2026, 5, 3), channel="BANK",
            direction="CREDIT", amount=Decimal("9999"), department=self.fund,
            allocation_status="AUTO", confirmed=False)       # held import
        self.assertEqual(b(), before)                        # must NOT inflate

    def test_reversed_original_unposted_from_ledger(self):
        from ledger.services import posting
        from ledger.models import JournalEntry
        posting.rebuild()
        self.assertEqual(JournalEntry.objects.filter(
            source_type="transaction", source_id=self.t.pk).count(), 1)
        self.t.reverse(self.u, "error")
        posting.rebuild()
        self.assertEqual(JournalEntry.objects.filter(
            source_type="transaction", source_id=self.t.pk).count(), 0)

    def test_fund_balance_matches_department_summary(self):
        import datetime as dt
        from decimal import Decimal
        from giving.models import Transaction
        from cashbook.models import Expense
        from reports.services.balances import fund_balance, department_summary
        Transaction.objects.create(date=dt.date(2026, 4, 1), channel="CASH",
            direction="CREDIT", amount=Decimal("1234"), department=self.fund,
            allocation_status="MANUAL", confirmed=True)
        Expense.objects.create(date=dt.date(2026, 4, 5), department=self.fund,
            description="x", amount=Decimal("234"), category="OTHER", status="PAID",
            recorded_by=self.u, approved_by=self.u)
        row = next(r for r in department_summary(None, None, consolidated=False)
                   if r["department"].id == self.fund.id)
        self.assertEqual(fund_balance(self.fund, None), row["closing"])


class PendingBankReceiptTests(TestCase):
    def setUp(self):
        import datetime as dt
        from decimal import Decimal
        from django.contrib.auth.models import User
        from departments.models import Department
        self.u = User.objects.create_superuser("pb", password="x")
        self.trust = Department.objects.create(name="Tithe", fund_type="TRUST")
        self.client.login(username="pb", password="x")

    def _txn(self, **kw):
        import datetime as dt
        from decimal import Decimal
        from giving.models import Transaction
        d = dict(date=dt.date.today(), channel="BANK", direction="CREDIT",
                 amount=Decimal("3000"), allocation_status="AUTO", confirmed=True)
        d.update(kw)
        return Transaction.objects.create(**d)

    def test_unreceipted_trust_donation_not_in_trust_but_in_pending(self):
        from decimal import Decimal
        from reports.services.balances import pending_receipts_total, trust_summary
        self._txn(department=self.trust, confirmed=False, core_ref="U1")  # unconfirmed
        self.assertEqual(pending_receipts_total(), Decimal("3000"))
        to_remit = {r["department"].id: r["to_remit"] for r in trust_summary()}
        self.assertEqual(to_remit.get(self.trust.id, Decimal(0)), Decimal(0))

    def test_unallocated_confirmed_donation_in_pending(self):
        from decimal import Decimal
        from reports.services.balances import pending_receipts_total
        self._txn(department=None, allocation_status="REVIEW", core_ref="U2")
        self.assertEqual(pending_receipts_total(), Decimal("3000"))

    def test_receipting_moves_from_pending_to_trust(self):
        from decimal import Decimal
        from reports.services.balances import pending_receipts_total, trust_summary
        t = self._txn(department=self.trust, confirmed=False, core_ref="U3")
        t.confirmed = True
        t.save()
        self.assertEqual(pending_receipts_total(), Decimal(0))
        to_remit = {r["department"].id: r["to_remit"] for r in trust_summary()}
        self.assertEqual(to_remit.get(self.trust.id), Decimal("3000"))

    def test_sofp_balances_with_pending(self):
        from django.urls import reverse
        from decimal import Decimal
        self._txn(department=self.trust, confirmed=False, core_ref="U4")
        self._txn(department=None, allocation_status="REVIEW", amount=Decimal("250"), core_ref="U5")
        r = self.client.get(reverse("report_financial_position"))
        ctx = r.context
        self.assertEqual(ctx["pending"], Decimal("3250"))
        self.assertTrue(ctx["balanced"])


class AccountingReviewTests(TestCase):
    """End-to-end accounting correctness after the comprehensive review."""
    def setUp(self):
        import datetime as dt
        from decimal import Decimal
        from django.contrib.auth.models import User
        from departments.models import Department
        self.u = User.objects.create_superuser("ar", password="x")
        self.fund = Department.objects.create(name="Building", fund_type="LOCAL",
                                              opening_balance=Decimal("0"))
        self.tithe = Department.objects.create(name="Tithe", fund_type="TRUST")
        self.client.login(username="ar", password="x")

    def _credit(self, amount, dept, **kw):
        import datetime as dt
        from decimal import Decimal
        from giving.models import Transaction
        d = dict(date=dt.date(2026, 5, 2), channel="BANK", direction="CREDIT",
                 amount=Decimal(amount), department=dept, allocation_status="AUTO",
                 confirmed=True)
        d.update(kw)
        return Transaction.objects.create(**d)

    def test_reversed_donation_not_double_counted_in_ie(self):
        import datetime as dt
        from django.urls import reverse
        from decimal import Decimal
        t = self._credit("1000", self.fund)
        self._credit("4000", self.fund)
        t.reverse(self.u, "error")            # positive contra created
        r = self.client.get(reverse("report_ie") + "?period=ANNUAL&year=2026")
        # income must be 4000, NOT 4000+1000(original)+1000(contra)
        self.assertEqual(r.context["income"], Decimal("4000"))

    def test_capital_excluded_from_ie_expense(self):
        import datetime as dt
        from django.urls import reverse
        from decimal import Decimal
        from cashbook.models import Expense
        Expense.objects.create(date=dt.date(2026, 5, 3), department=self.fund,
            description="Pews (recurrent)", amount=Decimal("2000"), category="OTHER",
            expenditure_type="RECURRENT", status="PAID", recorded_by=self.u, approved_by=self.u)
        Expense.objects.create(date=dt.date(2026, 5, 4), department=self.fund,
            description="New van", amount=Decimal("1000000"), category="CONSTRUCTION",
            expenditure_type="CAPITAL", status="PAID", recorded_by=self.u, approved_by=self.u)
        r = self.client.get(reverse("report_ie") + "?period=ANNUAL&year=2026")
        self.assertEqual(r.context["expense"], Decimal("2000"))     # capital excluded
        self.assertEqual(r.context["capital"], Decimal("1000000"))  # shown as memo

    def test_unconfirmed_income_excluded_from_ie(self):
        import datetime as dt
        from django.urls import reverse
        from decimal import Decimal
        self._credit("500", self.fund, confirmed=False)   # held for review
        r = self.client.get(reverse("report_ie") + "?period=ANNUAL&year=2026")
        self.assertEqual(r.context["income"], Decimal("0"))

    def test_sofp_balances_with_capital_and_reversal(self):
        import datetime as dt
        from django.urls import reverse
        from decimal import Decimal
        from cashbook.models import Expense
        self._credit("50000", self.fund)
        t = self._credit("1000", self.fund)
        t.reverse(self.u, "err")
        Expense.objects.create(date=dt.date(2026, 5, 4), department=self.fund,
            description="Van", amount=Decimal("30000"), category="CONSTRUCTION",
            expenditure_type="CAPITAL", status="PAID", recorded_by=self.u, approved_by=self.u)
        r = self.client.get(reverse("report_financial_position"))
        self.assertTrue(r.context["balanced"])


class RemittanceExpenseSplitTests(TestCase):
    """Trust remittances must not inflate the operating-expense total, but a
    trust fund's own ledger still reflects them in its closing balance."""

    def setUp(self):
        from django.contrib.auth.models import User
        from departments.models import Department
        self.u = User.objects.create_user("rex", password="x")
        self.trust = Department.objects.create(name="Tithe", fund_type="TRUST",
                                               category="TRUST", opening_balance=0)

    def _exp(self, amount, category):
        from cashbook.models import Expense
        import datetime as dt
        from decimal import Decimal
        return Expense.objects.create(
            date=dt.date(2026, 3, 10), department=self.trust,
            description=category, amount=Decimal(amount), category=category,
            status="PAID", recorded_by=self.u)

    def test_operating_excludes_remittance(self):
        from reports.services import balances
        import datetime as dt
        from decimal import Decimal
        self._exp("500000", "REMITTANCE")
        self._exp("2000", "STATIONERY")
        rows = balances.department_summary(dt.date(2026, 1, 1), dt.date(2026, 12, 31))
        t = balances.totals(rows)
        self.assertEqual(t["expenses"], Decimal("502000"))            # full
        self.assertEqual(t["expenses_operating"], Decimal("2000"))    # excl remittance
        self.assertEqual(t["remittances"], Decimal("500000"))

    def test_trust_ledger_still_reflects_remittance(self):
        from reports.services import balances
        import datetime as dt
        from decimal import Decimal
        self._exp("500000", "REMITTANCE")
        rows = balances.department_summary(dt.date(2026, 1, 1), dt.date(2026, 12, 31))
        row = next(r for r in rows if r["department"].id == self.trust.id
                   or any(c["department"].id == self.trust.id for c in r["children"]))
        # the remittance reduces the trust fund's closing (full expenses applied)
        self.assertEqual(row["expenses"], Decimal("500000"))


class FundLedgerExportTests(TestCase):
    """The fund ledger page offers an Excel/CSV download."""

    def setUp(self):
        from django.contrib.auth.models import User, Group
        from departments.models import Department
        self.u = User.objects.create_user("fx", password="x")
        g, _ = Group.objects.get_or_create(name="Treasurer")
        self.u.groups.add(g)
        self.dept = Department.objects.create(name="Camp Meeting", fund_type="LOCAL",
                                              category="MINISTRY", opening_balance=0)

    def test_xlsx_export(self):
        from django.test import Client
        c = Client(); c.force_login(self.u)
        r = c.get(f"/reports/fund/{self.dept.id}/?start=2026-01-01&end=2026-06-30&export=xlsx")
        self.assertEqual(r.status_code, 200)
        self.assertIn("spreadsheetml", r["Content-Type"])


class BackupExportTests(TestCase):
    """Database backup and full Excel export are available to the treasurer."""

    def setUp(self):
        from django.contrib.auth.models import User, Group
        self.u = User.objects.create_user("bk", password="x")
        g, _ = Group.objects.get_or_create(name="Treasurer")
        self.u.groups.add(g)

    def test_database_backup(self):
        from django.test import Client
        c = Client(); c.force_login(self.u)
        r = c.get("/backup/database/")
        self.assertEqual(r.status_code, 200)
        # on a real (file) DB this is an attachment; in tests the DB is in-memory
        # so the endpoint returns gracefully rather than erroring
        self.assertIn(r["Content-Type"], (
            "application/octet-stream", "text/plain"))

    def test_full_excel_export(self):
        from django.test import Client
        c = Client(); c.force_login(self.u)
        r = c.get("/backup/data-export/")
        self.assertEqual(r.status_code, 200)
        self.assertIn("spreadsheetml", r["Content-Type"])

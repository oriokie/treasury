"""Running balance and Type column for the Transactions page. Computed
chronologically (oldest to newest) and scoped to whatever filters are
currently applied, regardless of the page's own display sort order (Newest
first / Oldest first) - each row shows the cumulative balance up to and
including that transaction, under the current filters. A reversal is
subtracted (an offsetting entry, not new income, matching the same
principle as the Excel export fix), so the running total reflects what
actually happened to the balance.

Only ever queries the current page's rows plus one aggregate for
"everything before this page" - never the full unbounded history - so this
stays cheap regardless of total transaction count."""
import datetime as dt
from decimal import Decimal
from django.test import TestCase, Client, RequestFactory
from django.contrib.auth.models import User, Group
from departments.models import Department
from giving.models import Transaction
from giving.views import TransactionListView


def _tr():
    u = User.objects.create_user("tr_runbalance", password="x", is_superuser=True)
    u.groups.add(Group.objects.get_or_create(name="Treasurer")[0])
    return u


def _ctx_for(tr, querystring):
    rf = RequestFactory()
    req = rf.get(f"/transactions/{querystring}")
    req.user = tr
    view = TransactionListView()
    view.request = req
    view.kwargs = {}
    view.object_list = view.get_queryset()
    return view.get_context_data()


class RunningBalanceComputationTests(TestCase):
    def setUp(self):
        self.tr = _tr()
        self.d = Department.objects.create(name="RunBalTestFund", fund_type="LOCAL",
            category="MINISTRY")

    def test_simple_chronological_balance(self):
        t1 = Transaction.objects.create(date=dt.date(2026, 6, 1), amount=Decimal("1000"),
            direction="CREDIT", confirmed=True, channel="BANK", allocation_status="AUTO",
            department=self.d, reference="rb1")
        t2 = Transaction.objects.create(date=dt.date(2026, 6, 2), amount=Decimal("300"),
            direction="DEBIT", confirmed=True, channel="BANK", allocation_status="AUTO",
            department=self.d, reference="rb2")
        t3 = Transaction.objects.create(date=dt.date(2026, 6, 3), amount=Decimal("500"),
            direction="CREDIT", confirmed=True, channel="BANK", allocation_status="AUTO",
            department=self.d, reference="rb3")
        ctx = _ctx_for(self.tr, f"?department={self.d.id}&sort=oldest")
        bal = ctx["running_balances"]
        self.assertEqual(bal[t1.id], Decimal("1000"))
        self.assertEqual(bal[t2.id], Decimal("700"))
        self.assertEqual(bal[t3.id], Decimal("1200"))

    def test_reversal_subtracted_from_running_balance(self):
        t1 = Transaction.objects.create(date=dt.date(2026, 6, 4), amount=Decimal("500"),
            direction="CREDIT", confirmed=True, channel="BANK", allocation_status="AUTO",
            department=self.d, reference="rb4")
        t2 = Transaction.objects.create(date=dt.date(2026, 6, 5), amount=Decimal("500"),
            direction="CREDIT", confirmed=True, channel="BANK", allocation_status="AUTO",
            department=self.d, reference="rb5")
        contra = t2.reverse(self.tr, reason="test")
        ctx = _ctx_for(self.tr, f"?department={self.d.id}&sort=oldest")
        bal = ctx["running_balances"]
        self.assertEqual(bal[t1.id], Decimal("500"))
        self.assertEqual(bal[t2.id], Decimal("1000"))
        self.assertEqual(bal[contra.id], Decimal("500"))   # nets back down

    def test_running_balance_scoped_to_current_filter(self):
        """Filtering to one fund shows THAT fund's own running balance,
        not the whole church's."""
        d2 = Department.objects.create(name="RunBalOtherFund", fund_type="LOCAL",
            category="MINISTRY")
        t1 = Transaction.objects.create(date=dt.date(2026, 6, 6), amount=Decimal("100"),
            direction="CREDIT", confirmed=True, channel="BANK", allocation_status="AUTO",
            department=self.d, reference="rb6")
        Transaction.objects.create(date=dt.date(2026, 6, 6), amount=Decimal("99999"),
            direction="CREDIT", confirmed=True, channel="BANK", allocation_status="AUTO",
            department=d2, reference="rb6_other")
        ctx = _ctx_for(self.tr, f"?department={self.d.id}&sort=oldest")
        self.assertEqual(ctx["running_balances"][t1.id], Decimal("100"))

    def test_balance_independent_of_display_sort_order(self):
        """The balance for a given transaction must be the same number
        whether the page is displaying newest-first or oldest-first — it's
        always computed chronologically underneath."""
        t1 = Transaction.objects.create(date=dt.date(2026, 6, 7), amount=Decimal("200"),
            direction="CREDIT", confirmed=True, channel="BANK", allocation_status="AUTO",
            department=self.d, reference="rb7")
        t2 = Transaction.objects.create(date=dt.date(2026, 6, 8), amount=Decimal("300"),
            direction="CREDIT", confirmed=True, channel="BANK", allocation_status="AUTO",
            department=self.d, reference="rb8")
        ctx_oldest = _ctx_for(self.tr, f"?department={self.d.id}&sort=oldest")
        ctx_newest = _ctx_for(self.tr, f"?department={self.d.id}&sort=newest")
        self.assertEqual(ctx_oldest["running_balances"][t2.id],
                         ctx_newest["running_balances"][t2.id])

    def test_pagination_boundary_carries_opening_balance_correctly(self):
        """The second page's running balance must correctly continue from
        the first page's closing balance, not restart from zero."""
        for i in range(60):
            Transaction.objects.create(date=dt.date(2026, 1, 1) + dt.timedelta(days=i),
                amount=Decimal("100"), direction="CREDIT", confirmed=True, channel="BANK",
                allocation_status="AUTO", department=self.d, reference=f"rbpage{i}")
        ctx_p1 = _ctx_for(self.tr, f"?department={self.d.id}&sort=oldest")
        ctx_p2 = _ctx_for(self.tr, f"?department={self.d.id}&sort=oldest&page=2")
        last_of_p1 = list(ctx_p1["transactions"])[-1]
        first_of_p2 = list(ctx_p2["transactions"])[0]
        self.assertEqual(ctx_p1["running_balances"][last_of_p1.id],
                         ctx_p2["running_balances"][first_of_p2.id] - Decimal("100"))

    def test_empty_page_returns_empty_balances_no_crash(self):
        ctx = _ctx_for(self.tr, "?department=999999")
        self.assertEqual(ctx["running_balances"], {})


class RunningBalanceUITests(TestCase):
    def setUp(self):
        self.tr = _tr()
        self.c = Client(); self.c.force_login(self.tr)
        self.d = Department.objects.create(name="RunBalUIFund", fund_type="LOCAL",
            category="MINISTRY")

    def test_type_column_shows_credit_debit(self):
        Transaction.objects.create(date=dt.date(2026, 6, 10), amount=Decimal("500"),
            direction="CREDIT", confirmed=True, channel="BANK", allocation_status="AUTO",
            department=self.d, reference="typecoltest1")
        b = self.c.get("/transactions/?q=typecoltest1").content.decode()
        self.assertIn("Credit", b)

    def test_type_column_shows_reversal_distinctly(self):
        t = Transaction.objects.create(date=dt.date(2026, 6, 11), amount=Decimal("500"),
            direction="CREDIT", confirmed=True, channel="BANK", allocation_status="AUTO",
            department=self.d, reference="typecoltest2")
        t.reverse(self.tr, reason="test")
        b = self.c.get("/transactions/?q=typecoltest2").content.decode()
        self.assertIn("Reversal", b)
        self.assertIn("Reversed credit", b)

    def test_sort_toggle_switches_order(self):
        t1 = Transaction.objects.create(date=dt.date(2026, 6, 12), amount=Decimal("100"),
            direction="CREDIT", confirmed=True, channel="BANK", allocation_status="AUTO",
            department=self.d, reference="sorttest_early")
        t2 = Transaction.objects.create(date=dt.date(2026, 6, 13), amount=Decimal("100"),
            direction="CREDIT", confirmed=True, channel="BANK", allocation_status="AUTO",
            department=self.d, reference="sorttest_late")
        b_newest = self.c.get("/transactions/?department="+str(self.d.id)+"&sort=newest").content.decode()
        b_oldest = self.c.get("/transactions/?department="+str(self.d.id)+"&sort=oldest").content.decode()
        self.assertLess(b_newest.index("sorttest_late"), b_newest.index("sorttest_early"))
        self.assertLess(b_oldest.index("sorttest_early"), b_oldest.index("sorttest_late"))

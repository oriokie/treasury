from decimal import Decimal
from django.test import TestCase
from departments.models import Department


class SubgroupTests(TestCase):
    def test_subaccount_inherits_parent_fund_type(self):
        youth = Department.objects.create(name="Youth", fund_type=Department.FundType.LOCAL)
        choir = Department.objects.create(name="Youth Choir", parent=youth,
                                          fund_type=Department.FundType.TRUST)  # should be overridden
        self.assertEqual(choir.fund_type, Department.FundType.LOCAL)
        self.assertFalse(choir.is_trust)
        self.assertTrue(choir.is_subgroup)

    def test_subgroups_reverse_relation(self):
        youth = Department.objects.create(name="Youth", fund_type=Department.FundType.LOCAL)
        Department.objects.create(name="Potluck", parent=youth)
        Department.objects.create(name="Mission", parent=youth)
        self.assertEqual(youth.subgroups.count(), 2)

    def test_str_shows_parent_path(self):
        youth = Department.objects.create(name="Youth", fund_type=Department.FundType.LOCAL)
        potluck = Department.objects.create(name="Potluck", parent=youth)
        self.assertEqual(str(potluck), "Youth / Potluck")

    def test_deleting_parent_keeps_subgroup(self):
        youth = Department.objects.create(name="Youth", fund_type=Department.FundType.LOCAL)
        potluck = Department.objects.create(name="Potluck", parent=youth)
        youth.delete()
        potluck.refresh_from_db()
        self.assertIsNone(potluck.parent)


class BudgetSourceAndBoardTests(TestCase):
    def setUp(self):
        from django.contrib.auth.models import User
        from departments.models import Department, Budget, BudgetLine
        from decimal import Decimal
        self.u = User.objects.create_superuser("bs", password="x")
        self.lcb = Department.objects.create(name="LCB – Local Church Budget",
                                             fund_type=Department.FundType.LOCAL)
        self.youth = Department.objects.create(name="Youth", fund_type=Department.FundType.LOCAL)
        self.year = 2026
        b = Budget.objects.create(year=self.year, department=self.youth, amount=Decimal("0"))
        BudgetLine.objects.create(budget=b, name="Camp", amount=Decimal("70000"))          # own
        BudgetLine.objects.create(budget=b, name="PA hire", amount=Decimal("30000"),
                                  source_fund=self.lcb)                                     # LCB
        b.amount = b.lines_total
        b.save()
        self.budget = b
        self.client.login(username="bs", password="x")

    def test_source_kind(self):
        lines = {l.name: l.source_kind for l in self.budget.lines.all()}
        self.assertEqual(lines["Camp"], "OWN")
        self.assertEqual(lines["PA hire"], "LCB")

    def test_board_budget_splits_and_lcb_exposure(self):
        from reports.services.budget import board_budget
        from decimal import Decimal
        d = board_budget(self.year)
        self.assertEqual(d["totals"]["budget"], Decimal("100000"))
        self.assertEqual(d["totals"]["lcb"], Decimal("30000"))
        self.assertEqual(d["totals"]["own"], Decimal("70000"))
        alloc = {a["dept"].id: a["amount"] for a in d["lcb_alloc"]}
        self.assertEqual(alloc[self.youth.id], Decimal("30000"))

    def test_board_report_page_renders(self):
        from django.urls import reverse
        r = self.client.get(reverse("report_budget_board") + f"?year={self.year}")
        self.assertEqual(r.status_code, 200)

    def test_copy_prior_year_breakdown(self):
        from django.urls import reverse
        from departments.models import Budget, BudgetLine
        from decimal import Decimal
        # this budget's lines are year 2026; make 2027 copy from 2026
        cur, _ = Budget.objects.get_or_create(year=2027, department=self.youth,
                                              defaults={"amount": Decimal("0")})
        self.client.post(reverse("budget_lines", args=[self.youth.id]),
                         {"year": 2027, "action": "copy_prior"})
        cur.refresh_from_db()
        self.assertEqual(cur.lines.count(), 2)
        self.assertEqual(cur.lines_total, Decimal("100000"))
        # source_fund carried over
        self.assertTrue(cur.lines.filter(source_fund=self.lcb).exists())

    def test_blank_source_defaults_to_own(self):
        from departments.models import BudgetLine
        ln = self.budget.lines.get(name="Camp")
        self.assertIsNone(ln.source_fund)
        self.assertIn("own funds", ln.source_label)

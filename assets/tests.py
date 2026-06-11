import datetime as dt
from decimal import Decimal
from django.test import TestCase
from assets.models import FixedAsset, DepreciationRule


class DepreciationTests(TestCase):
    def test_straight_line(self):
        a = FixedAsset.objects.create(name="Chairs", category="FURNITURE",
            acquired_on=dt.date(2021, 1, 20), cost=Decimal("300000"),
            method="STRAIGHT", rate=Decimal("12.5"))
        # 2021..2026 inclusive of acquisition year = 6 charges * 37,500
        self.assertEqual(a.accumulated_depreciation(dt.date(2026, 6, 6)), Decimal("225000.00"))
        self.assertEqual(a.net_book_value(dt.date(2026, 6, 6)), Decimal("75000.00"))

    def test_straight_line_capped_at_salvage(self):
        a = FixedAsset.objects.create(name="Old", category="EQUIPMENT",
            acquired_on=dt.date(2000, 1, 1), cost=Decimal("100000"),
            salvage_value=Decimal("10000"), method="STRAIGHT", rate=Decimal("20"))
        # cannot depreciate below cost - salvage
        self.assertEqual(a.accumulated_depreciation(dt.date(2026, 1, 1)), Decimal("90000.00"))
        self.assertEqual(a.net_book_value(dt.date(2026, 1, 1)), Decimal("10000.00"))

    def test_reducing_balance(self):
        a = FixedAsset.objects.create(name="Laptops", category="IT",
            acquired_on=dt.date(2024, 9, 1), cost=Decimal("240000"),
            method="REDUCING", rate=Decimal("30"))
        # charge in 2024 (72,000) and on the 2025 anniversary (50,400); 2026 anniversary not reached
        self.assertEqual(a.accumulated_depreciation(dt.date(2026, 6, 6)), Decimal("122400.00"))

    def test_none_method(self):
        a = FixedAsset.objects.create(name="Land", category="LAND",
            acquired_on=dt.date(2000, 1, 1), cost=Decimal("5000000"),
            method="NONE", rate=Decimal("0"))
        self.assertEqual(a.accumulated_depreciation(dt.date(2026, 1, 1)), Decimal("0"))
        self.assertEqual(a.net_book_value(dt.date(2026, 1, 1)), Decimal("5000000.00"))

    def test_policy_falls_back_to_category_rule(self):
        DepreciationRule.objects.create(category="MUSICAL", method="STRAIGHT", rate=Decimal("15"))
        a = FixedAsset.objects.create(name="Keyboard", category="MUSICAL",
            acquired_on=dt.date(2026, 1, 1), cost=Decimal("100000"))  # no per-asset override
        method, rate = a._policy()
        self.assertEqual((method, rate), ("STRAIGHT", Decimal("15")))

    def test_disposed_asset_has_zero_nbv(self):
        a = FixedAsset.objects.create(name="Van", category="VEHICLE",
            acquired_on=dt.date(2020, 1, 1), cost=Decimal("1000000"),
            method="REDUCING", rate=Decimal("25"), disposed=True,
            disposed_on=dt.date(2026, 1, 1))
        self.assertEqual(a.net_book_value(dt.date(2026, 6, 1)), Decimal("0"))


class FinancialStatementTests(TestCase):
    def setUp(self):
        from django.contrib.auth.models import User
        from departments.models import Department
        from giving.models import Transaction
        from cashbook.models import Expense
        self.u = User.objects.create_superuser("fs", password="x")
        self.local = Department.objects.create(name="Youth", fund_type=Department.FundType.LOCAL)
        self.trust = Department.objects.create(name="Tithe", fund_type=Department.FundType.TRUST)
        Transaction.objects.create(date=dt.date(2026, 5, 10), channel="CASH", direction="CREDIT",
            amount=Decimal("5000"), department=self.local, allocation_status="MANUAL")
        Transaction.objects.create(date=dt.date(2026, 5, 10), channel="BANK", direction="CREDIT",
            amount=Decimal("9000"), department=self.trust, allocation_status="AUTO")
        Expense.objects.create(date=dt.date(2026, 5, 12), department=self.local,
            description="snacks", amount=Decimal("1200"), category="REFRESHMENTS",
            status=Expense.Status.PAID, recorded_by=self.u)
        FixedAsset.objects.create(name="PA", category="EQUIPMENT",
            acquired_on=dt.date(2026, 1, 1), cost=Decimal("100000"),
            method="STRAIGHT", rate=Decimal("0"))   # no depreciation -> NBV 100000
        self.client.login(username="fs", password="x")

    def test_income_statement_surplus_excludes_trust(self):
        from django.urls import reverse
        r = self.client.get(reverse("report_income_statement") + "?year=2026&month=5")
        self.assertEqual(r.status_code, 200)
        # local income 5000 - expenses 1200 = 3800 surplus; trust 9000 excluded
        self.assertEqual(r.context["total_income"], Decimal("5000"))
        self.assertEqual(r.context["surplus"], Decimal("3800"))
        self.assertEqual(r.context["trust_collected"], Decimal("9000"))

    def test_financial_position_balances(self):
        from django.urls import reverse
        r = self.client.get(reverse("report_financial_position") + "?as_of=2026-05-31")
        self.assertEqual(r.status_code, 200)
        # classified Net Assets format: assets == liabilities + net assets
        self.assertEqual(r.context["total_assets"], r.context["total_liab_and_na"])
        self.assertTrue(r.context["balanced"])
        self.assertEqual(r.context["net_assets"],
                         r.context["unallocated"] + r.context["allocated"] + r.context["nbv"])
        self.assertEqual(r.context["nbv"], Decimal("100000.00"))


class DisposalAccountingTests(TestCase):
    def setUp(self):
        import datetime as dt
        from decimal import Decimal
        from django.contrib.auth.models import User
        from departments.models import Department
        from assets.models import FixedAsset
        self.u = User.objects.create_superuser("dz", password="x")
        self.fund = Department.objects.create(name="General", fund_type="LOCAL")
        self.asset = FixedAsset.objects.create(name="Old Van", cost=Decimal("100000"),
            salvage_value=Decimal("0"), acquired_on=dt.date(2026, 1, 1),
            category="VEHICLE", method="NONE", rate=Decimal("0"))  # NBV stays 100000
        self.client.login(username="dz", password="x")

    def test_proceeds_excluded_from_income_only_gain_counts(self):
        import datetime as dt
        from decimal import Decimal
        from django.urls import reverse
        # sell for 120,000 when NBV is 100,000 -> gain 20,000
        self.client.post(reverse("asset_dispose", args=[self.asset.pk]), {
            "disposed_on": "2026-05-10", "proceeds": "120000",
            "method": "SOLD", "fund": str(self.fund.pk)})
        r = self.client.get(reverse("report_ie") + "?period=ANNUAL&year=2026")
        # income must NOT include the 120k proceeds
        self.assertEqual(r.context["income"], Decimal("0"))
        # only the 20k gain is in the result
        self.assertEqual(r.context["disposal_gain_loss"], Decimal("20000"))
        self.assertEqual(r.context["net"], Decimal("20000"))

    def test_proceeds_still_in_fund_cash(self):
        import datetime as dt
        from decimal import Decimal
        from django.urls import reverse
        from reports.services.balances import fund_balance
        self.client.post(reverse("asset_dispose", args=[self.asset.pk]), {
            "disposed_on": "2026-05-10", "proceeds": "120000",
            "method": "SOLD", "fund": str(self.fund.pk)})
        # cash in the fund DID increase by the proceeds (real money received)
        self.assertEqual(fund_balance(self.fund, dt.date(2026, 12, 31)), Decimal("120000"))

    def test_sofp_balances_after_disposal(self):
        from django.urls import reverse
        self.client.post(reverse("asset_dispose", args=[self.asset.pk]), {
            "disposed_on": "2026-05-10", "proceeds": "120000",
            "method": "SOLD", "fund": str(self.fund.pk)})
        r = self.client.get(reverse("report_financial_position"))
        self.assertTrue(r.context["balanced"])


class AssetViewTests(TestCase):
    def setUp(self):
        import datetime as dt
        from decimal import Decimal
        from django.contrib.auth.models import User, Group
        from core.roles import TREASURER, AUDITOR
        from assets.models import FixedAsset
        g = Group.objects.get_or_create(name=TREASURER)[0]
        self.tr = User.objects.create_user("av_tr", password="x"); self.tr.groups.add(g)
        self.au = User.objects.create_user("av_au", password="x")
        self.au.groups.add(Group.objects.get_or_create(name=AUDITOR)[0])
        self.asset = FixedAsset.objects.create(name="Generator", cost=Decimal("60000"),
            salvage_value=Decimal("0"), acquired_on=dt.date.today(),
            category="EQUIPMENT", method="STRAIGHT", rate=Decimal("10"))
        self.client.force_login(self.tr)

    def test_list_and_detail_render(self):
        from django.urls import reverse
        self.assertEqual(self.client.get(reverse("asset_list")).status_code, 200)
        self.assertEqual(self.client.get(
            reverse("asset_detail", args=[self.asset.pk])).status_code, 200)

    def test_create_asset(self):
        import datetime as dt
        from django.urls import reverse
        from assets.models import FixedAsset
        r = self.client.post(reverse("asset_create"), {
            "name": "Sound System", "category": "EQUIPMENT",
            "acquired_on": dt.date.today().isoformat(), "cost": "45000",
            "salvage_value": "5000", "method": "REDUCING", "rate": "20",
            "department": "", "location": "Sanctuary", "reference": "", "notes": ""})
        self.assertIn(r.status_code, (200, 302))
        self.assertTrue(FixedAsset.objects.filter(name="Sound System").exists())

    def test_depreciation_rules_page(self):
        from django.urls import reverse
        self.assertEqual(self.client.get(reverse("depreciation_rules")).status_code, 200)

    def test_auditor_cannot_create_asset(self):
        import datetime as dt
        from django.urls import reverse
        from assets.models import FixedAsset
        self.client.force_login(self.au)
        r = self.client.post(reverse("asset_create"), {
            "name": "Forbidden", "category": "EQUIPMENT",
            "acquired_on": dt.date.today().isoformat(), "cost": "100",
            "salvage_value": "0", "method": "NONE", "rate": "0"})
        self.assertIn(r.status_code, (302, 403))
        self.assertFalse(FixedAsset.objects.filter(name="Forbidden").exists())

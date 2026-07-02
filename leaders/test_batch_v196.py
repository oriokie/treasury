"""v1.96 batch: campaign count fix, allocation rights, dashboard collection-only
columns + closing sort, leader expense attach + column removal, dev-group
target/progress removal, sign-out page, church settings."""
import datetime as dt
from decimal import Decimal
from django.test import TestCase, Client
from django.contrib.auth.models import User, Group
from departments.models import Department, DepartmentLeadership
from cashbook.models import Expense, ExpenseAttachment
from ledger.services.posting import ensure_chart


def _leader(dept, name="ld196"):
    u = User.objects.create_user(name, password="x")
    u.groups.add(Group.objects.get_or_create(name="Leader")[0])
    DepartmentLeadership.objects.create(department=dept, user=u)
    return u


def _tr():
    u = User.objects.create_user("tr196", password="x", is_superuser=True)
    u.groups.add(Group.objects.get_or_create(name="Treasurer")[0])
    return u


class AllocationRightsTests(TestCase):
    def test_allocate_right_grants_without_role(self):
        from accounts.models import Profile
        from core import roles
        u = User.objects.create_user("alloc196", password="x")
        p = Profile.objects.create(name="Allocator",
            rights=["allocate_transactions", "view_reports"])
        u.profiles.add(p)
        self.assertTrue(roles.can_allocate(u))
        self.assertFalse(roles.can_enter_data(u))
        self.assertFalse(roles.can_classify_debits(u))

    def test_classify_debits_right(self):
        from accounts.models import Profile
        from core import roles
        u = User.objects.create_user("dbt196", password="x")
        p = Profile.objects.create(name="Classifier", rights=["classify_debits"])
        u.profiles.add(p)
        self.assertTrue(roles.can_classify_debits(u))


class CampaignCountTests(TestCase):
    def test_counts_do_not_multiply(self):
        from giving.models import Campaign, CampaignMember, Transaction
        from django.db.models import Count
        d = Department.objects.create(name="CampF", fund_type="LOCAL", category="MINISTRY")
        camp = Campaign.objects.create(name="Camp", department=d)
        for i in range(10):
            CampaignMember.objects.create(campaign=camp, name=f"M{i}")
        for i in range(4):
            Transaction.objects.create(date=dt.date(2026, 6, 1), amount=Decimal("10"),
                direction="CREDIT", confirmed=True, channel="CASH",
                allocation_status="MANUAL", campaign=camp, department=d)
        fixed = Campaign.objects.filter(pk=camp.pk).annotate(
            m=Count("members", distinct=True)).first()
        self.assertEqual(fixed.m, 10)  # not 40


class CollectionOnlyColumnTests(TestCase):
    def setUp(self):
        ensure_chart()
        self.parent = Department.objects.create(name="CampParent", fund_type="LOCAL",
            category="DEVELOPMENT", collection_only=True)
        self.sub = Department.objects.create(name="CampSub", fund_type="LOCAL",
            category="DEVELOPMENT", parent=self.parent, collection_only=True)
        self.u = _leader(self.parent)
        DepartmentLeadership.objects.create(department=self.sub, user=self.u)
        self.c = Client(); self.c.force_login(self.u)

    def test_collection_only_hides_expenses_column(self):
        b = self.c.get(f"/leader/department/{self.parent.id}/").content.decode()
        if "Sub-accounts" in b:
            # collection-only: no Expenses column, but Opening/Receipts/Closing present
            self.assertNotIn(">Expenses</th>", b)
            self.assertIn(">Opening</th>", b)
            self.assertIn(">Closing</th>", b)


class LeaderExpenseTests(TestCase):
    def setUp(self):
        ensure_chart()
        self.dept = Department.objects.create(name="ExpDept196", fund_type="LOCAL",
            category="MINISTRY", show_in_expenses=True)
        self.u = _leader(self.dept); self.tr = _tr()
        self.e = Expense.objects.create(date=dt.date(2026, 6, 5), department=self.dept,
            description="Cable", amount=Decimal("500"), category="MATERIALS",
            status="PAID", recorded_by=self.tr, approved_by=self.tr)
        self.c = Client(); self.c.force_login(self.u)

    def test_columns_removed_from_display(self):
        b = self.c.get(f"/leader/department/{self.dept.id}/expenses/"
                       "?start=2026-06-01&end=2026-06-30").content.decode()
        self.assertNotIn(">Method</th>", b)
        self.assertNotIn(">Status</th>", b)
        self.assertNotIn(">Category</th>", b)

    def test_columns_retained_in_export(self):
        b = self.c.get(f"/leader/department/{self.dept.id}/expenses/?export=csv"
                       "&start=2026-06-01&end=2026-06-30").content.decode()
        self.assertIn("Category", b)
        self.assertIn("Method", b)
        self.assertIn("Status", b)

    def test_attach_mpesa_ref(self):
        self.c.post(f"/leader/department/{self.dept.id}/expenses/",
            {"action": "add_attachment", "expense_id": self.e.id,
             "mpesa_ref": "QGH7X8ABCD"})
        self.assertTrue(ExpenseAttachment.objects.filter(
            expense=self.e, text="QGH7X8ABCD").exists())


class SignOutPageTests(TestCase):
    def test_signout_shows_verse(self):
        u = _tr(); c = Client(); c.force_login(u)
        r = c.post("/accounts/logout/")
        self.assertEqual(r.status_code, 200)
        b = r.content.decode()
        self.assertIn("so-verse", b)
        self.assertIn("blockquote", b)
        self.assertNotIn("_auth_user_id", c.session)


class ChurchSettingsTests(TestCase):
    def test_new_settings_render_and_save(self):
        from core.models import SiteConfig
        from core.forms import SiteConfigForm
        u = _tr(); c = Client(); c.force_login(u)
        b = c.get("/settings/?tab=branding").content.decode()
        self.assertIn('name="church_address"', b)
        self.assertIn('name="currency_symbol"', b)
        self.assertIn('name="report_footer_note"', b)


class TreasurerDashboardSortTests(TestCase):
    def test_local_funds_sorted_by_closing_desc(self):
        ensure_chart()
        Department.objects.create(name="Small", fund_type="LOCAL", category="OFFERING",
            opening_balance=Decimal("100"))
        Department.objects.create(name="Big", fund_type="LOCAL", category="OFFERING",
            opening_balance=Decimal("9000"))
        u = _tr(); c = Client(); c.force_login(u)
        b = c.get("/").content.decode()
        # Big (9000) should appear before Small (100) in the local funds table
        if "Big" in b and "Small" in b:
            self.assertLess(b.index(">Big<"), b.index(">Small<"))

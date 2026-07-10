"""Configurable LCB departments in Settings (with name-match fallback)."""
import datetime as dt
from decimal import Decimal
from django.test import TestCase, Client
from django.contrib.auth.models import User, Group
from core.models import SiteConfig
from departments.models import Department, lcb_fund
from cashbook.models import Expense
from reports.services import treasurer as T


class LcbConfigTests(TestCase):
    def setUp(self):
        self.u = User.objects.create_user("lc", password="x", is_superuser=True)
        self.u.groups.add(Group.objects.get_or_create(name="Treasurer")[0])
        self.c = Client(); self.c.force_login(self.u)
        # a fund that does NOT contain 'LCB' in its name
        self.parent = Department.objects.create(name="Church Operating",
            fund_type="LOCAL", category="OFFERING", show_in_expenses=True)
        self.child = Department.objects.create(name="Operating – Utilities",
            fund_type="LOCAL", category="OFFERING", parent=self.parent,
            show_in_expenses=True)

    def test_settings_has_picker(self):
        b = self.c.get("/settings/").content.decode()
        self.assertIn("Local Church Budget (LCB) funds", b)
        self.assertIn("lcb-picker", b)

    def test_config_overrides_name_match(self):
        cfg = SiteConfig.get()
        cfg.lcb_departments.set([self.parent])
        ids = T._lcb_dept_ids()
        # the configured parent AND its sub-account are included
        self.assertIn(self.parent.id, ids)
        self.assertIn(self.child.id, ids)

    def test_lcb_expenditure_uses_config(self):
        cfg = SiteConfig.get()
        cfg.lcb_departments.set([self.parent])
        Expense.objects.create(date=dt.date(2026, 6, 10), department=self.parent,
            description="x", amount=Decimal("4000"), category="UTILITIES",
            status="PAID", recorded_by=self.u)
        Expense.objects.create(date=dt.date(2026, 6, 11), department=self.child,
            description="y", amount=Decimal("1000"), category="UTILITIES",
            status="PAID", recorded_by=self.u)
        out = T.lcb_expenditure(dt.date(2026, 6, 1), dt.date(2026, 6, 30))
        self.assertEqual(out["total"], Decimal("5000"))   # parent + child

    def test_lcb_fund_respects_config(self):
        cfg = SiteConfig.get()
        cfg.lcb_departments.set([self.parent])
        self.assertEqual(lcb_fund().id, self.parent.id)

    def test_fallback_when_unconfigured(self):
        Department.objects.create(name="LCB – Local Church Budget",
            fund_type="LOCAL", category="OFFERING")
        SiteConfig.get().lcb_departments.clear()
        ids = T._lcb_dept_ids()
        self.assertTrue(ids)   # name-match fallback still works

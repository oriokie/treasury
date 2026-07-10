"""Leader dashboard: no charts; collection-only subgroups show name + total and
no expenses; a JPEG button is offered (#2)."""
import datetime as dt
from decimal import Decimal

from django.test import TestCase, Client
from django.contrib.auth.models import User, Group

from departments.models import Department, DepartmentLeadership
from giving.models import Transaction


class LeaderSimplifiedTests(TestCase):
    def setUp(self):
        self.parent = Department.objects.create(name="CAMP MEETING", fund_type="LOCAL",
            category="MINISTRY", show_in_expenses=False)
        self.s1 = Department.objects.create(name="CAMP-1", fund_type="LOCAL",
            category="MINISTRY", parent=self.parent, show_in_expenses=False)
        self.s2 = Department.objects.create(name="CAMP-2", fund_type="LOCAL",
            category="MINISTRY", parent=self.parent, show_in_expenses=False)
        Transaction.objects.create(date=dt.date.today(), channel="CASH", direction="CREDIT",
            amount=Decimal("500"), department=self.s1, allocation_status="MANUAL", confirmed=True)
        Transaction.objects.create(date=dt.date.today(), channel="CASH", direction="CREDIT",
            amount=Decimal("900"), department=self.s2, allocation_status="MANUAL", confirmed=True)
        self.lead = User.objects.create_user("lead", password="x")
        self.lead.groups.add(Group.objects.get_or_create(name="Leader")[0])
        DepartmentLeadership.objects.create(user=self.lead, department=self.parent)
        self.c = Client(); self.c.force_login(self.lead)

    def test_no_charts_and_simplified(self):
        b = self.c.get(f"/leader/department/{self.parent.id}/").content.decode()
        self.assertNotIn("ldMonthly", b)
        self.assertNotIn("chart.umd", b)
        self.assertIn(">Opening</th>", b)
        self.assertNotIn("Recent expenses", b)
        self.assertIn("tableToPng('ldSubgroups'", b)

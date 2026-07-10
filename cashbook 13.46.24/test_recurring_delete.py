"""Recurring schedules can be deleted; generated expenses remain (#3)."""
import datetime as dt
from decimal import Decimal
from django.test import TestCase, Client
from django.contrib.auth.models import User, Group
from departments.models import Department
from cashbook.models import RecurringExpense


class RecurringDeleteTests(TestCase):
    def setUp(self):
        u = User.objects.create_user("rd", password="x", is_superuser=True)
        u.groups.add(Group.objects.get_or_create(name="Treasurer")[0])
        self.u = u; self.c = Client(); self.c.force_login(u)
        self.d = Department.objects.create(name="LCB", fund_type="LOCAL",
            category="OFFERING", show_in_expenses=True)

    def test_delete_schedule(self):
        s = RecurringExpense.objects.create(description="Rent", department=self.d,
            amount=Decimal("5000"), frequency="MONTHLY", day_of_month=1,
            start_date=dt.date(2026, 1, 1), created_by=self.u)
        b = self.c.get("/expenses/recurring/").content.decode()
        self.assertIn(f"/expenses/recurring/{s.id}/delete/", b)
        self.c.post(f"/expenses/recurring/{s.id}/delete/")
        self.assertFalse(RecurringExpense.objects.filter(pk=s.id).exists())

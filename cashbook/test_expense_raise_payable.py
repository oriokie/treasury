"""Raise a payable from an expense (leader reimbursement / credit purchase)."""
import datetime as dt
from decimal import Decimal

from django.contrib.auth.models import Group, User
from django.test import Client, TestCase
from django.urls import reverse

from core.roles import TREASURER
from departments.models import Department
from cashbook.models import Expense, Payable


class ExpenseRaisePayableTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user("erp_tr", password="x", is_superuser=True)
        self.user.groups.add(Group.objects.get_or_create(name=TREASURER)[0])
        self.fund = Department.objects.create(
            name="Youth", fund_type="LOCAL", category="MINISTRY",
            show_in_expenses=True)
        self.exp = Expense.objects.create(
            date=dt.date(2026, 10, 1), department=self.fund,
            description="Camp transport — leader paid",
            amount=Decimal("3500"), category="TRANSPORT",
            claimant="Jane Leader", status=Expense.Status.PAID,
            recorded_by=self.user, approved_by=self.user)
        self.c = Client()
        self.c.force_login(self.user)

    def test_raise_payable_from_expense(self):
        r = self.c.post(reverse("expense_raise_payable", args=[self.exp.pk]))
        self.assertEqual(r.status_code, 302)
        bill = Payable.objects.order_by("-pk").first()
        self.assertIsNotNone(bill)
        self.assertEqual(bill.vendor, "Jane Leader")
        self.assertEqual(bill.amount, Decimal("3500"))
        self.assertEqual(bill.department_id, self.fund.pk)
        self.assertFalse(bill.settled)

    def test_detail_shows_raise_button(self):
        body = self.c.get(reverse("expense_detail", args=[self.exp.pk])).content.decode()
        self.assertIn("Raise payable", body)
        self.assertIn(reverse("expense_raise_payable", args=[self.exp.pk]), body)

    def test_edit_form_shows_raise_checkbox(self):
        body = self.c.get(reverse("expense_edit", args=[self.exp.pk])).content.decode()
        self.assertIn('name="raise_payable"', body)
        self.assertIn("Also open a payable", body)

    def test_edit_post_can_raise_payable(self):
        r = self.c.post(reverse("expense_edit", args=[self.exp.pk]), {
            "date": self.exp.date.isoformat(),
            "department": self.fund.pk,
            "description": self.exp.description,
            "amount": "3500",
            "category": self.exp.category,
            "claimant": "Jane Leader",
            "method": self.exp.method or "BANK",
            "raise_payable": "1",
        })
        self.assertEqual(r.status_code, 302)
        bill = Payable.objects.order_by("-pk").first()
        self.assertIsNotNone(bill)
        self.assertEqual(bill.vendor, "Jane Leader")
        self.assertEqual(bill.amount, Decimal("3500"))

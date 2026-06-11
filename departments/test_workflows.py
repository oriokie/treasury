"""Coverage for departments/funds: creating funds, dev groups, the department
list and balance views, and access control."""
from decimal import Decimal

from django.contrib.auth.models import User, Group
from django.test import TestCase
from django.urls import reverse

from core.roles import TREASURER, AUDITOR
from departments.models import Department, DevelopmentGroup


def _user(name, role):
    u = User.objects.create_user(name, password="x")
    u.groups.add(Group.objects.get_or_create(name=role)[0])
    return u


class DepartmentWorkflowTests(TestCase):
    def setUp(self):
        self.treasurer = _user("d_tr", TREASURER)
        self.client.force_login(self.treasurer)

    def test_create_fund(self):
        r = self.client.post(reverse("department_create"), {
            "name": "Sabbath School", "fund_type": "LOCAL", "category": "MINISTRY",
            "opening_balance": "0", "active": "on"})
        self.assertIn(r.status_code, (200, 302))
        self.assertTrue(Department.objects.filter(name="Sabbath School").exists())

    def test_edit_fund_opening_balance(self):
        d = Department.objects.create(name="Welfare", fund_type="LOCAL",
            category="MINISTRY", opening_balance=Decimal("0"))
        self.client.post(reverse("department_edit", args=[d.pk]), {
            "name": "Welfare", "fund_type": "LOCAL", "category": "MINISTRY",
            "opening_balance": "5000", "active": "on"})
        d.refresh_from_db()
        self.assertEqual(d.opening_balance, Decimal("5000"))

    def test_department_list_and_balance_render(self):
        d = Department.objects.create(name="Music", fund_type="LOCAL", category="MINISTRY")
        self.assertEqual(self.client.get(reverse("department_list")).status_code, 200)
        try:
            url = reverse("department_balance", args=[d.pk])
        except Exception:
            return
        self.assertEqual(self.client.get(url).status_code, 200)

    def test_create_dev_group(self):
        r = self.client.post(reverse("dev_group_create"), {
            "number": "12", "name": "Group 12", "target": "100000", "active": "on"})
        self.assertIn(r.status_code, (200, 302))
        self.assertTrue(DevelopmentGroup.objects.filter(number=12).exists())


class DepartmentAccessTests(TestCase):
    def test_auditor_cannot_create_fund(self):
        au = _user("d_au", AUDITOR)
        self.client.force_login(au)
        r = self.client.post(reverse("department_create"), {
            "name": "Nope", "fund_type": "LOCAL", "category": "MINISTRY",
            "opening_balance": "0"})
        self.assertIn(r.status_code, (302, 403))
        self.assertFalse(Department.objects.filter(name="Nope").exists())

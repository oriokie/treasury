"""Transactions & expenses UI (v2.89) — mastheads on the two busiest lists and
the grouped expense form.

Pins the contract: both lists open with the `.rpt-mast` masthead (restoring a
printed header — the old ws-head is hidden in print CSS); the expense form is
grouped (Amount & date / What it was for / Paid to & how) with the amount set
prominently; and the "Other details" fallback group guarantees a field added to
ExpenseForm later renders somewhere instead of silently vanishing (the frozen
allowlist trap, recommendation #74a).
"""
import datetime as dt
from decimal import Decimal

from django import forms
from django.contrib.auth.models import Group, User
from django.test import TestCase

from core.roles import TREASURER


class BusyPagesUiTests(TestCase):
    def setUp(self):
        self.treasurer = User.objects.create_user("t_busy", password="x")
        self.treasurer.groups.add(Group.objects.get_or_create(name=TREASURER)[0])
        self.client.force_login(self.treasurer)

    def test_transactions_masthead(self):
        h = self.client.get("/transactions/").content.decode()
        self.assertIn("rpt-mast", h)
        self.assertIn('class="rule"', h)
        # the page's own strengths kept intact
        self.assertIn("tx-quicktabs", h)
        self.assertIn("ws-summary", h)

    def test_expenses_masthead(self):
        h = self.client.get("/expenses/").content.decode()
        self.assertIn("rpt-mast", h)
        self.assertIn("ws-summary", h)

    def test_expense_form_groups(self):
        h = self.client.get("/expenses/new/").content.decode()
        for group in ("Amount &amp; date", "What it was for", "Paid to &amp; how"):
            self.assertIn(group, h)
        self.assertIn("xf-amount", h)          # amount set prominently
        self.assertIn("deptSearch", h)         # fund autocomplete intact
        self.assertIn("xf-fallback", h)        # safety net present

    def test_every_form_field_renders_somewhere(self):
        """The anti-#74a guarantee: every visible ExpenseForm field appears on
        the page — none is silently dropped by the group allowlists."""
        from cashbook.forms import ExpenseForm
        h = self.client.get("/expenses/new/").content.decode()
        form = ExpenseForm()
        for name, field in form.fields.items():
            if isinstance(field.widget, forms.HiddenInput):
                continue
            self.assertIn(f'name="{name}"', h,
                          f"ExpenseForm field '{name}' is missing from the page")

    def test_fallback_exclusion_list_has_no_holes(self):
        """The fallback group's 'not in' list must be exactly the union of the
        named groups (plus the hidden/custom-row fields). If a group gains a
        field name that isn't added to the exclusion list, it would render
        twice; if a name is excluded but in no group, it would vanish — either
        way this test fails, so the safety net provably has no holes."""
        import pathlib
        import re
        src = pathlib.Path("templates/cashbook/form.html").read_text()
        lists = re.findall(r"f\.name (?:not )?in '([^']+)'", src)
        self.assertGreaterEqual(len(lists), 4)
        *groups, exclusion = lists
        grouped = set()
        for g in groups:
            grouped |= set(g.split())
        self.assertEqual(set(exclusion.split()),
                         grouped | {"department", "budget_line"})

    def test_expense_saves_through_grouped_form(self):
        from departments.models import Department
        from cashbook.models import Expense
        dept = Department.objects.create(
            name="UI Test Fund", slug="ui-test-fund",
            fund_type=Department.FundType.LOCAL, active=True,
            show_in_expenses=True)
        data = {"date": dt.date.today().isoformat(), "department": dept.id,
                "description": "Grouped form save", "amount": "120.00",
                "category": "STATIONERY", "expenditure_type": "RECURRENT",
                "claimant": "A Person", "payee": "", "method": "CASH",
                "voucher_no": "", "charge": "", "override_balance": "1"}
        self.client.post("/expenses/new/", data, follow=True)
        e = Expense.objects.filter(description="Grouped form save").first()
        self.assertIsNotNone(e)
        self.assertEqual(e.amount, Decimal("120.00"))

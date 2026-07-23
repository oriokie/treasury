"""Accounting parentheses in the formal statements (v2.98, rec #118).

The four statements Edwin named — Income & Expenditure, the Income Statement,
Statement of Financial Position, Changes in Net Assets — plus the Trial Balance
present negative figures the accountant's way: "(1,234.50)", not "-1,234.50".
That is `money_acct`, whose parentheses are real characters (export-safe).

These tests pin the decision so a later edit can't silently drop a statement
back to a minus sign, and prove a real negative renders parenthesised end to
end.
"""
import pathlib

from django.contrib.auth.models import Group, User
from django.template import engines
from django.test import Client, TestCase

TEMPLATES = pathlib.Path(__file__).resolve().parent.parent / "templates"

STATEMENTS = [
    "reports/income_expenditure.html",
    "reports/income_statement.html",
    "reports/financial_position.html",
    "reports/changes_in_net_assets.html",
    "ledger/trial_balance.html",
]


class AccountingParenthesesTests(TestCase):
    def test_statements_use_money_acct_not_bare_money(self):
        import re
        offenders = []
        for rel in STATEMENTS:
            s = (TEMPLATES / rel).read_text()
            # a bare |money (not |money_acct) on a figure would print a minus
            if re.search(r"\|money(?!_acct)", s):
                offenders.append(rel)
        self.assertEqual(offenders, [],
                         "formal statement still uses bare |money (minus-sign "
                         "negatives) instead of |money_acct: %s" % offenders)

    def test_negative_renders_parenthesised_end_to_end(self):
        dj = engines["django"]
        t = dj.from_string("{% load treasury_extras %}{{ v|money_acct }}")
        self.assertEqual(t.render({"v": -4210.5}), "(4,210.50)")
        self.assertEqual(t.render({"v": 4210.5}), "4,210.50")

    def test_statements_render(self):
        from core.roles import TREASURER
        u = User.objects.create_user("acct_t", password="x")
        u.groups.add(Group.objects.get_or_create(name=TREASURER)[0])
        c = Client(); c.force_login(u)
        for url in ("/reports/income-expenditure/", "/reports/income-statement/",
                    "/reports/financial-position/",
                    "/reports/changes-in-net-assets/", "/ledger/trial-balance/"):
            self.assertEqual(c.get(url).status_code, 200, url)

"""Under 'require confirmation', confirming auto-allocated split parts must keep
their component funds — the dropdown must not silently re-point them (#8)."""
from decimal import Decimal
import datetime as dt

from django.test import TestCase, Client
from django.contrib.auth.models import User, Group

from departments.models import Department
from giving.models import Transaction, SplitFund, SplitComponent
from statements.models import StatementImport


class SplitConfirmTests(TestCase):
    def setUp(self):
        u = User.objects.create_user("scf", password="x", is_superuser=True)
        u.groups.add(Group.objects.get_or_create(name="Treasurer")[0])
        self.c = Client(); self.c.force_login(u)
        self.enf = Department.objects.create(name="Comb ENF", fund_type="TRUST",
                                             category="TRUST", selectable=False)
        self.lcb = Department.objects.create(name="Comb LCB", fund_type="LOCAL",
                                             category="OFFERING", selectable=False)
        # a selectable fund that would be the dropdown default
        self.other = Department.objects.create(name="13th Sabbath Acct", fund_type="TRUST",
                                               category="TRUST", selectable=True)
        sf = SplitFund.objects.create(name="Combined Offering")
        SplitComponent.objects.create(split_fund=sf, department=self.enf, percent=Decimal("50"))
        SplitComponent.objects.create(split_fund=sf, department=self.lcb, percent=Decimal("50"))
        self.imp = StatementImport.objects.create(filename="x.csv", uploaded_by=u, status="DONE")
        self.t1 = Transaction.objects.create(date=dt.date(2026, 6, 1), channel="BANK",
            direction="CREDIT", amount=Decimal("250"), department=self.enf,
            allocation_status="AUTO", confirmed=False, statement_import=self.imp, core_ref="S0")
        self.t2 = Transaction.objects.create(date=dt.date(2026, 6, 1), channel="BANK",
            direction="CREDIT", amount=Decimal("250"), department=self.lcb,
            allocation_status="AUTO", confirmed=False, statement_import=self.imp, core_ref="S1")

    def test_confirm_all_keeps_component_funds(self):
        # simulate the browser posting the dropdown default (the selectable fund)
        self.c.post(f"/statements/{self.imp.id}/review/", {
            "confirm_all": "1",
            f"dept_{self.t1.id}": str(self.other.id),
            f"dept_{self.t2.id}": str(self.other.id)})
        self.t1.refresh_from_db(); self.t2.refresh_from_db()
        self.assertEqual(self.t1.department_id, self.enf.id)
        self.assertEqual(self.t2.department_id, self.lcb.id)
        self.assertTrue(self.t1.confirmed and self.t2.confirmed)

    def test_review_locks_split_components(self):
        body = self.c.get(f"/statements/{self.imp.id}/review/").content.decode()
        # the component rows are shown as locked text, not as a re-pointing <select>
        self.assertIn("· split", body)

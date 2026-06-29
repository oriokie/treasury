"""#5 department close cascade + historical filtering."""
from decimal import Decimal
from django.test import TestCase, Client
from django.contrib.auth.models import User, Group
from departments.models import Department


class CloseCascadeTests(TestCase):
    def setUp(self):
        self.tr = User.objects.create_user("tr", password="x", is_superuser=True)
        self.tr.groups.add(Group.objects.get_or_create(name="Treasurer")[0])
        self.c = Client(); self.c.force_login(self.tr)
        self.p = Department.objects.create(name="Parent", fund_type="LOCAL",
            category="MINISTRY", opening_balance=0)
        self.s = Department.objects.create(name="Sub", fund_type="LOCAL",
            category="MINISTRY", parent=self.p, opening_balance=0)

    def test_closing_parent_cascades_to_subs(self):
        self.c.post(f"/departments/{self.p.id}/close/", {})
        self.p.refresh_from_db(); self.s.refresh_from_db()
        self.assertEqual(self.p.status, "CLOSED")
        self.assertEqual(self.s.status, "CLOSED")

    def test_main_list_hides_closed(self):
        self.p.status = "CLOSED"; self.p.save()
        body = self.c.get("/departments/").content.decode()
        self.assertNotIn(">Parent<", body)
        hist = self.c.get("/departments/historical/").content.decode()
        self.assertIn("Parent", hist)

    def test_reopen_sub_reopens_parent(self):
        self.p.status = "CLOSED"; self.p.save()
        self.s.status = "CLOSED"; self.s.save()
        self.c.post(f"/departments/{self.s.id}/reopen/", {})
        self.p.refresh_from_db(); self.s.refresh_from_db()
        self.assertEqual(self.p.status, "ACTIVE")
        self.assertEqual(self.s.status, "ACTIVE")

    def test_subaccount_can_be_closed(self):
        body = self.c.get("/departments/").content.decode()
        self.assertIn(f"/departments/{self.s.id}/close/", body)


class FundMembersTests(TestCase):
    def test_by_member_page_and_export(self):
        tr = User.objects.create_user("trm", password="x", is_superuser=True)
        tr.groups.add(Group.objects.get_or_create(name="Treasurer")[0])
        d = Department.objects.create(name="Giving", fund_type="LOCAL", category="OFFERING")
        c = Client(); c.force_login(tr)
        self.assertEqual(c.get(f"/reports/fund/{d.id}/members/").status_code, 200)
        self.assertEqual(c.get(f"/reports/fund/{d.id}/members/?export=xlsx").status_code, 200)


class ReconExportTests(TestCase):
    def test_recon_excel_and_print_head(self):
        import datetime as dt
        from statements.models import BankReconciliation
        tr = User.objects.create_user("trr", password="x", is_superuser=True)
        tr.groups.add(Group.objects.get_or_create(name="Treasurer")[0])
        rec = BankReconciliation.objects.create(statement_date=dt.date(2026, 6, 30),
            bank_balance=Decimal("50000"), created_by=tr)
        c = Client(); c.force_login(tr)
        page = c.get(f"/reconciliations/{rec.id}/").content.decode()
        self.assertIn("recon-print-head", page)
        self.assertEqual(c.get(f"/reconciliations/{rec.id}/?export=xlsx").status_code, 200)


class GlobalSearchTests(TestCase):
    def test_search_finds_records(self):
        import datetime as dt
        from cashbook.models import StaffAdvance
        tr = User.objects.create_user("trs", password="x", is_superuser=True)
        tr.groups.add(Group.objects.get_or_create(name="Treasurer")[0])
        d = Department.objects.create(name="Youthful", fund_type="LOCAL", category="MINISTRY")
        StaffAdvance.objects.create(staff_name="Zacharia Find", department=d,
            amount=Decimal("1000"), date_issued=dt.date(2026, 6, 5), purpose="x",
            issued_by=tr)
        c = Client(); c.force_login(tr)
        import json
        r = json.loads(c.get("/search/?q=Zacharia").content)
        self.assertTrue(any("Zacharia" in x["label"] for x in r["results"]))
        self.assertEqual(json.loads(c.get("/search/?q=z").content)["results"], [])

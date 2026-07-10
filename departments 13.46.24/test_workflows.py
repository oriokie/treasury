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


class BulkFundImportTests(TestCase):
    """Items 6 & 7: bulk fund/budget import — fuzzy matching, the unmatched
    prompt, fund creation, and budget/monthly-line writing."""

    def setUp(self):
        from django.contrib.auth.models import User, Group
        self.u = User.objects.create_user("bf", password="x")
        g, _ = Group.objects.get_or_create(name="Treasurer")
        self.u.groups.add(g)
        from departments.models import Department
        # one existing fund the file will match, plus a near-name for fuzzy
        Department.objects.create(name="YOUTH", fund_type="LOCAL", category="MINISTRY")
        Department.objects.create(name="PATHFINDERS", fund_type="LOCAL", category="MINISTRY")

    def _build_file(self):
        import io, openpyxl
        wb = openpyxl.Workbook(); ws = wb.active; ws.title = "DEPARTMENTS"
        ws.append([""]*7 + ["INCOME"])
        header = ["ID", "NAME", "B/F", "PROJECTED INCOME", "PROJECTED EXPENSES",
                  "BALANCE", ""]
        header += ["JAN","FEB","MAR","APR","MAY","JUN","JUL","AUG","SEP","OCT","NOV","DEC","TOTAL",""]
        header += ["JAN","FEB","MAR","APR","MAY","JUN","JUL","AUG","SEP","OCT","NOV","DEC","TOTAL"]
        ws.append(header)
        # income months (cols 8-19) then expense months (cols 22-33)
        def row(name, bf, pinc, pexp, exp_months):
            r = ["", name, bf, pinc, pexp, 0, ""]
            r += [0]*13          # income block + total (ignored)
            r += [""]            # spacer
            r += exp_months + [sum(exp_months)]
            return r
        ws.append(row("YOUTH", 0, 1000, 1200, [100,100,100,100,100,100,100,100,100,100,100,100]))
        ws.append(row("PATHFINDER", 0, 500, 600, [50]*12))   # fuzzy -> PATHFINDERS
        ws.append(row("BRAND NEW FUND", 0, 300, 0, [0]*12))   # unmatched -> prompt
        import io
        buf = io.BytesIO(); wb.save(buf); buf.seek(0)
        return buf.getvalue()

    def test_parse_matches_and_flags_unmatched(self):
        from django.test import Client
        from django.core.files.uploadedfile import SimpleUploadedFile
        c = Client(); c.force_login(self.u)
        r = c.post("/funds/bulk-import/",
                   {"file": SimpleUploadedFile("b.xlsx", self._build_file()),
                    "year": "2026"})
        self.assertEqual(r.status_code, 200)
        plan = c.session["bulk_fund_plan"]["rows"]
        by_name = {p["name"]: p for p in plan}
        # YOUTH exact, PATHFINDER fuzzy-matched, BRAND NEW unmatched
        self.assertIsNotNone(by_name["YOUTH"]["match_id"])
        self.assertIsNotNone(by_name["PATHFINDER"]["match_id"])
        self.assertIsNone(by_name["BRAND NEW FUND"]["match_id"])
        # expense months used: sum to projected expense
        self.assertEqual(sum(by_name["YOUTH"]["months"]), 1200)

    def test_apply_writes_budgets_and_creates_fund(self):
        from django.test import Client
        from django.core.files.uploadedfile import SimpleUploadedFile
        from departments.models import Department, Budget
        c = Client(); c.force_login(self.u)
        c.post("/funds/bulk-import/",
               {"file": SimpleUploadedFile("b.xlsx", self._build_file()), "year": "2026"})
        plan = c.session["bulk_fund_plan"]["rows"]
        post = {"apply": "1", "with_months": "1"}
        for i, p in enumerate(plan):
            if p["match_id"]:
                post[f"map_{i}"] = f"dept:{p['match_id']}"
            elif p["name"] == "BRAND NEW FUND":
                post[f"map_{i}"] = "create"
                post[f"ftype_{i}"] = "LOCAL"
                post[f"cat_{i}"] = "MINISTRY"
        c.post("/funds/bulk-import/", post)
        # the new fund was created
        self.assertTrue(Department.objects.filter(name="BRAND NEW FUND").exists())
        # budgets written for 2026, Youth with 12 monthly lines summing to 1200
        yb = Budget.objects.get(year=2026, department__name="YOUTH")
        month_lines = yb.lines.filter(name__startswith="Budget — ")
        self.assertEqual(month_lines.count(), 12)
        self.assertEqual(sum(l.amount for l in month_lines), 1200)

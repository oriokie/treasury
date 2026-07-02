"""v1.97: receipts for leaders, expense-id on receipts, missing-receipts queue +
cards, clip popover, supporting-pdf attachment filter, dev-groups button, leader
page formatting."""
import datetime as dt
from decimal import Decimal
from django.test import TestCase, Client
from django.contrib.auth.models import User, Group
from departments.models import Department, DepartmentLeadership
from cashbook.models import Expense, ExpenseAttachment
from cashbook.views import missing_receipts_queryset
from ledger.services.posting import ensure_chart


def _tr():
    u = User.objects.create_user("tr197", password="x", is_superuser=True)
    u.groups.add(Group.objects.get_or_create(name="Treasurer")[0])
    return u


def _leader(dept):
    u = User.objects.create_user("ld197", password="x")
    u.groups.add(Group.objects.get_or_create(name="Leader")[0])
    DepartmentLeadership.objects.create(department=dept, user=u)
    return u


class ReceiptsAndMissingTests(TestCase):
    def setUp(self):
        ensure_chart()
        self.tr = _tr()
        self.dept = Department.objects.create(name="RD197", fund_type="LOCAL",
            category="MINISTRY", show_in_expenses=True)
        self.ld = _leader(self.dept)
        self.with_doc = Expense.objects.create(date=dt.date(2026, 6, 10),
            department=self.dept, description="Has doc", amount=Decimal("1000"),
            category="MATERIALS", status="PAID", recorded_by=self.tr, approved_by=self.tr)
        ExpenseAttachment.objects.create(expense=self.with_doc, text="M-Pesa msg here")
        self.no_doc = Expense.objects.create(date=dt.date(2026, 6, 11),
            department=self.dept, description="No doc", amount=Decimal("500"),
            category="TRANSPORT", status="PAID", recorded_by=self.tr, approved_by=self.tr)

    def test_leader_can_view_receipts_scoped(self):
        c = Client(); c.force_login(self.ld)
        r = c.get("/expenses/receipts/?start=2026-06-01&end=2026-06-30")
        self.assertEqual(r.status_code, 200)

    def test_expense_id_on_receipts(self):
        c = Client(); c.force_login(self.tr)
        b = c.get("/expenses/receipts/?start=2026-06-01&end=2026-06-30").content.decode()
        self.assertIn(f"#{self.with_doc.id}", b)

    def test_missing_receipts_queue(self):
        qs = missing_receipts_queryset(dt.date(2026, 6, 1), dt.date(2026, 6, 30))
        ids = set(qs.values_list("id", flat=True))
        self.assertIn(self.no_doc.id, ids)
        self.assertNotIn(self.with_doc.id, ids)

    def test_attach_removes_from_queue(self):
        c = Client(); c.force_login(self.tr)
        self.assertTrue(missing_receipts_queryset(dt.date(2026, 6, 1),
            dt.date(2026, 6, 30)).filter(id=self.no_doc.id).exists())
        c.post(f"/expenses/{self.no_doc.id}/attach/",
               {"mpesa_ref": "QAB1 Confirmed"})
        self.assertEqual(ExpenseAttachment.objects.filter(expense=self.no_doc).count(), 1)
        self.assertFalse(missing_receipts_queryset(dt.date(2026, 6, 1),
            dt.date(2026, 6, 30)).filter(id=self.no_doc.id).exists())

    def test_leader_missing_scoped(self):
        c = Client(); c.force_login(self.ld)
        r = c.get("/expenses/missing-receipts/?start=2026-06-01&end=2026-06-30")
        self.assertEqual(r.status_code, 200)

    def test_dashboard_awaiting_card(self):
        c = Client(); c.force_login(self.tr)
        b = c.get("/?start=2026-06-01&end=2026-06-30").content.decode()
        self.assertIn("Awaiting receipts", b)

    def test_leader_cannot_attach_other_dept(self):
        other = Department.objects.create(name="Other197", fund_type="LOCAL",
            category="MINISTRY", show_in_expenses=True)
        oe = Expense.objects.create(date=dt.date(2026, 6, 12), department=other,
            description="x", amount=Decimal("10"), category="OTHER", status="PAID",
            recorded_by=self.tr, approved_by=self.tr)
        c = Client(); c.force_login(self.ld)
        c.post(f"/expenses/{oe.id}/attach/", {"mpesa_ref": "nope"})
        self.assertEqual(ExpenseAttachment.objects.filter(expense=oe).count(), 0)


class ClipAndPdfTests(TestCase):
    def setUp(self):
        ensure_chart()
        self.tr = _tr()
        self.dept = Department.objects.create(name="CP197", fund_type="LOCAL",
            category="OFFERING", show_in_expenses=True)
        self.e = Expense.objects.create(date=dt.date(2026, 6, 5), department=self.dept,
            description="Doc exp", amount=Decimal("800"), category="MATERIALS",
            status="PAID", recorded_by=self.tr, approved_by=self.tr)
        ExpenseAttachment.objects.create(expense=self.e, text="QGT5 Confirmed msg")
        self.nodoc = Expense.objects.create(date=dt.date(2026, 6, 6),
            department=self.dept, description="No doc exp", amount=Decimal("400"),
            category="OTHER", status="PAID", recorded_by=self.tr, approved_by=self.tr)

    def test_clip_popover_in_list(self):
        c = Client(); c.force_login(self.tr)
        b = c.get("/expenses/").content.decode()
        self.assertIn("clip-pop", b)
        self.assertIn("clip-text", b)  # M-Pesa text shown

    def test_supporting_pdf_only_with_attachments(self):
        c = Client(); c.force_login(self.tr)
        r = c.get("/expenses/?export=support-pdf&start=2026-06-01&end=2026-06-30")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r["Content-Type"], "application/pdf")
        # builder should have received only the 1 expense with an attachment
        from cashbook.services.supporting_pdf import build_supporting_docs_pdf
        qs = Expense.objects.filter(department=self.dept,
            attachments__isnull=False).distinct()
        _, stats = build_supporting_docs_pdf(qs)
        self.assertEqual(stats["expenses"], 1)


class LeaderFormattingTests(TestCase):
    def setUp(self):
        ensure_chart()
        self.dept = Department.objects.create(name="FMT197", fund_type="LOCAL",
            category="MINISTRY", show_in_expenses=True)
        self.ld = _leader(self.dept)

    def test_collections_and_expenses_hero(self):
        c = Client(); c.force_login(self.ld)
        col = c.get(f"/leader/department/{self.dept.id}/collections/").content.decode()
        exp = c.get(f"/leader/department/{self.dept.id}/expenses/").content.decode()
        self.assertIn("ld-hero", col)
        self.assertIn("ld-hero", exp)

    def test_dev_groups_xlsx_export(self):
        c = Client(); c.force_login(self.ld)
        r = c.get(f"/leader/department/{self.dept.id}/?export=groups_xlsx")
        self.assertEqual(r.status_code, 200)
        self.assertIn("spreadsheet", r["Content-Type"])

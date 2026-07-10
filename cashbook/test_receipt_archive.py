"""Receipt archive: receipts filed by incurred year/month, archive page + ZIP (#2)."""
import datetime as dt
from decimal import Decimal
from django.test import TestCase, Client
from django.contrib.auth.models import User, Group
from django.core.files.uploadedfile import SimpleUploadedFile
from departments.models import Department
from cashbook.models import Expense, ExpenseAttachment, expense_receipt_path


class ReceiptArchiveTests(TestCase):
    def setUp(self):
        self.u = User.objects.create_user("ra", password="x", is_superuser=True)
        self.u.groups.add(Group.objects.get_or_create(name="Treasurer")[0])
        self.c = Client(); self.c.force_login(self.u)
        self.d = Department.objects.create(name="LCB x", fund_type="LOCAL",
            category="OFFERING", show_in_expenses=True)

    def test_upload_path_uses_incurred_date(self):
        exp = Expense.objects.create(date=dt.date(2025, 3, 15), department=self.d,
            description="t", amount=Decimal("100"), category="OTHER",
            status="PAID", recorded_by=self.u)
        att = ExpenseAttachment(expense=exp)
        self.assertEqual(expense_receipt_path(att, "r.png"),
                         "receipts/expenses/2025/03/r.png")

    def test_archive_lists_and_zips(self):
        exp = Expense.objects.create(date=dt.date(2025, 3, 15), department=self.d,
            description="Audit doc", amount=Decimal("1200"), category="OTHER",
            status="PAID", recorded_by=self.u)
        att = ExpenseAttachment(expense=exp, label="r1")
        att.file.save("r.png", SimpleUploadedFile("r.png", b"\x89PNG\r\nx",
                      content_type="image/png"), save=False)
        att.save()
        b = self.c.get("/expenses/receipts/?start=2025-01-01&end=2025-12-31").content.decode()
        self.assertIn("Audit doc", b)
        self.assertIn("March 2025", b)
        z = self.c.get("/expenses/receipts/?start=2025-01-01&end=2025-12-31&download=zip")
        self.assertEqual(z["Content-Type"], "application/zip")
        self.assertGreater(len(z.content), 100)

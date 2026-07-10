"""Expense Register 'Supporting documents' PDF export."""
import datetime as dt
from decimal import Decimal
from django.test import TestCase, Client
from django.contrib.auth.models import User, Group
from django.core.files.base import ContentFile
from departments.models import Department
from cashbook.models import Expense, ExpenseAttachment
from ledger.services.posting import ensure_chart

_PNG = bytes.fromhex(
    "89504e470d0a1a0a0000000d494844520000000100000001080600000"
    "01f15c4890000000d49444154789c6360000002000100ffff0300000"
    "6000557bfabd40000000049454e44ae426082")


def _tr():
    u = User.objects.create_user("tr_pdf", password="x", is_superuser=True)
    u.groups.add(Group.objects.get_or_create(name="Treasurer")[0])
    return u


class SupportingPdfTests(TestCase):
    def setUp(self):
        ensure_chart()
        self.tr = _tr(); self.c = Client(); self.c.force_login(self.tr)
        self.d = Department.objects.create(name="LCB PDF", fund_type="LOCAL",
            category="OFFERING", show_in_expenses=True)
        self.e = Expense.objects.create(date=dt.date(2026, 6, 15), department=self.d,
            description="Mic", amount=Decimal("1200"), category="MATERIALS",
            status="PAID", recorded_by=self.tr, approved_by=self.tr,
            claimant="Jane", voucher_no="V-9")
        ExpenseAttachment.objects.create(expense=self.e,
            file=ContentFile(_PNG, name="r.png"))
        ExpenseAttachment.objects.create(expense=self.e, text="WhatsApp receipt")

    def test_pdf_generated(self):
        r = self.c.get("/expenses/?export=support-pdf&status=PAID")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r["Content-Type"], "application/pdf")
        self.assertTrue(r.content[:5] == b"%PDF-")
        self.assertGreater(len(r.content), 1000)

    def test_button_on_register(self):
        b = self.c.get("/expenses/").content.decode()
        self.assertIn("Supporting documents", b)
        self.assertIn("export=support-pdf", b)

    def test_graceful_when_no_matches(self):
        r = self.c.get("/expenses/?export=support-pdf&q=nonexistentxyz", follow=True)
        self.assertEqual(r.status_code, 200)  # redirected back, no crash

    def test_builder_handles_unsupported_attachment(self):
        from cashbook.services.supporting_pdf import build_supporting_docs_pdf
        data, stats = build_supporting_docs_pdf(
            Expense.objects.filter(id=self.e.id))
        self.assertTrue(data[:5] == b"%PDF-")
        self.assertEqual(stats["expenses"], 1)


class ThemeDefaultTests(TestCase):
    def test_defaults_are_light_and_system(self):
        from core.models import UserPreference
        self.assertEqual(UserPreference._meta.get_field("theme").default, "LIGHT")
        self.assertEqual(
            UserPreference._meta.get_field("font_family").default, "SYSTEM")

    def test_login_page_light_system(self):
        c = Client()
        b = c.get("/accounts/login/").content.decode()
        self.assertIn('data-theme="light"', b)
        self.assertIn('data-fontfamily="system"', b)

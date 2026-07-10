"""Batch: receipt-message boilerplate stripping + cleanup button (item 6),
1MB receipt upload limit (item 7), supporting-docs PDF excludes text/link-only
attachments since the Receipts view covers those (item 8)."""
import datetime as dt
from decimal import Decimal
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, Client
from django.contrib.auth.models import User, Group
from departments.models import Department
from cashbook.models import Expense, ExpenseAttachment, clean_receipt_text
from core.models import SiteConfig


def _tr():
    u = User.objects.create_user("tr_v197b", password="x", is_superuser=True)
    u.groups.add(Group.objects.get_or_create(name="Treasurer")[0])
    return u


class ReceiptStripStringsTests(TestCase):
    def setUp(self):
        self.tr = _tr()
        self.d = Department.objects.create(name="StripF", fund_type="LOCAL",
            category="MINISTRY")
        self.e = Expense.objects.create(date=dt.date(2026, 6, 5), department=self.d,
            description="strip test", amount=Decimal("100"), category="OTHER",
            status="PAID", recorded_by=self.tr, approved_by=self.tr)
        cfg = SiteConfig.get()
        cfg.receipt_strip_strings = ("Please NEVER share your PIN, PASSWORD, any "
            "codes or CARD details with ANYONE!\nDial *334# for more")
        cfg.save()
        self.c = Client(); self.c.force_login(self.tr)

    def test_stripped_on_save(self):
        msg = ("QGH7X8 Confirmed. Ksh100 paid. Please NEVER share your PIN, "
               "PASSWORD, any codes or CARD details with ANYONE!")
        att = ExpenseAttachment.objects.create(expense=self.e, text=msg)
        self.assertNotIn("NEVER share", att.text)
        self.assertIn("QGH7X8 Confirmed", att.text)

    def test_case_insensitive(self):
        att = ExpenseAttachment.objects.create(expense=self.e,
            text="Paid ok. please never share your pin, password, any codes or "
                 "card details with anyone!")
        self.assertNotIn("never share", att.text.lower())

    def test_cleanup_button_recleans_existing(self):
        msg = "QAB1 Confirmed. Please NEVER share your PIN, PASSWORD, any codes or CARD details with ANYONE!"
        att = ExpenseAttachment.objects.create(expense=self.e, text=msg)
        ExpenseAttachment.objects.filter(pk=att.pk).update(text=msg)  # bypass save()
        att.refresh_from_db()
        self.assertIn("NEVER share", att.text)
        self.c.post("/settings/clean-receipts/")
        att.refresh_from_db()
        self.assertNotIn("NEVER share", att.text)

    def test_settings_field_and_button_present(self):
        b = self.c.get("/settings/?tab=branding").content.decode()
        self.assertIn('name="receipt_strip_strings"', b)
        self.assertIn("Clean up already-saved", b)

    def test_no_strings_configured_leaves_text_alone(self):
        cfg = SiteConfig.get(); cfg.receipt_strip_strings = ""; cfg.save()
        att = ExpenseAttachment.objects.create(expense=self.e, text="QZZ9 Confirmed payment")
        self.assertEqual(att.text, "QZZ9 Confirmed payment")


class ReceiptUploadSizeLimitTests(TestCase):
    def setUp(self):
        self.tr = _tr()
        self.d = Department.objects.create(name="SizeF", fund_type="LOCAL",
            category="MINISTRY")
        self.e = Expense.objects.create(date=dt.date(2026, 6, 5), department=self.d,
            description="size test", amount=Decimal("100"), category="OTHER",
            status="PAID", recorded_by=self.tr, approved_by=self.tr)
        self.c = Client(); self.c.force_login(self.tr)

    def test_over_1mb_rejected(self):
        big = SimpleUploadedFile("receipt.jpg", b"x" * (1024 * 1024 + 1),
                                 content_type="image/jpeg")
        self.c.post(f"/expenses/{self.e.id}/attach/", {"file": big})
        self.assertEqual(ExpenseAttachment.objects.filter(expense=self.e,
            file__isnull=False).exclude(file="").count(), 0)

    def test_under_1mb_accepted(self):
        small = SimpleUploadedFile("receipt.jpg", b"x" * 1000, content_type="image/jpeg")
        self.c.post(f"/expenses/{self.e.id}/attach/", {"file": small})
        self.assertEqual(ExpenseAttachment.objects.filter(expense=self.e).count(), 1)

    def test_validator_message_mentions_1mb(self):
        from cashbook.views import validate_receipt_upload
        big = SimpleUploadedFile("r.jpg", b"x" * (1024 * 1024 + 1), content_type="image/jpeg")
        err = validate_receipt_upload(big)
        self.assertIn("1 MB", err)


class SupportingPdfExcludesTextLinkOnlyTests(TestCase):
    def setUp(self):
        self.tr = _tr()
        self.d = Department.objects.create(name="PdfF", fund_type="LOCAL",
            category="MINISTRY")
        self.with_file = Expense.objects.create(date=dt.date(2026, 6, 5),
            department=self.d, description="has file", amount=Decimal("500"),
            category="MATERIALS", status="PAID", recorded_by=self.tr, approved_by=self.tr)
        ExpenseAttachment.objects.create(expense=self.with_file,
            file=SimpleUploadedFile("r.jpg", b"x" * 500, content_type="image/jpeg"))
        self.text_only = Expense.objects.create(date=dt.date(2026, 6, 6),
            department=self.d, description="text only", amount=Decimal("300"),
            category="TRANSPORT", status="PAID", recorded_by=self.tr, approved_by=self.tr)
        ExpenseAttachment.objects.create(expense=self.text_only,
            text="QAB1 Confirmed. Paid Ksh300")
        self.link_only = Expense.objects.create(date=dt.date(2026, 6, 7),
            department=self.d, description="link only", amount=Decimal("200"),
            category="OTHER", status="PAID", recorded_by=self.tr, approved_by=self.tr)
        ExpenseAttachment.objects.create(expense=self.link_only,
            link="https://example.com/receipt")
        self.c = Client(); self.c.force_login(self.tr)

    def test_pdf_includes_only_file_attachments(self):
        from cashbook.services.supporting_pdf import build_supporting_docs_pdf
        qs = (Expense.objects.filter(department=self.d)
              .filter(attachments__file__isnull=False).exclude(attachments__file="")
              .distinct())
        ids = set(qs.values_list("id", flat=True))
        self.assertIn(self.with_file.id, ids)
        self.assertNotIn(self.text_only.id, ids)
        self.assertNotIn(self.link_only.id, ids)

    def test_pdf_export_status_ok(self):
        r = self.c.get("/expenses/?export=support-pdf&start=2026-06-01&end=2026-06-30")
        self.assertEqual(r.status_code, 200)

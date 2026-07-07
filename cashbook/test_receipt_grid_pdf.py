"""Compact, backend-generated PDF for the receipt archive: a grid layout
(several receipt thumbnails per page, like a contact sheet) instead of
relying on browser print-to-PDF, whose page count and layout vary
unpredictably by browser/OS. Also guards against a real pagination bug found
during development: eagerly starting a new page right after placing the
LAST item that exactly fills the grid produced a pointless blank trailing
page — fixed to only start a new page lazily, right before the next item
that actually needs it."""
import datetime as dt
import io
from decimal import Decimal
from collections import OrderedDict
from django.test import TestCase, Client
from django.contrib.auth.models import User, Group
from django.core.files.uploadedfile import SimpleUploadedFile
from departments.models import Department
from cashbook.models import Expense, ExpenseAttachment


def _tr():
    u = User.objects.create_user("tr_receiptpdf", password="x", is_superuser=True)
    u.groups.add(Group.objects.get_or_create(name="Treasurer")[0])
    return u


def _image_attachment(dept, tr, tag, date=None):
    from PIL import Image
    exp = Expense.objects.create(date=date or dt.date(2026, 6, 1), department=dept,
        description=tag, amount=Decimal("100"), category="OTHER",
        status="PAID", recorded_by=tr, approved_by=tr)
    img = Image.new("RGB", (400, 600), color=(200, 220, 210))
    buf = io.BytesIO(); img.save(buf, format="JPEG"); buf.seek(0)
    f = SimpleUploadedFile(f"{tag}.jpg", buf.read(), content_type="image/jpeg")
    return ExpenseAttachment.objects.create(expense=exp, file=f)


class ReceiptGridPdfLayoutTests(TestCase):
    """Direct tests of the layout function's pagination math."""
    def setUp(self):
        self.tr = _tr()
        self.d = Department.objects.create(name="GridPdfFund", fund_type="LOCAL",
            category="MINISTRY")

    def _page_count(self, groups):
        from cashbook.services.supporting_pdf import build_receipt_grid_pdf
        from pypdf import PdfReader
        data, stats = build_receipt_grid_pdf(groups, church="Test", currency="KES")
        return len(PdfReader(io.BytesIO(data)).pages), stats

    def test_grid_exactly_full_produces_no_blank_trailing_page(self):
        atts = [_image_attachment(self.d, self.tr, f"exact{i}") for i in range(9)]
        groups = OrderedDict(); groups["June 2026"] = atts
        pages, stats = self._page_count(groups)
        self.assertEqual(pages, 1)
        self.assertEqual(stats["documents"], 9)

    def test_one_more_than_grid_capacity_overflows_by_exactly_one_page(self):
        atts = [_image_attachment(self.d, self.tr, f"over{i}") for i in range(10)]
        groups = OrderedDict(); groups["June 2026"] = atts
        pages, _ = self._page_count(groups)
        self.assertEqual(pages, 2)

    def test_under_capacity_fits_one_page(self):
        atts = [_image_attachment(self.d, self.tr, f"under{i}") for i in range(5)]
        groups = OrderedDict(); groups["June 2026"] = atts
        pages, _ = self._page_count(groups)
        self.assertEqual(pages, 1)

    def test_multiple_months_no_forced_page_break_per_month(self):
        groups = OrderedDict()
        groups["May 2026"] = [_image_attachment(self.d, self.tr, f"may{i}") for i in range(2)]
        groups["June 2026"] = [_image_attachment(self.d, self.tr, f"jun{i}") for i in range(2)]
        pages, stats = self._page_count(groups)
        # 4 small documents across two months must not need 2 pages just
        # because they're in different months
        self.assertEqual(pages, 1)
        self.assertEqual(stats["documents"], 4)

    def test_text_only_attachment_gets_a_placeholder_not_dropped(self):
        exp = Expense.objects.create(date=dt.date(2026, 6, 1), department=self.d,
            description="text note", amount=Decimal("50"), category="OTHER",
            status="PAID", recorded_by=self.tr, approved_by=self.tr)
        att = ExpenseAttachment.objects.create(expense=exp, text="M-Pesa msg")
        groups = OrderedDict(); groups["June 2026"] = [att]
        pages, stats = self._page_count(groups)
        self.assertEqual(pages, 1)
        self.assertEqual(stats["documents"], 1)
        self.assertEqual(stats["other"], 1)
        self.assertEqual(stats["images"], 0)

    def test_empty_groups_still_produces_a_valid_single_page_pdf(self):
        pages, stats = self._page_count(OrderedDict())
        self.assertEqual(pages, 1)
        self.assertEqual(stats["documents"], 0)


class ReceiptGridPdfViewTests(TestCase):
    def setUp(self):
        self.tr = _tr()
        self.d = Department.objects.create(name="GridPdfViewFund", fund_type="LOCAL",
            category="MINISTRY")
        self.c = Client(); self.c.force_login(self.tr)

    def test_pdf_export_returns_pdf(self):
        _image_attachment(self.d, self.tr, "viewtest1")
        r = self.c.get("/expenses/receipts/?export=pdf&start=2026-06-01&end=2026-06-30")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r["Content-Type"], "application/pdf")

    def test_pdf_export_no_receipts_redirects_with_message(self):
        r = self.c.get("/expenses/receipts/?export=pdf&start=2020-01-01&end=2020-01-31",
                       follow=True)
        self.assertEqual(r.status_code, 200)

    def test_html_page_still_has_pdf_download_button(self):
        b = self.c.get("/expenses/receipts/").content.decode()
        self.assertIn("export=pdf", b)
        self.assertIn("Download PDF", b)

    def test_zip_download_still_works_unaffected(self):
        _image_attachment(self.d, self.tr, "ziptest1")
        r = self.c.get("/expenses/receipts/?download=zip&start=2026-06-01&end=2026-06-30")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r["Content-Type"], "application/zip")


class ReceiptArchiveDefaultPeriodTests(TestCase):
    """Bug fix: a fresh visit to the receipt archive (no date/period params
    at all) defaulted to "this month" (parse_period()'s normal fallback),
    which is very often empty — making the page, and the PDF/ZIP downloads
    that depend on the same range, look broken. Defaults to "this year so
    far" instead when nothing explicit is given; an explicit period preset
    or custom date range always takes precedence. Also added the standard
    period-selector UI to the page (it previously had none at all — no way
    to pick a different range without editing the URL by hand)."""
    def setUp(self):
        self.tr = _tr()
        self.d = Department.objects.create(name="DefaultPeriodFund", fund_type="LOCAL",
            category="MINISTRY")
        self.c = Client(); self.c.force_login(self.tr)

    def _make_attachment(self, date):
        return _image_attachment(self.d, self.tr, f"defperiod{date}", date=date)

    def test_fresh_visit_finds_an_earlier_month_in_the_same_year(self):
        self._make_attachment(dt.date(2026, 3, 10))
        b = self.c.get("/expenses/receipts/").content.decode()
        self.assertIn("1 document", b)

    def test_fresh_visit_does_not_find_last_year(self):
        self._make_attachment(dt.date(2025, 3, 10))
        b = self.c.get("/expenses/receipts/").content.decode()
        self.assertIn("0 document", b)

    def test_explicit_period_month_overrides_the_wider_default(self):
        self._make_attachment(dt.date(2026, 3, 10))
        b = self.c.get("/expenses/receipts/?period=month").content.decode()
        self.assertIn("0 document", b)

    def test_explicit_custom_range_overrides_the_wider_default(self):
        self._make_attachment(dt.date(2026, 3, 10))
        b = self.c.get("/expenses/receipts/?start=2026-01-01&end=2026-01-31").content.decode()
        self.assertIn("0 document", b)

    def test_pdf_download_works_with_no_explicit_params(self):
        self._make_attachment(dt.date(2026, 3, 10))
        r = self.c.get("/expenses/receipts/?export=pdf")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r["Content-Type"], "application/pdf")

    def test_zip_download_works_with_no_explicit_params(self):
        self._make_attachment(dt.date(2026, 3, 10))
        r = self.c.get("/expenses/receipts/?download=zip")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r["Content-Type"], "application/zip")

    def test_period_selector_ui_present(self):
        b = self.c.get("/expenses/receipts/").content.decode()
        self.assertIn("This month", b)
        self.assertIn("This quarter", b)
        self.assertIn("This year", b)

    def test_download_links_always_carry_the_resolved_dates(self):
        self._make_attachment(dt.date(2026, 3, 10))
        b = self.c.get("/expenses/receipts/").content.decode()
        self.assertIn("start=2026-01-01", b)
        self.assertIn("export=pdf", b)
        self.assertIn("download=zip", b)


class ReceiptGridPdfNoteContentTests(TestCase):
    """Bug fix: the compact receipt PDF's placeholder for a text/e-receipt-
    link attachment (no image file) only ever showed a generic label ("No
    file — text/e-receipt note") describing that a note existed, never the
    note's actual content — making the PDF useless for exactly the
    attachments it was meant to cover. Now renders the actual text or link
    content, wrapped and truncated (with a trailing "...", not the unicode
    ellipsis character, which reportlab's standard Helvetica font doesn't
    reliably render) to fit the available space. Also fixed a real
    off-by-one found while testing this: the truncation line-count estimate
    didn't account for the label's own line height, so the line carrying
    the "..." marker could be silently discarded by the drawing loop's own
    real-time space check before ever being drawn."""
    def setUp(self):
        self.tr = _tr()
        self.d = Department.objects.create(name="NoteContentFund", fund_type="LOCAL",
            category="MINISTRY")

    def _pdf_text(self, atts):
        from cashbook.services.supporting_pdf import build_receipt_grid_pdf
        data, stats = build_receipt_grid_pdf(OrderedDict([("June 2026", atts)]),
            church="Test", currency="KES")
        from pypdf import PdfReader
        reader = PdfReader(io.BytesIO(data))
        return "".join(page.extract_text() for page in reader.pages), stats

    def _text_attachment(self, text="", link="", tag="note"):
        exp = Expense.objects.create(date=dt.date(2026, 6, 1), department=self.d,
            description=tag, amount=Decimal("100"), category="OTHER",
            status="PAID", recorded_by=self.tr, approved_by=self.tr)
        return ExpenseAttachment.objects.create(expense=exp, text=text, link=link)

    def test_text_note_content_actually_rendered(self):
        att = self._text_attachment(text="M-Pesa confirmation ABC123XYZ from John")
        full_text, stats = self._pdf_text([att])
        self.assertIn("ABC123XYZ", full_text)
        self.assertIn("Text / e-receipt note", full_text)

    def test_link_content_actually_rendered(self):
        att = self._text_attachment(link="https://example.com/receipt/999")
        full_text, stats = self._pdf_text([att])
        self.assertIn("example.com/receipt/999", full_text)
        self.assertIn("E-receipt link", full_text)

    def test_no_content_at_all_shows_no_document_message(self):
        att = self._text_attachment()
        full_text, stats = self._pdf_text([att])
        self.assertIn("No document attached", full_text)

    def test_very_long_note_is_truncated_with_ascii_ellipsis(self):
        att = self._text_attachment(text="Word " * 400)
        full_text, stats = self._pdf_text([att])
        self.assertIn("...", full_text)
        # must not silently disappear - some of the beginning must still show
        self.assertIn("Word", full_text)

    def test_stats_counts_note_only_attachments_as_other(self):
        att = self._text_attachment(text="short note")
        full_text, stats = self._pdf_text([att])
        self.assertEqual(stats["other"], 1)
        self.assertEqual(stats["images"], 0)

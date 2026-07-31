"""Three changes to the receipt archive (/expenses/receipts/):

1. Filters — department, category, status, method and a text search — which
   also carry through to the PDF and ZIP downloads and onto the period-preset
   links, so an export always matches what is on screen.
2. The PDF now prints each expense's narration under its receipt. Without it
   every cell read "#123 · 04 Mar 26" and you had to open the system to learn
   what the money was for.
3. `_wrap_lines` wraps on word boundaries. It used to be a blind character
   slice, so narrations and text notes broke mid-word ("run to Kiam / bu
   district").
"""
import datetime as dt
import io
from decimal import Decimal

from django.contrib.auth.models import Group, User
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase

from cashbook.models import Expense, ExpenseAttachment
from departments.models import Department


def _treasurer(username="rf_tr"):
    u = User.objects.create_user(username, password="x", is_superuser=True)
    u.groups.add(Group.objects.get_or_create(name="Treasurer")[0])
    return u


class _Seed(TestCase):
    def setUp(self):
        self.tr = _treasurer()
        self.client.force_login(self.tr)
        self.fuel = Department.objects.create(name="RfFuel", fund_type="LOCAL",
                                              category="MINISTRY")
        self.music = Department.objects.create(name="RfMusic", fund_type="LOCAL",
                                               category="MINISTRY")

    def _att(self, dept, narration, amount="100", status="PAID", method="CASH",
             category="OTHER", date=dt.date(2026, 3, 4), payee="", image=False):
        exp = Expense.objects.create(
            date=date, department=dept, description=narration,
            amount=Decimal(amount), category=category, method=method,
            status=status, recorded_by=self.tr, approved_by=self.tr, payee=payee)
        if image:
            from PIL import Image
            img = Image.new("RGB", (400, 600), (210, 220, 215))
            buf = io.BytesIO(); img.save(buf, format="JPEG"); buf.seek(0)
            f = SimpleUploadedFile(f"r{exp.id}.jpg", buf.read(),
                                   content_type="image/jpeg")
            return ExpenseAttachment.objects.create(expense=exp, file=f)
        return ExpenseAttachment.objects.create(expense=exp, text=f"note {exp.id}")

    def _get(self, **params):
        params.setdefault("start", "2026-01-01")
        params.setdefault("end", "2026-12-31")
        return self.client.get("/expenses/receipts/", params)


class ReceiptFilterTests(_Seed):
    def setUp(self):
        super().setUp()
        self._att(self.fuel, "Diesel for the church van", amount="500",
                  method="CASH", status="PAID", payee="Total Station")
        self._att(self.music, "New microphone cables", amount="800",
                  method="BANK", status="PENDING", payee="Sound Shop")

    def test_unfiltered_shows_both(self):
        r = self._get()
        self.assertEqual(r.context["count"], 2)
        self.assertFalse(r.context["has_filters"])

    def test_department_filter(self):
        r = self._get(department=self.fuel.id)
        self.assertEqual(r.context["count"], 1)
        self.assertContains(r, "Diesel for the church van")
        self.assertNotContains(r, "New microphone cables")
        self.assertTrue(r.context["has_filters"])

    def test_status_and_method_filters(self):
        self.assertEqual(self._get(status="PENDING").context["count"], 1)
        self.assertEqual(self._get(method="BANK").context["count"], 1)
        self.assertEqual(self._get(method="CASH").context["count"], 1)

    def test_search_matches_narration_and_payee(self):
        self.assertEqual(self._get(q="microphone").context["count"], 1)
        self.assertEqual(self._get(q="Total Station").context["count"], 1)
        self.assertEqual(self._get(q="nothing here").context["count"], 0)

    def test_filters_combine(self):
        self.assertEqual(
            self._get(department=self.fuel.id, method="BANK").context["count"], 0)
        self.assertEqual(
            self._get(department=self.fuel.id, method="CASH").context["count"], 1)

    def test_total_reflects_the_filter(self):
        self.assertEqual(self._get().context["total_amount"], Decimal("1300"))
        self.assertEqual(self._get(department=self.fuel.id)
                         .context["total_amount"], Decimal("500"))

    def test_total_counts_each_expense_once_not_once_per_attachment(self):
        """An expense can carry several receipts; summing per attachment would
        overstate the page (and the PDF header) total."""
        exp = Expense.objects.get(description="Diesel for the church van")
        ExpenseAttachment.objects.create(expense=exp, text="second page of the receipt")
        r = self._get(department=self.fuel.id)
        self.assertEqual(r.context["count"], 2)              # two documents
        self.assertEqual(r.context["total_amount"], Decimal("500"))   # one expense

    def test_a_non_numeric_department_does_not_500(self):
        r = self._get(department="abc")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.context["count"], 2)      # ignored, not applied

    def test_downloads_and_presets_carry_the_filters(self):
        r = self._get(department=self.fuel.id, q="diesel")
        body = r.content.decode()
        self.assertIn(f"department={self.fuel.id}", body)
        self.assertIn("q=diesel", body)
        self.assertIn("export=pdf", body)
        self.assertIn("download=zip", body)
        # and the period presets keep them too. `&` is written as the HTML
        # entity in the href, which is what makes the link valid markup.
        self.assertIn(f"period=month&amp;department={self.fuel.id}", body)
        self.assertIn("q=diesel", body)

    def test_zip_download_respects_the_filter(self):
        import zipfile
        self._att(self.fuel, "Filtered image receipt", image=True)
        self._att(self.music, "Other image receipt", image=True)
        r = self.client.get("/expenses/receipts/", {
            "start": "2026-01-01", "end": "2026-12-31",
            "department": self.fuel.id, "download": "zip"})
        self.assertEqual(r.status_code, 200)
        names = zipfile.ZipFile(io.BytesIO(r.content)).read("index.txt").decode()
        self.assertIn("Filtered image receipt", names)
        self.assertNotIn("Other image receipt", names)


class LeaderScopeStillWinsTests(_Seed):
    """A leader may only ever see funds they lead. The department filter must
    narrow that, never widen it — a hand-typed ?department=<other fund> must
    not become a way around the scoping."""

    def setUp(self):
        super().setUp()
        self._att(self.fuel, "Fuel receipt")
        self._att(self.music, "Music receipt")
        from departments.models import DepartmentLeadership
        self.leader = User.objects.create_user("rf_leader", password="x")
        self.leader.groups.add(Group.objects.get_or_create(name="Leader")[0])
        DepartmentLeadership.objects.create(user=self.leader, department=self.fuel)

    def test_leader_sees_only_their_own_fund(self):
        self.client.force_login(self.leader)
        r = self._get()
        self.assertEqual(r.context["count"], 1)
        self.assertContains(r, "Fuel receipt")

    def test_leader_cannot_reach_another_fund_via_the_filter(self):
        self.client.force_login(self.leader)
        r = self._get(department=self.music.id)
        self.assertEqual(r.context["count"], 0)
        self.assertNotContains(r, "Music receipt")

    def test_the_picker_only_offers_funds_the_leader_leads(self):
        self.client.force_login(self.leader)
        r = self._get()
        offered = [d.name for d in r.context["departments"]]
        self.assertEqual(offered, ["RfFuel"])


class PdfNarrationTests(_Seed):
    """The narration is the point of the redesign — assert it is really in the
    PDF's text layer, not merely passed to the builder."""

    def _pdf_text(self, **params):
        from pypdf import PdfReader
        params.setdefault("start", "2026-01-01")
        params.setdefault("end", "2026-12-31")
        params["export"] = "pdf"
        r = self.client.get("/expenses/receipts/", params)
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r["Content-Type"], "application/pdf")
        reader = PdfReader(io.BytesIO(r.content))
        return "\n".join(p.extract_text() or "" for p in reader.pages)

    def test_narration_is_printed_under_each_receipt(self):
        self._att(self.fuel, "Diesel for the church van", image=True)
        text = self._pdf_text()
        self.assertIn("Diesel", text)
        self.assertIn("church van", text)

    def test_header_states_the_period_and_the_totals(self):
        self._att(self.fuel, "Diesel for the church van", amount="500")
        text = self._pdf_text()
        self.assertIn("Expense Receipts", text)
        self.assertIn("2026", text)
        self.assertIn("500.00", text)

    def test_header_names_the_filter_in_force(self):
        """A filtered bundle must not be mistakable for a complete one."""
        self._att(self.fuel, "Diesel for the church van")
        self._att(self.music, "New microphone cables")
        text = self._pdf_text(department=self.fuel.id)
        self.assertIn("RfFuel", text)
        self.assertIn("Diesel", text)
        self.assertNotIn("microphone", text)

    def test_pdf_only_contains_the_filtered_receipts(self):
        self._att(self.fuel, "Keepme narration")
        self._att(self.music, "Dropme narration")
        text = self._pdf_text(department=self.fuel.id)
        self.assertIn("Keepme", text)
        self.assertNotIn("Dropme", text)


class WrapLinesTests(TestCase):
    def test_wraps_on_word_boundaries(self):
        from cashbook.services.supporting_pdf import _wrap_lines
        lines = _wrap_lines("Fuel and toll for the pastor visitation run to "
                            "Kiambu district", 30)
        for ln in lines:
            self.assertLessEqual(len(ln), 30)
        # no word is split across two lines
        self.assertEqual(" ".join(lines),
                         "Fuel and toll for the pastor visitation run to Kiambu district")

    def test_a_single_overlong_token_still_hard_splits(self):
        """An e-receipt URL has no spaces — it must still be broken rather
        than overflow the cell."""
        from cashbook.services.supporting_pdf import _wrap_lines
        url = "https://receipts.example.com/" + "a" * 80
        lines = _wrap_lines(url, 20)
        for ln in lines:
            self.assertLessEqual(len(ln), 20)
        self.assertEqual("".join(lines), url)

    def test_blank_text_yields_one_empty_line(self):
        from cashbook.services.supporting_pdf import _wrap_lines
        self.assertEqual(_wrap_lines("", 10), [""])
        self.assertEqual(_wrap_lines(None, 10), [""])

    def test_collapses_runs_of_whitespace(self):
        from cashbook.services.supporting_pdf import _wrap_lines
        self.assertEqual(_wrap_lines("a   b\n\nc", 40), ["a b c"])


class MonthLabelOrphanTests(TestCase):
    """A month divider must never be left alone at the foot of a page with its
    receipts overleaf — it needs room for itself AND its first item."""

    def setUp(self):
        self.tr = _treasurer("rf_tr2")
        self.d = Department.objects.create(name="RfOrphan", fund_type="LOCAL",
                                           category="MINISTRY")

    def test_label_moves_to_the_next_page_with_its_first_receipt(self):
        from collections import OrderedDict
        from pypdf import PdfReader
        from cashbook.services.supporting_pdf import build_receipt_grid_pdf
        from PIL import Image

        def tall(tag):
            exp = Expense.objects.create(
                date=dt.date(2026, 6, 1), department=self.d, description=tag,
                amount=Decimal("10"), category="OTHER", status="PAID",
                recorded_by=self.tr, approved_by=self.tr)
            img = Image.new("RGB", (400, 900), (200, 200, 200))
            buf = io.BytesIO(); img.save(buf, format="JPEG"); buf.seek(0)
            return ExpenseAttachment.objects.create(
                expense=exp, file=SimpleUploadedFile(f"{tag}.jpg", buf.read(),
                                                     content_type="image/jpeg"))

        groups = OrderedDict()
        groups["June 2026"] = [tall(f"june{i}") for i in range(6)]
        groups["July 2026"] = [tall("julyfirst")]
        data, _ = build_receipt_grid_pdf(groups, church="T", currency="KES")
        pages = [p.extract_text() or "" for p in PdfReader(io.BytesIO(data)).pages]
        july_pages = [i for i, t in enumerate(pages) if "July 2026" in t]
        self.assertTrue(july_pages, "the July label was never drawn")
        # the label and its only receipt must land on the same page
        self.assertIn("julyfirst", pages[july_pages[0]])

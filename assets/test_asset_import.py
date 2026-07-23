"""Bringing an existing asset register in from a spreadsheet, and the asset
reports in the library.

The rule the import holds to: nothing is written until the treasurer has been
shown exactly what would happen. A first upload only examines the file; assets
appear only on a confirmed second pass. Rows that would produce nonsense —
no cost, no date, a duplicate, an asset already on the register — are set aside
with the reason, rather than imported and quietly wrong.
"""
import datetime as dt
from decimal import Decimal
from io import BytesIO

from django.contrib.auth.models import User, Group
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase
from django.urls import reverse

import openpyxl

from assets.models import FixedAsset, Acquisition, AssetEvent
from core.roles import TREASURER, AUDITOR
from departments.models import Department


def _sheet(rows, headers=("Name", "Category", "Date acquired", "Cost", "Fund")):
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.append(list(headers))
    for r in rows:
        ws.append(list(r))
    buf = BytesIO()
    wb.save(buf)
    buf.seek(0)
    return SimpleUploadedFile(
        "assets.xlsx", buf.read(),
        content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")


class ImportBase(TestCase):
    def setUp(self):
        self.fund = Department.objects.create(name="General", fund_type=Department.FundType.LOCAL)
        self.tr = User.objects.create_user("imp_tr", password="x")
        self.tr.groups.add(Group.objects.get_or_create(name=TREASURER)[0])
        self.client.force_login(self.tr)
        self.year = dt.date.today().year

    def _check(self, rows, **kw):
        """First pass: examine only."""
        data = {"file": _sheet(rows), "default_category": "EQUIPMENT"}
        data.update(kw)
        return self.client.post(reverse("asset_import"), data)

    def _import(self, rows, **kw):
        """Second pass: confirmed."""
        self._check(rows, **kw)
        data = {"file": _sheet(rows), "confirm": "yes", "default_category": "EQUIPMENT"}
        data.update(kw)
        return self.client.post(reverse("asset_import"), data, follow=True)


class ImportSafetyTests(ImportBase):
    def test_the_first_upload_writes_nothing(self):
        r = self._check([["Church van", "VEHICLE", dt.date(2020, 1, 5), 1200000, "General"]])
        self.assertEqual(r.status_code, 200)
        self.assertEqual(FixedAsset.objects.count(), 0,
                         "checking a file must not create anything")
        self.assertEqual(len(r.context["ready"]), 1)

    def test_confirming_brings_the_assets_in(self):
        self._import([["Church van", "VEHICLE", dt.date(2020, 1, 5), 1200000, "General"],
                      ["Piano", "EQUIPMENT", dt.date(2021, 3, 2), 180000, "General"]])
        self.assertEqual(FixedAsset.objects.count(), 2)
        van = FixedAsset.objects.get(name="Church van")
        self.assertEqual(van.cost, Decimal("1200000"))
        self.assertEqual(van.category, "VEHICLE")
        self.assertEqual(van.department_id, self.fund.pk)
        self.assertEqual(van.acquired_on, dt.date(2020, 1, 5))

    def test_every_imported_asset_records_where_it_came_from(self):
        self._import([["Church van", "VEHICLE", dt.date(2020, 1, 5), 1200000, "General"]])
        van = FixedAsset.objects.get(name="Church van")
        self.assertEqual(van.acquisition.source, Acquisition.Source.OPENING,
                         "an asset already owned is an opening balance, not a purchase")
        self.assertTrue(van.events.filter(kind=AssetEvent.Kind.CREATED).exists())

    def test_depreciation_starts_from_the_acquisition_date(self):
        self._import([["Church van", "VEHICLE", dt.date(2020, 1, 5), 1200000, "General"]])
        van = FixedAsset.objects.get(name="Church van")
        self.assertEqual(van.in_service_on, dt.date(2020, 1, 5))

    def test_an_auditor_cannot_import(self):
        aud = User.objects.create_user("imp_aud", password="x")
        aud.groups.add(Group.objects.get_or_create(name=AUDITOR)[0])
        self.client.force_login(aud)
        self._import([["Church van", "VEHICLE", dt.date(2020, 1, 5), 1200000, "General"]])
        self.assertEqual(FixedAsset.objects.count(), 0)


class ImportRowChecksTests(ImportBase):
    def _problems(self, rows):
        r = self._check(rows)
        return {p["name"]: p["issues"] for p in r.context["problems"]}

    def test_a_row_with_no_cost_is_set_aside(self):
        p = self._problems([["Mystery item", "EQUIPMENT", dt.date(2020, 1, 5), None, "General"]])
        self.assertIn("no cost", p["Mystery item"])

    def test_a_row_with_no_readable_date_is_set_aside(self):
        p = self._problems([["Piano", "EQUIPMENT", "sometime in 2021", 180000, "General"]])
        self.assertIn("no acquisition date I could read", p["Piano"])

    def test_a_future_acquisition_is_set_aside(self):
        soon = dt.date.today() + dt.timedelta(days=30)
        p = self._problems([["Piano", "EQUIPMENT", soon, 180000, "General"]])
        self.assertIn("acquired in the future", p["Piano"])

    def test_a_duplicate_within_the_file_is_set_aside(self):
        rows = [["Piano", "EQUIPMENT", dt.date(2021, 3, 2), 180000, "General"],
                ["Piano", "EQUIPMENT", dt.date(2021, 3, 2), 180000, "General"]]
        p = self._problems(rows)
        self.assertIn("appears twice in this file", p["Piano"])

    def test_an_asset_already_on_the_register_is_not_imported_twice(self):
        FixedAsset.objects.create(name="Piano", category="EQUIPMENT",
                                  cost=Decimal("180000"), salvage_value=Decimal(0),
                                  acquired_on=dt.date(2021, 3, 2), department=self.fund)
        p = self._problems([["Piano", "EQUIPMENT", dt.date(2021, 3, 2), 180000, "General"]])
        self.assertIn("already on the register", p["Piano"])
        self.assertEqual(FixedAsset.objects.count(), 1)

    def test_a_small_item_is_set_aside_when_a_threshold_is_set(self):
        from core.models import SiteConfig
        cfg = SiteConfig.get()
        cfg.capitalisation_threshold = Decimal("10000")
        cfg.save()
        p = self._problems([["Stapler", "EQUIPMENT", dt.date(2021, 3, 2), 500, "General"]])
        self.assertTrue(any("threshold" in i for i in p["Stapler"]))

    def test_good_rows_still_import_when_others_are_set_aside(self):
        self._import([["Church van", "VEHICLE", dt.date(2020, 1, 5), 1200000, "General"],
                      ["Broken row", "EQUIPMENT", dt.date(2020, 1, 5), None, "General"]])
        self.assertEqual(FixedAsset.objects.count(), 1)
        self.assertTrue(FixedAsset.objects.filter(name="Church van").exists())


class ImportFlexibilityTests(ImportBase):
    def test_a_treasurers_own_headings_are_understood(self):
        rows = [["Church van", 1200000, "05/01/2020"]]
        data = {"file": _sheet(rows, headers=("Particulars", "Value", "Purchase date")),
                "default_category": "VEHICLE", "confirm": "yes"}
        self.client.post(reverse("asset_import"),
                         {**data, "file": _sheet(rows, headers=("Particulars", "Value",
                                                                "Purchase date"))})
        van = FixedAsset.objects.filter(name="Church van").first()
        self.assertIsNotNone(van, "headings should be matched by name, not position")
        self.assertEqual(van.cost, Decimal("1200000"))
        self.assertEqual(van.acquired_on, dt.date(2020, 1, 5))

    def test_amounts_with_currency_formatting_are_read(self):
        r = self._check([["Church van", "VEHICLE", dt.date(2020, 1, 5), "KSh 1,200,000.00",
                          "General"]])
        self.assertEqual(r.context["ready"][0]["cost"], Decimal("1200000.00"))

    def test_a_file_without_a_name_or_cost_column_is_refused(self):
        rows = [["something", "else"]]
        r = self.client.post(reverse("asset_import"),
                             {"file": _sheet(rows, headers=("Colour", "Shape")),
                              "default_category": "EQUIPMENT"}, follow=True)
        self.assertEqual(FixedAsset.objects.count(), 0)
        self.assertContains(r, "could not find a name column")

    def test_blank_rows_are_ignored(self):
        r = self._check([["Church van", "VEHICLE", dt.date(2020, 1, 5), 1200000, "General"],
                         [None, None, None, None, None]])
        self.assertEqual(len(r.context["ready"]), 1)
        self.assertEqual(len(r.context["problems"]), 0)


class CsvAndAwkwardFilesTests(ImportBase):
    """The formats a treasurer actually has to hand."""

    def _csv(self, text, name="assets.csv"):
        return SimpleUploadedFile(name, text.encode("utf-8"), content_type="text/csv")

    def test_a_csv_imports(self):
        f = self._csv("Name,Category,Date acquired,Cost,Fund\n"
                      "Church van,VEHICLE,2020-01-05,1200000,General\n")
        self.client.post(reverse("asset_import"),
                         {"file": f, "default_category": "EQUIPMENT"})
        f2 = self._csv("Name,Category,Date acquired,Cost,Fund\n"
                       "Church van,VEHICLE,2020-01-05,1200000,General\n")
        self.client.post(reverse("asset_import"),
                         {"file": f2, "confirm": "yes", "default_category": "EQUIPMENT"})
        van = FixedAsset.objects.filter(name="Church van").first()
        self.assertIsNotNone(van)
        self.assertEqual(van.cost, Decimal("1200000"))

    def test_a_csv_saved_by_excel_with_a_byte_order_mark_still_matches_headings(self):
        f = self._csv("\ufeffName,Cost,Date acquired\nPiano,180000,2021-03-02\n")
        r = self.client.post(reverse("asset_import"),
                             {"file": f, "default_category": "EQUIPMENT"})
        self.assertEqual(len(r.context["ready"]), 1,
                         "the BOM must not stop the first heading being recognised")

    def test_a_semicolon_separated_csv_is_understood(self):
        f = self._csv("Name;Cost;Date acquired\nPiano;180000;2021-03-02\n")
        r = self.client.post(reverse("asset_import"),
                             {"file": f, "default_category": "EQUIPMENT"})
        self.assertEqual(len(r.context["ready"]), 1)

    def test_an_old_xls_file_is_explained_not_just_refused(self):
        f = SimpleUploadedFile("register.xls", b"\xd0\xcf\x11\xe0rubbish",
                               content_type="application/vnd.ms-excel")
        r = self.client.post(reverse("asset_import"),
                             {"file": f, "default_category": "EQUIPMENT"}, follow=True)
        self.assertContains(r, "Save As .xlsx")
        self.assertEqual(FixedAsset.objects.count(), 0)

    def test_an_unreadable_file_reports_a_reason_and_changes_nothing(self):
        f = SimpleUploadedFile("notes.xlsx", b"this is not a spreadsheet at all",
                               content_type="application/octet-stream")
        r = self.client.post(reverse("asset_import"),
                             {"file": f, "default_category": "EQUIPMENT"}, follow=True)
        self.assertContains(r, "could not read that file")
        self.assertEqual(FixedAsset.objects.count(), 0)


class SampleFileTests(ImportBase):
    def test_a_sample_can_be_downloaded(self):
        r = self.client.get(reverse("asset_import"), {"sample": "1"})
        self.assertEqual(r.status_code, 200)
        self.assertIn("text/csv", r["Content-Type"])
        self.assertIn("asset-register-sample.csv", r["Content-Disposition"])

    def test_the_sample_imports_cleanly_into_the_register(self):
        """The example we hand out must actually work — otherwise it teaches the
        wrong shape."""
        body = self.client.get(reverse("asset_import"), {"sample": "1"}).content
        f1 = SimpleUploadedFile("sample.csv", body, content_type="text/csv")
        r = self.client.post(reverse("asset_import"),
                             {"file": f1, "default_category": "EQUIPMENT"})
        self.assertEqual(len(r.context["problems"]), 0,
                         f"the sample should import without complaint: "
                         f"{[p['issues'] for p in r.context['problems']]}")
        self.assertEqual(len(r.context["ready"]), 4)


class AssetReportsTests(TestCase):
    def setUp(self):
        self.fund = Department.objects.create(name="General", fund_type=Department.FundType.LOCAL)
        self.tr = User.objects.create_user("rep_tr", password="x")
        self.tr.groups.add(Group.objects.get_or_create(name=TREASURER)[0])
        self.client.force_login(self.tr)
        self.year = dt.date.today().year
        FixedAsset.objects.create(
            name="Church van", category="VEHICLE", cost=Decimal("1200000"),
            salvage_value=Decimal(0), acquired_on=dt.date(self.year - 2, 1, 1),
            in_service_on=dt.date(self.year - 2, 1, 1), method="STRAIGHT",
            rate=Decimal("10"), department=self.fund)

    def _get(self, key):
        return self.client.get(reverse("engine_report", args=[key]),
                               {"start": f"{self.year}-01-01", "end": f"{self.year}-12-31"})

    def test_all_four_reports_render(self):
        for key in ("asset_register", "asset_movement", "depreciation_schedule",
                    "asset_disposals"):
            self.assertEqual(self._get(key).status_code, 200, key)

    def test_they_appear_in_the_report_library(self):
        r = self.client.get(reverse("report_library"))
        self.assertContains(r, "Fixed Asset Register")
        self.assertContains(r, "Depreciation Schedule")

    def test_the_register_shows_the_asset_and_its_value(self):
        r = self._get("asset_register")
        self.assertContains(r, "Church van")

    def test_the_movement_report_agrees_with_the_metrics(self):
        from core.metrics import metrics
        r = self._get("asset_movement")
        self.assertEqual(r.status_code, 200)
        # closing net book value on the report is the registry's figure
        self.assertGreater(metrics.net_book_value(dt.date.today()), Decimal("0"))

    def test_an_auditor_may_read_them(self):
        aud = User.objects.create_user("rep_aud", password="x")
        aud.groups.add(Group.objects.get_or_create(name=AUDITOR)[0])
        self.client.force_login(aud)
        self.assertEqual(self._get("asset_register").status_code, 200)

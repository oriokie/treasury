import json
from decimal import Decimal

from django.contrib.auth.models import User, Group
from django.test import TestCase
from django.urls import reverse

from core.models import SiteConfig, SmsLog
from core.services.sms import send_sms
from departments.models import Department
from envelopes.models import Envelope, EnvelopeLine
from giving.models import Transaction


class EnvelopeLedgerTests(TestCase):
    def setUp(self):
        Group.objects.get_or_create(name="Treasurer")
        self.user = User.objects.create_superuser("t", password="x")
        self.tithe = Department.objects.create(name="Tithe", fund_type=Department.FundType.TRUST)
        self.lcb = Department.objects.create(name="Local Church Budget",
                                             fund_type=Department.FundType.LOCAL)
        self.client.login(username="t", password="x")

    def test_cash_envelope_creates_lines_and_transactions(self):
        rows = [{"name": "Jane Doe", "member_id": None, "receipt": "5001",
                 "channel": "CASH",
                 "amounts": {str(self.tithe.id): "500", str(self.lcb.id): "100"}}]
        self.client.post(reverse("envelope_ledger"),
                         {"date": "2026-05-30", "rows": json.dumps(rows)})
        env = Envelope.objects.get(receipt_no="5001")
        self.assertEqual(env.total, Decimal("600"))
        self.assertEqual(env.lines.count(), 2)
        # cash envelopes flow into the central ledger as ENVELOPE transactions
        self.assertEqual(Transaction.objects.filter(
            channel=Transaction.Channel.ENVELOPE).count(), 2)

    def test_duplicate_receipt_is_skipped(self):
        Envelope.objects.create(date="2026-05-30", receipt_no="5001",
                                contributor_name="X", recorded_by=self.user)
        rows = [{"name": "Jane", "receipt": "5001", "channel": "CASH",
                 "amounts": {str(self.tithe.id): "500"}}]
        self.client.post(reverse("envelope_ledger"),
                         {"date": "2026-05-30", "rows": json.dumps(rows)})
        self.assertEqual(Envelope.objects.filter(receipt_no="5001").count(), 1)

    def test_next_receipt_increments(self):
        Envelope.objects.create(date="2026-05-30", receipt_no="106706",
                                contributor_name="X", recorded_by=self.user)
        r = self.client.get(reverse("next_receipt"))
        self.assertEqual(r.json()["next"], "106707")


class SmsTests(TestCase):
    def test_disabled_sms_logs_disabled_and_does_not_send(self):
        cfg = SiteConfig.get()
        cfg.sms_enabled = False
        cfg.save()
        log = send_sms("0712345678", "hello", cfg)
        self.assertEqual(log.status, SmsLog.Status.DISABLED)

    def test_enabled_without_credentials_fails_gracefully(self):
        cfg = SiteConfig.get()
        cfg.sms_enabled = True
        cfg.save()
        log = send_sms("0712345678", "hello", cfg)
        self.assertEqual(log.status, SmsLog.Status.FAILED)


class EnvelopeColumnsImportTests(TestCase):
    def setUp(self):
        from django.contrib.auth.models import User
        from giving.models import SplitFund, SplitComponent
        self.user = User.objects.create_superuser("imp", password="x")
        self.tithe = Department.objects.create(name="Tithe", fund_type=Department.FundType.TRUST)
        Department.objects.create(name="Church Building Fund", fund_type=Department.FundType.LOCAL)
        self.client.login(username="imp", password="x")

    def test_catalog_excludes_building(self):
        from envelopes.views import column_catalog
        labels = [c["label"] for c in column_catalog()]
        self.assertIn("Tithe", labels)
        self.assertFalse(any("building" in l.lower() for l in labels))

    def test_template_downloads_xlsx(self):
        from django.urls import reverse
        r = self.client.get(reverse("envelope_template") + f"?cols={self.tithe.id}")
        self.assertEqual(r.status_code, 200)
        self.assertIn("spreadsheetml", r["Content-Type"])

    def test_import_creates_envelopes(self):
        import io, openpyxl
        from django.urls import reverse
        from django.core.files.uploadedfile import SimpleUploadedFile
        from envelopes.models import Envelope
        wb = openpyxl.Workbook(); ws = wb.active
        ws.append(["No", "Contributor Name", "Phone", "Receipt No", "Channel", "Tithe"])
        ws.append([1, "Test Importer", "", "777001", "CASH", 250])
        buf = io.BytesIO(); wb.save(buf); buf.seek(0)
        up = SimpleUploadedFile("f.xlsx", buf.read())
        self.client.post(reverse("envelope_import"), {"date": "2026-05-30", "file": up})
        env = Envelope.objects.get(receipt_no="777001")
        self.assertEqual(env.total, 250)


class BankToEnvelopeTests(TestCase):
    def setUp(self):
        from django.contrib.auth.models import User
        import datetime as dt
        self.user = User.objects.create_superuser("bk", password="x")
        self.dept = Department.objects.create(name="Tithe", fund_type=Department.FundType.TRUST)
        from giving.models import Transaction
        self.d = dt.date(2026, 5, 30)
        Transaction.objects.create(
            date=self.d, channel=Transaction.Channel.BANK,
            direction=Transaction.Direction.CREDIT, amount=1000,
            department=self.dept, payer_name="John Doe", mpesa_ref="UEX1",
            allocation_status=Transaction.Status.AUTO)
        self.client.login(username="bk", password="x")

    def test_pull_creates_envelope_without_new_transaction(self):
        from django.urls import reverse
        from giving.models import Transaction
        from envelopes.models import Envelope
        before = Transaction.objects.count()
        self.client.post(reverse("envelope_pull_bank"), {"month": "2026-05"})
        self.assertEqual(Transaction.objects.count(), before)  # counted once
        self.assertEqual(Envelope.objects.count(), 1)
        self.assertTrue(Transaction.objects.get(mpesa_ref="UEX1").processed_via_envelope)

    def test_pull_is_idempotent(self):
        from django.urls import reverse
        from envelopes.models import Envelope
        self.client.post(reverse("envelope_pull_bank"), {"month": "2026-05"})
        self.client.post(reverse("envelope_pull_bank"), {"month": "2026-05"})
        self.assertEqual(Envelope.objects.count(), 1)


class SabbathBucketTests(TestCase):
    def test_sunday_rolls_to_next_sabbath(self):
        import datetime as dt
        from envelopes.views import sabbath_bucket
        self.assertEqual(sabbath_bucket(dt.date(2026, 5, 30)), dt.date(2026, 5, 30))  # Sat
        self.assertEqual(sabbath_bucket(dt.date(2026, 5, 31)), dt.date(2026, 6, 6))   # Sun -> next Sat
        self.assertEqual(sabbath_bucket(dt.date(2026, 6, 1)), dt.date(2026, 6, 6))    # Mon -> Sat


class EnvelopeReceiptTests(TestCase):
    def setUp(self):
        import datetime as dt
        from decimal import Decimal
        from django.contrib.auth.models import User
        from departments.models import Department
        from envelopes.models import Envelope, EnvelopeLine
        self.u = User.objects.create_superuser("er", password="x")
        self.fund = Department.objects.create(name="Tithe", fund_type=Department.FundType.TRUST)
        self.env = Envelope.objects.create(receipt_no="R-0001",
            contributor_name="Jane Mumbi", date=dt.date(2026, 5, 2), total=Decimal("1500"), recorded_by=self.u)
        EnvelopeLine.objects.create(envelope=self.env, department=self.fund,
                                    amount=Decimal("1500"))
        self.client.login(username="er", password="x")

    def test_standard_receipt(self):
        from django.urls import reverse
        r = self.client.get(reverse("envelope_receipt", args=[self.env.id]))
        self.assertEqual(r.status_code, 200)
        body = r.content.decode()
        self.assertIn("OFFICIAL RECEIPT", body)
        self.assertIn("R-0001", body)
        self.assertIn("Jane Mumbi", body)
        self.assertIn("1,500", body)

    def test_etr_compact_receipt(self):
        from django.urls import reverse
        r = self.client.get(reverse("envelope_receipt", args=[self.env.id]) + "?format=etr")
        self.assertEqual(r.status_code, 200)
        self.assertIn('class="etr"', r.content.decode())

    def test_custom_message_from_settings(self):
        from django.urls import reverse
        from core.models import SiteConfig
        cfg = SiteConfig.get(); cfg.receipt_message = "Asante sana for your faithfulness."; cfg.save()
        r = self.client.get(reverse("envelope_receipt", args=[self.env.id]))
        self.assertIn("Asante sana", r.content.decode())


class EnvelopeDeleteAndCountTests(TestCase):
    def setUp(self):
        import datetime as dt
        from decimal import Decimal
        from django.contrib.auth.models import User, Group
        from core.roles import TREASURER
        from departments.models import Department
        from envelopes.views import _save_envelope
        from core.models import SiteConfig
        self.u = User.objects.create_user("ed", password="x")
        self.u.groups.add(Group.objects.get_or_create(name=TREASURER)[0])
        self.fund = Department.objects.create(name="Loose", fund_type="LOCAL")
        self.sab = dt.date(2026, 5, 2)
        self.env = _save_envelope(date=self.sab, name="Jane", receipt="R1", channel="CASH",
            lines=[(self.fund, Decimal("1000"))], member=None, user=self.u, cfg=SiteConfig.get())
        self.client.login(username="ed", password="x")

    def test_cash_envelope_creates_ledger_entry(self):
        from giving.models import Transaction
        self.assertEqual(Transaction.objects.filter(channel="ENVELOPE").count(), 1)

    def test_delete_removes_envelope_and_ledger(self):
        from django.urls import reverse
        from giving.models import Transaction
        from envelopes.models import Envelope
        self.client.post(reverse("envelope_delete", args=[self.env.id]))
        self.assertFalse(Envelope.objects.filter(id=self.env.id).exists())
        self.assertEqual(Transaction.objects.filter(channel="ENVELOPE").count(), 0)

    def test_reversed_envelope_is_voided(self):
        # reverse the envelope's ledger entry -> envelope shows as voided
        from envelopes.models import EnvelopeLine
        line = self.env.lines.first()
        line.transaction.reverse(self.u, "error")
        self.env.refresh_from_db()
        self.assertTrue(self.env.is_voided)

    def test_expected_excludes_reversed(self):
        from decimal import Decimal
        from envelopes.views import CountSessionCreate
        before = CountSessionCreate()._expected(self.sab)
        self.env.lines.first().transaction.reverse(self.u, "error")
        after = CountSessionCreate()._expected(self.sab)
        self.assertEqual(before - after, Decimal("1000"))


class CountBreakdownDBTests(TestCase):
    def test_db_breakdown_matches_expected(self):
        import datetime as dt
        from decimal import Decimal
        from django.contrib.auth.models import User
        from departments.models import Department
        from giving.models import Transaction
        from cashbook.models import Expense
        from core.utils import sabbath_of
        from envelopes.views import CountSessionCreate
        u = User.objects.create_superuser("cb", password="x")
        fund = Department.objects.create(name="Loose", fund_type="LOCAL")
        sab = sabbath_of(dt.date(2026, 5, 2))
        Transaction.objects.create(date=sab, channel="CASH", direction="CREDIT",
            amount=Decimal("1000"), department=fund, allocation_status="MANUAL", confirmed=True)
        Transaction.objects.create(date=sab - dt.timedelta(days=3), channel="ENVELOPE",
            direction="CREDIT", amount=Decimal("2000"), department=fund,
            allocation_status="MANUAL", confirmed=True)
        Transaction.objects.create(date=sab, channel="BANK", direction="CREDIT",
            amount=Decimal("9999"), department=fund, allocation_status="MANUAL", confirmed=True)
        Expense.objects.create(date=sab, department=fund, description="out",
            amount=Decimal("300"), category="OTHER", method="CASH", status="PAID",
            recorded_by=u, approved_by=u)
        b = CountSessionCreate()._breakdown(sab)
        self.assertEqual(b["cash"], Decimal("1000"))
        self.assertEqual(b["envelope"], Decimal("2000"))
        self.assertEqual(b["disbursed"], Decimal("300"))
        self.assertEqual(b["net"], Decimal("2700"))

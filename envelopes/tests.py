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


class EnvelopeSendButtonGatingTests(TestCase):
    """SMS/WhatsApp send buttons appear only when those channels are enabled."""

    def setUp(self):
        from django.contrib.auth.models import User, Group
        from departments.models import Department
        from envelopes.models import Envelope, EnvelopeLine
        from core.utils import last_saturday
        from decimal import Decimal
        self.u = User.objects.create_user("eg", password="x")
        g, _ = Group.objects.get_or_create(name="Treasurer")
        self.u.groups.add(g)
        # an envelope in the current month so a Sabbath section renders
        d = Department.objects.create(name="Tithe", fund_type="TRUST",
                                      category="OFFERING", is_trust=True)
        sat = last_saturday()
        env = Envelope.objects.create(date=sat, contributor_name="A",
                                      recorded_by=self.u)
        EnvelopeLine.objects.create(envelope=env, department=d,
                                    amount=Decimal("100"))
        self.month = f"{sat.year}-{sat.month:02d}"

    def test_buttons_hidden_when_disabled(self):
        from django.test import Client
        from core.models import SiteConfig
        cfg = SiteConfig.get()
        cfg.sms_enabled = False; cfg.whatsapp_enabled = False; cfg.save()
        c = Client(); c.force_login(self.u)
        html = c.get(f"/envelopes/?month={self.month}").content.decode()
        self.assertNotIn("SMS all", html)
        self.assertNotIn("WhatsApp all", html)

    def test_sms_button_shown_when_enabled(self):
        from django.test import Client
        from core.models import SiteConfig
        cfg = SiteConfig.get()
        cfg.sms_enabled = True; cfg.whatsapp_enabled = False; cfg.save()
        c = Client(); c.force_login(self.u)
        html = c.get(f"/envelopes/?month={self.month}").content.decode()
        self.assertIn("SMS all", html)
        self.assertNotIn("WhatsApp all", html)


class BankReceiptOneTests(TestCase):
    """Per-transaction 'Receipt as envelope' with optional manual receipt number,
    without double-counting the money."""

    def setUp(self):
        from django.contrib.auth.models import User, Group
        from departments.models import Department
        self.u = User.objects.create_user("br", password="x")
        g, _ = Group.objects.get_or_create(name="Treasurer")
        self.u.groups.add(g)
        self.d = Department.objects.create(name="Tithe", fund_type="TRUST",
                                           category="OFFERING", is_trust=True)

    def _txn(self, ref="R1", amt="1000"):
        from giving.models import Transaction
        import datetime as dt
        from decimal import Decimal
        return Transaction.objects.create(
            date=dt.date(2026, 6, 6), channel="BANK", direction="CREDIT",
            amount=Decimal(amt), allocation_status="MANUAL", confirmed=True,
            department=self.d, core_ref=ref, payer_name="Giver",
            service_sabbath=dt.date(2026, 6, 6), sabbath_confirm_pending=False)

    def test_manual_receipt_number(self):
        from django.test import Client
        from envelopes.models import Envelope
        t = self._txn()
        c = Client(); c.force_login(self.u)
        c.post(f"/transactions/{t.id}/receipt-envelope/", {"receipt_no": "T-500"})
        t.refresh_from_db()
        env = Envelope.objects.get(receipt_no="T-500")
        self.assertTrue(t.processed_via_envelope)
        self.assertEqual(env.bank_transaction_id, t.id)
        self.assertEqual(env.total, t.amount)

    def test_auto_receipt_number(self):
        from django.test import Client
        from envelopes.models import Envelope
        t = self._txn(ref="R2")
        c = Client(); c.force_login(self.u)
        c.post(f"/transactions/{t.id}/receipt-envelope/", {})
        self.assertTrue(Envelope.objects.filter(bank_transaction=t).exists())

    def test_no_double_receipt(self):
        from django.test import Client
        from envelopes.models import Envelope
        t = self._txn(ref="R3")
        c = Client(); c.force_login(self.u)
        c.post(f"/transactions/{t.id}/receipt-envelope/", {"receipt_no": "A1"})
        c.post(f"/transactions/{t.id}/receipt-envelope/", {"receipt_no": "A2"})
        self.assertEqual(Envelope.objects.filter(bank_transaction=t).count(), 1)
        self.assertFalse(Envelope.objects.filter(receipt_no="A2").exists())

    def test_duplicate_receipt_number_rejected(self):
        from django.test import Client
        from envelopes.models import Envelope
        t1 = self._txn(ref="R4")
        t2 = self._txn(ref="R5")
        c = Client(); c.force_login(self.u)
        c.post(f"/transactions/{t1.id}/receipt-envelope/", {"receipt_no": "DUP"})
        c.post(f"/transactions/{t2.id}/receipt-envelope/", {"receipt_no": "DUP"})
        # only the first used DUP; the second was rejected, t2 not receipted
        t2.refresh_from_db()
        self.assertFalse(t2.processed_via_envelope)


class SabbathExcelCleanupTests(TestCase):
    """The per-Sabbath Excel: receipt suffix stripped; Combined Offering shown as
    one block in entries but split in the summary; summary has borders."""

    def setUp(self):
        from django.contrib.auth.models import User, Group
        from departments.models import Department
        from giving.models import SplitFund, SplitComponent
        self.u = User.objects.create_user("se", password="x")
        g, _ = Group.objects.get_or_create(name="Treasurer")
        self.u.groups.add(g)
        # build a Combined Offering split fund with trust + local halves
        self.trust = Department.objects.create(name="Combined Offering (Trust 50%)",
            fund_type="TRUST", category="OFFERING", is_trust=True, selectable=False)
        self.local = Department.objects.create(name="Combined Offering (Local 50%)",
            fund_type="LOCAL", category="OFFERING", is_trust=False, selectable=False)
        sf = SplitFund.objects.create(name="Combined Offering")
        SplitComponent.objects.create(split_fund=sf, department=self.trust, percent=50)
        SplitComponent.objects.create(split_fund=sf, department=self.local, percent=50)

    def test_excel_cleanups(self):
        import io, openpyxl
        from django.test import Client
        from envelopes.models import Envelope, EnvelopeLine
        from core.utils import last_saturday
        from decimal import Decimal
        sat = last_saturday()
        env = Envelope.objects.create(date=sat, contributor_name="Member X",
                                      receipt_no="JUN1-0421", recorded_by=self.u)
        EnvelopeLine.objects.create(envelope=env, department=self.trust, amount=Decimal("300"))
        EnvelopeLine.objects.create(envelope=env, department=self.local, amount=Decimal("300"))
        env.recompute_total(); env.save()

        c = Client(); c.force_login(self.u)
        r = c.get(f"/envelopes/sabbath.xlsx?date={sat.isoformat()}")
        self.assertEqual(r.status_code, 200)
        wb = openpyxl.load_workbook(io.BytesIO(r.content)); ws = wb.active

        header, hr = None, None
        for row in ws.iter_rows(min_row=1, max_row=10):
            vals = [c.value for c in row]
            if "Contributor" in vals:
                header, hr = vals, row[0].row
                break
        # one combined column, no half-columns in entries
        self.assertEqual(header.count("Combined Offering"), 1)
        self.assertNotIn("Combined Offering (Trust 50%)", header)
        # receipt suffix stripped + combined block = full amount
        for row in ws.iter_rows(min_row=hr + 1):
            if row[1].value == "Member X":
                self.assertEqual(str(row[2].value), "0421")
                self.assertEqual(row[header.index("Combined Offering")].value, 600)
                break
        # summary keeps the halves split
        alltext = "\n".join(str(c.value) for rr in ws.iter_rows() for c in rr if c.value)
        self.assertIn("Combined Offering (Trust 50%)", alltext)
        self.assertIn("Combined Offering (Local 50%)", alltext)


class MarkReceiptedOnlyTests(TestCase):
    """Item 3: flag a bank gift as receipted (manual envelope already written)
    without creating a new envelope record."""

    def test_mark_only(self):
        from django.contrib.auth.models import User, Group
        from django.test import Client
        from departments.models import Department
        from giving.models import Transaction
        from envelopes.models import Envelope
        import datetime as dt
        from decimal import Decimal
        u = User.objects.create_user("mo", password="x")
        g, _ = Group.objects.get_or_create(name="Treasurer")
        u.groups.add(g)
        d = Department.objects.create(name="Tithe", fund_type="TRUST",
                                      category="OFFERING", is_trust=True)
        t = Transaction.objects.create(date=dt.date(2026, 6, 20), channel="BANK",
            direction="CREDIT", amount=Decimal("900"), allocation_status="MANUAL",
            confirmed=True, department=d, core_ref="MO1", payer_name="X",
            service_sabbath=dt.date(2026, 6, 20), sabbath_confirm_pending=False)
        before = Envelope.objects.count()
        c = Client(); c.force_login(u)
        c.post(f"/transactions/{t.id}/receipt-envelope/", {"mark_only": "1"})
        t.refresh_from_db()
        self.assertTrue(t.processed_via_envelope)
        self.assertEqual(Envelope.objects.count(), before)


class BulkReceiptStartNumberTests(TestCase):
    """Item 4: bulk bank receipting honours an optional starting receipt number."""

    def test_start_number_used(self):
        from django.contrib.auth.models import User, Group
        from django.test import Client
        from departments.models import Department
        from giving.models import Transaction
        from envelopes.models import Envelope
        import datetime as dt
        from decimal import Decimal
        u = User.objects.create_user("bs", password="x")
        g, _ = Group.objects.get_or_create(name="Treasurer")
        u.groups.add(g)
        d = Department.objects.create(name="Tithe", fund_type="TRUST",
                                      category="OFFERING", is_trust=True)
        Transaction.objects.create(date=dt.date(2026, 6, 20), channel="BANK",
            direction="CREDIT", amount=Decimal("1200"), allocation_status="AUTO",
            confirmed=True, department=d, core_ref="BS1", payer_name="Y",
            service_sabbath=dt.date(2026, 6, 20), sabbath_confirm_pending=False)
        c = Client(); c.force_login(u)
        c.post("/envelopes/pull-bank/", {"month": "2026-06", "start_receipt": "700"})
        env = Envelope.objects.filter(bank_transaction__core_ref="BS1").first()
        self.assertIsNotNone(env)
        self.assertEqual(env.receipt_no, "B700")

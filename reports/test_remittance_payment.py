"""Remittance batches require a payment instrument before being marked sent;
the payment is the settlement record and clearing posts no extra journals."""
import datetime as dt
from decimal import Decimal
from django.test import TestCase, Client
from django.contrib.auth.models import User, Group
from departments.models import Department
from cashbook.models import RemittanceBatch, Expense, PaymentInstrument
from ledger.models import JournalEntry
from ledger.services.posting import ensure_chart


def _treasurer():
    u = User.objects.create_user("tr", password="x", is_superuser=True)
    u.groups.add(Group.objects.get_or_create(name="Treasurer")[0])
    return u


class RemittancePaymentWorkflowTests(TestCase):
    def setUp(self):
        ensure_chart()
        self.tr = _treasurer()
        self.c = Client(); self.c.force_login(self.tr)
        self.trust = Department.objects.create(name="ENF", fund_type="TRUST",
            category="TRUST", is_trust=True)
        self.b = RemittanceBatch.create_batch(total_amount=Decimal("12000"),
            status="DRAFT", created_by=self.tr)
        self.e = Expense.objects.create(date=dt.date(2026, 6, 1),
            department=self.trust, description="Remit", amount=Decimal("12000"),
            category="REMITTANCE", status="PENDING", recorded_by=self.tr,
            remittance_batch=self.b)

    def _approve(self):
        self.c.post(f"/reports/trust/remittance/batch/{self.b.id}/approve/", {})
        self.b.refresh_from_db()

    def test_cannot_mark_sent_without_payment(self):
        self._approve()
        self.assertEqual(self.b.status, "APPROVED")
        self.c.post(f"/reports/trust/remittance/batch/{self.b.id}/remit/", {})
        self.b.refresh_from_db()
        self.assertEqual(self.b.status, "APPROVED")   # still not sent

    def test_issue_payment_links_and_posts_no_journal(self):
        self._approve()
        j0 = JournalEntry.objects.count()
        self.c.post(f"/reports/trust/remittance/batch/{self.b.id}/issue-payment/",
            {"method": "EFT", "instrument_number": "EFT-55",
             "date_issued": "2026-06-05"})
        self.b.refresh_from_db()
        self.assertIsNotNone(self.b.payment_id)
        self.assertEqual(self.b.payment.method, "EFT")
        self.assertTrue(self.b.is_settled)
        self.assertEqual(JournalEntry.objects.count(), j0)

    def test_full_workflow_to_sent_and_cleared(self):
        self._approve()
        self.c.post(f"/reports/trust/remittance/batch/{self.b.id}/issue-payment/",
            {"method": "CHEQUE", "instrument_number": "00123",
             "date_issued": "2026-06-05"})
        self.c.post(f"/reports/trust/remittance/batch/{self.b.id}/remit/", {})
        self.b.refresh_from_db()
        self.assertEqual(self.b.status, "REMITTED")
        # legacy cheque fields kept in step for cheque method
        self.assertEqual(self.b.cheque_no, "00123")
        # clear via the payment register — only flips status
        j0 = JournalEntry.objects.count()
        self.c.post("/payments/", {"action": "clear", "pk": self.b.payment.id})
        self.b.payment.refresh_from_db()
        self.assertEqual(self.b.payment.status, "CLEARED")
        self.assertEqual(JournalEntry.objects.count(), j0)

    def test_settlement_helpers(self):
        self._approve()
        self.assertFalse(self.b.is_settled)
        self.c.post(f"/reports/trust/remittance/batch/{self.b.id}/issue-payment/",
            {"method": "MPESA", "instrument_number": "QA12CD",
             "date_issued": "2026-06-05"})
        self.b.refresh_from_db()
        self.assertTrue(self.b.is_settled)
        self.assertIn("QA12CD", self.b.settlement_label)

    def test_two_cheques_summing_to_total_settles(self):
        self._approve()
        self.c.post(f"/reports/trust/remittance/batch/{self.b.id}/issue-payment/", {
            "method": ["CHEQUE", "CHEQUE"],
            "instrument_number": ["1001", "1002"],
            "date_issued": ["2026-06-05", "2026-06-05"],
            "amount": ["5000.00", "7000.00"],
        })
        self.b.refresh_from_db()
        self.assertEqual(self.b.payments.count(), 2)
        self.assertEqual(self.b.settled_amount, Decimal("12000.00"))
        self.assertTrue(self.b.is_settled)
        self.assertIn("1001", self.b.settlement_label)
        self.assertIn("1002", self.b.settlement_label)
        # primary FK kept for compatibility
        self.assertIsNotNone(self.b.payment_id)
        self.assertEqual(self.b.payment.instrument_number, "1001")
        # can now mark sent
        self.c.post(f"/reports/trust/remittance/batch/{self.b.id}/remit/", {})
        self.b.refresh_from_db()
        self.assertEqual(self.b.status, "REMITTED")
        self.assertEqual(self.b.cheque_no, "1001")

    def test_partial_instrument_does_not_settle(self):
        self._approve()
        self.c.post(f"/reports/trust/remittance/batch/{self.b.id}/issue-payment/", {
            "method": "CHEQUE", "instrument_number": "2001",
            "date_issued": "2026-06-05", "amount": "4000.00",
        })
        self.b.refresh_from_db()
        self.assertEqual(self.b.settled_amount, Decimal("4000.00"))
        self.assertFalse(self.b.is_settled)
        self.assertEqual(self.b.remaining_to_settle, Decimal("8000.00"))
        # mark sent still blocked
        self.c.post(f"/reports/trust/remittance/batch/{self.b.id}/remit/", {})
        self.b.refresh_from_db()
        self.assertEqual(self.b.status, "APPROVED")
        # incremental second cheque completes settlement
        self.c.post(f"/reports/trust/remittance/batch/{self.b.id}/issue-payment/", {
            "method": "CHEQUE", "instrument_number": "2002",
            "date_issued": "2026-06-06", "amount": "8000.00",
        })
        self.b.refresh_from_db()
        self.assertTrue(self.b.is_settled)
        self.assertEqual(self.b.payments.count(), 2)

    def test_over_total_rejected(self):
        self._approve()
        n0 = PaymentInstrument.objects.filter(remittance_batch=self.b).count()
        self.c.post(f"/reports/trust/remittance/batch/{self.b.id}/issue-payment/", {
            "method": ["CHEQUE", "CHEQUE"],
            "instrument_number": ["3001", "3002"],
            "date_issued": ["2026-06-05", "2026-06-05"],
            "amount": ["8000.00", "5000.00"],  # 13000 > 12000
        })
        self.b.refresh_from_db()
        self.assertEqual(
            PaymentInstrument.objects.filter(remittance_batch=self.b).count(), n0)
        self.assertFalse(self.b.is_settled)
        # partial then overshoot also rejected
        self.c.post(f"/reports/trust/remittance/batch/{self.b.id}/issue-payment/", {
            "method": "CHEQUE", "instrument_number": "3001",
            "date_issued": "2026-06-05", "amount": "8000.00",
        })
        self.b.refresh_from_db()
        self.assertEqual(self.b.settled_amount, Decimal("8000.00"))
        self.c.post(f"/reports/trust/remittance/batch/{self.b.id}/issue-payment/", {
            "method": "CHEQUE", "instrument_number": "3002",
            "date_issued": "2026-06-05", "amount": "5000.00",  # would make 13000
        })
        self.b.refresh_from_db()
        self.assertEqual(self.b.payments.count(), 1)
        self.assertEqual(self.b.settled_amount, Decimal("8000.00"))
        self.assertFalse(self.b.is_settled)


class RouteRenameTests(TestCase):
    def setUp(self):
        self.u = _treasurer()
        self.c = Client(); self.c.force_login(self.u)

    def test_payments_route_and_cheque_redirect(self):
        self.assertEqual(self.c.get("/payments/").status_code, 200)
        r = self.c.get("/cheques/")
        self.assertEqual(r.status_code, 301)
        self.assertEqual(r.url, "/payments/")
        ro = self.c.get("/cheques/outstanding/")
        self.assertEqual(ro.status_code, 301)

    def test_payment_register_label(self):
        body = self.c.get("/payments/").content.decode()
        self.assertIn("Payment register", body)
        self.assertNotIn("Cheque register", body)

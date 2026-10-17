"""Bulk move of envelope receipts between Sabbaths — checkbox selection on
the Sabbath entries page, and paste-by-receipt-numbers on /envelopes/move/.
"""
import datetime as dt
from decimal import Decimal

from django.contrib.auth.models import Group, User
from django.test import TestCase
from django.urls import reverse

from core.roles import ASSISTANT, AUDITOR, TREASURER
from departments.models import Department
from envelopes.models import Envelope, EnvelopeLine
from giving.models import Transaction

SAB = dt.date(2026, 6, 6)
NEXT = dt.date(2026, 6, 13)


def _user(username, role):
    u = User.objects.create_user(username, password="x")
    u.groups.add(Group.objects.get_or_create(name=role)[0])
    return u


class BulkReassignTests(TestCase):
    def setUp(self):
        self.tr = _user("br_tr", TREASURER)
        self.client.force_login(self.tr)
        self.fund = Department.objects.create(name="BrFund", fund_type="LOCAL")

    def _env(self, receipt, date=SAB, amount=100):
        e = Envelope.objects.create(
            date=date, receipt_no=receipt, contributor_name="SOMEONE",
            channel="CASH", total=Decimal(amount), recorded_by=self.tr)
        txn = Transaction.objects.create(
            date=date, amount=Decimal(amount), direction="CREDIT",
            confirmed=True, channel="ENVELOPE", allocation_status="MANUAL",
            department=self.fund)
        EnvelopeLine.objects.create(
            envelope=e, department=self.fund, amount=Decimal(amount),
            transaction=txn)
        return e

    def test_move_selected_ids(self):
        a = self._env("BR-1")
        b = self._env("BR-2")
        stay = self._env("BR-STAY")
        r = self.client.post(reverse("envelope_bulk_reassign"), {
            "sabbath": NEXT.isoformat(),
            "from": SAB.isoformat(),
            "next": reverse("envelope_sabbath_entries", args=[SAB.isoformat()]),
            "ids": [str(a.id), str(b.id)],
        })
        self.assertEqual(r.status_code, 302)
        self.assertEqual(
            r.url, reverse("envelope_sabbath_entries", args=[SAB.isoformat()]))
        a.refresh_from_db()
        b.refresh_from_db()
        stay.refresh_from_db()
        self.assertEqual(a.date, NEXT)
        self.assertEqual(b.date, NEXT)
        self.assertEqual(stay.date, SAB)
        # Linked cash transactions follow the envelope.
        self.assertEqual(
            set(Transaction.objects.filter(
                envelope_lines__envelope__in=[a, b]).values_list("date", flat=True)),
            {NEXT})

    def test_move_by_pasted_receipt_numbers(self):
        a = self._env("br-10")   # stored lower; paste may differ in case
        b = self._env("BR-11")
        r = self.client.post(reverse("envelope_bulk_reassign"), {
            "sabbath": NEXT.isoformat(),
            "receipts": "BR-10\nBR-11, BR-MISSING",
        })
        self.assertEqual(r.status_code, 302)
        self.assertEqual(
            r.url, reverse("envelope_sabbath_entries", args=[NEXT.isoformat()]))
        a.refresh_from_db()
        b.refresh_from_db()
        self.assertEqual(a.date, NEXT)
        self.assertEqual(b.date, NEXT)

    def test_paste_form_renders_with_from_prefill(self):
        r = self.client.get(
            reverse("envelope_bulk_reassign") + f"?from={SAB.isoformat()}")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.context["from_sabbath"], SAB)
        self.assertContains(r, "Receipt numbers")
        self.assertContains(r, NEXT.isoformat())

    def test_nothing_selected_returns_with_error(self):
        r = self.client.post(reverse("envelope_bulk_reassign"), {
            "sabbath": NEXT.isoformat(),
            "from": SAB.isoformat(),
            "ids": [],
        })
        self.assertEqual(r.status_code, 302)
        self.assertEqual(
            r.url, reverse("envelope_sabbath_entries", args=[SAB.isoformat()]))

    def test_already_on_target_is_a_no_op(self):
        e = self._env("BR-SAME", date=NEXT)
        r = self.client.post(reverse("envelope_bulk_reassign"), {
            "sabbath": NEXT.isoformat(),
            "ids": [str(e.id)],
        })
        self.assertEqual(r.status_code, 302)
        e.refresh_from_db()
        self.assertEqual(e.date, NEXT)

    def test_single_reassign_honours_next(self):
        e = self._env("BR-ONE")
        next_url = reverse("envelope_sabbath_entries", args=[SAB.isoformat()])
        r = self.client.post(reverse("envelope_reassign", args=[e.id]), {
            "sabbath": NEXT.isoformat(),
            "next": next_url,
        })
        self.assertEqual(r.status_code, 302)
        self.assertEqual(r.url, next_url)
        e.refresh_from_db()
        self.assertEqual(e.date, NEXT)

    def test_auditor_cannot_bulk_move(self):
        e = self._env("BR-AUD")
        self.client.force_login(_user("br_aud", AUDITOR))
        r = self.client.post(reverse("envelope_bulk_reassign"), {
            "sabbath": NEXT.isoformat(),
            "ids": [str(e.id)],
        })
        self.assertIn(r.status_code, (302, 403))
        e.refresh_from_db()
        self.assertEqual(e.date, SAB)

    def test_assistant_may_bulk_move(self):
        e = self._env("BR-ASST")
        self.client.force_login(_user("br_asst", ASSISTANT))
        r = self.client.post(reverse("envelope_bulk_reassign"), {
            "sabbath": NEXT.isoformat(),
            "ids": [str(e.id)],
        })
        self.assertEqual(r.status_code, 302)
        e.refresh_from_db()
        self.assertEqual(e.date, NEXT)

    def test_open_redirect_is_rejected(self):
        e = self._env("BR-REDIR")
        r = self.client.post(reverse("envelope_bulk_reassign"), {
            "sabbath": NEXT.isoformat(),
            "ids": [str(e.id)],
            "next": "https://evil.example/phish",
        })
        self.assertEqual(r.status_code, 302)
        self.assertEqual(
            r.url, reverse("envelope_sabbath_entries", args=[NEXT.isoformat()]))

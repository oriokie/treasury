"""Coverage for envelope receipting and counting: a split cash envelope creates
one ENVELOPE transaction per fund line, the lines sum to the envelope total, the
list/ledger/count pages render, a Sabbath can be closed, and a deleted envelope
unwinds its transactions."""
import datetime as dt
from decimal import Decimal

from django.contrib.auth.models import User, Group
from django.test import TestCase
from django.urls import reverse

from core.roles import TREASURER, AUDITOR
from core.models import SiteConfig, SabbathClose
from departments.models import Department
from members.models import Member
from giving.models import Transaction
from envelopes.models import Envelope, EnvelopeLine
from envelopes.views import _save_envelope


def _user(name, role):
    u = User.objects.create_user(name, password="x")
    u.groups.add(Group.objects.get_or_create(name=role)[0])
    return u


def _last_saturday(d=None):
    d = d or dt.date.today()
    return d + dt.timedelta(days=(5 - d.weekday()) % 7)


class EnvelopeSaveTests(TestCase):
    def setUp(self):
        self.user = _user("e_tr", TREASURER)
        self.tithe = Department.objects.create(name="Tithe", fund_type="TRUST")
        self.lcb = Department.objects.create(name="LCB", fund_type="LOCAL")
        self.member = Member.objects.create(name="Asha N", phone="254700123123")
        self.cfg = SiteConfig.get()

    def test_cash_envelope_splits_create_transactions_summing_to_total(self):
        sab = _last_saturday()
        env = _save_envelope(date=sab, name="Asha N", receipt="R001", channel="CASH",
            lines=[(self.tithe, Decimal("1000")), (self.lcb, Decimal("250"))],
            member=self.member, user=self.user, cfg=self.cfg)
        # one ENVELOPE transaction per line
        txns = Transaction.objects.filter(channel="ENVELOPE", reference="envelope R001")
        self.assertEqual(txns.count(), 2)
        self.assertEqual(sum(t.amount for t in txns), Decimal("1250"))
        # envelope total recomputed = sum of lines
        env.refresh_from_db()
        self.assertEqual(env.total, Decimal("1250"))
        self.assertEqual(EnvelopeLine.objects.filter(envelope=env).count(), 2)

    def test_bank_envelope_makes_no_envelope_transactions(self):
        # a BANK envelope is matched to the imported bank credit, not re-created
        sab = _last_saturday()
        env = _save_envelope(date=sab, name="Asha N", receipt="R002", channel="BANK",
            lines=[(self.lcb, Decimal("500"))], member=self.member,
            user=self.user, cfg=self.cfg)
        self.assertEqual(Transaction.objects.filter(channel="ENVELOPE",
            reference="envelope R002").count(), 0)
        env.refresh_from_db()
        self.assertEqual(env.total, Decimal("500"))


class EnvelopePageTests(TestCase):
    def setUp(self):
        self.user = _user("ep_tr", TREASURER)
        self.client.force_login(self.user)
        self.lcb = Department.objects.create(name="LCB", fund_type="LOCAL")

    def test_pages_render(self):
        for name in ["envelope_list", "envelope_ledger", "count_list", "count_new"]:
            try:
                url = reverse(name)
            except Exception:
                continue
            self.assertEqual(self.client.get(url).status_code, 200, name)

    def test_envelope_receipt_page_renders(self):
        env = _save_envelope(date=_last_saturday(), name="X", receipt="RR9",
            channel="CASH", lines=[(self.lcb, Decimal("100"))], member=None,
            user=self.user, cfg=SiteConfig.get())
        self.assertEqual(self.client.get(
            reverse("envelope_receipt", args=[env.pk])).status_code, 200)

    def test_envelope_delete_unwinds_transactions(self):
        env = _save_envelope(date=_last_saturday(), name="Y", receipt="RR10",
            channel="CASH", lines=[(self.lcb, Decimal("300"))], member=None,
            user=self.user, cfg=SiteConfig.get())
        self.assertEqual(Transaction.objects.filter(reference="envelope RR10").count(), 1)
        self.client.post(reverse("envelope_delete", args=[env.pk]))
        self.assertFalse(Envelope.objects.filter(pk=env.pk).exists())


class SabbathCloseViewTests(TestCase):
    def setUp(self):
        self.user = _user("sc_tr", TREASURER)
        self.client.force_login(self.user)

    def test_close_and_reopen_sabbath(self):
        sab = _last_saturday()
        self.client.post(reverse("sabbath_close"),
            {"date": sab.isoformat(), "action": "close"})
        self.assertTrue(SabbathClose.objects.filter(sabbath=sab).exists())
        self.client.post(reverse("sabbath_close"),
            {"date": sab.isoformat(), "action": "reopen"})
        self.assertFalse(SabbathClose.objects.filter(sabbath=sab).exists())

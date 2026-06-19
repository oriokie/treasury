"""Telegram envelope-entry flow: the conversational state machine, its
configurable parameters, and the safeguards (locked periods, new-member gating,
attribution). Driven through handle_update with no live bot."""
import datetime as dt
from decimal import Decimal

from django.test import TestCase

from core.models import SiteConfig, PeriodLock
from core.services.telegram_bot import handle_update
from departments.models import Department
from members.models import Member
from envelopes.models import Envelope
from giving.models import Transaction


class TelegramEnvelopeTests(TestCase):
    def setUp(self):
        self.cfg = SiteConfig.get()
        self.cfg.telegram_enabled = True
        self.cfg.telegram_envelope_enabled = True
        self.cfg.telegram_pin = "1234"
        self.cfg.telegram_allow_new_member = False
        self.cfg.telegram_envelope_confirm = True
        self.cfg.telegram_envelope_channel = "CASH"
        self.cfg.save()
        from django.contrib.auth.models import User, Group
        self.treas = User.objects.create_user("tg_treas", password="x", is_superuser=True)
        self.treas.groups.add(Group.objects.get_or_create(name="Treasurer")[0])
        self.f1 = Department.objects.create(name="Tithe", fund_type="TRUST", category="TRUST")
        self.f2 = Department.objects.create(name="Combined", fund_type="LOCAL", category="OFFERING")
        self.cfg.telegram_envelope_funds.set([self.f1, self.f2])
        self.member = Member.objects.create(name="Ruth Momanyi", phone="254712000111")
        self._chat = 70001

    def send(self, text, chat=None):
        r = handle_update({"message": {"chat": {"id": chat or self._chat}, "text": text}})
        return r[0]["text"] if r else ""

    def _unlock(self, chat=None):
        self.send("1234", chat)

    def test_full_flow_records_envelope(self):
        self._unlock()
        self.send("/envelope")
        self.send("-")                 # current Sabbath
        self.send("Ruth")              # member match
        self.send("2000")              # Tithe
        r = self.send("500")           # Combined -> last fund -> confirm
        self.assertIn("Total", r)
        self.assertIn("2,500", r)
        out = self.send("yes")
        self.assertIn("Recorded envelope", out)
        env = Envelope.objects.get(member=self.member)
        self.assertEqual(env.total, Decimal("2500"))
        self.assertEqual(env.lines.count(), 2)
        self.assertEqual(Transaction.objects.filter(member=self.member,
                                                     channel="ENVELOPE").count(), 2)

    def test_skip_fund_with_dash(self):
        self._unlock()
        self.send("/envelope"); self.send("-"); self.send("Ruth")
        self.send("1000")              # Tithe
        self.send("-")                 # skip Combined
        self.send("yes")
        env = Envelope.objects.get(member=self.member)
        self.assertEqual(env.lines.count(), 1)
        self.assertEqual(env.total, Decimal("1000"))

    def test_new_member_gating_off_refuses(self):
        self._unlock()
        self.send("/envelope"); self.send("-")
        r = self.send("Totally Unknown Person")
        self.assertIn("turned off", r)
        self.assertFalse(Member.objects.filter(name__icontains="Totally Unknown").exists())

    def test_new_member_gating_on_creates(self):
        self.cfg.telegram_allow_new_member = True
        self.cfg.save()
        self._unlock()
        self.send("/envelope"); self.send("-")
        self.send("Fresh Person")
        self.assertTrue(Member.objects.filter(name__icontains="Fresh Person").exists())

    def test_disabled_blocks_command(self):
        self.cfg.telegram_envelope_enabled = False
        self.cfg.save()
        self._unlock()
        r = self.send("/envelope")
        self.assertIn("switched off", r)

    def test_locked_period_blocks_save(self):
        sab = __import__("core.utils", fromlist=["sabbath_of"]).sabbath_of(dt.date.today())
        PeriodLock.objects.create(year=sab.year, month=sab.month, locked_by=self.treas)
        self._unlock()
        self.send("/envelope"); self.send("-"); self.send("Ruth")
        self.send("2000"); self.send("500")
        out = self.send("yes")
        self.assertIn("not recorded", out.lower())
        self.assertFalse(Envelope.objects.filter(member=self.member).exists())

    def test_confirm_off_saves_immediately(self):
        self.cfg.telegram_envelope_confirm = False
        self.cfg.save()
        self._unlock()
        self.send("/envelope"); self.send("-"); self.send("Ruth")
        self.send("2000")
        out = self.send("500")         # last fund -> saves directly (no confirm)
        self.assertIn("Recorded envelope", out)
        self.assertTrue(Envelope.objects.filter(member=self.member).exists())

    def test_requires_pin_first(self):
        # without unlocking, /envelope should not start the flow
        r = self.send("/envelope", chat=70999)
        self.assertIn("PIN", r)
        self.assertFalse(Envelope.objects.exists())

    def test_attributed_to_signed_in_user(self):
        from accounts.models import Profile  # noqa (ensure app loaded)
        from core.models import TelegramProfile
        from django.contrib.auth.models import User
        u = User.objects.create_user("assist1", password="x")
        TelegramProfile.objects.create(user=u, pin="5678")
        self.send("5678", chat=70123)           # personal PIN signs in as u
        self.send("/envelope", chat=70123); self.send("-", chat=70123)
        self.send("Ruth", chat=70123); self.send("2000", chat=70123)
        self.send("500", chat=70123); self.send("yes", chat=70123)
        env = Envelope.objects.get(member=self.member)
        self.assertEqual(env.recorded_by_id, u.id)

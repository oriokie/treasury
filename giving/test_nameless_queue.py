"""STK / bank credits without a usable giver name.

A Co-op STK Push credit often lands as
``UJT…~DEVGR21~2547…~DEVGR100`` — the last segment is a purpose code, not a
person. Phone match still links a member and the ledger shows their name;
without a phone match the gift waits in the nameless queue for bulk linking,
and no member named DEVGR100 is created.
"""
import datetime as dt
from decimal import Decimal

from django.contrib.auth.models import Group, User
from django.test import Client, TestCase
from django.urls import reverse

from core.roles import TREASURER
from departments.models import Department
from giving.models import Transaction
from giving.services.nameless import leave_as_is, link_to_member, nameless_gifts_qs
from members.models import Member
from members.services.matching import is_person_name, match_or_create_member
from statements.models import BankAccount
from statements.services.ingest import ingest_event
from statements.services.parser import parse_narration


class PersonNameTests(TestCase):
    def test_devgr_is_not_a_person(self):
        self.assertFalse(is_person_name("DEVGR100"))
        self.assertFalse(is_person_name("grp21"))
        self.assertTrue(is_person_name("Jane Wanjiru"))
        self.assertTrue(is_person_name("KEVIN OGEGA"))


class DisplayNameTests(TestCase):
    def test_linked_member_wins_over_ref_payer_name(self):
        m = Member.objects.create(name="MARY ATIENO", phone="254745688422")
        t = Transaction.objects.create(
            date=dt.date(2026, 10, 29), channel="BANK", direction="CREDIT",
            amount=Decimal("100"), allocation_status="AUTO", confirmed=True,
            member=m, payer_name="DEVGR100", payer_phone="254745688422",
            reference="DEVGR21", core_ref="DISP1")
        self.assertEqual(t.display_name, "MARY ATIENO")
        self.assertEqual(t.paid_by_display, "")

    def test_real_payer_shown_when_different_from_member(self):
        m = Member.objects.create(name="AARON NYARERU", phone="254712000111")
        t = Transaction.objects.create(
            date=dt.date(2026, 10, 29), channel="BANK", direction="CREDIT",
            amount=Decimal("100"), allocation_status="AUTO", confirmed=True,
            member=m, payer_name="JANE FRIEND", attributed_via_code=True,
            core_ref="DISP2")
        self.assertEqual(t.display_name, "AARON NYARERU")
        self.assertEqual(t.paid_by_display, "JANE FRIEND")


class IngestSkipsRefNameTests(TestCase):
    def setUp(self):
        self.fund = Department.objects.create(name="Development", fund_type="LOCAL",
                                              category="DEVELOPMENT")
        BankAccount.objects.create(name="Main", bank_name="Co-op",
                                   account_number="01128158400600",
                                   is_default=True, active=True)

    def test_narration_parses_devgr_as_name_today(self):
        # Document the raw parse — ingest then strips it.
        parsed = parse_narration(
            "UJT6UBGSOE~DEVGR21~254745688422~DEVGR100")
        self.assertEqual(parsed["name"].upper(), "DEVGR100")
        self.assertEqual(parsed["phone"], "254745688422")

    def test_phone_match_links_member_without_keeping_ref_as_payer(self):
        m = Member.objects.create(name="MARY ATIENO", phone="254745688422")
        txn, outcome = ingest_event(
            date=dt.date(2026, 10, 29), amount=Decimal("100"),
            direction=Transaction.Direction.CREDIT,
            reference="DEVGR21", phone="254745688422", name="DEVGR100",
            raw_narration="UJT6UBGSOE~DEVGR21~254745688422~DEVGR100",
            core_ref="CB0548726_29102026_2", bank_receipt="UJT6UBGSOE",
            mpesa_ref="UJT6UBGSOE")
        self.assertEqual(outcome, "created")
        self.assertEqual(txn.member_id, m.id)
        self.assertEqual(txn.payer_name, "")
        self.assertEqual(txn.display_name, "MARY ATIENO")
        self.assertFalse(Member.objects.filter(name__iexact="DEVGR100").exists())

    def test_unknown_phone_leaves_gift_unlinked(self):
        txn, _ = ingest_event(
            date=dt.date(2026, 10, 29), amount=Decimal("100"),
            direction=Transaction.Direction.CREDIT,
            reference="DEVGR21", phone="254700000099", name="DEVGR100",
            raw_narration="x", core_ref="NAMELESS1", bank_receipt="NAMELESS1",
            mpesa_ref="NAMELESS1")
        self.assertIsNone(txn.member_id)
        self.assertEqual(txn.payer_name, "")
        self.assertIn(txn, list(nameless_gifts_qs()))
        self.assertFalse(Member.objects.filter(name__iexact="DEVGR100").exists())


class NamelessQueueViewTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user("nl_tr", password="x", is_superuser=True)
        self.user.groups.add(Group.objects.get_or_create(name=TREASURER)[0])
        self.c = Client()
        self.c.force_login(self.user)
        self.member = Member.objects.create(name="PETER OTIENO", phone="254711222333")
        self.orphan = Transaction.objects.create(
            date=dt.date(2026, 10, 29), channel="BANK", direction="CREDIT",
            amount=Decimal("250"), allocation_status="AUTO", confirmed=True,
            member=None, payer_name="", payer_phone="254700111222",
            reference="DEVGR21", core_ref="NLQ1",
            raw_narration="UJT~DEVGR21~254700111222~DEVGR100")

    def test_queue_lists_unlinked_bank_credits(self):
        r = self.c.get(reverse("nameless_queue"))
        self.assertEqual(r.status_code, 200)
        self.assertContains(r, "DEVGR21")
        self.assertContains(r, "250.00")

    def test_bulk_link_attaches_member_and_fills_name(self):
        r = self.c.post(reverse("nameless_queue"), {
            "ids": [str(self.orphan.id)], "member": str(self.member.id)})
        self.assertEqual(r.status_code, 302)
        self.orphan.refresh_from_db()
        self.assertEqual(self.orphan.member_id, self.member.id)
        self.assertEqual(self.orphan.payer_name, "PETER OTIENO")
        self.assertNotIn(self.orphan, list(nameless_gifts_qs()))

    def test_link_helper_copies_phone_when_blank(self):
        t = Transaction.objects.create(
            date=dt.date(2026, 10, 28), channel="BANK", direction="CREDIT",
            amount=Decimal("50"), allocation_status="AUTO", confirmed=True,
            member=None, payer_name="", payer_phone="", core_ref="NLQ2")
        self.assertTrue(link_to_member(t, self.member))
        t.refresh_from_db()
        self.assertEqual(t.payer_phone, "254711222333")

    def test_nav_link_present_for_treasurer(self):
        body = self.c.get(reverse("transaction_list")).content.decode()
        self.assertIn(reverse("nameless_queue"), body)

    def test_excluded_from_income_not_in_queue(self):
        """Loan receipts / capital credits share member=None but are not gifts."""
        loanish = Transaction.objects.create(
            date=dt.date(2026, 10, 29), channel="BANK", direction="CREDIT",
            amount=Decimal("50000"), allocation_status="MANUAL", confirmed=True,
            member=None, payer_name="", core_ref="LOANISH1",
            excluded_from_income=True)
        self.assertNotIn(loanish, list(nameless_gifts_qs()))
        self.assertIn(self.orphan, list(nameless_gifts_qs()))

    def test_leave_as_is_dismisses_from_queue(self):
        r = self.c.post(reverse("nameless_queue"), {
            "action": "leave", "ids": [str(self.orphan.id)]})
        self.assertEqual(r.status_code, 302)
        self.orphan.refresh_from_db()
        self.assertTrue(self.orphan.name_not_required)
        self.assertIsNone(self.orphan.member_id)
        self.assertNotIn(self.orphan, list(nameless_gifts_qs()))

    def test_leave_as_is_helper(self):
        t = Transaction.objects.create(
            date=dt.date(2026, 10, 28), channel="BANK", direction="CREDIT",
            amount=Decimal("40"), allocation_status="AUTO", confirmed=True,
            member=None, payer_name="", core_ref="NLLEAVE")
        self.assertTrue(leave_as_is(t))
        t.refresh_from_db()
        self.assertTrue(t.name_not_required)
        self.assertFalse(leave_as_is(t))  # already dismissed

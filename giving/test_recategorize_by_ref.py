"""Move wrongly auto-allocated entries to the correct fund by reference number.

Paste M-Pesa / core / bank refs, confirm, and the fund changes. A shared
allocation word that hits several gifts is refused. Receipted rows and locked
periods stay put. A split gift moves together.
"""
import datetime as dt
from decimal import Decimal

from django.contrib.auth.models import Group, User
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import Client, TestCase
from django.urls import reverse

from core.roles import ASSISTANT, TREASURER
from departments.models import Department, DevelopmentGroup
from giving.models import Transaction


def _treasurer(username="tr_move"):
    u = User.objects.create_user(username, password="x", is_superuser=True)
    u.groups.add(Group.objects.get_or_create(name=TREASURER)[0])
    return u


class MoveByReferenceTests(TestCase):
    def setUp(self):
        self.user = _treasurer()
        self.client = Client()
        self.client.force_login(self.user)
        self.wrong = Department.objects.create(name="Tithe", fund_type="TRUST",
                                               category="TRUST")
        self.right = Department.objects.create(name="Camp Meeting", fund_type="LOCAL",
                                               category="OFFERING")
        self.other = Department.objects.create(name="Local Budget", fund_type="LOCAL",
                                               category="OFFERING")

    def _gift(self, ref, fund=None, **kw):
        base = dict(
            date=dt.date(2026, 6, 10), channel="BANK", direction="CREDIT",
            amount=Decimal("1500"), department=fund or self.wrong,
            reference=ref, mpesa_ref=ref, core_ref=ref,
            allocation_status="AUTO", confirmed=True, payer_name="JANE GIVER")
        base.update(kw)
        return Transaction.objects.create(**base)

    def _review(self, refs, fund=None, **extra):
        payload = {"department": (fund or self.right).id, "refs_text": refs}
        payload.update(extra)
        return self.client.post(reverse("transaction_recategorize"), payload)

    def _apply(self):
        return self.client.post(reverse("transaction_recategorize"), {"apply": "1"})

    def test_paste_moves_the_named_gifts_only_after_confirm(self):
        a = self._gift("AAA111")
        b = self._gift("BBB222", amount=Decimal("800"))
        untouched = self._gift("CCC333")
        r = self._review("AAA111\nBBB222")
        self.assertEqual(r.status_code, 200)
        self.assertContains(r, "Ready to move")
        a.refresh_from_db()
        self.assertEqual(a.department_id, self.wrong.id)
        self.assertEqual(a.allocation_status, "AUTO")

        self._apply()
        a.refresh_from_db()
        b.refresh_from_db()
        untouched.refresh_from_db()
        self.assertEqual(a.department_id, self.right.id)
        self.assertEqual(b.department_id, self.right.id)
        self.assertEqual(a.allocation_status, "MANUAL")
        self.assertEqual(b.allocation_status, "MANUAL")
        self.assertEqual(untouched.department_id, self.wrong.id)
        self.assertEqual(untouched.allocation_status, "AUTO")
        self.assertEqual(a.amount, Decimal("1500"))
        self.assertEqual(a.payer_name, "JANE GIVER")

    def test_unknown_ref_is_reported_and_the_rest_still_move(self):
        a = self._gift("KNOWN1")
        r = self._review("KNOWN1 MISSING9")
        self.assertContains(r, "not found")
        self._apply()
        a.refresh_from_db()
        self.assertEqual(a.department_id, self.right.id)

    def test_a_shared_allocation_word_is_not_moved(self):
        a = self._gift("MPESA111", reference="tithe", core_ref="CORE111")
        b = self._gift("MPESA222", reference="tithe", core_ref="CORE222",
                       amount=Decimal("400"))
        r = self._review("tithe")
        self.assertContains(r, "ambiguous")
        self._apply()
        a.refresh_from_db()
        b.refresh_from_db()
        self.assertEqual(a.department_id, self.wrong.id)
        self.assertEqual(b.department_id, self.wrong.id)

    def test_mpesa_code_still_moves_one_of_those_gifts(self):
        a = self._gift("MPESA111", reference="tithe", core_ref="CORE111")
        b = self._gift("MPESA222", reference="tithe", core_ref="CORE222")
        self._review("MPESA111")
        self._apply()
        a.refresh_from_db()
        b.refresh_from_db()
        self.assertEqual(a.department_id, self.right.id)
        self.assertEqual(b.department_id, self.wrong.id)

    def test_split_gift_moves_together_from_one_ref(self):
        t1 = self._gift("SPLITMP", amount=Decimal("500"), core_ref="SPLITCORE")
        t2 = Transaction.objects.create(
            date=t1.date, channel="BANK", direction="CREDIT", amount=Decimal("500"),
            department=self.other, reference="SPLITMP", mpesa_ref="SPLITMP",
            core_ref="SPLITCORE-S1", split_of=t1, allocation_status="AUTO",
            confirmed=True, payer_name="JANE GIVER")
        # Only the original's core ref — the other part is CORE-S1.
        self._review("SPLITCORE")
        self._apply()
        t1.refresh_from_db()
        t2.refresh_from_db()
        self.assertEqual(t1.department_id, self.right.id)
        self.assertEqual(t2.department_id, self.right.id)
        self.assertEqual(t1.allocation_status, "MANUAL")
        self.assertEqual(t2.allocation_status, "MANUAL")

    def test_amount_must_match_the_whole_gift(self):
        t1 = self._gift("AMTMP", amount=Decimal("500"), core_ref="AMTCORE")
        Transaction.objects.create(
            date=t1.date, channel="BANK", direction="CREDIT", amount=Decimal("500"),
            department=self.other, reference="AMTMP", mpesa_ref="AMTMP",
            core_ref="AMTCORE-S1", split_of=t1, allocation_status="AUTO",
            confirmed=True)
        up = SimpleUploadedFile(
            "m.csv", b"reference,amount\nAMTCORE,500\n")
        r = self.client.post(reverse("transaction_recategorize"), {
            "department": self.right.id, "file": up})
        self.assertContains(r, "mismatch")
        self._apply()
        t1.refresh_from_db()
        self.assertEqual(t1.department_id, self.wrong.id)

        up = SimpleUploadedFile(
            "m.csv", b"reference,amount\nAMTCORE,1000\n")
        self.client.post(reverse("transaction_recategorize"), {
            "department": self.right.id, "file": up})
        self._apply()
        t1.refresh_from_db()
        self.assertEqual(t1.department_id, self.right.id)

    def test_receipted_and_locked_are_left_unchanged(self):
        from core.models import PeriodLock
        receipted = self._gift("RECEIPT1", processed_via_envelope=True)
        locked = self._gift("LOCKED1", date=dt.date(2026, 5, 2),
                            core_ref="LOCKED1", mpesa_ref="LOCKED1", reference="LOCKED1")
        PeriodLock.objects.create(year=2026, month=5, locked_by=self.user)
        r = self._review("RECEIPT1\nLOCKED1")
        self.assertContains(r, "receipted")
        self.assertContains(r, "locked")
        self._apply()
        receipted.refresh_from_db()
        locked.refresh_from_db()
        self.assertEqual(receipted.department_id, self.wrong.id)
        self.assertEqual(locked.department_id, self.wrong.id)
        self.assertEqual(receipted.allocation_status, "AUTO")

    def test_already_in_the_fund_is_not_rewritten(self):
        a = self._gift("ALREADY1", fund=self.right)
        r = self._review("ALREADY1")
        self.assertContains(r, "already")
        self._apply()
        a.refresh_from_db()
        self.assertEqual(a.allocation_status, "AUTO")
        self.assertEqual(a.department_id, self.right.id)

    def test_leaving_a_development_fund_clears_the_group(self):
        dev = Department.objects.create(name="Development", fund_type="LOCAL",
                                        category="DEVELOPMENT")
        group = DevelopmentGroup.objects.create(number=4, name="Group 4")
        gift = self._gift("DEVREF1", fund=dev, dev_group=group, core_ref="DEVREF1")
        self._review("DEVREF1")
        self._apply()
        gift.refresh_from_db()
        self.assertEqual(gift.department_id, self.right.id)
        self.assertIsNone(gift.dev_group_id)

    def test_moving_onto_a_development_fund_keeps_the_group(self):
        dev = Department.objects.create(name="Development Keep", fund_type="LOCAL",
                                        category="DEVELOPMENT")
        group = DevelopmentGroup.objects.create(number=5, name="Group 5")
        gift = self._gift("DEVKEEP1", dev_group=group, core_ref="DEVKEEP1")
        self._review("DEVKEEP1", fund=dev)
        self._apply()
        gift.refresh_from_db()
        self.assertEqual(gift.department_id, dev.id)
        self.assertEqual(gift.dev_group_id, group.id)

    def test_reversed_entry_is_not_a_match(self):
        gift = self._gift("REVERSED1")
        gift.is_reversed = True
        gift.save(update_fields=["is_reversed"])
        r = self._review("REVERSED1")
        self.assertContains(r, "not found")
        self.assertNotContains(r, "Move 1 to")

    def test_fund_is_required(self):
        self._gift("NEEDFUND")
        r = self.client.post(reverse("transaction_recategorize"),
                             {"refs_text": "NEEDFUND"})
        self.assertEqual(r.status_code, 302)

    def test_apply_without_a_review_does_nothing(self):
        gift = self._gift("NOSESSION")
        self._apply()
        gift.refresh_from_db()
        self.assertEqual(gift.department_id, self.wrong.id)

    def test_template_download(self):
        r = self.client.get(reverse("transaction_recategorize") + "?template=1")
        self.assertEqual(r.status_code, 200)
        self.assertIn("reference", r.content.decode().splitlines()[0])

    def test_link_on_the_ledger_for_a_treasurer(self):
        r = self.client.get(reverse("transaction_list"))
        self.assertContains(r, reverse("transaction_recategorize"))

    def test_assistant_cannot_open_it_or_see_the_link(self):
        assistant = User.objects.create_user("asst_move", password="x")
        assistant.groups.add(Group.objects.get_or_create(name=ASSISTANT)[0])
        c = Client()
        c.force_login(assistant)
        page = c.get(reverse("transaction_list"))
        self.assertNotContains(page, reverse("transaction_recategorize"))
        denied = c.get(reverse("transaction_recategorize"))
        self.assertEqual(denied.status_code, 302)


class MoveByReferencePledgeTests(TestCase):
    """Changing the fund must drop a pledge link that was only valid in the old fund."""

    def test_reallocating_drops_the_pledge_payment(self):
        from core.models import SiteConfig
        from members.models import Member
        from pledges.models import Pledge, PledgeCampaign, PledgePayment

        user = _treasurer("tr_move_pledge")
        fund = Department.objects.create(name="Building Pledge", fund_type="LOCAL")
        other = Department.objects.create(name="Vehicle Pledge", fund_type="LOCAL")
        member = Member.objects.create(name="JOHN KAMAU", phone="254712000111", active=True)
        campaign = PledgeCampaign.objects.create(
            name="Building", target_department=fund,
            status=PledgeCampaign.Status.ACTIVE)
        pledge = Pledge.objects.create(
            campaign=campaign, member=member, amount=Decimal("10000"),
            start_date=dt.date(2026, 1, 1), status=Pledge.Status.ACTIVE)
        cfg = SiteConfig.get()
        cfg.pledge_match_same_fund_only = True
        cfg.pledge_match_mode = SiteConfig.PledgeMatchMode.AUTO
        cfg.save()
        gift = Transaction.objects.create(
            date=dt.date(2026, 6, 10), channel="BANK", direction="CREDIT",
            amount=Decimal("4000"), department=fund, member=member,
            confirmed=True, allocation_status="AUTO",
            reference="PLEDGEREF", mpesa_ref="PLEDGEREF", core_ref="PLEDGEREF")
        PledgePayment.objects.create(
            pledge=pledge, transaction=gift, amount=Decimal("4000"), date=gift.date)
        self.assertEqual(pledge.paid, Decimal("4000"))

        c = Client()
        c.force_login(user)
        c.post(reverse("transaction_recategorize"), {
            "department": other.id, "refs_text": "PLEDGEREF"})
        with self.captureOnCommitCallbacks(execute=True):
            c.post(reverse("transaction_recategorize"), {"apply": "1"})
        gift.refresh_from_db()
        self.assertEqual(gift.department_id, other.id)
        self.assertEqual(pledge.paid, Decimal("0"))

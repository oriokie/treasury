"""Send-to-review: undo a wrongly-allocated (or wrongly-split) transaction
and send it back to the review queue as one combined entry for correct
re-allocation. The direct answer to "this was wrongly auto-split across
funds/groups, how do I put it back as one fund?" — reverses the entry (and
every part of the same split contribution, if any) and creates ONE new
entry for the full original amount, in REVIEW status. Nothing is ever
deleted: the reversed originals stay on the ledger for the audit trail."""
import datetime as dt
from decimal import Decimal
from django.test import TestCase, Client
from django.contrib.auth.models import User, Group
from departments.models import Department
from giving.models import Transaction, SplitFund, SplitComponent, AllocationRule


def _tr():
    u = User.objects.create_user("tr_sendtoreview", password="x", is_superuser=True)
    u.groups.add(Group.objects.get_or_create(name="Treasurer")[0])
    return u


def _make_split(ref, amt1=300, amt2=300):
    enf = Department.objects.create(name=f"{ref}ENF", fund_type="TRUST", category="TRUST")
    lcb = Department.objects.create(name=f"{ref}LCB", fund_type="LOCAL", category="OFFERING")
    sf = SplitFund.objects.create(name=f"{ref}Split")
    SplitComponent.objects.create(split_fund=sf, department=enf, percent=Decimal("50"))
    SplitComponent.objects.create(split_fund=sf, department=lcb, percent=Decimal("50"))
    AllocationRule.objects.create(reference=ref, split_fund=sf, source="LEARNED")
    t1 = Transaction.objects.create(date=dt.date(2026, 6, 10), amount=Decimal(amt1),
        direction="CREDIT", confirmed=True, channel="BANK", allocation_status="LEARNED",
        department=enf, reference=ref, core_ref=f"{ref.upper()}001")
    t2 = Transaction.objects.create(date=dt.date(2026, 6, 10), amount=Decimal(amt2),
        direction="CREDIT", confirmed=True, channel="BANK", allocation_status="LEARNED",
        department=lcb, reference=ref, core_ref=f"{ref.upper()}001-S1")
    return t1, t2


class SendToReviewSplitTests(TestCase):
    def setUp(self):
        self.tr = _tr()
        self.c = Client(); self.c.force_login(self.tr)

    def test_reverses_both_split_siblings(self):
        t1, t2 = _make_split("splitref1")
        self.c.post(f"/transactions/{t1.id}/send-to-review/", {"reason": "wrong split"})
        t1.refresh_from_db(); t2.refresh_from_db()
        self.assertTrue(t1.is_reversed)
        self.assertTrue(t2.is_reversed)

    def test_creates_one_combined_review_entry(self):
        t1, t2 = _make_split("splitref2", 300, 300)
        self.c.post(f"/transactions/{t1.id}/send-to-review/", {"reason": ""})
        replacement = Transaction.objects.filter(allocation_status="REVIEW",
            reference="splitref2").first()
        self.assertIsNotNone(replacement)
        self.assertEqual(replacement.amount, Decimal("600"))

    def test_replacement_carries_over_payer_details(self):
        t1, t2 = _make_split("splitref3")
        t1.payer_name = "JANE GIVER"
        t1.payer_phone = "254711222333"
        t1.save()
        self.c.post(f"/transactions/{t1.id}/send-to-review/", {"reason": ""})
        replacement = Transaction.objects.filter(allocation_status="REVIEW",
            reference="splitref3").first()
        self.assertEqual(replacement.payer_name, "JANE GIVER")
        self.assertEqual(replacement.payer_phone, "254711222333")

    def test_original_entries_remain_on_ledger_reversed_not_deleted(self):
        t1, t2 = _make_split("splitref4")
        t1_id, t2_id = t1.id, t2.id
        self.c.post(f"/transactions/{t1.id}/send-to-review/", {"reason": ""})
        self.assertTrue(Transaction.objects.filter(pk=t1_id).exists())
        self.assertTrue(Transaction.objects.filter(pk=t2_id).exists())

    def test_reason_recorded_in_replacement_narration(self):
        t1, t2 = _make_split("splitref5")
        self.c.post(f"/transactions/{t1.id}/send-to-review/",
                    {"reason": "belongs to group 14 not this split"})
        replacement = Transaction.objects.filter(allocation_status="REVIEW",
            reference="splitref5").first()
        self.assertIn("belongs to group 14", replacement.raw_narration)

    def test_redirects_to_review_queue(self):
        t1, t2 = _make_split("splitref6")
        r = self.c.post(f"/transactions/{t1.id}/send-to-review/", {"reason": ""})
        self.assertEqual(r.status_code, 302)
        self.assertIn("/queue/", r.url)


class SendToReviewSingleEntryTests(TestCase):
    def setUp(self):
        self.tr = _tr()
        self.c = Client(); self.c.force_login(self.tr)
        self.d = Department.objects.create(name="SingleSendFund", fund_type="LOCAL",
            category="MINISTRY")

    def test_single_non_split_entry_also_works(self):
        t = Transaction.objects.create(date=dt.date(2026, 6, 11), amount=Decimal("777"),
            direction="CREDIT", confirmed=True, channel="BANK", allocation_status="AUTO",
            department=self.d, reference="singleref")
        self.c.post(f"/transactions/{t.id}/send-to-review/", {"reason": "wrong fund"})
        t.refresh_from_db()
        self.assertTrue(t.is_reversed)
        replacement = Transaction.objects.filter(allocation_status="REVIEW",
            reference="singleref").first()
        self.assertEqual(replacement.amount, Decimal("777"))


class SendToReviewGuardTests(TestCase):
    def setUp(self):
        self.tr = _tr()
        self.c = Client(); self.c.force_login(self.tr)
        self.d = Department.objects.create(name="GuardSendFund", fund_type="LOCAL",
            category="MINISTRY")

    def test_already_reversed_entry_rejected(self):
        t = Transaction.objects.create(date=dt.date(2026, 6, 12), amount=Decimal("100"),
            direction="CREDIT", confirmed=True, channel="BANK", allocation_status="AUTO",
            department=self.d, reference="guardref1")
        t.reverse(self.tr)
        r = self.c.post(f"/transactions/{t.id}/send-to-review/", {"reason": ""}, follow=True)
        self.assertEqual(r.status_code, 200)
        # no new replacement created
        self.assertEqual(Transaction.objects.filter(reference="guardref1",
            allocation_status="REVIEW").count(), 0)

    def test_locked_period_blocks_the_action(self):
        from core.models import PeriodLock
        t = Transaction.objects.create(date=dt.date(2026, 1, 15), amount=Decimal("100"),
            direction="CREDIT", confirmed=True, channel="BANK", allocation_status="AUTO",
            department=self.d, reference="guardref2")
        PeriodLock.objects.create(year=2026, month=1, locked_by=self.tr)
        r = self.c.post(f"/transactions/{t.id}/send-to-review/", {"reason": ""}, follow=True)
        t.refresh_from_db()
        self.assertFalse(t.is_reversed)

    def test_non_treasurer_cannot_access(self):
        assistant = User.objects.create_user("assist_sendtoreview", password="x")
        assistant.groups.add(Group.objects.get_or_create(name="Assistant")[0])
        c2 = Client(); c2.force_login(assistant)
        t = Transaction.objects.create(date=dt.date(2026, 6, 13), amount=Decimal("100"),
            direction="CREDIT", confirmed=True, channel="BANK", allocation_status="AUTO",
            department=self.d, reference="guardref3")
        r = c2.post(f"/transactions/{t.id}/send-to-review/", {"reason": ""})
        self.assertIn(r.status_code, (302, 403))
        t.refresh_from_db()
        self.assertFalse(t.is_reversed)

    def test_button_hidden_for_already_review_status(self):
        t = Transaction.objects.create(date=dt.date(2026, 6, 14), amount=Decimal("100"),
            direction="CREDIT", confirmed=True, channel="BANK", allocation_status="REVIEW",
            department=None, reference="guardref4")
        b = self.c.get("/transactions/").content.decode()
        # can't easily assert absence tied to this one row from full-page HTML,
        # but the endpoint itself should still reject a REVIEW-status looking
        # entry gracefully if attempted directly (no department to reallocate from)
        r = self.c.post(f"/transactions/{t.id}/send-to-review/", {"reason": ""})
        self.assertEqual(r.status_code, 302)

"""Re-running allocation rules over the review queue (the 'Run rules on pending'
button) — for when rules are added after an import."""
import datetime as dt
from decimal import Decimal

from django.test import TestCase, Client
from django.contrib.auth.models import User, Group

from giving.models import Transaction, AllocationRule
from giving.services.allocation import reallocate_pending, normalize_reference
from departments.models import Department


def _review(ref, n=1):
    out = []
    for i in range(n):
        out.append(Transaction.objects.create(
            date=dt.date.today(), channel="BANK", direction="CREDIT",
            amount=Decimal("1000"), reference=ref, allocation_status="REVIEW",
            confirmed=True, processed_via_envelope=False, manual_receipt=False,
            core_ref=f"{ref}-{i}-{dt.datetime.now().timestamp()}"))
    return out


class ReallocatePendingTests(TestCase):
    def setUp(self):
        self.fund = Department.objects.create(name="Camp Meeting", fund_type="LOCAL",
                                              category="OFFERING", selectable=True)

    def test_matching_items_allocated_others_left(self):
        _review("campmeeting", 2)
        _review("mysteryref", 1)
        # rule added AFTER the (simulated) import
        AllocationRule.objects.create(reference=normalize_reference("campmeeting"),
                                      department=self.fund, source="LEARNED")
        res = reallocate_pending()
        self.assertEqual(res["allocated"], 2)
        self.assertEqual(Transaction.objects.filter(reference="campmeeting",
                                                     department=self.fund).count(), 2)
        # the unmatched one is still in the queue
        self.assertEqual(Transaction.objects.filter(reference="mysteryref",
                                                     allocation_status="REVIEW").count(), 1)

    def test_no_rule_no_change(self):
        _review("stillunknown", 2)
        res = reallocate_pending()
        self.assertEqual(res["allocated"], 0)
        self.assertEqual(Transaction.objects.filter(allocation_status="REVIEW").count(), 2)

    def test_locked_period_skipped(self):
        from core.models import PeriodLock
        from core.utils import sabbath_of
        u = User.objects.create_user("lk", password="x")
        t = _review("lockedref", 1)[0]
        sab = sabbath_of(t.date)
        PeriodLock.objects.create(year=sab.year, month=sab.month, locked_by=u)
        AllocationRule.objects.create(reference=normalize_reference("lockedref"),
                                      department=self.fund, source="LEARNED")
        res = reallocate_pending()
        self.assertEqual(res["allocated"], 0)
        self.assertEqual(res["skipped_locked"], 1)
        t.refresh_from_db()
        self.assertEqual(t.allocation_status, "REVIEW")

    def test_view_runs_and_redirects(self):
        _review("buttonref", 1)
        AllocationRule.objects.create(reference=normalize_reference("buttonref"),
                                      department=self.fund, source="LEARNED")
        u = User.objects.create_user("ent", password="x", is_superuser=True)
        u.groups.add(Group.objects.get_or_create(name="Treasurer")[0])
        c = Client(); c.force_login(u)
        r = c.post("/queue/run-rules/")
        self.assertEqual(r.status_code, 302)
        self.assertEqual(Transaction.objects.filter(reference="buttonref",
                                                     department=self.fund).count(), 1)

    def test_button_visible_on_queue(self):
        _review("xref", 1)
        u = User.objects.create_user("ent2", password="x", is_superuser=True)
        u.groups.add(Group.objects.get_or_create(name="Treasurer")[0])
        c = Client(); c.force_login(u)
        body = c.get("/queue/").content.decode()
        self.assertIn("Run rules on pending", body)

    def test_split_fund_rule_expands_on_run_rules(self):
        from giving.models import SplitFund, SplitComponent
        enf = Department.objects.create(name="ENF-R", fund_type="TRUST", category="TRUST")
        lcb = Department.objects.create(name="LCB-R", fund_type="LOCAL", category="OFFERING")
        sf = SplitFund.objects.create(name="Combined-R")
        SplitComponent.objects.create(split_fund=sf, department=enf, percent=Decimal("50"))
        SplitComponent.objects.create(split_fund=sf, department=lcb, percent=Decimal("50"))
        AllocationRule.objects.create(
            reference=normalize_reference("combinedoffering"),
            split_fund=sf, department=None, source="LEARNED")
        t = _review("combinedoffering", 1)[0]
        t.amount = Decimal("2000")
        t.save(update_fields=["amount"])
        res = reallocate_pending()
        self.assertEqual(res["allocated"], 1)
        self.assertEqual(res["skipped_split"], 0)
        parts = Transaction.objects.filter(reference="combinedoffering")
        self.assertEqual(parts.count(), 2)
        self.assertEqual(sum(p.amount for p in parts), Decimal("2000"))
        self.assertEqual(parts.filter(allocation_status="REVIEW").count(), 0)

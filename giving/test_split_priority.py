"""A configured split fund must win over a stray single-fund rule for the same
reference, so e.g. a 'Combined Offering' split never lands wholly in one account."""
import datetime as dt
from decimal import Decimal

from django.test import TestCase

from departments.models import Department
from giving.models import AllocationRule, SplitFund, SplitComponent
from giving.services.allocation import allocate, normalize_reference


class SplitFundPriorityTests(TestCase):
    def setUp(self):
        self.sabbath = Department.objects.create(name="13th Sabbath Offering Account",
                                                 fund_type="TRUST", category="TRUST")
        enf = Department.objects.create(name="Combined ENF", fund_type="TRUST", category="TRUST")
        lcb = Department.objects.create(name="Combined LCB", fund_type="LOCAL", category="OFFERING")
        self.split = SplitFund.objects.create(name="Combined Offering")
        SplitComponent.objects.create(split_fund=self.split, department=enf, percent=Decimal("50"))
        SplitComponent.objects.create(split_fund=self.split, department=lcb, percent=Decimal("50"))
        self.ref = normalize_reference("combined offering")

    def test_split_wins_when_stray_dept_rule_is_older(self):
        AllocationRule.objects.create(reference=self.ref, department=self.sabbath, source="LEARNED")
        AllocationRule.objects.create(reference=self.ref, split_fund=self.split, source="SEED")
        resolver, _ = allocate("combined offering")
        self.assertEqual(resolver, self.split)

    def test_split_wins_when_stray_dept_rule_is_newer(self):
        AllocationRule.objects.create(reference=self.ref, split_fund=self.split, source="SEED")
        AllocationRule.objects.create(reference=self.ref, department=self.sabbath, source="LEARNED")
        resolver, _ = allocate("combined offering")
        self.assertEqual(resolver, self.split)

    def test_split_amount_divides_not_all_to_sabbath(self):
        AllocationRule.objects.create(reference=self.ref, department=self.sabbath, source="LEARNED")
        AllocationRule.objects.create(reference=self.ref, split_fund=self.split, source="SEED")
        resolver, _ = allocate("combined offering")
        parts = resolver.split(Decimal("1000"))
        # two equal halves, neither to the 13th Sabbath account
        self.assertEqual(len(parts), 2)
        self.assertTrue(all(d.id != self.sabbath.id for d, _ in parts))
        self.assertEqual(sum(a for _, a in parts), Decimal("1000"))

    def test_single_dept_rule_still_works(self):
        # no split fund involved -> ordinary department rule resolves normally
        ref = normalize_reference("just tithe")
        d = Department.objects.create(name="Tithe Acct", fund_type="TRUST", category="TRUST")
        AllocationRule.objects.create(reference=ref, department=d, source="SEED")
        resolver, _ = allocate("just tithe")
        self.assertEqual(resolver, d)

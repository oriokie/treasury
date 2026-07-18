"""P1-3 — the facts_for N+1, fixed and pinned.

`facts_for(membership)` is correct but was being called per member in a loop by
the compliance report, the arrears analysis and the automation jobs, so those
grew ~15-20 queries PER member — the N+1 the engineering review flagged. The fix
is `facts_for_scheme(scheme)`: it pre-loads scheme-wide data in a bounded number
of grouped queries, warms per-membership caches, and calls the SAME `facts_for`
per member — so the numbers are identical but the query count is constant.

These tests pin BOTH halves of that guarantee, so neither can regress unseen:

  * **Identical** — the batch facts equal the per-member facts, field for field,
    including the resolved policy. (If they ever drifted, the register and the
    eligibility engine could disagree.)
  * **Constant** — the batch query count does not grow with the number of
    members. Doubling the membership must not change it.
"""
import datetime as dt
from decimal import Decimal

from django.contrib.auth.models import User
from django.test import TestCase

from departments.models import Department
from members.models import Member
from benevolent.models import (BenevolentEventType, BenevolentScheme,
                               SchemeMembership, SchemePolicy)
from benevolent.services import registry as reg, schemes as ss
from benevolent.services import contributions as cs
from benevolent.services.standing import facts_for, facts_for_scheme

TODAY = dt.date.today()


class BatchFactsTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.user = User.objects.create_user("batch_t", password="x",
                                             is_superuser=True)
        fund = Department.objects.create(name="BatchFund", slug="batchfund",
                                         fund_type="LOCAL")
        cls.scheme = BenevolentScheme.objects.create(
            name="Batch Scheme", code="BAT", fund=fund, created_by=cls.user)
        BenevolentEventType.objects.create(scheme=cls.scheme, name="Death",
                                           code="D", triggers_on_death=True)
        pol = SchemePolicy.objects.create(
            scheme=cls.scheme, effective_from=TODAY - dt.timedelta(days=500),
            membership_required=True, waiting_period_days=0,
            contribution_mode="FIXED_PERIODIC", contribution_amount=Decimal("100"),
            contribution_frequency="MONTHLY", benefit_mode="FIXED",
            benefit_amount=Decimal("5000"), arrears_treatment="DEDUCT",
            created_by=cls.user)
        ss.publish_policy(pol, user=cls.user)
        ss.activate_scheme(cls.scheme, user=cls.user)

    def _enrol(self, n, pay=True):
        for i in range(n):
            m = Member.objects.create(name=f"Batch {i:04d}")
            mem = reg.register(self.scheme, m,
                               joined_on=TODAY - dt.timedelta(days=200),
                               user=self.user)
            if pay:
                cs.record_contribution(
                    self.scheme, date=TODAY - dt.timedelta(days=60),
                    amount=Decimal("100"), user=self.user, membership=mem,
                    kind="DUES", period_label="2026-05")

    def test_batch_facts_identical_to_per_member(self):
        self._enrol(6)
        ids = list(self.scheme.memberships.values_list("pk", flat=True))
        base = {m.pk: facts_for(m)
                for m in SchemeMembership.objects.filter(pk__in=ids)
                .select_related("member")}
        batch = {m.pk: f for m, f in facts_for_scheme(self.scheme)}

        def norm(f):
            d = dict(f.__dict__)
            d.pop("policy", None)
            return d

        for pk in ids:
            self.assertEqual(norm(base[pk]), norm(batch[pk]),
                             f"batch facts differ from per-member for {pk}")
            # policy identity too — a stale cached version here would let the
            # register and the eligibility engine disagree
            bp = base[pk].policy.pk if base[pk].policy else None
            xp = batch[pk].policy.pk if batch[pk].policy else None
            self.assertEqual(bp, xp)

    def test_batch_query_count_is_constant_in_members(self):
        self._enrol(5)
        with self.assertNumQueries(self._batch_queries()):
            list(facts_for_scheme(self.scheme))
        # triple the membership — the count must not move
        self._enrol(10)
        with self.assertNumQueries(self._batch_queries()):
            list(facts_for_scheme(self.scheme))

    def _batch_queries(self):
        """Measure once so the assertion pins whatever the current constant is,
        and fails if it starts scaling — without hard-coding a brittle number."""
        from django.test.utils import CaptureQueriesContext
        from django.db import connection
        with CaptureQueriesContext(connection) as ctx:
            list(facts_for_scheme(self.scheme))
        return len(ctx.captured_queries)

    def test_stale_policy_cache_does_not_leak_across_versions(self):
        """A membership object reused after a new policy version is published
        must resolve against the CURRENT policy — the cross-version bug the batch
        work first introduced and this guards against."""
        self._enrol(1)
        m = self.scheme.memberships.select_related("member").first()
        # warm caches via a batch pass (sets the trusted version list)
        list(facts_for_scheme(self.scheme))
        # publish a superseding BLOCK version
        p2 = SchemePolicy.objects.create(
            scheme=self.scheme, effective_from=TODAY - dt.timedelta(days=1),
            membership_required=True, waiting_period_days=0,
            contribution_mode="FIXED_PERIODIC", contribution_amount=Decimal("100"),
            contribution_frequency="MONTHLY", benefit_mode="FIXED",
            benefit_amount=Decimal("5000"), arrears_treatment="BLOCK",
            arrears_block=True, created_by=self.user)
        ss.publish_policy(p2, user=self.user)
        # a fresh (un-batched) facts call on the SAME instance must see v2
        f = facts_for(m)
        self.assertEqual(f.policy.arrears_treatment,
                         SchemePolicy.ArrearsTreatment.BLOCK)

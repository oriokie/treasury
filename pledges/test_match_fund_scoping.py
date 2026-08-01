"""What counts as giving toward a pledge.

A pledge is a promise to a particular appeal, so the money that fulfils it has
to be money given to that appeal. The matcher used to ask a weaker question,
and got two things wrong in opposite directions:

    camp_dept = pledge.campaign.target_department_id
    if camp_dept and t.department_id and t.department_id != camp_dept:
        continue

  1. `and t.department_id` — an UNALLOCATED gift skipped the test entirely and
     was applied to the pledge. Unallocated means nobody has yet said what the
     money was for, which is the opposite of evidence that it was for this.
  2. The comparison was against the campaign's fund alone, so a gift to one of
     its SUB-ACCOUNTS was rejected. Church funds are a tree on purpose — a camp
     meeting appeal names CAMP MEETING, but no money is ever recorded against
     it: it lands in CAMP_1 … CAMP_30. So the one comparison excluded every
     real contribution and admitted the ones that were never meant for the
     appeal.

Between them, a member's tithe could pay off their building-fund pledge while
their actual building-fund gift did not.
"""
import datetime as dt
from decimal import Decimal

from django.contrib.auth.models import Group, User
from django.test import TestCase

from core.roles import TREASURER
from departments.models import Department
from giving.models import Transaction
from members.models import Member
from pledges.models import Pledge, PledgeCampaign, PledgePayment
from pledges.services import matching as match_svc

TODAY = dt.date(2026, 6, 1)


class _Appeal(TestCase):
    """A building appeal whose money lands in per-group sub-accounts, plus an
    unrelated fund (tithe) to give the member somewhere else to give."""

    def setUp(self):
        self.user = User.objects.create_user("pm_tr", password="x", is_superuser=True)
        self.user.groups.add(Group.objects.get_or_create(name=TREASURER)[0])

        self.appeal_fund = Department.objects.create(
            name="Building Appeal", slug="build-appeal",
            fund_type=Department.FundType.LOCAL,
            category=Department.Category.MINISTRY)
        self.group_fund = Department.objects.create(
            name="BUILD_1", slug="build-1", parent=self.appeal_fund,
            category=Department.Category.MINISTRY)
        self.deep_fund = Department.objects.create(
            name="BUILD_1_A", slug="build-1a", parent=self.group_fund,
            category=Department.Category.MINISTRY)
        self.other_fund = Department.objects.create(
            name="Tithe", slug="tithe-pm", fund_type=Department.FundType.TRUST,
            category=Department.Category.MINISTRY)

        self.campaign = PledgeCampaign.objects.create(
            name="Build 2026", target_department=self.appeal_fund,
            start_date=dt.date(2026, 1, 1))
        self.member = Member.objects.create(name="PLEDGE GIVER")
        self.pledge = Pledge.objects.create(
            campaign=self.campaign, member=self.member, amount=Decimal("10000"),
            start_date=dt.date(2026, 1, 1), status=Pledge.Status.ACTIVE)

    def _gift(self, amount, department, member=None):
        return Transaction.objects.create(
            date=TODAY, amount=Decimal(amount), direction="CREDIT", channel="BANK",
            confirmed=True, allocation_status="MANUAL",
            department=department, member=member or self.member)

    def _candidate_ids(self):
        return {t.id for t in match_svc.candidate_contributions(self.pledge)}


class WhatCountsTests(_Appeal):
    def test_a_gift_to_the_campaign_fund_counts(self):
        t = self._gift("1000", self.appeal_fund)
        self.assertIn(t.id, self._candidate_ids())

    def test_a_gift_to_a_subgroup_fund_counts(self):
        """The reported case. On a real sheet this is where ALL the money is."""
        t = self._gift("1000", self.group_fund)
        self.assertIn(t.id, self._candidate_ids(),
                      "a gift to the appeal's own sub-account was not counted")

    def test_a_gift_deeper_in_the_tree_counts(self):
        t = self._gift("1000", self.deep_fund)
        self.assertIn(t.id, self._candidate_ids())

    def test_a_gift_to_an_unrelated_fund_does_not_count(self):
        """"Not any amount that the member sent." Their tithe is not their
        building pledge."""
        t = self._gift("1000", self.other_fund)
        self.assertNotIn(t.id, self._candidate_ids())

    def test_an_unallocated_gift_does_not_count(self):
        """It used to, because the check was skipped when the gift named no
        fund. Nobody has said what this money was for yet."""
        t = self._gift("1000", None)
        self.assertNotIn(t.id, self._candidate_ids(),
                         "money with no fund on it was applied to a pledge")

    def test_a_campaign_with_no_fund_cannot_be_scoped(self):
        """Nothing to compare against, so the older behaviour stands rather
        than silently matching nothing and leaving a treasurer wondering why
        auto-match stopped working."""
        self.campaign.target_department = None
        self.campaign.save()
        t = self._gift("1000", self.other_fund)
        self.assertIn(t.id, self._candidate_ids())


class AutoMatchTests(_Appeal):
    def test_it_applies_a_subgroup_gift(self):
        self._gift("4000", self.group_fund)
        applied = match_svc.auto_match_pledge(self.pledge, user=self.user)
        self.assertEqual(applied, Decimal("4000"))

    def test_it_refuses_an_unrelated_gift(self):
        self._gift("4000", self.other_fund)
        applied = match_svc.auto_match_pledge(self.pledge, user=self.user)
        self.assertEqual(applied, Decimal("0"))
        self.assertFalse(PledgePayment.objects.exists())

    def test_a_members_tithe_cannot_pay_off_their_building_pledge(self):
        """Both at once, which is the combination that made the figures wrong:
        the tithe was applied and the real appeal gift was not."""
        tithe = self._gift("9000", self.other_fund)
        real = self._gift("1000", self.group_fund)
        match_svc.auto_match_pledge(self.pledge, user=self.user)
        matched = set(PledgePayment.objects.values_list("transaction_id", flat=True))
        self.assertEqual(matched, {real.id})

    def test_the_sweep_scopes_by_fund_as_well(self):
        self._gift("4000", self.other_fund)
        touched, total = match_svc.auto_match_all(user=self.user)
        self.assertEqual((touched, total), (0, Decimal("0")))

    def test_the_sweep_resolves_each_campaigns_funds_once(self):
        """Every pledge of one campaign walks the same subtree. Doing it per
        pledge put a bulk sweep back on two queries per pledge for an answer it
        already had."""
        from django.db import connection
        from django.test.utils import CaptureQueriesContext
        for i in range(6):
            m = Member.objects.create(name=f"SWEEP GIVER {i}")
            Pledge.objects.create(campaign=self.campaign, member=m,
                                  amount=Decimal("500"),
                                  start_date=dt.date(2026, 1, 1),
                                  status=Pledge.Status.ACTIVE)
        with CaptureQueriesContext(connection) as ctx:
            match_svc.auto_match_all(user=self.user)
        # `parent_id" IN` is the walk's own WHERE clause. Matching on
        # "parent_id" alone counts every ordinary Department select too — the
        # manager select_relates the parent, so the column is in all of them.
        walks = [q for q in ctx.captured_queries if 'parent_id" IN' in q["sql"]]
        self.assertLessEqual(
            len(walks), 3,
            f"the fund subtree was walked {len(walks)} times for 7 pledges of "
            "one campaign; it should be resolved once and reused")


class PartApplifiedGiftTests(_Appeal):
    """The remainder of a gift that only partly filled a pledge.

    `already_matched_txn_ids` excluded every contribution any pledge had ever
    touched, so the unspent part of one was stranded for good — a member who
    gave 10,000 against a 4,000 pledge had 6,000 of their own money made
    invisible, and the pledge went on reading as unpaid however much they gave.
    The treasurer then chases somebody who has already paid.

    That this was wrong was visible in the code it fed: `suggest_matches_for_pledge`
    subtracts what has already been applied to each candidate, and that
    subtraction could never once have found anything to subtract.
    """

    def test_the_unapplied_remainder_stays_available(self):
        self._gift("10000", self.group_fund)
        self.assertEqual(match_svc.auto_match_pledge(self.pledge, user=self.user),
                         Decimal("10000"))

    def test_a_raised_pledge_can_draw_on_what_is_left(self):
        self.pledge.amount = Decimal("4000")
        self.pledge.save()
        self._gift("10000", self.group_fund)
        match_svc.auto_match_pledge(self.pledge, user=self.user)
        self.pledge.refresh_from_db()
        self.assertEqual(self.pledge.outstanding, Decimal("0"))

        self.pledge.amount = Decimal("10000")
        self.pledge.save()
        self.pledge.refresh_from_db()
        again = match_svc.auto_match_pledge(self.pledge, user=self.user)
        self.assertEqual(again, Decimal("6000"),
                         "the rest of the member's own gift was unreachable")

    def test_a_gift_is_never_applied_beyond_its_amount(self):
        """The guard the old exclusion was there for. It still has to hold —
        `free` is what enforces it now."""
        self.pledge.amount = Decimal("4000")
        self.pledge.save()
        gift = self._gift("5000", self.group_fund)
        match_svc.auto_match_pledge(self.pledge, user=self.user)

        second = Pledge.objects.create(
            campaign=self.campaign, member=self.member, amount=Decimal("50000"),
            start_date=dt.date(2026, 1, 1), status=Pledge.Status.ACTIVE)
        match_svc.auto_match_pledge(second, user=self.user)

        total = sum(pp.amount for pp in
                    PledgePayment.objects.filter(transaction=gift))
        self.assertEqual(total, Decimal("5000"),
                         "more was applied than the member actually gave")

    def test_a_fully_spent_gift_is_offered_no_further(self):
        gift = self._gift("4000", self.group_fund)
        self.pledge.amount = Decimal("4000")
        self.pledge.save()
        match_svc.auto_match_pledge(self.pledge, user=self.user)
        other = Pledge.objects.create(
            campaign=self.campaign, member=self.member, amount=Decimal("9000"),
            start_date=dt.date(2026, 1, 1), status=Pledge.Status.ACTIVE)
        self.assertNotIn(
            gift.id, {t.id for t in match_svc.candidate_contributions(other)})

    def test_suggestions_report_only_what_is_left(self):
        self.pledge.amount = Decimal("4000")
        self.pledge.save()
        self._gift("10000", self.group_fund)
        match_svc.auto_match_pledge(self.pledge, user=self.user)
        other = Pledge.objects.create(
            campaign=self.campaign, member=self.member, amount=Decimal("9000"),
            start_date=dt.date(2026, 1, 1), status=Pledge.Status.ACTIVE)
        rows = match_svc.suggest_matches_for_pledge(other)
        self.assertEqual([r["free"] for r in rows], [Decimal("6000")])


class InlineHookTests(_Appeal):
    """The same rule on the path that runs when a contribution is created."""

    def setUp(self):
        super().setUp()
        from core.models import SiteConfig
        self.cfg = SiteConfig.get()
        self.cfg.pledge_match_same_fund_only = True
        self.cfg.pledge_match_window_days = 400
        self.cfg.save()

    def _pledges_for(self, txn):
        return match_svc.active_pledges_for_contribution(txn, self.cfg)

    def test_a_subgroup_gift_finds_the_pledge(self):
        t = self._gift("1000", self.group_fund)
        self.assertIn(self.pledge, self._pledges_for(t))

    def test_an_unrelated_gift_finds_nothing(self):
        t = self._gift("1000", self.other_fund)
        self.assertEqual(self._pledges_for(t), [])

    def test_an_unallocated_gift_finds_nothing(self):
        t = self._gift("1000", None)
        self.assertEqual(self._pledges_for(t), [])

    def test_switching_the_setting_off_still_ignores_the_fund(self):
        """`pledge_match_same_fund_only` is a church's choice and stays one."""
        self.cfg.pledge_match_same_fund_only = False
        self.cfg.save()
        t = self._gift("1000", self.other_fund)
        self.assertIn(self.pledge, self._pledges_for(t))


class SubtreeHelperTests(TestCase):
    def test_it_returns_the_root_and_every_level_below(self):
        from departments.models import subtree_ids
        root = Department.objects.create(name="Root F", slug="root-f",
                                         category=Department.Category.MINISTRY)
        kid = Department.objects.create(name="Kid F", slug="kid-f", parent=root,
                                        category=Department.Category.MINISTRY)
        grandkid = Department.objects.create(name="GKid F", slug="gkid-f", parent=kid,
                                             category=Department.Category.MINISTRY)
        self.assertEqual(subtree_ids([root.id]), {root.id, kid.id, grandkid.id})

    def test_no_root_means_no_subtree(self):
        from departments.models import subtree_ids
        self.assertEqual(subtree_ids([]), set())
        self.assertEqual(subtree_ids([None]), set())

    def test_leaders_still_get_their_whole_tree(self):
        """`departments_led_by` now shares this helper — the scoping that keeps
        a leader inside their own funds must not have changed."""
        from departments.models import DepartmentLeadership, departments_led_by
        user = User.objects.create_user("subtree_leader", password="x")
        root = Department.objects.create(name="Led Root", slug="led-root",
                                         category=Department.Category.MINISTRY)
        kid = Department.objects.create(name="Led Kid", slug="led-kid", parent=root,
                                        category=Department.Category.MINISTRY)
        Department.objects.create(name="Unled", slug="unled",
                                  category=Department.Category.MINISTRY)
        DepartmentLeadership.objects.create(user=user, department=root)
        self.assertEqual({d.id for d in departments_led_by(user)}, {root.id, kid.id})

"""Per-gift verdicts on the pledge page.

The complaint that reached us was never "the sweep errored" — it was one M-Pesa
line, visible on the member's statement, that never appeared on their pledge.
These tests pin the two halves of that: a gift into a per-group sub-account of
the appeal's fund must match, and when a gift genuinely cannot match, the
pledge page must say which rule excluded it.
"""
import datetime as dt
from decimal import Decimal

from django.contrib.auth.models import Group, User
from django.test import Client, TestCase

from core.roles import TREASURER
from departments.models import Department
from giving.models import Transaction
from members.models import Member
from pledges.models import Pledge, PledgeCampaign
from pledges.services import matching as match_svc


class GiftVerdictTests(TestCase):
    """Modelled on the real record: a DEVELOPMENT appeal whose money lands in
    per-group sub-accounts (DEV_GROUP_12), paid by M-Pesa."""

    def setUp(self):
        self.user = User.objects.create_user("tr", password="x",
                                             is_superuser=True)
        self.user.groups.add(Group.objects.get_or_create(name=TREASURER)[0])
        self.dev = Department.objects.create(name="DEVELOPMENT",
                                            fund_type="LOCAL")
        self.group12 = Department.objects.create(
            name="Group 12", parent=self.dev, fund_type="LOCAL")
        self.other = Department.objects.create(name="AWM", fund_type="LOCAL")
        self.member = Member.objects.create(
            name="EDWIN KENYANSA", phone="254711165935", active=True)
        self.campaign = PledgeCampaign.objects.create(
            name="Development Phase I", target_department=self.dev,
            status=PledgeCampaign.Status.ACTIVE)
        self.pledge = Pledge.objects.create(
            campaign=self.campaign, member=self.member,
            amount=Decimal("15000"), start_date=dt.date(2026, 8, 2),
            status=Pledge.Status.ACTIVE)

    def _gift(self, amount, fund, day=20, **kw):
        opts = dict(date=dt.date(2026, 8, day), channel="BANK",
                    direction="CREDIT", amount=Decimal(amount), department=fund,
                    member=self.member, payer_name="EDWIN KENYANSA",
                    payer_phone="254711165935", confirmed=True,
                    allocation_status="AUTO")
        opts.update(kw)
        return Transaction.objects.create(**opts)

    def test_gift_into_a_group_sub_account_matches(self):
        """The money never lands on DEVELOPMENT itself — it lands in a group."""
        gift = self._gift("10", self.group12)
        ids = {c["txn"].id
               for c in match_svc.candidate_contributions(self.pledge)}
        self.assertIn(gift.id, ids)
        plan = match_svc.plan_auto_match_all()
        self.assertEqual([r["txn"].id for r in plan], [gift.id])

    def test_a_matchable_gift_is_reported_as_such(self):
        self._gift("10", self.group12)
        rows = match_svc.explain_pledge_gifts(self.pledge)
        self.assertEqual([r["verdict"] for r in rows], ["Will match"])

    def test_wrong_fund_names_both_funds(self):
        self._gift("10", self.other)
        rows = match_svc.explain_pledge_gifts(self.pledge)
        self.assertEqual(rows[0]["verdict"], "Not matched")
        self.assertIn("AWM", rows[0]["detail"])
        self.assertIn("DEVELOPMENT", rows[0]["detail"])

    def test_gift_before_the_pledge_date_says_so_with_dates(self):
        self._gift("10", self.group12, day=1)
        rows = match_svc.explain_pledge_gifts(self.pledge)
        self.assertIn("before this pledge's start date", rows[0]["detail"])
        self.assertIn("02/08/26", rows[0]["detail"])

    def test_unconfirmed_gift_is_named(self):
        self._gift("10", self.group12, confirmed=False)
        rows = match_svc.explain_pledge_gifts(self.pledge)
        self.assertIn("unconfirmed", rows[0]["detail"])

    def test_unallocated_gift_is_named(self):
        self._gift("10", None)
        rows = match_svc.explain_pledge_gifts(self.pledge)
        self.assertIn("no fund on it", rows[0]["detail"])

    def test_fully_applied_gift_is_not_reported_as_a_problem(self):
        gift = self._gift("10", self.group12)
        match_svc.auto_match_pledge(self.pledge, user=self.user)
        rows = match_svc.explain_pledge_gifts(self.pledge)
        verdicts = {r["txn"].id: r["verdict"] for r in rows}
        self.assertEqual(verdicts[gift.id], "Already applied")

    def test_the_pledge_page_explains_the_excluded_gift(self):
        self._gift("10", self.other)
        c = Client()
        c.force_login(self.user)
        r = c.get(f"/pledges/{self.pledge.id}/")
        self.assertContains(r, "not on this pledge")
        self.assertContains(r, "AWM")

    def test_the_pledge_page_stays_quiet_when_nothing_is_excluded(self):
        self._gift("10", self.group12)
        c = Client()
        c.force_login(self.user)
        r = c.get(f"/pledges/{self.pledge.id}/")
        self.assertNotContains(r, "not on this pledge")

    def test_another_members_giving_is_never_explained_here(self):
        stranger = Member.objects.create(name="TERESIA MOGIRE",
                                         phone="254718188346", active=True)
        Transaction.objects.create(
            date=dt.date(2026, 8, 20), channel="BANK", direction="CREDIT",
            amount=Decimal("5600"), department=self.other, member=stranger,
            payer_name="TERESIA MOGIRE", payer_phone="254718188346",
            confirmed=True, allocation_status="MANUAL")
        rows = match_svc.explain_pledge_gifts(self.pledge)
        self.assertEqual(rows, [])

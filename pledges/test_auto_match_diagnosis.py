"""The empty auto-match preview has to say WHY it is empty.

"Nothing to auto-match" is the same sentence whether every gift is already
linked, every promise is still a draft, or the giving went to another fund —
and a treasurer reading it cannot tell which, so the feature reads as broken
when it is working exactly as configured.
"""
import datetime as dt
from decimal import Decimal

from django.contrib.auth.models import Group, User
from django.test import Client, TestCase

from core.models import SiteConfig
from core.roles import TREASURER
from departments.models import Department
from giving.models import Transaction
from members.models import Member
from pledges.models import Pledge, PledgeCampaign, PledgePayment
from pledges.services import matching as match_svc

TODAY = dt.date(2026, 7, 15)


class AutoMatchDiagnosisTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user("diag", password="x",
                                             is_superuser=True)
        self.user.groups.add(Group.objects.get_or_create(name=TREASURER)[0])
        self.appeal_fund = Department.objects.create(name="Roof Appeal",
                                                    fund_type="LOCAL")
        self.other_fund = Department.objects.create(name="Tithe",
                                                    fund_type="TRUST")
        self.member = Member.objects.create(name="GRACE WANJIRU",
                                            phone="254711000333")
        self.campaign = PledgeCampaign.objects.create(
            name="Roof", target_department=self.appeal_fund,
            status=PledgeCampaign.Status.ACTIVE)
        self.pledge = Pledge.objects.create(
            campaign=self.campaign, member=self.member,
            amount=Decimal("60000"), start_date=dt.date(2026, 6, 1),
            status=Pledge.Status.ACTIVE)

    def _gift(self, amount, fund, date=TODAY, confirmed=True):
        return Transaction.objects.create(
            date=date, channel="BANK", direction="CREDIT",
            amount=Decimal(amount), department=fund, member=self.member,
            confirmed=confirmed, allocation_status="AUTO")

    def _reasons(self):
        return match_svc.diagnose_empty_plan()["reasons"]

    def test_a_draft_promise_is_named_as_the_blocker(self):
        """Every public-form pledge starts as a draft, and a draft is never
        matched — the commonest reason the page looks broken."""
        self.pledge.status = Pledge.Status.DRAFT
        self.pledge.save()
        self._gift("20000", self.appeal_fund)
        self.assertFalse(match_svc.plan_auto_match_all())
        joined = " ".join(self._reasons())
        self.assertIn("awaiting approval", joined)

    def test_unconfirmed_giving_is_named(self):
        self._gift("20000", self.appeal_fund, confirmed=False)
        self.assertFalse(match_svc.plan_auto_match_all())
        self.assertIn("unconfirmed", " ".join(self._reasons()))

    def test_a_gift_to_another_fund_is_named(self):
        self._gift("20000", self.other_fund)
        self.assertFalse(match_svc.plan_auto_match_all())
        joined = " ".join(self._reasons())
        self.assertIn("outside the campaign's own fund", joined)

    def test_a_gift_given_before_the_promise_is_named(self):
        self._gift("20000", self.appeal_fund, date=dt.date(2026, 5, 1))
        self.assertFalse(match_svc.plan_auto_match_all())
        self.assertIn("before the pledge date", " ".join(self._reasons()))

    def test_an_unallocated_gift_is_named(self):
        self._gift("20000", None)
        self.assertFalse(match_svc.plan_auto_match_all())
        self.assertIn("no fund on them", " ".join(self._reasons()))

    def test_already_applied_giving_is_named(self):
        gift = self._gift("60000", self.appeal_fund)
        PledgePayment.objects.create(pledge=self.pledge, transaction=gift,
                                     amount=Decimal("60000"), date=gift.date)
        self.pledge.recompute_status()
        # The promise is now full, which is its own reason.
        self.assertFalse(match_svc.plan_auto_match_all())
        self.assertIn("paid in full", " ".join(self._reasons()))

    def test_no_giving_at_all_is_named(self):
        self.assertFalse(match_svc.plan_auto_match_all())
        self.assertIn("No giving is on record", " ".join(self._reasons()))

    def test_no_open_pledges_is_named(self):
        self.pledge.status = Pledge.Status.CANCELLED
        self.pledge.save()
        self.assertIn("no active or lapsed pledges", " ".join(self._reasons()))

    def test_a_working_sweep_reports_nothing_to_diagnose(self):
        """The diagnosis must never fire while real matches exist."""
        self._gift("20000", self.appeal_fund)
        self.assertTrue(match_svc.plan_auto_match_all())

    def test_the_preview_page_shows_the_reason(self):
        self._gift("20000", self.other_fund)
        c = Client()
        c.force_login(self.user)
        r = c.get("/pledges/auto-match/")
        self.assertEqual(r.status_code, 200)
        self.assertContains(r, "Nothing to auto-match")
        self.assertContains(r, "outside the campaign&#x27;s own fund")

    def test_the_preview_says_nothing_is_applied_yet(self):
        """The dashboard button used to apply matches on the spot; it now only
        opens this page, so the page has to say that the job is unfinished."""
        self._gift("20000", self.appeal_fund)
        c = Client()
        c.force_login(self.user)
        r = c.get("/pledges/auto-match/")
        self.assertContains(r, "Nothing is linked yet")
        self.assertContains(r, "Apply")

    def test_the_window_setting_is_quoted_in_the_reason(self):
        cfg = SiteConfig.get()
        cfg.pledge_match_window_days = 30
        cfg.save()
        self.pledge.end_date = dt.date(2026, 6, 10)
        self.pledge.save()
        self._gift("20000", self.appeal_fund, date=dt.date(2026, 9, 30))
        self.assertFalse(match_svc.plan_auto_match_all())
        self.assertIn("30-day", " ".join(self._reasons()))

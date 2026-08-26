"""Extra phones / family members on a pledge widen auto-match identity."""
import datetime as dt
from decimal import Decimal

from django.contrib.auth.models import Group, User
from django.test import TestCase, Client
from django.urls import reverse

from core.roles import TREASURER
from departments.models import Department
from giving.models import Transaction
from members.models import Member
from pledges.models import Pledge, PledgeCampaign, PledgeMatchAlias
from pledges.services import matching as match_svc


class PledgeMatchAliasTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user("pma", password="x", is_superuser=True)
        self.user.groups.add(Group.objects.get_or_create(name=TREASURER)[0])
        self.c = Client()
        self.c.force_login(self.user)
        self.fund = Department.objects.create(name="Roof Alias", fund_type="LOCAL")
        self.husband = Member.objects.create(
            name="JOHN DOE", phone="254711111111", active=True)
        self.wife = Member.objects.create(
            name="JANE DOE", phone="254722222222", active=True)
        self.campaign = PledgeCampaign.objects.create(
            name="Roof Alias", target_department=self.fund,
            status=PledgeCampaign.Status.ACTIVE)
        self.pledge = Pledge.objects.create(
            campaign=self.campaign, member=self.husband,
            amount=Decimal("20000"), start_date=dt.date(2026, 1, 1),
            status=Pledge.Status.ACTIVE,
            submitted_contact="JOHN DOE / 254711111111")

    def test_extra_phone_matches_gift(self):
        PledgeMatchAlias.objects.create(
            pledge=self.pledge, phone="254733333333", label="Second line",
            created_by=self.user)
        gift = Transaction.objects.create(
            date=dt.date(2026, 6, 10), channel="BANK", direction="CREDIT",
            amount=Decimal("5000"), department=self.fund, member=None,
            payer_name="J DOE", payer_phone="0733333333",
            confirmed=True, allocation_status="AUTO")
        ids = {c["txn"].id for c in match_svc.candidate_contributions(self.pledge)}
        self.assertIn(gift.id, ids)
        applied = match_svc.auto_match_pledge(self.pledge, user=self.user)
        self.assertEqual(applied, Decimal("5000"))

    def test_family_member_matches_gift(self):
        PledgeMatchAlias.objects.create(
            pledge=self.pledge, member=self.wife, label="Wife",
            created_by=self.user)
        gift = Transaction.objects.create(
            date=dt.date(2026, 6, 11), channel="BANK", direction="CREDIT",
            amount=Decimal("7000"), department=self.fund, member=self.wife,
            payer_name="JANE DOE", payer_phone="254722222222",
            confirmed=True, allocation_status="AUTO")
        ids = {c["txn"].id for c in match_svc.candidate_contributions(self.pledge)}
        self.assertIn(gift.id, ids)
        self.assertEqual(
            match_svc.auto_match_pledge(self.pledge, user=self.user),
            Decimal("7000"))

    def test_wife_phone_without_member_fk_still_matches(self):
        """Spouse pays from her line; gift not linked to her register row."""
        PledgeMatchAlias.objects.create(
            pledge=self.pledge, member=self.wife, created_by=self.user)
        gift = Transaction.objects.create(
            date=dt.date(2026, 6, 12), channel="BANK", direction="CREDIT",
            amount=Decimal("3000"), department=self.fund, member=None,
            payer_name="J DOE", payer_phone="0722222222",
            confirmed=True, allocation_status="AUTO")
        ids = {c["txn"].id for c in match_svc.candidate_contributions(self.pledge)}
        self.assertIn(gift.id, ids)

    def test_unrelated_gift_still_excluded(self):
        PledgeMatchAlias.objects.create(
            pledge=self.pledge, phone="254733333333", created_by=self.user)
        other = Transaction.objects.create(
            date=dt.date(2026, 6, 10), channel="BANK", direction="CREDIT",
            amount=Decimal("5000"), department=self.fund, member=None,
            payer_name="STRANGER", payer_phone="254799999999",
            confirmed=True, allocation_status="AUTO")
        ids = {c["txn"].id for c in match_svc.candidate_contributions(self.pledge)}
        self.assertNotIn(other.id, ids)

    def test_add_and_remove_via_views(self):
        url = reverse("pledge_match_alias_add", args=[self.pledge.id])
        r = self.c.post(url, {"phone": "0744444444", "label": "Work"})
        self.assertEqual(r.status_code, 302)
        alias = self.pledge.match_aliases.get()
        self.assertEqual(alias.phone, "254744444444")
        self.assertEqual(alias.label, "Work")

        r = self.c.post(reverse("pledge_match_alias_delete",
                                args=[self.pledge.id, alias.id]))
        self.assertEqual(r.status_code, 302)
        self.assertEqual(self.pledge.match_aliases.count(), 0)

    def test_inline_hook_sees_alias_phone(self):
        PledgeMatchAlias.objects.create(
            pledge=self.pledge, phone="254755555555", created_by=self.user)
        gift = Transaction.objects.create(
            date=dt.date(2026, 6, 13), channel="BANK", direction="CREDIT",
            amount=Decimal("2000"), department=self.fund, member=None,
            payer_phone="0755555555", confirmed=True, allocation_status="AUTO")
        hits = match_svc.active_pledges_for_contribution(gift)
        self.assertIn(self.pledge, hits)

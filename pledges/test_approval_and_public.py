"""Approving pledges (single and bulk, two roles) and the per-campaign form.

Approval is what turns a promise into a figure the campaign counts. A leader
runs the appeal and knows who stood up, so they get it for their own funds —
which makes the scope rule the whole of the security here, and the reason it is
written once in ``pledges.services.approval`` and re-derived on every POST
rather than trusted from a list of ids in a form.
"""
from decimal import Decimal

from django.contrib.auth.models import Group, User
from django.test import TestCase
from django.urls import reverse

from core.models import SiteConfig
from core.roles import LEADER, TREASURER
from departments.models import Department, DepartmentLeadership
from members.models import Member
from pledges.models import Pledge, PledgeCampaign
from pledges.services import approval


class _Two(TestCase):
    """Two funds, two campaigns, one leader who holds only the first."""

    def setUp(self):
        self.treasurer = User.objects.create_user("t_appr", password="x")
        self.treasurer.groups.add(
            Group.objects.get_or_create(name=TREASURER)[0])
        self.leader = User.objects.create_user("l_appr", password="x")
        self.leader.groups.add(Group.objects.get_or_create(name=LEADER)[0])

        self.mine = Department.objects.create(name="Youth", fund_type="LOCAL")
        self.theirs = Department.objects.create(name="Choir", fund_type="LOCAL")
        DepartmentLeadership.objects.create(user=self.leader,
                                            department=self.mine)
        self.member = Member.objects.create(name="ASHA MUTUA", active=True)
        self.c_mine = PledgeCampaign.objects.create(
            name="Youth Camp", target_department=self.mine,
            status=PledgeCampaign.Status.ACTIVE)
        self.c_theirs = PledgeCampaign.objects.create(
            name="Choir Robes", target_department=self.theirs,
            status=PledgeCampaign.Status.ACTIVE)

    def _draft(self, campaign, amount="5000"):
        return Pledge.objects.create(campaign=campaign, member=self.member,
                                     amount=Decimal(amount),
                                     status=Pledge.Status.DRAFT)


class ScopeTests(_Two):
    def test_a_treasurer_sees_every_draft(self):
        a, b = self._draft(self.c_mine), self._draft(self.c_theirs)
        ids = set(approval.approvable_for(self.treasurer)
                  .values_list("pk", flat=True))
        self.assertEqual(ids, {a.pk, b.pk})

    def test_a_leader_sees_only_their_own_fund_s(self):
        a, _b = self._draft(self.c_mine), self._draft(self.c_theirs)
        ids = set(approval.approvable_for(self.leader)
                  .values_list("pk", flat=True))
        self.assertEqual(ids, {a.pk})

    def test_a_campaign_on_a_sub_account_still_counts_as_theirs(self):
        """Campaigns are often run against a child fund of the department a
        leader is actually given."""
        child = Department.objects.create(name="Youth Camp Fund",
                                          fund_type="LOCAL", parent=self.mine)
        sub = PledgeCampaign.objects.create(
            name="Camp 2026", target_department=child,
            status=PledgeCampaign.Status.ACTIVE)
        p = self._draft(sub)
        self.assertIn(p.pk, set(approval.approvable_for(self.leader)
                                .values_list("pk", flat=True)))

    def test_only_drafts_are_offered(self):
        p = self._draft(self.c_mine)
        p.status = Pledge.Status.ACTIVE
        p.save()
        self.assertEqual(list(approval.approvable_for(self.treasurer)), [])

    def test_someone_with_no_funds_sees_nothing(self):
        nobody = User.objects.create_user("nobody_appr", password="x")
        nobody.groups.add(Group.objects.get_or_create(name=LEADER)[0])
        self._draft(self.c_mine)
        self.assertEqual(list(approval.approvable_for(nobody)), [])


class ApproveTests(_Two):
    def test_approving_activates_and_records_who(self):
        p = self._draft(self.c_mine)
        self.assertTrue(approval.approve(p, self.leader))
        p.refresh_from_db()
        self.assertEqual(p.status, Pledge.Status.ACTIVE)
        self.assertEqual(p.approved_by, self.leader)
        self.assertIsNotNone(p.approved_at)

    def test_approving_twice_changes_nothing(self):
        p = self._draft(self.c_mine)
        approval.approve(p, self.treasurer)
        self.assertFalse(approval.approve(p, self.treasurer))

    def test_bulk_skips_what_is_out_of_scope(self):
        mine, theirs = self._draft(self.c_mine), self._draft(self.c_theirs)
        approved, skipped = approval.approve_many([mine.pk, theirs.pk],
                                                  self.leader)
        self.assertEqual((approved, skipped), (1, 1))
        mine.refresh_from_db(); theirs.refresh_from_db()
        self.assertEqual(mine.status, Pledge.Status.ACTIVE)
        self.assertEqual(theirs.status, Pledge.Status.DRAFT)

    def test_bulk_for_a_treasurer_takes_both(self):
        mine, theirs = self._draft(self.c_mine), self._draft(self.c_theirs)
        approved, skipped = approval.approve_many([mine.pk, theirs.pk],
                                                  self.treasurer)
        self.assertEqual((approved, skipped), (2, 0))

    def test_rubbish_ids_are_ignored_rather_than_raising(self):
        self.assertEqual(approval.approve_many(["", "abc", "999999"],
                                               self.treasurer), (0, 1))


class QueuePageTests(_Two):
    def test_the_treasurer_queue_lists_everything(self):
        self._draft(self.c_mine); self._draft(self.c_theirs)
        self.client.force_login(self.treasurer)
        r = self.client.get(reverse("pledge_approvals"))
        self.assertEqual(r.status_code, 200)
        self.assertEqual(len(r.context["rows"]), 2)
        self.assertEqual(r.context["total"], Decimal("10000"))

    def test_the_leader_queue_lists_only_theirs(self):
        self._draft(self.c_mine); self._draft(self.c_theirs)
        self.client.force_login(self.leader)
        r = self.client.get(reverse("leader_pledge_approvals"))
        self.assertEqual(r.status_code, 200)
        self.assertEqual(len(r.context["rows"]), 1)

    def test_a_leader_cannot_reach_the_office_queue(self):
        """ReadAccessMixin keeps leaders out of unscoped office screens; the
        leader door exists precisely so that stays true."""
        self.client.force_login(self.leader)
        r = self.client.get(reverse("pledge_approvals"))
        self.assertIn(r.status_code, (302, 403))

    def test_bulk_post_approves_the_ticked_rows(self):
        a, b = self._draft(self.c_mine), self._draft(self.c_theirs)
        self.client.force_login(self.treasurer)
        self.client.post(reverse("pledge_approvals"),
                         {"pledge": [a.pk, b.pk]})
        a.refresh_from_db(); b.refresh_from_db()
        self.assertEqual(a.status, Pledge.Status.ACTIVE)
        self.assertEqual(b.status, Pledge.Status.ACTIVE)

    def test_a_leader_posting_another_fund_s_id_is_refused(self):
        theirs = self._draft(self.c_theirs)
        self.client.force_login(self.leader)
        self.client.post(reverse("leader_pledge_approvals"),
                         {"pledge": [theirs.pk]})
        theirs.refresh_from_db()
        self.assertEqual(theirs.status, Pledge.Status.DRAFT)

    def test_a_leader_can_approve_one_at_a_time_too(self):
        p = self._draft(self.c_mine)
        self.client.force_login(self.leader)
        self.client.post(reverse("pledge_approve", args=[p.pk]),
                         {"action": "approve"})
        p.refresh_from_db()
        self.assertEqual(p.status, Pledge.Status.ACTIVE)

    def test_a_leader_cannot_approve_another_fund_s_single_pledge(self):
        p = self._draft(self.c_theirs)
        self.client.force_login(self.leader)
        self.client.post(reverse("pledge_approve", args=[p.pk]),
                         {"action": "approve"})
        p.refresh_from_db()
        self.assertEqual(p.status, Pledge.Status.DRAFT)

    def test_cancelling_stays_the_treasurer_s(self):
        """Undoing a pledge the church has been counting on is a different act
        from confirming one was made."""
        p = self._draft(self.c_mine)
        approval.approve(p, self.leader)
        self.client.force_login(self.leader)
        self.client.post(reverse("pledge_approve", args=[p.pk]),
                         {"action": "cancel"})
        p.refresh_from_db()
        self.assertEqual(p.status, Pledge.Status.ACTIVE)


class PublicFormTests(_Two):
    def setUp(self):
        super().setUp()
        cfg = SiteConfig.get()
        cfg.pledge_public_form_enabled = True
        cfg.save()

    def _post(self, url, **over):
        data = {"name": "GRACE WANJIRU", "phone": "0712345678",
                "amount": "5000", "note": ""}
        data.update(over)
        s = self.client.session
        s["pledge_form_ts"] = 0        # past the anti-bot delay
        s.save()
        return self.client.post(url, data)

    def test_the_campaign_form_names_its_campaign_and_offers_no_chooser(self):
        r = self.client.get(reverse("public_pledge_campaign",
                                    args=[self.c_mine.pk]))
        self.assertEqual(r.status_code, 200)
        body = r.content.decode()
        self.assertIn("Youth Camp", body)
        self.assertNotIn('name="campaign"', body)

    def test_how_will_you_give_is_gone(self):
        r = self.client.get(reverse("public_pledge_campaign",
                                    args=[self.c_mine.pk]))
        self.assertNotIn("How will you give", r.content.decode())
        self.assertNotIn('name="frequency"', r.content.decode())

    def test_a_pledge_lands_on_the_campaign_in_the_url(self):
        self._post(reverse("public_pledge_campaign", args=[self.c_mine.pk]))
        p = Pledge.objects.filter(self_submitted=True).first()
        self.assertIsNotNone(p)
        self.assertEqual(p.campaign, self.c_mine)
        self.assertEqual(p.status, Pledge.Status.DRAFT)

    def test_a_posted_campaign_cannot_redirect_the_pledge(self):
        """The URL is the campaign; a field claiming otherwise is ignored."""
        self._post(reverse("public_pledge_campaign", args=[self.c_mine.pk]),
                   campaign=self.c_theirs.pk)
        self.assertEqual(Pledge.objects.get(self_submitted=True).campaign,
                         self.c_mine)

    def test_frequency_defaults_rather_than_being_asked(self):
        self._post(reverse("public_pledge_campaign", args=[self.c_mine.pk]))
        self.assertEqual(Pledge.objects.get(self_submitted=True).frequency,
                         Pledge.Frequency.MONTHLY)

    def test_a_closed_campaign_s_link_is_not_found(self):
        self.c_mine.status = PledgeCampaign.Status.CLOSED
        self.c_mine.save()
        r = self.client.get(reverse("public_pledge_campaign",
                                    args=[self.c_mine.pk]))
        self.assertEqual(r.status_code, 404)

    def test_the_general_form_still_offers_the_chooser(self):
        r = self.client.get(reverse("public_pledge"))
        self.assertEqual(r.status_code, 200)
        self.assertIn('name="campaign"', r.content.decode())

    def test_a_submitted_pledge_reaches_the_right_approval_queue(self):
        """End to end: what a member submits is what a leader is asked to
        approve."""
        self._post(reverse("public_pledge_campaign", args=[self.c_mine.pk]))
        ids = set(approval.approvable_for(self.leader)
                  .values_list("pk", flat=True))
        self.assertEqual(len(ids), 1)

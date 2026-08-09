"""Which pledges a campaign counts, and which pledges may take money at all.

These are one question asked twice. A campaign's three published figures —
pledges, pledged, received — sit side by side on the campaign page and, when a
treasurer opts in, on the public pledge page that anyone with the link can
open without logging in. A giver reads the three as one sentence, so they have
to be about the same set of pledges.

They were not. `total_pledged` and `approved_pledge_count` both ignored drafts
and cancellations; `total_received` summed every PledgePayment in the campaign
and never asked what state its pledge was in. Money matched to a pledge that
was later cancelled stayed in the public "Received" figure for good, and money
recorded against a self-submitted draft went into it before anyone had checked
the promise was real. The fix is one definition of "counted"
(`Pledge.RECOGNISED_STATUSES`, read by `PledgeCampaign.counted_pledges`) plus a
gate that stops money reaching a pledge that should not accept it.
"""
import datetime as dt
from decimal import Decimal

from django.contrib.auth.models import Group, User
from django.contrib.messages import get_messages
from django.test import Client, TestCase
from django.urls import reverse

from core.models import SiteConfig
from core.roles import TREASURER
from departments.models import Department
from giving.models import Transaction
from members.models import Member
from pledges.models import (Pledge, PledgeCampaign, PledgeMatchSuggestion,
                            PledgePayment)


class _Appeal(TestCase):
    """One campaign that publishes its standing, and a treasurer at the desk."""

    def setUp(self):
        self.treasurer = User.objects.create_user("t_stand", password="x")
        self.treasurer.groups.add(
            Group.objects.get_or_create(name=TREASURER)[0])
        self.client.force_login(self.treasurer)
        self.fund = Department.objects.create(name="Roof Fund",
                                              fund_type="LOCAL",
                                              category="DEVELOPMENT")
        self.member = Member.objects.create(name="ASHA MUTUA",
                                            phone="254700111333")
        self.camp = PledgeCampaign.objects.create(
            name="Roof Appeal", target_department=self.fund,
            goal_amount=Decimal("500000"), show_public_progress=True,
            status=PledgeCampaign.Status.ACTIVE, start_date=dt.date(2026, 1, 1))

    def _pledge(self, amount="50000", status=Pledge.Status.ACTIVE):
        return Pledge.objects.create(campaign=self.camp, member=self.member,
                                     amount=Decimal(amount), status=status,
                                     start_date=dt.date(2026, 1, 1))

    def _gift(self, amount="50000", ref="ROOF1"):
        return Transaction.objects.create(
            date=dt.date(2026, 6, 1), channel="CASH", direction="CREDIT",
            amount=Decimal(amount), department=self.fund, member=self.member,
            allocation_status="MANUAL", confirmed=True, core_ref=ref)

    def _public_body(self):
        cfg = SiteConfig.get()
        cfg.pledge_public_form_enabled = True
        cfg.save()
        return Client().get(reverse("public_pledge_campaign",
                                    args=[self.camp.pk])).content.decode()


class CountedFigureTests(_Appeal):
    """`total_received` counts the same pledges its two neighbours count."""

    def test_money_matched_to_a_cancelled_pledge_leaves_the_campaign_standing(self):
        """The drift end to end: match a gift to a pledge, cancel the pledge,
        and the campaign used to report nobody pledging nothing while still
        claiming the 50,000."""
        p = self._pledge("50000")
        gift = self._gift("50000")
        self.client.post(reverse("pledge_match", args=[p.pk]),
                         {"action": "match", "transaction": gift.pk,
                          "amount": "50000"})
        self.assertEqual(self.camp.total_received, Decimal("50000"))

        r = self.client.post(reverse("pledge_approve", args=[p.pk]),
                             {"action": "cancel"})
        self.assertEqual(self.camp.approved_pledge_count, 0)
        self.assertEqual(self.camp.total_pledged, Decimal("0"))
        self.assertEqual(self.camp.total_received, Decimal("0"))
        # and the treasurer is told which figure just moved, and why
        self.assertIn("no longer counts toward the campaign total",
                      " ".join(str(m) for m in get_messages(r.wsgi_request)))

    def test_cancelling_leaves_the_money_itself_untouched(self):
        """Dropping the amount out of one campaign's standing is not the same
        as losing it. The contribution is real and stays in the ledger, and the
        match stays on the pledge as the record of what was matched to what."""
        p = self._pledge("50000")
        gift = self._gift("50000")
        self.client.post(reverse("pledge_match", args=[p.pk]),
                         {"action": "match", "transaction": gift.pk,
                          "amount": "50000"})
        self.client.post(reverse("pledge_approve", args=[p.pk]),
                         {"action": "cancel"})
        gift.refresh_from_db()
        p.refresh_from_db()
        self.assertEqual(gift.amount, Decimal("50000"))
        self.assertEqual(p.payments.count(), 1)
        self.assertEqual(p.paid, Decimal("50000"))

    def test_a_draft_s_matched_money_stays_out_of_the_received_figure(self):
        """The same leak from the other end, and the one older records can
        still carry: a payment written against a pledge nobody has approved."""
        draft = self._pledge("50000", status=Pledge.Status.DRAFT)
        PledgePayment.objects.create(pledge=draft, transaction=self._gift(),
                                     amount=Decimal("50000"),
                                     date=dt.date(2026, 6, 1))
        self.assertEqual(self.camp.approved_pledge_count, 0)
        self.assertEqual(self.camp.total_pledged, Decimal("0"))
        self.assertEqual(self.camp.total_received, Decimal("0"))

    def test_the_public_page_never_shows_money_the_other_figures_disown(self):
        """The page with no login on it. A live pledge keeps the standing block
        on screen, so this asserts the cancelled pledge's money is absent rather
        than the whole block being hidden."""
        live = self._pledge("10000")
        gone = self._pledge("50000")
        PledgePayment.objects.create(pledge=gone, transaction=self._gift(),
                                     amount=Decimal("50000"),
                                     date=dt.date(2026, 6, 1))
        gone.refresh_from_db()
        gone.status = Pledge.Status.CANCELLED
        gone.save()

        body = self._public_body()
        self.assertIn("10,000", body)          # the pledge that still stands
        self.assertNotIn("50,000", body)       # the one the church let go
        self.assertEqual(self.camp.counted_pledges.get(), live)


class PaymentGateTests(_Appeal):
    """A pledge that the campaign does not count cannot be given money.

    Enforced in the view rather than the template: pledge_detail.html offers
    the "record a payment directly" form to any treasurer whatever the pledge's
    status, and a POST does not need the form anyway.
    """

    def _match_post(self, pledge, data):
        return self.client.post(reverse("pledge_match", args=[pledge.pk]), data)

    def test_a_draft_pledge_refuses_a_directly_recorded_payment(self):
        """A self-submitted draft is a promise nobody has checked. Money
        against it used to be counted as received by the appeal."""
        draft = self._pledge("50000", status=Pledge.Status.DRAFT)
        r = self._match_post(draft, {"action": "manual", "amount": "5000",
                                     "note": "cash at service"})
        self.assertEqual(draft.payments.count(), 0)
        self.assertIn("awaiting approval",
                      " ".join(str(m) for m in get_messages(r.wsgi_request)))

    def test_a_draft_pledge_refuses_a_matched_contribution(self):
        draft = self._pledge("50000", status=Pledge.Status.DRAFT)
        gift = self._gift("50000")
        self._match_post(draft, {"action": "match", "transaction": gift.pk,
                                 "amount": "50000"})
        self.assertEqual(PledgePayment.objects.count(), 0)

    def test_a_cancelled_pledge_refuses_one_too(self):
        gone = self._pledge("50000", status=Pledge.Status.CANCELLED)
        r = self._match_post(gone, {"action": "manual", "amount": "5000"})
        self.assertEqual(gone.payments.count(), 0)
        self.assertIn("reactivate",
                      " ".join(str(m) for m in get_messages(r.wsgi_request)))

    def test_an_approved_pledge_still_takes_a_payment(self):
        """The gate refuses two statuses, not the treasurer's ordinary work."""
        p = self._pledge("50000")
        self._match_post(p, {"action": "manual", "amount": "5000"})
        p.refresh_from_db()
        self.assertEqual(p.paid, Decimal("5000"))
        self.assertEqual(self.camp.total_received, Decimal("5000"))

    def test_a_suggestion_confirmed_after_the_pledge_was_cancelled_applies_nothing(self):
        """Suggestions are raised against active pledges and then wait in a
        queue, which is long enough for the pledge to be cancelled underneath
        one."""
        p = self._pledge("50000")
        gift = self._gift("50000")
        s = PledgeMatchSuggestion.objects.create(transaction=gift, pledge=p,
                                                 amount=Decimal("50000"))
        p.status = Pledge.Status.CANCELLED
        p.save()
        self.client.post(reverse("pledge_suggestion_action", args=[s.pk]),
                         {"action": "confirm"})
        s.refresh_from_db()
        self.assertEqual(PledgePayment.objects.count(), 0)
        self.assertEqual(s.status, PledgeMatchSuggestion.Status.PENDING)

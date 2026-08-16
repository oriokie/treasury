"""An appeal, from the launch of a campaign to a member's pledge being fulfilled.

The whole arc a church actually walks: a treasurer opens a building appeal with
a goal on a fund; promises arrive two ways — written down at the desk by an
assistant, and typed in by members themselves on the public link; a treasurer
approves them; the money then comes in through the ordinary giving flow, into
the appeal's fund or one of its sub-accounts; and the treasurer presses
auto-match, which is the only thing that ever turns a real contribution into
progress against a promise.

What this suite adds to the pledge app's own tests is the SEAMS, because a
pledge is the one thing in this application that is a figure without being
money. The ones asserted here:

* a promise nobody has approved yet must not move a single published figure —
  and the figure it would move is on a page anyone with the link can open;
* money matched to a pledge that is later CANCELLED must stop counting toward
  the appeal, while staying exactly where it is in the ledger (the HIGH audit
  finding: a cancelled pledge went on inflating the public "Received");
* auto-matching may only credit gifts given to the appeal's OWN fund or a
  sub-account of it, and only from the pledge date onward — a member's tithe is
  not their building pledge, and giving they had already done before they
  promised anything is not payment of that promise;
* one gift is credited once. Auto-match is a button a treasurer presses again
  when she is not sure it took, and there are two doors onto it (the sweep and
  the single pledge's own control), so "counted twice" is one impatient click
  away;
* the standing shown to a giver on the public page and the standing shown to
  the treasurer in the office are the same campaign read twice, so they have to
  say the same thing.

Nothing in the pledge module posts to the ledger. That is itself an invariant
worth ending on: every one of these workflows finishes with the books balanced
and the appeal's fund holding exactly the gifts that were given to it, no more
and no less, however the pledges around them were approved or cancelled.
"""
import datetime as dt
import re
import time
from decimal import Decimal

from django.db.models import Sum
from django.test import Client
from django.urls import reverse

from core.models import SiteConfig
from departments.models import Department
from giving.models import Transaction
from members.models import Member, MemberTag
from pledges.models import Pledge, PledgeCampaign, PledgePayment

from .base import TODAY, BusinessWorkflowTest, WorkflowError

#: The appeal's own dates. The campaign outlives the test period on purpose: a
#: real appeal is open while the money comes in, and the public form only
#: offers campaigns that have not ended.
CAMPAIGN_START = dt.date(2026, 7, 1)
CAMPAIGN_END = dt.date(2027, 6, 30)
PLEDGE_END = dt.date(2026, 12, 31)


class AppealWorkflow(BusinessWorkflowTest):
    """A church with a roof to mend, a congregation to ask, and two officers.

    The fund tree is the shape a real appeal has: the campaign names SANCTUARY
    ROOF, but a good deal of the money is recorded against the youth group's
    sub-account under it, because that is how the group's own effort is kept
    visible. TITHE is here to give a member somewhere else to give — the fund
    scoping rule cannot be tested against a church with only one fund.
    """

    def setUp(self):
        super().setUp()
        self.appeal_fund = Department.objects.create(
            name="Sanctuary Roof", slug="wf-roof",
            fund_type=Department.FundType.LOCAL,
            category=Department.Category.MINISTRY)
        self.youth_sub_fund = Department.objects.create(
            name="Sanctuary Roof — Youth", slug="wf-roof-youth",
            parent=self.appeal_fund, fund_type=Department.FundType.LOCAL,
            category=Department.Category.MINISTRY)

        # A populated roll, not one member: the report groups by tag, the
        # matcher resolves by name key, and both need somebody else in the
        # register to be wrong about.
        self.board = MemberTag.objects.create(name="Board")
        self.grace = Member.objects.create(name="GRACE WANJIRU",
                                           phone="0712345678")
        self.grace.tags.add(self.board)
        self.peter = Member.objects.create(name="PETER OTIENO",
                                           phone="0722334455")
        self.mary = Member.objects.create(name="MARY ATIENO",
                                          phone="0733445566")

        cfg = SiteConfig.get()
        # The public pledge link is off by default; a church running an appeal
        # turns it on. Set here rather than driven through the settings screen
        # because it is the PRECONDITION of this workflow, not a step of it —
        # the same way the worked example sets require_expense_approval.
        cfg.pledge_public_form_enabled = True
        cfg.save()

        self.office = self.acting_as(self.treasurer)     # a Treasurer
        self.desk = self.acting_as(self.assistant)       # an Assistant

    # -- private helpers -------------------------------------------------------
    # None of these performs a workflow step by another route: each one is a
    # POST to the real view, named so the steps below read as sentences.

    def _launch_appeal(self, **over):
        """The treasurer opens the campaign. Returns the created campaign."""
        data = {
            "name": "Sanctuary Roof Appeal",
            "description": "Re-roofing the sanctuary before the rains.",
            "target_department": self.appeal_fund.id,
            "goal_amount": "500000",
            "start_date": CAMPAIGN_START.isoformat(),
            "end_date": CAMPAIGN_END.isoformat(),
            "status": PledgeCampaign.Status.ACTIVE,
            "show_public_progress": "on",
        }
        data.update(over)
        self.submit(self.office, "pledge_campaign_create", data)
        campaign = PledgeCampaign.objects.filter(name=data["name"]).first()
        if campaign is None:
            raise WorkflowError(
                "The campaign form accepted the post but no campaign exists.")
        return campaign

    def _pledge_at_the_desk(self, campaign, member, amount, client=None,
                            **over):
        """A promise written down in the office. An assistant's pledge is a
        DRAFT; a treasurer's is approved as it is entered."""
        data = {
            "campaign": campaign.id, "member": member.id,
            "amount": str(amount),
            "frequency": Pledge.Frequency.MONTHLY,
            "start_date": TODAY.isoformat(),
            "end_date": PLEDGE_END.isoformat(),
            "note": "Pledged at the launch service",
        }
        data.update(over)
        before = set(Pledge.objects.values_list("id", flat=True))
        self.submit(client or self.desk, "pledge_create", data)
        new = Pledge.objects.exclude(id__in=before).first()
        if new is None:
            raise WorkflowError(
                f"pledge_create accepted the post but {member.name} has no "
                f"pledge — the step did nothing.")
        return new

    def _approve(self, pledge, client=None):
        self.submit(client or self.office, "pledge_approve",
                    {"action": "approve"}, args=[pledge.pk])
        pledge.refresh_from_db()
        return pledge

    def _give(self, member, amount, date, fund, client=None, payer_name="",
              reference=""):
        """A real contribution, entered on the cash screen like any other.

        This is the only thing in the whole workflow that moves money, and it
        is deliberately NOT part of the pledge module: a pledge is fulfilled by
        matching giving that already happened.
        """
        data = {
            "date": date.isoformat(),
            "channel": Transaction.Channel.CASH,
            "fund": f"d:{fund.id}",
            "amount": str(amount),
            "reference": reference,
            "payer_name": payer_name,
            "confirm_duplicate": "1",   # the treasurer has said it is not one
        }
        if member is not None:
            data["member"] = member.id
        before = set(Transaction.objects.values_list("id", flat=True))
        self.submit(client or self.office, "cash_new", data)
        txn = Transaction.objects.exclude(id__in=before).first()
        if txn is None:
            raise WorkflowError(
                f"cash_new accepted the post but no contribution of {amount} "
                f"exists — the gift was not recorded.")
        return txn

    def _auto_match(self, client=None):
        """The treasurer opens Auto-match, reviews the preview, and confirms.

        The dashboard button is now a GET to the preview. Applying requires
        `confirm=1`. Omitting per-row `match` checkboxes applies the whole
        plan — the same as leaving every box ticked.
        """
        return self.submit(client or self.office, "pledge_auto_match_all",
                           {"confirm": "1"})

    # -- the public form, driven the way a browser drives it -------------------

    def _open_public_form(self, campaign, client=None):
        """GET the public link as a member would: no account, no session."""
        browser = client or Client()
        response = browser.get(reverse("public_pledge_campaign",
                                       args=[campaign.pk]), follow=True)
        if response.status_code != 200:
            raise WorkflowError(
                f"The public pledge link returned {response.status_code} — a "
                f"member with the link cannot pledge.")
        for url, _code in response.redirect_chain:
            if "login" in url:
                raise WorkflowError(
                    "The public pledge form redirected an anonymous member to "
                    "the login page — this is defect #121 again.")
        return browser, response

    def _pause_like_a_human(self, browser):
        """Satisfy the too-fast-submit defence without sleeping for real.

        The view stamps the session when the form is rendered and rejects a
        post that comes back inside MIN_SECONDS, on the grounds that only a
        script fills a form that fast. A person takes half a minute. Winding
        the stamp back is the same fact stated without spending the seconds;
        the defence itself still runs, and the bot cases below still trip it.
        """
        session = browser.session
        session["pledge_form_ts"] = time.time() - 30
        session.save()

    def _submit_public_pledge(self, campaign, name, phone, amount,
                              browser=None, honeypot="", note="",
                              expect_pledge=True):
        """Fill in the public form and press submit, as the member does."""
        if browser is None:
            browser, _ = self._open_public_form(campaign)
        self._pause_like_a_human(browser)
        before = set(Pledge.objects.values_list("id", flat=True))
        response = browser.post(
            reverse("public_pledge_campaign", args=[campaign.pk]),
            {"name": name, "phone": phone, "amount": str(amount),
             "note": note, "website": honeypot},
            follow=True)
        if response.status_code >= 400:
            raise WorkflowError(
                f"The public pledge form returned {response.status_code}.")
        # The view re-renders itself with an `error` in context when it refuses
        # — the public form's equivalent of a bound form with errors, which
        # `submit()` cannot see because this form is not a Django form.
        problem = (response.context or {}).get("error") if response.context else None
        new = Pledge.objects.exclude(id__in=before).first()
        if expect_pledge and new is None:
            raise WorkflowError(
                f"The public form accepted {name}'s pledge of {amount} and "
                f"created nothing"
                + (f" — it said: {problem}" if problem else "") + ".")
        return new, response

    # -- reading figures off the pages a human reads them off ------------------

    @staticmethod
    def _public_standing(html):
        """The three figures the public standing block publishes.

        Read out of the rendered HTML rather than off the model, because the
        point of the assertion is what a giver is shown. Returns {} when the
        block is absent, which is itself the correct answer for an appeal with
        nothing approved yet.
        """
        pattern = re.compile(
            r'<span class="sv">(?:<span class="cur">[^<]*</span>)?'
            r'([\d,]+)</span><span class="sl">([A-Za-z]+)</span>')
        return {label.lower(): Decimal(value.replace(",", ""))
                for value, label in pattern.findall(html)}

    @staticmethod
    def _office_standing(html):
        """The same three figures as the campaign page states them."""
        pattern = re.compile(
            r'<span class="wm-l">([A-Za-z]+)</span><span class="wm-v">'
            r'(?:<span class="cur">[^<]*</span>)?([\d,]+)</span>')
        return {label.lower(): Decimal(value.replace(",", ""))
                for label, value in pattern.findall(html)}

    @staticmethod
    def _money(value):
        """A figure at the scale money is written in.

        `assert_agree` compares the STRING form of each Decimal, so 100000 read
        off a page and Decimal('100000.00') summed out of the database are
        reported as a disagreement when they are the same amount. Every figure
        handed to it from this file goes through here first, so a failure there
        is a real difference and not a difference of notation.
        """
        return Decimal(value).quantize(Decimal("0.01"))

    @staticmethod
    def _payments_total(campaign=None, pledge=None):
        qs = PledgePayment.objects.all()
        if campaign is not None:
            qs = qs.filter(pledge__campaign=campaign)
        if pledge is not None:
            qs = qs.filter(pledge=pledge)
        return qs.aggregate(s=Sum("amount"))["s"] or Decimal("0")

    # =========================================================================
    # The spine: launch -> promise -> approval -> giving -> matching -> fulfilled
    # =========================================================================
    def test_an_appeal_runs_from_launch_to_a_fulfilled_pledge(self):
        # 1. the treasurer opens the appeal, with a goal, on the roof fund
        campaign = self._launch_appeal()
        self.assertEqual(campaign.goal_amount, Decimal("500000"))
        self.assertEqual(campaign.target_department_id, self.appeal_fund.id)
        self.visit(self.office, "pledge_campaign_detail", args=[campaign.pk])

        # 2. nothing has been promised yet, and the appeal says so
        self.assertEqual(campaign.total_pledged, Decimal("0"))
        self.assertEqual(campaign.approved_pledge_count, 0)

        # 3. Grace stands up at the launch service; the assistant writes her
        #    promise down. An assistant cannot approve, so it waits.
        pledge = self._pledge_at_the_desk(campaign, self.grace, "60000")
        self.assertEqual(pledge.status, Pledge.Status.DRAFT,
                         "an assistant's pledge must wait for a treasurer")
        self.assertEqual(campaign.total_pledged, Decimal("0"),
                         "an unapproved promise moved the appeal's total")

        # 4. the treasurer approves it, and only now is it part of the appeal
        self._approve(pledge)
        self.assertEqual(pledge.status, Pledge.Status.ACTIVE)
        self.assertEqual(campaign.total_pledged, Decimal("60000"))
        self.assertEqual(campaign.approved_pledge_count, 1)
        self.assertEqual(campaign.total_received, Decimal("0"),
                         "a promise is not money")

        # 5. Grace gives 25,000 in cash toward the roof. The money is in the
        #    fund immediately — and against the pledge not at all, because
        #    nobody has matched it yet. This seam is where the double-count
        #    lives: the fund must move, the appeal's "received" must not.
        self._give(self.grace, "25000", dt.date(2026, 7, 18), self.appeal_fund)
        self.assert_fund_balance(self.appeal_fund, Decimal("25000"))
        self.assertEqual(campaign.total_received, Decimal("0"))
        self.assertEqual(pledge.paid, Decimal("0"))

        # 6. the treasurer runs the auto-matcher, which credits it
        self._auto_match()
        pledge.refresh_from_db()
        self.assertEqual(pledge.paid, Decimal("25000"))
        self.assertEqual(pledge.outstanding, Decimal("35000"))
        self.assertEqual(pledge.status, Pledge.Status.ACTIVE,
                         "a part-paid pledge is still active")
        self.assertEqual(campaign.total_received, Decimal("25000"))

        # and Grace's own page says part-paid, in the figures printed on it
        part_paid = self._office_standing(
            self.visit(self.office, "pledge_detail",
                       args=[pledge.pk]).content.decode())
        self.assertEqual(part_paid["pledged"], Decimal("60000"))
        self.assertEqual(part_paid["received"], Decimal("25000"))
        self.assertEqual(part_paid["outstanding"], Decimal("35000"))

        # 7. she gives the balance through the youth group's sub-account —
        #    the same appeal, a different fund row
        self._give(self.grace, "35000", dt.date(2026, 7, 25),
                   self.youth_sub_fund)
        self._auto_match()
        pledge.refresh_from_db()
        self.assertEqual(pledge.paid, Decimal("60000"))
        self.assertEqual(pledge.outstanding, Decimal("0"))
        self.assertEqual(
            pledge.status, Pledge.Status.FULFILLED,
            "a pledge paid in full should read as fulfilled, not merely active")
        fulfilled_page = self.visit(self.office, "pledge_detail",
                                    args=[pledge.pk]).content.decode()
        fully_paid = self._office_standing(fulfilled_page)
        self.assertEqual(fully_paid["received"], Decimal("60000"))
        self.assertEqual(fully_paid["outstanding"], Decimal("0"))
        self.assertIn("Fulfilled", fulfilled_page,
                      "the pledge page does not say the promise was kept")

        # 8. the money: 60,000 given to the appeal, consolidated over the
        #    sub-account, and a ledger that still balances
        self.assert_fund_balance(self.appeal_fund, Decimal("60000"))
        self.assert_fund_balance(self.trust_fund, Decimal("0"))
        self.assert_books_balance("after an appeal was pledged and paid")
        self.assert_trial_balance_balances()

        # 9. the same 60,000 read four ways: what the appeal says it received,
        #    what the match links add up to, what the campaign report totals,
        #    and what actually landed in the fund.
        report = self.visit(self.office, "pledge_campaign_report",
                            args=[campaign.pk])
        self.assert_agree(
            "one fulfilled pledge, read four ways",
            campaign_total_received=self._money(campaign.total_received),
            pledge_payment_links=self._money(
                self._payments_total(campaign=campaign)),
            campaign_report_paid=self._money(report.context["totals"]["paid"]),
            money_in_the_fund=self._money(Transaction.objects.filter(
                department_id__in=[self.appeal_fund.id, self.youth_sub_fund.id]
            ).aggregate(s=Sum("amount"))["s"]),
        )

    def test_a_treasurer_recording_a_pledge_approves_it_in_the_same_act(self):
        """The other office route. A treasurer entering a promise she took
        herself is both the recorder and the approver, so the pledge is active
        as it is saved — and it counts immediately, which is the point of the
        distinction from the assistant's draft."""
        campaign = self._launch_appeal()
        pledge = self._pledge_at_the_desk(campaign, self.mary, "30000",
                                          client=self.office)
        self.assertEqual(pledge.status, Pledge.Status.ACTIVE)
        self.assertEqual(pledge.approved_by_id, self.treasurer.id)
        self.assertEqual(campaign.total_pledged, Decimal("30000"))
        self.assertEqual(campaign.approved_pledge_count, 1)
        # and it is a promise, not money: nothing has reached the fund
        self.assert_fund_balance(self.appeal_fund, Decimal("0"))
        self.assert_books_balance("after a treasurer recorded a pledge")

    # =========================================================================
    # The public route
    # =========================================================================
    def test_a_member_with_only_the_link_can_pledge_and_a_treasurer_reviews_it(self):
        """The route with no login at all — the shape of failure #121.

        Everything after the submit is the treasurer's: the promise arrives
        UNVERIFIED, it counts for nothing until she approves it, and the member
        it belongs to is resolved from the phone number the giver typed.
        """
        campaign = self._launch_appeal()

        # 1. Peter opens the link from a WhatsApp message. No account.
        browser, page = self._open_public_form(campaign)
        self.assertIn("Sanctuary Roof Appeal", page.content.decode())

        # 2. he fills it in and submits
        pledge, response = self._submit_public_pledge(
            campaign, "Peter Otieno", "0722334455", "40000", browser=browser,
            note="Will pay after harvest")
        self.assertContains(response, "Thank")          # the thanks page opened

        # 3. what arrived is a draft against the RIGHT member — matched on the
        #    phone number, not created as a stranger
        self.assertEqual(pledge.status, Pledge.Status.DRAFT)
        self.assertTrue(pledge.self_submitted)
        self.assertEqual(pledge.member_id, self.peter.id,
                         "the public form did not recognise an existing member "
                         "by their phone number")
        self.assertEqual(pledge.campaign_id, campaign.id)

        # 4. and it counts for NOTHING until a treasurer has looked at it —
        #    including on the public page the next giver opens
        self.assertEqual(campaign.total_pledged, Decimal("0"))
        self.assertEqual(campaign.approved_pledge_count, 0)
        _, second_view = self._open_public_form(campaign)
        self.assertEqual(
            self._public_standing(second_view.content.decode()), {},
            "an unapproved self-submitted pledge was published to the public "
            "standing block")

        # 5. the treasurer finds it in her queue and approves it there
        queue = self.visit(self.office, "pledge_approvals")
        self.assertIn(pledge.id, [p.id for p in queue.context["rows"]],
                      "a self-submitted pledge never reached the approval queue")
        self._approve(pledge)
        self.assertEqual(pledge.status, Pledge.Status.ACTIVE)
        self.assertEqual(campaign.total_pledged, Decimal("40000"))

        # 6. now the giver sees it, and the promise still is not money
        _, third_view = self._open_public_form(campaign)
        standing = self._public_standing(third_view.content.decode())
        self.assertEqual(standing.get("pledges"), Decimal("1"))
        self.assertEqual(standing.get("pledged"), Decimal("40000"))
        self.assertEqual(standing.get("received"), Decimal("0"))
        self.assert_books_balance("after a self-submitted pledge was approved")

    def test_the_general_public_link_offers_the_open_appeals_and_no_others(self):
        """`/pledge/` with no campaign in the URL — the link on a bulletin.

        The member picks the appeal from a chooser, so what the chooser
        contains is the whole security question of this page: an appeal still
        being drafted in the office must not be collecting promises from the
        congregation.
        """
        live = self._launch_appeal()
        drafting = self._launch_appeal(
            name="Pastor's Retirement Gift", goal_amount="120000",
            status=PledgeCampaign.Status.DRAFT, show_public_progress="")

        browser = Client()
        page = browser.get(reverse("public_pledge"), follow=True)
        self.assertEqual(page.status_code, 200)
        html = page.content.decode()
        self.assertIn(f'<option value="{live.id}">', html,
                      "the open appeal is not offered on the public link")
        self.assertNotIn(f'<option value="{drafting.id}">', html,
                         "an appeal still in draft was offered to the public")

        # she chooses the roof appeal and submits
        self._pause_like_a_human(browser)
        before = set(Pledge.objects.values_list("id", flat=True))
        browser.post(reverse("public_pledge"),
                     {"name": "Mary Atieno", "phone": "0733445566",
                      "campaign": live.id, "amount": "15000", "website": ""},
                     follow=True)
        pledge = Pledge.objects.exclude(id__in=before).first()
        self.assertIsNotNone(pledge, "the general public link created nothing")
        self.assertEqual(pledge.campaign_id, live.id)
        self.assertEqual(pledge.member_id, self.mary.id)
        self.assertEqual(pledge.status, Pledge.Status.DRAFT)

        # and a post naming the draft campaign is refused however it is sent
        self._pause_like_a_human(browser)
        browser.post(reverse("public_pledge"),
                     {"name": "Mary Atieno", "phone": "0733445566",
                      "campaign": drafting.id, "amount": "15000",
                      "website": ""}, follow=True)
        self.assertEqual(drafting.pledges.count(), 0,
                         "a pledge was accepted against a campaign the public "
                         "form does not offer")

    def test_the_public_form_still_turns_bots_away(self):
        """The defences are what make the public route safe to have at all, so
        a workflow test that walked past them would be describing a different
        application."""
        campaign = self._launch_appeal()

        # a bot fills the hidden field: silently dropped, nothing created
        browser, _ = self._open_public_form(campaign)
        pledge, response = self._submit_public_pledge(
            campaign, "Spam Robot", "0700000000", "99000", browser=browser,
            honeypot="https://buy-now.example", expect_pledge=False)
        self.assertIsNone(pledge, "a honeypot submission created a pledge")

        # a script posts the instant the page renders: refused, nothing created
        fast = Client()
        fast.get(reverse("public_pledge_campaign", args=[campaign.pk]))
        fast.post(reverse("public_pledge_campaign", args=[campaign.pk]),
                  {"name": "Very Fast", "phone": "0700000001",
                   "amount": "88000", "website": ""})
        self.assertEqual(Pledge.objects.count(), 0,
                         "a form submitted faster than a human can type was "
                         "accepted")
        self.assertEqual(campaign.total_pledged, Decimal("0"))

    # =========================================================================
    # Cancellation — the HIGH finding, on the page the public can see
    # =========================================================================
    def test_cancelling_a_pledge_stops_its_money_inflating_the_public_total(self):
        """Money matched to a pledge the church has let go must leave the
        appeal's headline figures and stay in the ledger.

        Both halves matter and they pull in opposite directions: unlink the
        contributions and the church has quietly lost 30,000 of real giving;
        leave them counted and the public page advertises an appeal further
        along than it is, backed by a promise nobody stands behind.
        """
        campaign = self._launch_appeal()
        grace_pledge = self._pledge_at_the_desk(campaign, self.grace, "60000")
        mary_pledge = self._pledge_at_the_desk(campaign, self.mary, "30000")
        self._approve(grace_pledge)
        self._approve(mary_pledge)

        self._give(self.grace, "25000", dt.date(2026, 7, 18), self.appeal_fund)
        self._give(self.mary, "30000", dt.date(2026, 7, 20), self.appeal_fund)
        self._auto_match()

        self.assertEqual(campaign.total_pledged, Decimal("90000"))
        self.assertEqual(campaign.total_received, Decimal("55000"))
        mary_pledge.refresh_from_db()
        self.assertEqual(mary_pledge.status, Pledge.Status.FULFILLED)

        # Mary emigrates; the treasurer cancels her pledge.
        self.submit(self.office, "pledge_approve", {"action": "cancel"},
                    args=[mary_pledge.pk])
        mary_pledge.refresh_from_db()
        self.assertEqual(mary_pledge.status, Pledge.Status.CANCELLED)

        # the appeal drops BOTH her promise and the money matched to it
        self.assertEqual(campaign.total_pledged, Decimal("60000"),
                         "a cancelled promise is still being counted")
        self.assertEqual(
            campaign.total_received, Decimal("25000"),
            "a cancelled pledge's money is still inflating the appeal's "
            "received figure — this is the figure on the PUBLIC page")
        self.assertEqual(campaign.approved_pledge_count, 1)

        # and the giver reading the public page sees the same, lower, honest
        # figures — not the office's and the public's answers drifting apart
        _, public = self._open_public_form(campaign)
        standing = self._public_standing(public.content.decode())
        self.assert_agree(
            "the appeal's standing after a cancellation",
            public_page_received=self._money(standing["received"]),
            campaign_total_received=self._money(campaign.total_received),
        )
        self.assert_agree(
            "the appeal's pledged total after a cancellation",
            public_page_pledged=self._money(standing["pledged"]),
            campaign_total_pledged=self._money(campaign.total_pledged),
        )

        # the money itself has not moved an inch: 55,000 was given and 55,000
        # is still in the fund, and the match links remain as the record
        self.assert_fund_balance(self.appeal_fund, Decimal("55000"))
        self.assertEqual(self._payments_total(pledge=mary_pledge),
                         Decimal("30000"),
                         "cancelling a pledge rewrote the record of what had "
                         "been matched to it")
        self.assert_books_balance("after a pledge was cancelled")
        self.assert_trial_balance_balances()

    def test_a_cancelled_pledge_cannot_be_topped_up_with_more_money(self):
        """The other way the same figure gets inflated: not old money left
        counted, but new money let in behind a promise the church dropped."""
        campaign = self._launch_appeal()
        pledge = self._pledge_at_the_desk(campaign, self.mary, "30000")
        self._approve(pledge)
        self.submit(self.office, "pledge_approve", {"action": "cancel"},
                    args=[pledge.pk])

        self._give(self.mary, "30000", dt.date(2026, 7, 20), self.appeal_fund)
        self.submit(self.office, "pledge_match", {"action": "auto"},
                    args=[pledge.pk])
        self._auto_match()

        self.assertEqual(self._payments_total(pledge=pledge), Decimal("0"),
                         "money was matched to a cancelled pledge")
        self.assertEqual(campaign.total_received, Decimal("0"))
        self.assert_fund_balance(self.appeal_fund, Decimal("30000"))
        self.assert_books_balance("after matching was refused")

    # =========================================================================
    # What auto-matching may credit
    # =========================================================================
    def test_auto_matching_credits_only_gifts_given_to_the_appeal(self):
        """Three gifts from the same member on the same day, one appeal.

        Only the two given to the appeal's own fund tree are its money. Her
        tithe is not her building pledge, however much the amounts line up.
        """
        campaign = self._launch_appeal()
        pledge = self._pledge_at_the_desk(campaign, self.grace, "60000")
        self._approve(pledge)

        self._give(self.grace, "20000", dt.date(2026, 7, 18), self.appeal_fund)
        self._give(self.grace, "15000", dt.date(2026, 7, 18),
                   self.youth_sub_fund)
        tithe = self._give(self.grace, "18000", dt.date(2026, 7, 18),
                           self.trust_fund)

        self._auto_match()
        pledge.refresh_from_db()
        self.assertEqual(
            pledge.paid, Decimal("35000"),
            "auto-matching credited a gift that was not given to the appeal")
        self.assertFalse(
            PledgePayment.objects.filter(transaction=tithe).exists(),
            "the member's tithe was applied to her building pledge")

        # every shilling is still where it was given
        self.assert_fund_balance(self.appeal_fund, Decimal("35000"))
        self.assert_fund_balance(self.trust_fund, Decimal("18000"))
        self.assert_books_balance("after scoped auto-matching")

    def test_auto_matching_ignores_giving_done_before_the_promise(self):
        """A gift given before the pledge was made cannot be payment of it.

        Counting it credits the member twice — once as the giving they had
        already done, once as progress on a promise they had not yet made — and
        it makes the appeal look further along than it is on the day it opens.
        """
        campaign = self._launch_appeal()
        # she gave to the roof fund a fortnight before the appeal asked her to
        earlier = self._give(self.grace, "20000", dt.date(2026, 7, 5),
                             self.appeal_fund)
        pledge = self._pledge_at_the_desk(campaign, self.grace, "60000")
        self._approve(pledge)
        # and again, the day after she promised
        self._give(self.grace, "20000", dt.date(2026, 7, 16), self.appeal_fund)

        self._auto_match()
        pledge.refresh_from_db()
        self.assertEqual(
            pledge.paid, Decimal("20000"),
            "a gift given before the pledge date was counted toward it")
        self.assertFalse(
            PledgePayment.objects.filter(transaction=earlier).exists(),
            "the earlier gift was matched to a promise made after it")
        self.assertEqual(campaign.total_received, Decimal("20000"))

        # the fund holds both gifts — the earlier one is real money, it is
        # simply not payment of this promise
        self.assert_fund_balance(self.appeal_fund, Decimal("40000"))
        self.assert_books_balance("after date-scoped auto-matching")

    # =========================================================================
    # One gift, credited once
    # =========================================================================
    def test_pressing_auto_match_again_does_not_credit_the_gift_twice(self):
        """The treasurer presses the button a second time.

        She does this constantly and for a good reason: the sweep reports "no
        new matches found" in the same grey box it reports having found some,
        and after a page reload nobody can remember which. There are two doors
        onto the same act — the dashboard's sweep over every pledge, and the
        pledge's own "auto-match" control — so this walks through both, twice
        each. Double-counting is the fault this suite was commissioned over,
        and the pledge module is where it would be hardest to see, because no
        balance goes wrong when it happens: only the appeal's belief about how
        much of its promise has been kept.
        """
        campaign = self._launch_appeal()
        pledge = self._pledge_at_the_desk(campaign, self.grace, "60000")
        self._approve(pledge)
        self._give(self.grace, "25000", dt.date(2026, 7, 18), self.appeal_fund)

        self._auto_match()
        self.assertEqual(pledge.paid, Decimal("25000"))

        # the sweep again, and then the pledge's own button, twice
        self._auto_match()
        self.submit(self.office, "pledge_match", {"action": "auto"},
                    args=[pledge.pk])
        self.submit(self.office, "pledge_match", {"action": "auto"},
                    args=[pledge.pk])

        pledge.refresh_from_db()
        self.assertEqual(
            PledgePayment.objects.filter(pledge=pledge).count(), 1,
            "the same contribution was matched to the pledge more than once")
        self.assertEqual(
            pledge.paid, Decimal("25000"),
            "pressing auto-match again credited the same gift a second time")
        self.assertEqual(pledge.outstanding, Decimal("35000"))
        self.assertEqual(campaign.total_received, Decimal("25000"))

        # the credit can never exceed the giving behind it: 25,000 was given
        # and 25,000 is what the appeal may claim to have received
        self.assert_agree(
            "what Grace gave and what the appeal credits her with",
            given_to_the_appeal=self._money("25000"),
            credited_against_promises=self._money(
                self._payments_total(campaign=campaign)),
        )
        self.assert_fund_balance(self.appeal_fund, Decimal("25000"))
        self.assert_books_balance("after auto-matching four times")

    def test_a_gift_larger_than_the_promise_leaves_its_remainder_free(self):
        """Grace gives 100,000 against a 60,000 promise.

        Two things must be true at once, and they have been in conflict before:
        her pledge is kept and no more than kept (60,000 credited, not 100,000),
        AND the other 40,000 is not written off. It is real money sitting in the
        appeal's fund, and when she promises again it must be reachable — the
        matcher used to strike out any gift it had touched at all, which made
        the remainder of a part-applied contribution permanently invisible and
        left the church chasing a member who had already paid.
        """
        campaign = self._launch_appeal()
        first = self._pledge_at_the_desk(campaign, self.grace, "60000")
        self._approve(first)
        gift = self._give(self.grace, "100000", dt.date(2026, 7, 18),
                          self.appeal_fund)

        self._auto_match()
        first.refresh_from_db()
        self.assertEqual(first.paid, Decimal("60000"),
                         "the pledge was credited with more than it promised")
        self.assertEqual(first.status, Pledge.Status.FULFILLED)
        self.assertEqual(campaign.total_received, Decimal("60000"))

        # she promises again at the next appeal service; the 40,000 she has
        # already given is what pays it
        second = self._pledge_at_the_desk(campaign, self.grace, "20000",
                                          client=self.office)
        self._auto_match()
        second.refresh_from_db()
        self.assertEqual(
            second.paid, Decimal("20000"),
            "the unspent remainder of a gift could not be reached by a later "
            "promise")
        self.assertEqual(second.status, Pledge.Status.FULFILLED)

        # and across both promises the church has still only credited what was
        # actually given — 80,000 of the 100,000, with 20,000 still free
        credited = self._payments_total(campaign=campaign)
        self.assertEqual(credited, Decimal("80000"))
        self.assertLessEqual(
            credited, gift.amount,
            "more was credited against promises than the member ever gave")
        self.assert_agree(
            "the appeal's received figure and the links behind it",
            campaign_total_received=self._money(campaign.total_received),
            pledge_payment_links=self._money(credited),
        )
        # every shilling of the gift is in the fund, credited or not
        self.assert_fund_balance(self.appeal_fund, Decimal("100000"))
        self.assert_books_balance("after a gift larger than the promise")
        self.assert_trial_balance_balances()

    def test_a_gift_to_the_appeal_is_not_credited_to_another_appeal(self):
        """Two appeals running at once, one member pledging to both.

        The sweep is church-wide — it walks every active pledge, not the ones
        belonging to the campaign the treasurer was looking at — so the only
        thing keeping the two appeals apart is the fund each names. A church
        runs a building appeal and a mission appeal in the same quarter as a
        matter of course, and the same faithful members pledge to both.
        """
        roof = self._launch_appeal()
        missions = self._launch_appeal(
            name="Mission Field Appeal", goal_amount="200000",
            target_department=self.local_fund.id, show_public_progress="")

        roof_pledge = self._pledge_at_the_desk(roof, self.grace, "60000")
        self._approve(roof_pledge)
        mission_pledge = self._pledge_at_the_desk(missions, self.grace, "20000",
                                                  client=self.office)

        # she gives once, to the roof
        self._give(self.grace, "30000", dt.date(2026, 7, 18), self.appeal_fund)
        self._auto_match()

        roof_pledge.refresh_from_db()
        mission_pledge.refresh_from_db()
        self.assertEqual(roof_pledge.paid, Decimal("30000"))
        self.assertEqual(
            mission_pledge.paid, Decimal("0"),
            "a gift to the roof appeal was credited against a promise made to "
            "a different appeal")
        self.assertEqual(missions.total_received, Decimal("0"))
        self.assertEqual(roof.total_received, Decimal("30000"))

        self.assert_fund_balance(self.appeal_fund, Decimal("30000"))
        self.assert_fund_balance(self.local_fund, Decimal("0"))
        self.assert_books_balance("with two appeals running at once")

    def test_the_appeal_is_cumulative_while_the_fund_is_as_at_a_date(self):
        """The two figures are asked different questions and must not be
        conflated.

        A fund balance is always "as at" a date — that is what makes it a
        balance. An appeal's standing is not: it is everything promised and
        everything received since the appeal opened, and it goes on the public
        page with no date on it at all. So a gift given in August is in the
        appeal's total the day it arrives and is correctly absent from July's
        fund summary, and a test that anchors both to one date would never
        notice if the appeal quietly acquired a period.
        """
        campaign = self._launch_appeal()
        pledge = self._pledge_at_the_desk(campaign, self.grace, "60000")
        self._approve(pledge)
        self._give(self.grace, "25000", dt.date(2026, 7, 18), self.appeal_fund)
        self._give(self.grace, "35000", dt.date(2026, 8, 5), self.appeal_fund)
        self._auto_match()

        pledge.refresh_from_db()
        self.assertEqual(pledge.paid, Decimal("60000"))
        self.assertEqual(pledge.status, Pledge.Status.FULFILLED)
        self.assertEqual(
            campaign.total_received, Decimal("60000"),
            "the appeal's standing lost the August gift to a period boundary")

        # the fund, read at two dates, tells the honest as-at story
        self.assert_fund_balance(self.appeal_fund, Decimal("25000"))
        self.assert_fund_balance(self.appeal_fund, Decimal("60000"),
                                 as_of=dt.date(2026, 8, 31))
        self.assert_books_balance("across a period boundary")
        self.assert_trial_balance_balances()

    # =========================================================================
    # The same campaign, read in two places
    # =========================================================================
    def test_the_public_standing_agrees_with_the_office_view(self):
        """A giver and the treasurer look at the same appeal from two doors.

        The office reads it off the campaign page, the giver off the public
        link. Neither knows about the other, and the pair of them is where the
        figures have drifted before.
        """
        campaign = self._launch_appeal()
        grace_pledge = self._pledge_at_the_desk(campaign, self.grace, "60000")
        peter_pledge = self._pledge_at_the_desk(campaign, self.peter, "40000")
        self._approve(grace_pledge)
        self._approve(peter_pledge)
        self._give(self.grace, "25000", dt.date(2026, 7, 18), self.appeal_fund)
        self._give(self.peter, "10000", dt.date(2026, 7, 19),
                   self.youth_sub_fund)
        self._auto_match()

        office_page = self.visit(self.office, "pledge_campaign_detail",
                                 args=[campaign.pk])
        office = self._office_standing(office_page.content.decode())
        _, public_page = self._open_public_form(campaign)
        public = self._public_standing(public_page.content.decode())
        report = self.visit(self.office, "pledge_campaign_report",
                            args=[campaign.pk])

        self.assert_agree(
            "“Pledged” on the campaign page, the public page and the "
            "campaign report",
            office_campaign_page=self._money(office["pledged"]),
            public_standing_page=self._money(public["pledged"]),
            campaign_report_total=self._money(report.context["totals"]["amount"]),
            model_total_pledged=self._money(campaign.total_pledged),
        )
        self.assert_agree(
            "“Received” on the campaign page, the public page and "
            "the campaign report",
            office_campaign_page=self._money(office["received"]),
            public_standing_page=self._money(public["received"]),
            campaign_report_paid=self._money(report.context["totals"]["paid"]),
            pledge_payment_links=self._money(
                self._payments_total(campaign=campaign)),
        )
        self.assert_agree(
            "how many pledges the appeal has",
            public_standing_page=public["pledges"],
            approved_pledge_count=campaign.approved_pledge_count,
            report_rows=len(report.context["rows"]),
        )
        self.assert_books_balance("after reading the appeal from two doors")

    def test_the_members_own_statement_agrees_with_the_appeal(self):
        """The fourth door onto the same money, and the only one the member
        herself is ever handed.

        A year-end statement is what a giver takes to be reassured that what
        she gave was recorded. It reads her giving off her pledges; the appeal
        reads the same links off its campaign. Nobody would ever see the two
        disagree — she has one page and the treasurer has the other — which is
        exactly why they are asserted together here.
        """
        campaign = self._launch_appeal()
        pledge = self._pledge_at_the_desk(campaign, self.grace, "60000")
        self._approve(pledge)
        self._give(self.grace, "25000", dt.date(2026, 7, 18), self.appeal_fund)
        self._auto_match()

        statement = self.visit(self.office, "pledge_member_statement",
                               args=[self.grace.pk], query="?year=2026")
        self.assert_agree(
            "what Grace has given toward her promise",
            member_statement_for_the_year=self._money(
                statement.context["total_paid_year"]),
            her_pledge_s_own_record=self._money(pledge.paid),
            the_appeal_s_received_figure=self._money(campaign.total_received),
        )
        self.assert_agree(
            "what Grace has promised",
            member_statement_total=self._money(
                statement.context["total_pledged"]),
            the_appeal_s_pledged_figure=self._money(campaign.total_pledged),
        )
        self.assert_books_balance("after reading a member's own statement")

    def _appeal_with_one_approved_and_one_draft(self):
        """60,000 approved and part-paid; 40,000 typed in and not yet reviewed."""
        campaign = self._launch_appeal()
        approved = self._pledge_at_the_desk(campaign, self.grace, "60000")
        self._approve(approved)
        self._give(self.grace, "25000", dt.date(2026, 7, 18), self.appeal_fund)
        self._auto_match()
        draft, _ = self._submit_public_pledge(
            campaign, "Peter Otieno", "0722334455", "40000")
        self.assertEqual(draft.status, Pledge.Status.DRAFT)
        return campaign, approved, draft

    def test_a_draft_pledge_does_not_move_what_a_giver_is_shown(self):
        """The public standing and the campaign page ignore an unreviewed
        promise. Anyone may type one into the public form."""
        campaign, _approved, _draft = \
            self._appeal_with_one_approved_and_one_draft()

        office_page = self.visit(self.office, "pledge_campaign_detail",
                                 args=[campaign.pk])
        office = self._office_standing(office_page.content.decode())
        _, public_page = self._open_public_form(campaign)
        public = self._public_standing(public_page.content.decode())

        self.assert_agree(
            "“Pledged” with a draft awaiting review",
            office_campaign_page=self._money(office["pledged"]),
            public_standing_page=self._money(public["pledged"]),
            model_total_pledged=self._money(campaign.total_pledged),
            only_the_approved_promise=self._money("60000"),
        )
        self.assertEqual(public["pledges"], Decimal("1"),
                         "an unreviewed promise raised the public tally")
        self.assert_books_balance("with a draft pledge outstanding")

    def test_a_draft_pledge_does_not_move_the_campaign_report_either(self):
        """DEFECT: the campaign report counts pledges nobody has approved.

        `CampaignPledgeReportView._rows` excludes only CANCELLED, so a DRAFT —
        including one a stranger typed into the public form a minute ago — is a
        row, and its amount is in the report's TOTAL and in the "% of goal
        pledged" the report band states. The campaign page beside it, the
        public standing block and `PledgeCampaign.total_pledged` all count only
        `RECOGNISED_STATUSES`.

        So one appeal answers "how much has been pledged" two ways in two
        places a treasurer reads on the same afternoon: 60,000 on the campaign
        page and on the public link, 100,000 on the campaign report — the one
        that gets printed and taken to a board. Nothing here is money, which is
        why it has survived: no balance is wrong, only the figure the church
        believes it has been promised.

        What should happen: the report's totals row (and `_goal_figures`) count
        the same set as `PledgeCampaign.counted_pledges`. Listing the drafts as
        rows is useful — a treasurer wants to see what is waiting — but they
        should be marked and excluded from the total, or totalled separately.
        """
        campaign, _approved, _draft = \
            self._appeal_with_one_approved_and_one_draft()
        report = self.visit(self.office, "pledge_campaign_report",
                            args=[campaign.pk])
        self.assert_agree(
            "“Pledged” on the campaign page and on the campaign report",
            campaign_report_total=self._money(report.context["totals"]["amount"]),
            model_total_pledged=self._money(campaign.total_pledged),
        )

    def test_the_campaign_page_counts_the_promises_behind_its_own_total(self):
        """DEFECT: the campaign page contradicts itself on one screen.

        The headline metric renders `total_pledged` (approved promises only)
        with `pledge_count` as its sub-label — and `pledge_count` excludes only
        CANCELLED, so it counts drafts. With 60,000 approved and a 40,000
        draft waiting, the metric reads "KES 60,000 · 2 pledges": a total and a
        count that are not about the same promises.

        Six lines higher, the SAME page tells the treasurer what the public
        link shows — "1 pledges, KES 60,000 pledged ... Drafts awaiting your
        approval are not counted" — so the page states the tally twice and
        disagrees with itself. `approved_pledge_count` exists precisely for
        this and is what the public block uses; the campaign metric was not
        moved onto it.

        What should happen: the count printed beneath `total_pledged` is
        `approved_pledge_count`. A separate "N awaiting approval" is the useful
        way to keep the drafts visible, and the page already has a place for it.
        """
        campaign, _approved, _draft = \
            self._appeal_with_one_approved_and_one_draft()
        page = self.visit(self.office, "pledge_campaign_detail",
                          args=[campaign.pk])
        html = page.content.decode()
        stated = re.search(r'<span class="wm-sub">(\d+) pledges</span>', html)
        self.assertIsNotNone(stated, "the campaign page no longer states a "
                                     "pledge count beside its total")
        self.assert_agree(
            "the pledge count printed beneath the campaign's pledged total",
            campaign_page_sub_label=Decimal(stated.group(1)),
            promises_in_that_total=campaign.approved_pledge_count,
        )

    # =========================================================================
    # Where the workflow ends
    # =========================================================================
    def test_every_page_the_appeal_ends_on_actually_opens(self):
        """A campaign that can be run but not looked at afterwards has not been
        run. This is the failure this application shipped five times."""
        campaign = self._launch_appeal()
        pledge = self._pledge_at_the_desk(campaign, self.grace, "60000")
        self._approve(pledge)
        self._give(self.grace, "60000", dt.date(2026, 7, 18), self.appeal_fund)
        self._auto_match()

        self.visit(self.office, "pledge_dashboard")
        self.visit(self.office, "pledge_campaign_list")
        self.visit(self.office, "pledge_campaign_detail", args=[campaign.pk])
        self.visit(self.office, "pledge_detail", args=[pledge.pk])
        self.visit(self.office, "pledge_list", query="?campaign=%d" % campaign.pk)
        self.visit(self.office, "pledge_campaign_report", args=[campaign.pk])
        self.visit(self.office, "pledge_campaign_report", args=[campaign.pk],
                   query="?group=tag&sort=outstanding")
        self.visit(self.office, "pledge_report")
        # the year is stated rather than defaulted: the statement defaults to
        # the real calendar year, and this workflow's money is dated 2026
        self.visit(self.office, "pledge_member_statement", args=[self.grace.pk],
                   query="?year=2026")
        # and the auditor, who may read everything and change nothing
        reading_room = self.acting_as(self.auditor)
        self.visit(reading_room, "pledge_campaign_detail", args=[campaign.pk])
        self.visit(reading_room, "pledge_campaign_report", args=[campaign.pk])

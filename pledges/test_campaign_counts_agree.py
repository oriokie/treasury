"""One appeal, one answer to "how much has been pledged, by how many".

A campaign publishes its standing in four places a treasurer can reach in one
afternoon: the campaign page, the public pledge link, the campaign report, and
the workbook that report exports. `PledgeCampaign.counted_pledges` — the
promises in `Pledge.RECOGNISED_STATUSES` — is the definition all of them are
supposed to be built from, and a DRAFT is deliberately outside it: since the
public pledge link went in, anyone with the URL can create one by typing it,
so nothing a giver or a board is shown may move before somebody has checked
the promise is real.

Two places had their own arithmetic instead.

The campaign report's rows excluded only CANCELLED, so a draft was in the
TOTAL and in the "% of goal pledged" band above it — 60,000 approved beside a
40,000 draft printed as 100,000 pledged on the one copy that leaves the office
for a board meeting. And the campaign page printed `total_pledged` (approved
only) under a `pledge_count` sub-label (drafts included): "KES 60,000 ·
2 pledges", one promise's money labelled with two, six lines below the same
page's public-link note correctly saying one.

Neither was money — no balance was wrong — which is exactly why both survived.
These tests pin the rule at the level the rule lives at: the report's totals
count what the campaign counts, and the count printed under a figure counts
the promises inside that figure. `pledge_count` itself is left alone; it is
the honest count of the live queue and the campaign list column wants it.
"""
import datetime as dt
import re
from decimal import Decimal

from django.contrib.auth.models import Group, User
from django.test import TestCase
from django.urls import reverse

from core.models import SiteConfig
from core.roles import TREASURER
from departments.models import Department
from giving.models import Transaction
from members.models import Member, MemberTag
from pledges.models import Pledge, PledgeCampaign, PledgePayment


class _Appeal(TestCase):
    """60,000 approved and part-paid; 40,000 typed in and not yet reviewed."""

    def setUp(self):
        self.treasurer = User.objects.create_user("t_agree", password="x")
        self.treasurer.groups.add(
            Group.objects.get_or_create(name=TREASURER)[0])
        self.client.force_login(self.treasurer)
        self.fund = Department.objects.create(name="Roof Fund",
                                              fund_type="LOCAL",
                                              category="DEVELOPMENT")
        self.board = MemberTag.objects.create(name="Church Board")
        self.grace = Member.objects.create(name="GRACE ATIENO",
                                           phone="254700111222")
        self.grace.tags.set([self.board])
        self.peter = Member.objects.create(name="PETER OTIENO",
                                           phone="254722334455")
        self.peter.tags.set([self.board])
        self.camp = PledgeCampaign.objects.create(
            name="Roof Appeal", target_department=self.fund,
            goal_amount=Decimal("500000"), show_public_progress=True,
            status=PledgeCampaign.Status.ACTIVE, start_date=dt.date(2026, 1, 1))

        self.approved = self._pledge(self.grace, "60000",
                                     Pledge.Status.ACTIVE)
        self._pay(self.approved, "25000")
        self.draft = self._pledge(self.peter, "40000", Pledge.Status.DRAFT,
                                  self_submitted=True)

        self.report_url = reverse("pledge_campaign_report", args=[self.camp.pk])
        self.page_url = reverse("pledge_campaign_detail", args=[self.camp.pk])

    def _pledge(self, member, amount, status, self_submitted=False):
        return Pledge.objects.create(
            campaign=self.camp, member=member, amount=Decimal(amount),
            status=status, self_submitted=self_submitted,
            start_date=dt.date(2026, 1, 1))

    def _pay(self, pledge, amount, ref=None):
        """A confirmed gift matched to a pledge.

        Written straight through the models rather than the match screen
        because one of these tests needs money sitting on a DRAFT — which the
        match screen now refuses, and which older records still carry.
        """
        t = Transaction.objects.create(
            date=dt.date(2026, 6, 1), channel="CASH", direction="CREDIT",
            amount=Decimal(amount), department=self.fund, member=pledge.member,
            allocation_status="MANUAL", confirmed=True,
            core_ref=ref or f"ROOF{pledge.pk}")
        return PledgePayment.objects.create(pledge=pledge, transaction=t,
                                            amount=Decimal(amount),
                                            date=dt.date(2026, 6, 1))


class ReportTotalsTests(_Appeal):
    """The report totals the promises the campaign recognises, and no others."""

    def test_the_report_total_is_the_campaigns_own_pledged_figure(self):
        """The disagreement in one line: the report and the model, side by
        side, on the same appeal."""
        totals = self.client.get(self.report_url).context["totals"]
        self.assertEqual(totals["amount"], Decimal("60000"))
        self.assertEqual(totals["amount"], self.camp.total_pledged)
        self.assertEqual(totals["n"], self.camp.approved_pledge_count)

    def test_money_written_against_a_draft_stays_out_of_given_too(self):
        """`total_received` already disowns it, so the report has to as well or
        the pair disagree from the other end."""
        self._pay(self.draft, "10000", ref="ROOFLEGACY")
        totals = self.client.get(self.report_url).context["totals"]
        self.assertEqual(totals["paid"], Decimal("25000"))
        self.assertEqual(totals["paid"], self.camp.total_received)
        self.assertEqual(totals["outstanding"], Decimal("35000"))

    def test_the_draft_is_still_listed_so_a_treasurer_can_see_it_waiting(self):
        """Excluded from the total is not the same as hidden. The point of
        this report is who has promised what, and a promise awaiting review is
        something a treasurer is here to act on."""
        rows = {r["member"].name: r for r in
                self.client.get(self.report_url).context["rows"]}
        self.assertIn("PETER OTIENO", rows)
        self.assertFalse(rows["PETER OTIENO"]["counted"])
        self.assertTrue(rows["GRACE ATIENO"]["counted"])
        self.assertIn("awaiting approval", rows["PETER OTIENO"]["status"])

    def test_the_awaiting_figures_account_for_the_difference(self):
        """The rows add to more than the TOTAL, so the report states the gap
        rather than leaving a reader to find it with a calculator."""
        totals = self.client.get(self.report_url).context["totals"]
        self.assertEqual(totals["awaiting_n"], 1)
        self.assertEqual(totals["awaiting_amount"], Decimal("40000"))

    def test_the_goal_band_is_built_from_the_corrected_total(self):
        """The band is the figure a board actually reads — "x% of goal
        pledged" — and it is computed from the totals row, so it inherited the
        inflation. 60,000 of a 500,000 goal is 12%, not 20%."""
        goal = self.client.get(self.report_url).context["goal"]
        self.assertEqual(goal["pct_pledged"], 12)
        self.assertEqual(goal["short"], Decimal("440000"))
        self.assertEqual(goal["fulfilment"], 41)      # 25,000 of 60,000

    def test_a_tag_group_subtotal_follows_the_same_rule(self):
        """Both members hold the board tag, so a group that counted drafts
        would restate the whole error one heading further down."""
        groups = {g["name"]: g for g in
                  self.client.get(self.report_url,
                                  {"group": "tag"}).context["groups"]}
        board = groups["Church Board"]
        self.assertEqual(board["totals"]["amount"], Decimal("60000"))
        self.assertEqual(board["totals"]["n"], 1)
        self.assertEqual(board["totals"]["awaiting_amount"], Decimal("40000"))
        self.assertEqual([r["member"].name for r in board["rows"]],
                         ["GRACE ATIENO", "PETER OTIENO"])

    def test_the_export_totals_what_the_screen_totals(self):
        """A workbook outlives the screen it came off. Its TOTAL is the
        campaign's pledged figure, and the draft it also lists is named
        underneath so the column can be added up by hand and still make
        sense."""
        body = self.client.get(self.report_url,
                               {"export": "csv"}).content.decode()
        total_line = next(l for l in body.splitlines()
                          if l.startswith("TOTAL"))
        self.assertIn("60000", total_line)
        self.assertNotIn("100000", body)
        self.assertIn("AWAITING APPROVAL", body)
        self.assertIn("40000", body)

    def test_the_workbook_writes_that_line_too(self):
        """The awaiting line mixes text into the money columns, and openpyxl is
        stricter about that than csv is. Exercised with a draft present because
        that is the only shape in which the row exists at all."""
        r = self.client.get(self.report_url, {"export": "xlsx"})
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.content)

    def test_an_appeal_with_nothing_waiting_reports_exactly_as_before(self):
        """The rule bites only on drafts: approve the promise and the report is
        the whole roll again, with no awaiting line in the workbook."""
        self.draft.status = Pledge.Status.ACTIVE
        self.draft.save()
        totals = self.client.get(self.report_url).context["totals"]
        self.assertEqual(totals["amount"], Decimal("100000"))
        self.assertEqual(totals["n"], 2)
        self.assertEqual(totals["awaiting_n"], 0)
        self.assertNotIn("AWAITING APPROVAL",
                         self.client.get(self.report_url,
                                         {"export": "csv"}).content.decode())

    def test_a_cancelled_pledge_is_neither_counted_nor_listed(self):
        """A withdrawn promise is not a promise waiting for review, and the
        report has always been right about that. Guarding it because the fix
        moved the line the totals are drawn at."""
        self.draft.status = Pledge.Status.CANCELLED
        self.draft.save()
        r = self.client.get(self.report_url)
        self.assertEqual([x["member"].name for x in r.context["rows"]],
                         ["GRACE ATIENO"])
        self.assertEqual(r.context["totals"]["awaiting_n"], 0)


class CampaignPageTests(_Appeal):
    """The page states its tally twice; both statements must be the one tally."""

    SUB_LABEL = re.compile(r'<span class="wm-sub">(\d+) pledges</span>')
    #: Matched as the rendered sub-label rather than as loose text: the status
    #: filter below carries the option "Draft (awaiting approval)" on every
    #: campaign, so a plain substring search reads as a headline that is not
    #: there.
    WAITING = re.compile(r'<span class="wm-sub">(\d+) awaiting approval</span>')

    def setUp(self):
        super().setUp()
        # The public-link note is the page's OTHER statement of the tally, and
        # it only renders when the public pledge form is switched on — which is
        # the configuration the contradiction was found in.
        cfg = SiteConfig.get()
        cfg.pledge_public_form_enabled = True
        cfg.save()

    def _body(self):
        return self.client.get(self.page_url).content.decode()

    def test_the_count_under_the_total_counts_the_promises_in_that_total(self):
        """"KES 60,000 · 2 pledges" was a total built from one promise and
        labelled with two."""
        body = self._body()
        stated = self.SUB_LABEL.search(body)
        self.assertIsNotNone(stated,
                             "the campaign page no longer states a pledge "
                             "count beside its total")
        self.assertEqual(int(stated.group(1)),
                         self.camp.approved_pledge_count)
        self.assertEqual(int(stated.group(1)), 1)

    def test_the_page_no_longer_contradicts_its_own_public_link_note(self):
        """Six lines apart on one screen: the note about what the public link
        publishes, and the headline metric. They were 1 and 2."""
        body = self._body()
        note = re.search(r"running total: (\d+) pledges", body)
        self.assertIsNotNone(note, "the public-link note lost its tally")
        self.assertEqual(int(note.group(1)),
                         int(self.SUB_LABEL.search(body).group(1)))

    def test_the_waiting_draft_is_said_out_loud_rather_than_dropped(self):
        """This is the page a treasurer reviews drafts on. Moving the headline
        onto the approved count must not make the unreviewed promise disappear
        from the summary."""
        waiting = self.WAITING.search(self._body())
        self.assertIsNotNone(waiting, "the draft is nowhere in the summary")
        self.assertEqual(int(waiting.group(1)), 1)

    def test_nothing_waiting_says_nothing(self):
        """No "0 awaiting approval" on an appeal with a clean queue."""
        self.draft.status = Pledge.Status.ACTIVE
        self.draft.save()
        body = self._body()
        self.assertIsNone(self.WAITING.search(body))
        self.assertEqual(int(self.SUB_LABEL.search(body).group(1)), 2)

    def test_pledge_count_itself_is_untouched(self):
        """The fix is at the call site, not a redefinition. `pledge_count` is
        the live queue — every pledge the church has not withdrawn — which is
        what the campaign list column and the dashboard mean by it. Redefining
        it to satisfy this page would have quietly changed those instead."""
        self.assertEqual(self.camp.pledge_count, 2)
        self.assertEqual(self.camp.approved_pledge_count, 1)

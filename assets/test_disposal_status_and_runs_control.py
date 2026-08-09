"""Two rules about how the register reports itself back to the treasurer.

**A recorded disposal reports itself on the register's face.** The lifecycle
service refuses DISPOSED as a bare status change — a disposal needs a date, a
method, proceeds and a fund, and posts a journal — and for a long time nothing
else set it either, so `FixedAsset.Status.DISPOSED` was unreachable through the
application. A projector sold for 300,000 went on sitting in the board's "Held
for disposal" column beside the assets genuinely still waiting to be sold, and
could never be archived off the board (HELD_SALE leads only back to IN_SERVICE
and IDLE, both refused once `disposed` is true, and ARCHIVED is reachable only
from DISPOSED). The document now reports the status through
`lifecycle.mark_disposed`, which is the one door to it.

**The runs page asks its control at a date the page can act on.** Depreciation
is charged by whole months from the FIRST of the month, so the register carries
the current month from its 1st; a run is dated at its month END and cannot be
posted before the month is over. Asked at today, therefore, the register↔ledger
control on the depreciation runs page read red by exactly one month's charge for
the whole register on about thirty days in thirty-one, and told the treasurer to
"post any outstanding monthly runs" that did not exist and could not have
helped. It is now asked at the month end of the run the page offers. The month
convention is untouched — it decides published net book value — so what these
tests pin is that the VERDICT moved and the register did not, and, just as
importantly, that a genuine break is still reported red.
"""
import datetime as dt
from decimal import Decimal

from django.contrib.auth.models import Group, User
from django.test import TestCase
from django.urls import reverse

from assets.models import AssetEvent, FixedAsset
from assets.services import lifecycle, runs as run_svc
from core.metrics import metrics
from core.roles import TREASURER
from departments.models import Department
from ledger.services import posting

S = FixedAsset.Status


def previous_month_end(on):
    return on.replace(day=1) - dt.timedelta(days=1)


class _Base(TestCase):
    def setUp(self):
        self.fund = Department.objects.create(
            name="General", fund_type=Department.FundType.LOCAL)
        self.tr = User.objects.create_user("runs_tr", password="x")
        self.tr.groups.add(Group.objects.get_or_create(name=TREASURER)[0])
        self.client.force_login(self.tr)


class DisposalReportsItsStatusTests(_Base):
    """The disposal document, posted through the real URL — the only path a
    treasurer has."""

    def setUp(self):
        super().setUp()
        self.asset = FixedAsset.objects.create(
            name="Sanctuary projector", category=FixedAsset.Category.EQUIPMENT,
            cost=Decimal("480000"), salvage_value=Decimal(0),
            acquired_on=dt.date(2024, 2, 10), in_service_on=dt.date(2024, 2, 10),
            method="STRAIGHT", rate=Decimal("15"),
            department=self.fund, status=S.HELD_SALE)

    def _dispose(self, on=dt.date(2026, 7, 20), proceeds="300000"):
        return self.client.post(
            reverse("asset_dispose", args=[self.asset.pk]),
            {"disposed_on": on.isoformat(), "proceeds": proceeds,
             "method": FixedAsset.DisposalMethod.SOLD, "fund": str(self.fund.pk)})

    def test_recording_a_disposal_moves_the_asset_to_disposed(self):
        self._dispose()
        self.asset.refresh_from_db()
        self.assertTrue(self.asset.disposed)
        self.assertEqual(
            self.asset.status, S.DISPOSED,
            "a sold asset is still shown in the column it was in before the sale")

    def test_the_disposal_is_on_the_timeline(self):
        """The status change is the register telling its own story; a disposal
        that leaves no event is a sale nobody can find afterwards."""
        self._dispose()
        event = self.asset.events.filter(kind=AssetEvent.Kind.DISPOSED).first()
        self.assertIsNotNone(event, "the disposal wrote no timeline entry")
        self.assertIn("Disposed", event.summary)
        self.assertEqual(event.actor, self.tr)

    def test_a_sold_asset_can_be_archived_off_the_board(self):
        """Before the document reported the status, HELD_SALE led only back to
        IN_SERVICE and IDLE — both refused once `disposed` is true — so a sold
        asset was stuck on the board for ever."""
        self._dispose()
        self.asset.refresh_from_db()
        self.assertEqual(lifecycle.allowed_transitions(self.asset), [S.ARCHIVED])
        lifecycle.transition(self.asset, S.ARCHIVED, user=self.tr)
        self.asset.refresh_from_db()
        self.assertEqual(self.asset.status, S.ARCHIVED)

    def test_the_status_is_still_refused_as_a_bare_change(self):
        """`mark_disposed` is a door beside the refusal, not a replacement for
        it: DISPOSED must stay unreachable from the Kanban board, which cannot
        supply a date, a method, proceeds or a fund."""
        with self.assertRaises(lifecycle.TransitionError):
            lifecycle.transition(self.asset, S.DISPOSED, user=self.tr)
        self.asset.refresh_from_db()
        self.assertEqual(self.asset.status, S.HELD_SALE)
        self.assertFalse(self.asset.disposed)
        self.assertNotIn(S.DISPOSED, lifecycle.allowed_transitions(self.asset))

    def test_mark_disposed_refuses_a_row_carrying_no_disposal(self):
        """The door only opens for a disposal that has actually been recorded —
        otherwise it would be exactly the back way into DISPOSED that
        `transition()` closes."""
        with self.assertRaises(lifecycle.TransitionError):
            lifecycle.mark_disposed(self.asset, user=self.tr)
        self.asset.refresh_from_db()
        self.assertEqual(self.asset.status, S.HELD_SALE)

    def test_recording_a_disposal_twice_leaves_one_status_and_one_event(self):
        self._dispose()
        self.asset.refresh_from_db()
        lifecycle.mark_disposed(self.asset, user=self.tr)   # a replay/rebuild
        self.assertEqual(self.asset.status, S.DISPOSED)
        self.assertEqual(self.asset.events.filter(kind=AssetEvent.Kind.DISPOSED).count(), 1)

    def test_the_register_totals_key_off_the_disposal_and_not_the_status(self):
        """The reconciliation must not have moved. Cost, accumulated
        depreciation and net book value are all drawn from
        `assets.models.assets_live_at`, which asks `disposed`/`disposed_on` and
        never `status` — so writing the status is a report, not a restatement.
        """
        as_of = dt.date(2026, 7, 31)
        self._dispose(on=dt.date(2026, 7, 20))
        self.asset.refresh_from_db()
        with_status = (metrics.fixed_assets_cost(as_of),
                       metrics.accumulated_depreciation(as_of))
        # put the status back to what the application used to leave it at
        FixedAsset.objects.filter(pk=self.asset.pk).update(status=S.HELD_SALE)
        self.assertEqual(
            (metrics.fixed_assets_cost(as_of),
             metrics.accumulated_depreciation(as_of)),
            with_status,
            "the register's totals changed with the status — they must follow "
            "the recorded disposal, not the column the asset is drawn in")


class RunsPageControlTests(_Base):
    """A register posted as far as the page allows, read mid-month.

    Dates are derived from the real today so the assertions mean the same thing
    in any month: `control_end` is the last month end a run can cover, and the
    ledger is brought fully up to it.
    """

    #: 600,000 at 12% straight line is 6,000 a month.
    MONTHLY = Decimal("6000")

    def setUp(self):
        super().setUp()
        from giving.models import Transaction
        self.today = dt.date.today()
        self.control_end = previous_month_end(self.today)
        # The ledger's opening date is 1 January of the first transaction year;
        # seeding it in the control year means the opening entry and every run
        # this test posts sit on or before `control_end`.
        Transaction.objects.create(
            date=dt.date(self.control_end.year, 1, 5), amount=Decimal("1000"),
            direction="CREDIT", channel="CASH", allocation_status="MANUAL",
            confirmed=True, department=self.fund, reference="SEED")
        self.asset = FixedAsset.objects.create(
            name="Sound system", category=FixedAsset.Category.EQUIPMENT,
            cost=Decimal("600000"), salvage_value=Decimal(0),
            acquired_on=dt.date(self.control_end.year - 1, 1, 1),
            in_service_on=dt.date(self.control_end.year - 1, 1, 1),
            method="STRAIGHT", rate=Decimal("12"),
            department=self.fund, status=S.IN_SERVICE)
        posting.rebuild()
        for month in range(1, self.control_end.month + 1):
            run_svc.post_run(run_svc.generate_run(self.control_end.year, month))

    def _page(self):
        response = self.client.get(reverse("depreciation_runs"))
        self.assertEqual(response.status_code, 200)
        return response

    def test_the_control_is_asked_at_the_month_end_the_page_offers_a_run_for(self):
        """The two have to be the same date, or the panel can complain about
        something the form below it cannot fix."""
        ctx = self._page().context
        self.assertEqual(ctx["control_as_of"], self.control_end)
        self.assertEqual((ctx["suggest_year"], ctx["suggest_month"]),
                         (self.control_end.year, self.control_end.month))

    def test_a_fully_posted_register_reads_as_agreeing_mid_month(self):
        """The defect: on any day that is not a month end this said the
        register was broken, with nothing left to post."""
        ctx = self._page().context
        self.assertFalse(ctx["suggest_pending"],
                         "the fixture has not posted everything the page offers")
        self.assertTrue(
            ctx["reconciled"],
            f"the page says the register does not reconcile and offers nothing "
            f"that would make it: {ctx['control']}")
        for key in ("cost", "accdep", "nbv"):
            self.assertEqual(ctx["control"][key]["diff"], Decimal(0))

    def test_the_current_month_is_named_rather_than_shown_as_a_break(self):
        """The register does still carry the current month from its 1st. That
        difference is real and is printed as what it is — the month's charge —
        instead of being left to read as a control failure."""
        page = self._page()
        self.assertEqual(page.context["in_month_charge"], self.MONTHLY)
        self.assertEqual(page.context["rec"]["accdep"]["diff"], self.MONTHLY)
        # and it is on the page, not merely in the context
        self.assertContains(page, self.today.strftime("%B"))

    def test_a_real_break_is_still_reported(self):
        """The point of the control. An asset on the register that the ledger
        has never heard of — an invoice not yet approved, an import — must still
        turn the panel red, or moving the date has simply switched the light off.
        """
        FixedAsset.objects.create(
            name="Unbacked minibus", category=FixedAsset.Category.VEHICLE,
            cost=Decimal("1200000"), salvage_value=Decimal(0),
            acquired_on=dt.date(self.control_end.year, 1, 1),
            in_service_on=dt.date(self.control_end.year, 1, 1),
            method="NONE", rate=Decimal(0),
            department=self.fund, status=S.IN_SERVICE)
        ctx = self._page().context
        self.assertFalse(ctx["reconciled"])
        self.assertEqual(ctx["control"]["cost"]["diff"], Decimal("1200000"))

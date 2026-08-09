"""A fixed asset, from the payment that bought it to the day it is sold.

The register is a subsidiary ledger. Everything a treasurer does to an asset —
buying it, commissioning it, depreciating it, lending it out, moving it between
funds, selling it — has to leave the register and the general ledger saying the
same thing about three numbers: cost, accumulated depreciation, and net book
value. That is what `core.metrics.register_vs_ledger` measures, and it is the
only control in the application that watches the two agree.

Every step in this file therefore ends in the same place: the reconciliation,
asked at a month end with that month's depreciation posted, must read exactly
zero on all three. Each part of that has unit tests of its own. What none of
them can show is that a treasurer who does the ordinary things in the ordinary
order still ends the month reconciled — and the interesting failures in this
area have all been at the joins: value on the register that the ledger has
never heard of, a disposal struck against the wrong carrying amount, a journal
posted only when somebody remembers to rebuild.

Two conventions worth knowing before reading the steps:

* **Depreciation is charged by whole months.** An asset commissioned on 10
  February is charged for the whole of February. So the register's accumulated
  depreciation at 15 July already includes July, while the ledger only carries
  it once the July run has been posted (dated the 31st). The two agree at
  MONTH ENDS with every run through that month posted, and nowhere else — so
  that is where this file asks. The depreciation runs page does not ask there
  — it asks as at `date.today()` — which is the defect recorded at the bottom
  of this file.
* **The register is temporal.** Cost counts from the acquisition date to the
  disposal date, and the ledger matches it because the payment that bought the
  asset carries the cost in on its own date.
"""
import calendar
import datetime as dt
from decimal import Decimal

from django.db.models import Sum

from assets.models import (Acquisition, AssetAssignment, AssetTransfer,
                           DepreciationRun, FixedAsset, Location)
from cashbook.models import Expense
from core import roles
from core.metrics import metrics
from departments.models import Department
from giving.models import Transaction
from ledger.models import JournalEntry, JournalLine

from .base import BusinessWorkflowTest, WorkflowError

#: The year this workflow runs in. The harness's fixed "today" is 15 July 2026.
YEAR = 2026
OPENING = dt.date(YEAR, 1, 1)          # the ledger's asset opening date
BOUGHT = dt.date(YEAR, 2, 10)          # the projector is bought and delivered
ISSUED = dt.date(YEAR, 7, 1)           # lent to the AV volunteer
RETURNED = dt.date(YEAR, 7, 10)
MOVED = dt.date(YEAR, 7, 12)           # handed over to the youth ministry
SOLD = dt.date(YEAR, 7, 20)

PROJECTOR_COST = Decimal("480000")     # 15% straight line -> 6,000 a month
PROJECTOR_MONTHLY = Decimal("6000")
MINIBUS_COST = Decimal("1200000")      # 20% straight line -> 20,000 a month
MINIBUS_MONTHLY = Decimal("20000")
LAND_COST = Decimal("2400000")         # land does not depreciate
PROCEEDS = Decimal("300000")


def month_end(year, month):
    return dt.date(year, month, calendar.monthrange(year, month)[1])


def money(value):
    """Two decimal places.

    `assert_agree` compares the STRING form of each Decimal, so 3,600,000 and
    3,600,000.00 are reported as disagreeing even though they are the same
    amount. Every figure handed to it here goes through this first, so a
    failure means the money differs and not the scale it was written at.
    """
    return Decimal(value or 0).quantize(Decimal("0.01"))


class AssetFromAcquisitionToDisposal(BusinessWorkflowTest):
    """One projector, walked the whole way, with the reconciliation asked at
    every stage."""

    def setUp(self):
        super().setUp()
        self.office = self.acting_as(self.treasurer)
        # A second treasurer. A fund transfer must be approved by someone other
        # than the person who asked for it, and so must a payment, so a church
        # modelled with one treasurer cannot walk this process at all.
        self.checker = self.make_user("wf_treasurer_2", roles.TREASURER)
        self.back_office = self.acting_as(self.checker)
        # The volunteer the projector gets lent to. Not an officer — the person
        # holding an asset usually is not one.
        self.volunteer = self.make_user("wf_av_volunteer")

        # The fund the projector ends up belonging to. Background: a church
        # already has its funds before it buys anything.
        self.youth_fund = Department.objects.create(
            name="Youth Ministry", slug="wf-youth",
            fund_type=Department.FundType.LOCAL,
            category=Department.Category.MINISTRY)

        # Locations. There is no HTTP entry point for creating one (no view, no
        # URL), so these are background rows — see the report.
        self.sanctuary = Location.objects.create(name="Sanctuary")
        self.youth_hall = Location.objects.create(name="Youth hall")

        # The depreciation policy, set the way a treasurer sets it: from the
        # rules page, per category.
        self.submit(self.office, "depreciation_rules", {
            "method_EQUIPMENT": "STRAIGHT", "rate_EQUIPMENT": "15",
            "method_VEHICLE": "STRAIGHT", "rate_VEHICLE": "20",
        })

        # Money in the bank, received the way money is received.
        Transaction.objects.create(
            date=dt.date(YEAR, 1, 5), amount=Decimal("800000"), direction="CREDIT",
            channel="BANK", confirmed=True, allocation_status="MANUAL",
            department=self.local_fund, payer_name="BUILDING FUND APPEAL")

        # The register the church already keeps. A workflow tested against an
        # empty register proves nothing about the totals it moves (#125), and
        # the reconciliation is a TOTAL — it only means something when there is
        # something else in it to be got wrong.
        self.land = FixedAsset.objects.create(
            name="Church plot (LR 209/4471)", category=FixedAsset.Category.LAND,
            cost=LAND_COST, salvage_value=Decimal(0),
            acquired_on=dt.date(2018, 5, 1), in_service_on=dt.date(2018, 5, 1),
            department=self.local_fund, status=FixedAsset.Status.IN_SERVICE)
        self.minibus = FixedAsset.objects.create(
            name="Church minibus", category=FixedAsset.Category.VEHICLE,
            cost=MINIBUS_COST, salvage_value=Decimal(0),
            acquired_on=dt.date(2024, 2, 1), in_service_on=dt.date(2024, 2, 1),
            department=self.local_fund, status=FixedAsset.Status.IN_SERVICE,
            location_fk=self.sanctuary)

        # The general ledger as the church left it: the asset opening entry
        # brings the control accounts up to that existing register. This is
        # background state, not a step of the workflow — everything the
        # workflow itself posts, it posts through the app.
        from ledger.services import posting
        posting.rebuild()

    # -- private helpers (see the report: none of these belong in base.py) -----

    def _ledger(self, system_key, as_of=None):
        """A control account's balance (debits less credits) as at a date."""
        qs = JournalLine.objects.filter(account__system_key=system_key)
        if as_of:
            qs = qs.filter(entry__date__lte=as_of)
        agg = qs.aggregate(d=Sum("debit"), c=Sum("credit"))
        return (agg["d"] or Decimal(0)) - (agg["c"] or Decimal(0))

    def assert_register_ties_to_ledger(self, as_of, stage):
        """The control this whole file exists for.

        Cost, accumulated depreciation and net book value, each read from the
        register and from the general ledger, must be the same figure. A
        difference here means the subsidiary ledger and the control accounts
        disagree — which is the one thing the register is supposed to make
        impossible.
        """
        rec = metrics.register_vs_ledger(as_of)
        bad = {k: v for k, v in rec.items() if v["diff"] != 0}
        if bad:
            detail = "\n".join(
                f"  {k:<8} register {v['register']:>14,.2f}  "
                f"ledger {v['ledger']:>14,.2f}  out by {v['diff']:>12,.2f}"
                for k, v in rec.items())
            raise WorkflowError(
                f"The register and the ledger disagree as at {as_of:%d %b %Y} "
                f"({stage}):\n{detail}")
        return rec

    def _post_depreciation(self, *months, year=YEAR):
        """The treasurer runs the month's depreciation from the runs page."""
        for month in months:
            self.submit(self.office, "depreciation_runs",
                        {"action": "generate_post", "year": str(year),
                         "month": str(month)})
            run = DepreciationRun.objects.get(year=year, month=month)
            if run.status != DepreciationRun.Status.POSTED:
                raise WorkflowError(
                    f"The {year}-{month:02d} depreciation run is "
                    f"{run.get_status_display()}, not posted — the page accepted "
                    f"the click and charged nothing.")

    def _post_every_run_the_page_offers(self):
        """Bring the ledger completely up to date, as far as the app allows.

        That means every month from January 2026 up to but NOT including the
        current calendar one, because that is the whole set the runs page ever
        offers: its prompt is hardcoded to the PREVIOUS calendar month, and a
        run is dated at its own month end, so the current month's charge could
        not reach a ledger read "as at today" before the month has ended
        anyway. Computed from the real date rather than written out as 1..7, so
        the assertions that follow mean the same thing in any month.
        """
        today = dt.date.today()
        year, month = YEAR, 1
        while (year, month) < (today.year, today.month):
            self._post_depreciation(month, year=year)
            year, month = (year + 1, 1) if month == 12 else (year, month + 1)

    def _put_the_projector_on_the_register(self):
        """Step: the treasurer adds the projector to the register."""
        self.submit(self.office, "asset_create", {
            "name": "Sanctuary projector", "category": FixedAsset.Category.EQUIPMENT,
            "status": FixedAsset.Status.PLANNED,
            "acquired_on": BOUGHT.isoformat(), "in_service_on": "",
            "cost": str(PROJECTOR_COST), "salvage_value": "0",
            "method": "", "rate": "",
            "department": self.local_fund.id, "location_fk": self.sanctuary.id,
            "location": "", "tag": "AV-001", "serial_no": "EPX-99213",
            "reference": "INV-4471", "notes": "Replaces the failed 2019 unit.",
            "acq_source": Acquisition.Source.PURCHASE, "donor_name": "",
        })
        return FixedAsset.objects.get(name="Sanctuary projector")

    def _pay_for_it(self, asset):
        """Step: the payment that bought it, recorded and approved."""
        self.submit(self.office, "expense_create", {
            "date": BOUGHT.isoformat(), "department": self.local_fund.id,
            "description": "Sanctuary projector", "amount": str(PROJECTOR_COST),
            "category": Expense.Category.OTHER,
            "expenditure_type": Expense.ExpenditureType.CAPITAL,
            "capitalized_asset": asset.pk,
            "method": Expense.Method.BANK, "payee": "Nairobi AV Supplies",
            "voucher_no": "V-2201",
        })
        payment = Expense.objects.get(voucher_no="V-2201")
        self.submit(self.back_office, "expense_approve", {"action": "approve"},
                    args=[payment.pk])
        payment.refresh_from_db()
        return payment

    def _acquire_and_commission(self):
        asset = self._put_the_projector_on_the_register()
        self._pay_for_it(asset)
        self.submit(self.office, "asset_transition",
                    {"status": FixedAsset.Status.IN_SERVICE,
                     "note": "Installed and tested"}, args=[asset.pk])
        asset.refresh_from_db()
        return asset

    def _issue(self, asset, to=None):
        self.submit(self.office, "asset_assign", {
            "custodian": (to or self.volunteer).pk, "holder_name": "",
            "location": self.youth_hall.pk, "from_date": ISSUED.isoformat(),
            "condition_out": "Good, lamp at 40%", "note": "Youth week of prayer",
        }, args=[asset.pk])

    def _check_in(self, asset):
        self.submit(self.office, "asset_checkin", {
            "to_date": RETURNED.isoformat(), "condition_in": "Good, lamp at 45%",
        }, args=[asset.pk])

    def _transfer_to_youth(self, asset):
        self.submit(self.office, "asset_transfer", {
            "to_location": self.youth_hall.pk, "to_fund": self.youth_fund.pk,
            "date": MOVED.isoformat(),
            "reason": "The youth ministry now runs the AV desk",
        }, args=[asset.pk])
        transfer = AssetTransfer.objects.get(asset=asset)
        self.submit(self.back_office, "asset_transfer_decide",
                    {"decision": "approve"}, args=[transfer.pk])
        transfer.refresh_from_db()
        asset.refresh_from_db()
        return transfer

    def _dispose(self, asset, proceeds=PROCEEDS, fund=None):
        self.submit(self.office, "asset_dispose", {
            "disposed_on": SOLD.isoformat(), "proceeds": str(proceeds),
            "method": FixedAsset.DisposalMethod.SOLD,
            "fund": (fund or self.youth_fund).pk,
        }, args=[asset.pk])
        asset.refresh_from_db()
        return asset

    # -- the workflow ---------------------------------------------------------

    def test_a_projector_from_purchase_to_sale_ties_to_the_ledger_throughout(self):
        # 1. The month before anything happens, the treasurer runs January's
        #    depreciation. This is the baseline: the register the church
        #    already keeps agrees with the ledger before we touch it.
        self._post_depreciation(1)
        jan = self.assert_register_ties_to_ledger(month_end(YEAR, 1), "before we start")
        self.assert_agree(
            "the register the church already keeps, at 31 January",
            register_cost=money(jan["cost"]["register"]),
            land_plus_minibus=money(LAND_COST + MINIBUS_COST))

        # 2. February: the treasurer buys a projector and puts it on the
        #    register. It is PLANNED — bought and delivered, not yet installed.
        asset = self._put_the_projector_on_the_register()
        self.assertEqual(asset.status, FixedAsset.Status.PLANNED)
        self.assertIsNotNone(
            getattr(asset, "acquisition", None),
            "the register must record HOW an asset was acquired, not just that it exists")
        self.assertEqual(asset.acquisition.source, Acquisition.Source.PURCHASE)
        self.assertEqual(asset.acquisition.amount, PROJECTOR_COST)

        # 3. January is untouched by a February purchase. The register is
        #    temporal, so adding an asset today cannot restate a month that has
        #    already been reconciled.
        self.assert_register_ties_to_ledger(month_end(YEAR, 1), "after a February purchase")

        # 4. Right now the register carries value the ledger has never heard
        #    of: an asset with no payment behind it. That is a real state a
        #    treasurer can be in for as long as the invoice sits unapproved,
        #    and the app has a page whose whole job is to explain it — so the
        #    difference and the explanation must be the same number.
        february = month_end(YEAR, 2)
        unbacked = metrics.register_vs_ledger(february)
        coverage = metrics.acquisition_coverage(february)
        self.assert_agree(
            "an asset with no payment behind it, as the control sees it and as "
            "the pre-flight explains it",
            reconciliation_difference=money(unbacked["cost"]["diff"]),
            preflight_predicted=money(coverage["totals"]["predicted_diff"]),
            the_projectors_cost=money(PROJECTOR_COST))

        # 5. The payment is recorded and a SECOND treasurer approves it. Only
        #    then does the cost reach the ledger — and it must land in fixed
        #    assets, not in running costs, because the church bought a thing.
        payment = self._pay_for_it(asset)
        self.assertEqual(payment.status, Expense.Status.APPROVED)
        self.assertEqual(payment.capitalized_asset_id, asset.pk,
                         "the payment must stay linked to the asset it bought")
        self.assert_books_balance("after paying for the projector")

        # 6. It is commissioned into service. That is the date depreciation
        #    runs from, so it is the step that starts the charge.
        self.submit(self.office, "asset_transition",
                    {"status": FixedAsset.Status.IN_SERVICE,
                     "note": "Installed and tested"}, args=[asset.pk])
        asset.refresh_from_db()
        self.assertEqual(asset.status, FixedAsset.Status.IN_SERVICE)
        self.assertEqual(asset.in_service_on, BOUGHT,
                         "commissioning must record when depreciation starts")

        # 7. February's depreciation is run and posted. Now the whole register
        #    — old assets and new — ties to the ledger again.
        self._post_depreciation(2)
        feb = self.assert_register_ties_to_ledger(february, "the month of purchase")
        self.assert_agree(
            "cost at 28 February, read from the register and from the ledger",
            register=money(feb["cost"]["register"]),
            ledger=money(feb["cost"]["ledger"]),
            land_minibus_and_projector=money(
                LAND_COST + MINIBUS_COST + PROJECTOR_COST))
        self.assert_agree(
            "the projector's first month of depreciation",
            register_accumulated=money(asset.accumulated_depreciation(february)),
            one_month=money(PROJECTOR_MONTHLY))
        self.assert_books_balance("after February's depreciation")

        # 8. March to June: the treasurer runs the month, every month. Net book
        #    value has to fall by exactly what was charged — not by whatever
        #    the engine would say if asked again later.
        for month in (3, 4, 5, 6):
            before = asset.net_book_value(month_end(YEAR, month - 1))
            self._post_depreciation(month)
            after = asset.net_book_value(month_end(YEAR, month))
            charged = (DepreciationRun.objects.get(year=YEAR, month=month)
                       .lines.get(asset=asset).amount)
            self.assert_agree(
                f"the projector's net book value against {YEAR}-{month:02d}'s charge",
                fall_in_net_book_value=money(before - after),
                charged_by_the_run=money(charged),
                the_monthly_rate=money(PROJECTOR_MONTHLY))
            self.assert_register_ties_to_ledger(
                month_end(YEAR, month), f"after {YEAR}-{month:02d}'s run")
        self.assert_books_balance("after four more months of depreciation")

        # The whole church's charge for June, read two ways: the run, and the
        # ledger's depreciation expense account. (The third reading — the
        # `depreciation_expense` metric the statements use — does NOT agree
        # with these; see
        # test_the_depreciation_the_statement_reports_is_the_depreciation_the_ledger_posted.)
        june = month_end(YEAR, 6)
        june_run = DepreciationRun.objects.get(year=YEAR, month=6)
        self.assert_agree(
            "June's depreciation charge for the whole register",
            the_run=money(june_run.total_charge),
            depreciation_expense_in_the_ledger=money(
                self._ledger("DEPRECIATION_EXPENSE", june)
                - self._ledger("DEPRECIATION_EXPENSE", month_end(YEAR, 5))),
            projector_plus_minibus=money(PROJECTOR_MONTHLY + MINIBUS_MONTHLY))

        # 9. July: the projector is issued to the AV volunteer for youth week.
        self._issue(asset)
        asset.refresh_from_db()
        held = AssetAssignment.objects.get(asset=asset, to_date__isnull=True)
        self.assertEqual(held.custodian, self.volunteer)
        self.assertEqual(asset.custodian, self.volunteer,
                         "the register must be able to answer 'who has it?'")

        # 10. While it is in his hands it can be neither written off nor sold.
        #     Checked here, in the middle of the workflow, because this is
        #     where a treasurer actually meets the rule.
        self.submit(self.office, "asset_transition",
                    {"status": FixedAsset.Status.HELD_SALE}, args=[asset.pk])
        asset.refresh_from_db()
        self.assertEqual(
            asset.status, FixedAsset.Status.IN_SERVICE,
            "an asset in someone's hands was marked held for disposal")
        self._dispose(asset)
        self.assertFalse(
            asset.disposed,
            "an asset still issued to a custodian was disposed of — its "
            "assignment could then never be closed")

        # 11. It comes back.
        self._check_in(asset)
        asset.refresh_from_db()
        held.refresh_from_db()
        self.assertEqual(held.to_date, RETURNED)
        self.assertIsNone(asset.custodian)

        # 12. The youth ministry takes it over: a change of owning fund, which
        #     is an accounting event, so it is requested by one treasurer and
        #     approved by another.
        nbv_at_transfer = asset.net_book_value(MOVED)
        transfer = self._transfer_to_youth(asset)
        self.assertEqual(transfer.status, AssetTransfer.Status.APPROVED)
        self.assertEqual(transfer.approved_by, self.checker)
        self.assertEqual(asset.department, self.youth_fund)

        # The move is equity-only, so the control accounts do not feel it — and
        # that is exactly why a missing transfer journal used to be invisible.
        # Assert the journal itself, at the carrying value it moved.
        entry = JournalEntry.objects.get(source_type="asset_transfer",
                                         source_id=transfer.pk)
        moved = entry.lines.aggregate(d=Sum("debit"))["d"] or Decimal(0)
        self.assert_agree(
            "the value moved between the two funds",
            journal=money(moved),
            net_book_value_on_the_day=money(nbv_at_transfer),
            cost_less_six_months=money(PROJECTOR_COST - PROJECTOR_MONTHLY * 6))
        self.assert_books_balance("after transferring the projector between funds")

        # 13. It is held for disposal — now allowed, because nobody holds it —
        #     and then sold.
        self.submit(self.office, "asset_transition",
                    {"status": FixedAsset.Status.HELD_SALE}, args=[asset.pk])
        asset.refresh_from_db()
        self.assertEqual(asset.status, FixedAsset.Status.HELD_SALE)

        carrying = asset.net_book_value(SOLD)
        self._dispose(asset)
        self.assertTrue(asset.disposed, "the sale did not go through")
        self.assertEqual(asset.disposed_on, SOLD)
        self.assert_agree(
            "the loss on selling a 480,000 projector for 300,000 with six "
            "months of depreciation behind it",
            recorded_on_the_register=money(asset.disposal_gain_loss),
            proceeds_less_carrying_value=money(PROCEEDS - carrying),
            expected=money("-144000"))

        # 14. July's depreciation is run last, as it is at a month end. The
        #     projector left mid-month, so the month it left is still charged —
        #     and the disposal takes that same accumulated total back out, so
        #     the two must not both land.
        self._post_depreciation(7)
        july = month_end(YEAR, 7)
        final = self.assert_register_ties_to_ledger(july, "after the sale")

        # 15. What the books say at the end of it all.
        self.assert_agree(
            "cost after the projector has gone",
            register=money(final["cost"]["register"]),
            ledger=money(final["cost"]["ledger"]),
            land_and_minibus_only=money(LAND_COST + MINIBUS_COST))
        self.assert_agree(
            "accumulated depreciation after the projector has gone",
            register=money(final["accdep"]["register"]),
            ledger=money(final["accdep"]["ledger"]),
            thirty_months_of_minibus=money(MINIBUS_MONTHLY * 30))
        self.assert_agree(
            "net book value, three ways",
            reconciliation=money(final["nbv"]["register"]),
            ledger=money(final["nbv"]["ledger"]),
            the_metric=money(metrics.net_book_value(july)))

        # The money: the building fund paid for it, the youth fund sold it.
        self.assert_fund_balance(self.local_fund, Decimal("320000"), july)
        self.assert_fund_balance(self.youth_fund, PROCEEDS, july)
        self.assert_books_balance("at the end of the asset's life")
        self.assert_trial_balance_balances()

    # -- the rules that guard the ends of the process -------------------------

    def test_an_asset_in_someones_hands_can_be_neither_written_off_nor_sold(self):
        """Both ways out of the register have to ask the same question.

        The disposal flow does not go through `transition()`, so for a long
        time only one of them did — and the register could write off something
        that was still checked out, leaving an assignment nobody could ever
        close.
        """
        asset = self._acquire_and_commission()
        self._post_depreciation(1, 2)
        self._issue(asset)

        # held for disposal: refused
        self.submit(self.office, "asset_transition",
                    {"status": FixedAsset.Status.HELD_SALE}, args=[asset.pk])
        asset.refresh_from_db()
        self.assertEqual(asset.status, FixedAsset.Status.IN_SERVICE)

        # sold: refused, and no money moved
        self._dispose(asset)
        self.assertFalse(asset.disposed)
        self.assertEqual(
            Transaction.objects.filter(department=self.youth_fund).count(), 0,
            "a refused disposal still banked the proceeds")
        self.assert_fund_balance(self.youth_fund, Decimal("0"), month_end(YEAR, 7))
        self.assert_register_ties_to_ledger(month_end(YEAR, 2), "after two refusals")

        # and the assignment is still open and still closeable
        self._check_in(asset)
        self.assertFalse(
            AssetAssignment.objects.filter(asset=asset, to_date__isnull=True).exists())

        # now it may be held for disposal
        self.submit(self.office, "asset_transition",
                    {"status": FixedAsset.Status.HELD_SALE}, args=[asset.pk])
        asset.refresh_from_db()
        self.assertEqual(asset.status, FixedAsset.Status.HELD_SALE)
        self.assert_books_balance("after refusing to dispose of an issued asset")

    def test_a_fund_transfer_cannot_be_approved_by_the_person_who_asked_for_it(self):
        """Segregation of duties, walked. Moving an asset between funds moves
        money between two funds' net assets, so one person must not be able to
        do it alone."""
        asset = self._acquire_and_commission()
        self._post_depreciation(1, 2)

        self.submit(self.office, "asset_transfer", {
            "to_location": self.youth_hall.pk, "to_fund": self.youth_fund.pk,
            "date": MOVED.isoformat(), "reason": "Handover to the youth ministry",
        }, args=[asset.pk])
        transfer = AssetTransfer.objects.get(asset=asset)
        self.assertEqual(transfer.requested_by, self.treasurer)

        # the requester tries to wave it through
        self.submit(self.office, "asset_transfer_decide", {"decision": "approve"},
                    args=[transfer.pk])
        transfer.refresh_from_db()
        asset.refresh_from_db()
        self.assertEqual(transfer.status, AssetTransfer.Status.PENDING,
                         "a treasurer approved their own transfer")
        self.assertEqual(asset.department, self.local_fund,
                         "the asset moved fund on a transfer nobody approved")
        self.assertFalse(
            JournalEntry.objects.filter(source_type="asset_transfer",
                                        source_id=transfer.pk).exists(),
            "an unapproved transfer posted an inter-fund journal")

        # an auditor cannot either
        reading_room = self.acting_as(self.auditor)
        self.submit(reading_room, "asset_transfer_decide", {"decision": "approve"},
                    args=[transfer.pk])
        transfer.refresh_from_db()
        self.assertEqual(transfer.status, AssetTransfer.Status.PENDING)

        # the second treasurer can, and only then does the value move
        self.submit(self.back_office, "asset_transfer_decide", {"decision": "approve"},
                    args=[transfer.pk])
        transfer.refresh_from_db()
        asset.refresh_from_db()
        self.assertEqual(transfer.status, AssetTransfer.Status.APPROVED)
        self.assertEqual(asset.department, self.youth_fund)
        entry = JournalEntry.objects.get(source_type="asset_transfer",
                                         source_id=transfer.pk)
        self.assert_agree(
            "the carrying value that moved between the funds",
            journal=money(entry.lines.aggregate(d=Sum("debit"))["d"]),
            net_book_value=money(asset.net_book_value(MOVED)))
        self.assert_books_balance("after an approved inter-fund asset transfer")
        # equity moved; the control accounts did not
        self.assert_register_ties_to_ledger(month_end(YEAR, 2), "after a fund transfer")

    # -- what the disposal reports --------------------------------------------

    def test_the_loss_on_the_sale_is_what_income_and_expenditure_reports(self):
        """One disposal, four readings.

        The register stores the gain/(loss); the ledger posts it; the metric
        aggregates it; the Income & Expenditure statement prints it. They are
        four different routes to one number, and the recurring fault in this
        codebase is exactly that kind of number drifting apart — here it can
        even change sign, because for a while the ledger re-ran the
        depreciation engine instead of believing what the register recorded.
        """
        asset = self._acquire_and_commission()
        self._post_depreciation(1, 2, 3, 4, 5, 6)
        self._transfer_to_youth(asset)
        self._dispose(asset)
        self._post_depreciation(7)

        start, end = dt.date(YEAR, 7, 1), month_end(YEAR, 7)
        statement = self.visit(self.office, "report_ie",
                               query=f"?start={start}&end={end}")
        printed = statement.context["disposal_gain_loss"]

        self.assert_agree(
            "the loss on the projector, read four ways",
            recorded_on_the_register=money(asset.disposal_gain_loss),
            the_metric=money(metrics.disposal_gain_loss(start, end)),
            income_and_expenditure_statement=money(printed),
            # an expense account carries a debit balance, so the loss reaches
            # the income result as a negative
            posted_to_the_ledger=money(-self._ledger("ASSET_DISPOSAL_LOSS", end)),
        )
        self.assertEqual(printed, Decimal("-144000"))

        # The proceeds are a capital receipt, not income: the fund is 300,000
        # better off, but the statement's income line must not show it.
        self.assert_fund_balance(self.youth_fund, PROCEEDS, end)
        self.assertEqual(
            statement.context["income"], Decimal("0"),
            "the proceeds of selling an asset were reported as income")

        # And the carrying value that the loss was struck against is the same
        # one the movement-in-fixed-assets note reads.
        self.assert_agree(
            "the projector's carrying value when it left",
            proceeds_less_the_loss=money(PROCEEDS - asset.disposal_gain_loss),
            disposed_carrying_value_metric=money(
                metrics.disposed_carrying_value(start, end)),
            cost_less_six_months=money(PROJECTOR_COST - PROJECTOR_MONTHLY * 6))
        self.assert_books_balance("after reporting the disposal")
        self.assert_register_ties_to_ledger(end, "the month of the sale")

    def test_a_donated_asset_reaches_the_ledger_the_moment_it_is_recorded(self):
        """The other way an asset arrives.

        A donation brings value in with no cash behind it, so it is the one
        acquisition that posts a journal of its own. It used to post nothing
        until somebody happened to rebuild the ledger, and because the missing
        entry was on both sides of the reconciliation it stayed missing
        quietly. Recording it must move the register and the ledger together.
        """
        self._post_depreciation(1, 2)
        february = month_end(YEAR, 2)
        before = metrics.register_vs_ledger(february)

        self.submit(self.office, "asset_create", {
            "name": "Yamaha grand piano", "category": FixedAsset.Category.MUSICAL,
            "status": FixedAsset.Status.IN_SERVICE,
            "acquired_on": dt.date(YEAR, 2, 8).isoformat(),
            "in_service_on": dt.date(YEAR, 2, 8).isoformat(),
            "cost": "650000", "salvage_value": "0",
            # a per-asset policy, so the gift does not depend on a category rule
            "method": "NONE", "rate": "0",
            "department": self.local_fund.id, "location_fk": self.sanctuary.id,
            "location": "", "tag": "MUS-001", "serial_no": "", "reference": "",
            "notes": "Given by the Otieno family.",
            "acq_source": Acquisition.Source.DONATION,
            "donor_name": "The Otieno family",
        })
        piano = FixedAsset.objects.get(name="Yamaha grand piano")
        self.assertEqual(piano.acquisition.source, Acquisition.Source.DONATION)

        after = metrics.register_vs_ledger(february)
        self.assert_agree(
            "a donated asset moves the register and the ledger by the same amount",
            register_increase=money(
                after["cost"]["register"] - before["cost"]["register"]),
            ledger_increase=money(after["cost"]["ledger"] - before["cost"]["ledger"]),
            fair_value=money("650000"))
        self.assert_register_ties_to_ledger(february, "after a donation")
        self.assert_books_balance("after recognising a donated asset")

        # It is not income — it is an increase in net assets — so the statement
        # reports it separately from the cash income total.
        start = dt.date(YEAR, 2, 1)
        statement = self.visit(self.office, "report_ie",
                               query=f"?start={start}&end={february}")
        self.assert_agree(
            "the donated piano as the statement reports it",
            statement=money(statement.context["donated_assets"]),
            the_metric=money(
                metrics.non_cash_items(start, february)["donated_assets"]),
            fair_value=money("650000"))
        self.assertEqual(statement.context["income"], Decimal("0"),
                         "a gift in kind was counted as cash income")

    # -- the pages the workflow lands on --------------------------------------

    def test_every_page_this_workflow_passes_through_actually_opens(self):
        """Where the process ends. Five times this application has shipped a
        feature whose every part worked and whose page a user could not reach;
        a sold asset that cannot be looked at afterwards has not been dealt
        with."""
        asset = self._acquire_and_commission()
        self._post_depreciation(2)
        self.visit(self.office, "asset_detail", args=[asset.pk])
        self.visit(self.office, "asset_list")
        self.visit(self.office, "asset_board")
        self.visit(self.office, "depreciation_runs")
        self.visit(self.office, "asset_preflight")

        self._issue(asset)
        self.visit(self.office, "asset_detail", args=[asset.pk])
        self._check_in(asset)
        self._transfer_to_youth(asset)
        self.visit(self.office, "asset_detail", args=[asset.pk])

        self._dispose(asset)
        self.assertTrue(asset.disposed)
        # the pages that must still render a disposed asset
        self.visit(self.office, "asset_detail", args=[asset.pk])
        self.visit(self.office, "asset_list")
        self.visit(self.office, "asset_board")
        self.visit(self.office, "depreciation_runs")

        # and the read-only role can follow the whole story without being
        # turned away at the door
        reading_room = self.acting_as(self.auditor)
        self.visit(reading_room, "asset_detail", args=[asset.pk])
        self.visit(reading_room, "asset_board")
        self.visit(reading_room, "depreciation_runs")

    def test_the_runs_page_shows_the_treasurer_the_control_it_claims_to(self):
        """The reconciliation is printed at the top of the runs page, and it
        has to be the same reconciliation the control computes — a screen that
        quotes its own numbers is worse than no screen."""
        asset = self._acquire_and_commission()
        self._post_every_run_the_page_offers()
        page = self.visit(self.office, "depreciation_runs")
        rec = page.context["rec"]

        # The three rows the panel prints have to be arithmetically each
        # other's: net book value is cost less accumulated depreciation, on
        # both sides. A panel whose own rows do not add up cannot be used to
        # judge anything.
        for side in ("register", "ledger"):
            self.assert_agree(
                f"the runs page's own three rows, {side} side",
                printed_net_book_value=money(rec["nbv"][side]),
                cost_less_accumulated=money(
                    rec["cost"][side] - rec["accdep"][side]))

        # Cost is the half of the control that does tie on an ordinary day: the
        # register and the ledger agree about what the church has bought.
        self.assert_agree(
            "what the church owns, on the register and in the ledger",
            runs_page_register=money(rec["cost"]["register"]),
            runs_page_ledger=money(rec["cost"]["ledger"]),
            land_minibus_and_projector=money(
                LAND_COST + MINIBUS_COST + PROJECTOR_COST))
        self.assertTrue(page.context["can_manage"])
        self.assertEqual(asset.status, FixedAsset.Status.IN_SERVICE)

    # -- the defects this workflow found --------------------------------------

    def test_the_runs_page_can_be_brought_to_reconciled_by_its_own_actions(self):
        """DEFECT: the register↔ledger control on the depreciation runs page
        cannot be made to read "agree" through anything the page offers, so on
        an ordinary day it tells the treasurer the register is broken and gives
        them nothing to do about it.

        `DepreciationRunsView.get` (assets/views.py:349) asks
        `metrics.register_vs_ledger(date.today())`. The two sides of that
        comparison are cut off differently:

        * the REGISTER charges a whole month from the first day of the month
          (`depreciation.months_between` counts the commissioning month in
          full), so at any date in August it already carries August;
        * the LEDGER can only carry a run dated at its own month end
          (`runs.generate_run` sets `run_date` to the last day, and
          `post_depreciation_run` dates the journal from it), and the control
          filters `entry__date__lte=as_of`.

        So the accumulated-depreciation line — and therefore net book value —
        is out by exactly one month's charge for the whole register on every
        day that is not a month end, and the current month's run cannot close
        the gap because its journal is dated after the date being asked about.

        The page also never offers that run: `suggest_year/suggest_month` are
        hardcoded to the PREVIOUS calendar month. So once a diligent treasurer
        has posted everything the page asks for, the form disappears, and the
        panel still reads "The register and ledger differ — post any
        outstanding monthly runs below to bring them into line" with no
        outstanding run below to post.

        The cost is not a cosmetic one. This is the only control in the
        application that watches the subsidiary register against the general
        ledger, and it is rendered as a red/green verdict. A verdict that is
        red on 30 days in 31 for a register that is perfectly correct is a
        verdict nobody will read, and a REAL break — an asset on the register
        the ledger has never heard of, which is precisely what step 4 of the
        main workflow above walks through — will be sitting in the same panel,
        indistinguishable.

        Reproduced below: the whole register posted right up to the last month
        the page will accept, and the panel still says no.
        """
        self._acquire_and_commission()
        self._post_every_run_the_page_offers()
        page = self.visit(self.office, "depreciation_runs")
        rec = page.context["rec"]

        # There is nothing left for the treasurer to do: the page is not asking
        # for a run. (This has to hold for the failure below to be the defect
        # described and not simply an unposted month.)
        self.assertFalse(
            page.context["suggest_pending"],
            "the page is still asking for a run, so the gap below is just that")

        # The gap is one month of depreciation for the whole register — the
        # current month, which the ledger cannot yet carry.
        self.assert_agree(
            "the gap the control reports against one month's charge",
            accumulated_depreciation_diff=money(rec["accdep"]["diff"]),
            net_book_value_diff=money(-rec["nbv"]["diff"]),
            projector_plus_minibus_for_one_month=money(
                PROJECTOR_MONTHLY + MINIBUS_MONTHLY))

        self.assertTrue(
            page.context["reconciled"],
            f"the runs page says the register does not reconcile, and offers "
            f"nothing that would make it: {rec}")

    def test_a_sold_asset_is_shown_as_disposed_on_the_board(self):
        """DEFECT: a disposal never sets the asset's status, so a sold asset
        stays in whatever column it was in and the board's "Disposed" column
        can never be reached.

        `AssetDisposeView` writes `disposed`, `disposed_on`, the proceeds, the
        method, the gain/(loss) and the fund — but not `status`. The lifecycle
        service refuses to set DISPOSED itself ("Record a disposal instead"),
        and nothing else in the application sets it either, so
        `FixedAsset.Status.DISPOSED` is unreachable through the app. The board
        (`assets/views.py:AssetBoardView`) buckets by status and renders a
        column for it, so a projector sold on 20 July is still displayed under
        "Held for disposal" — alongside the assets that are genuinely still
        waiting to be sold, and counted in that column's total.

        It is also a dead end: `TRANSITIONS[HELD_SALE]` is `[IN_SERVICE, IDLE]`
        and both are refused once `disposed` is true, while ARCHIVED is only
        reachable from DISPOSED. So a sold asset can never be archived off the
        board either.
        """
        asset = self._acquire_and_commission()
        self._post_depreciation(2)
        self.submit(self.office, "asset_transition",
                    {"status": FixedAsset.Status.HELD_SALE}, args=[asset.pk])
        self._dispose(asset)
        self.assertTrue(asset.disposed)

        board = self.visit(self.office, "asset_board")
        columns = {c["status"]: c for c in board.context["columns"]}
        sold_names = [card["asset"].name
                      for card in columns[FixedAsset.Status.DISPOSED]["cards"]]
        waiting_names = [card["asset"].name
                         for card in columns[FixedAsset.Status.HELD_SALE]["cards"]]
        self.assertIn("Sanctuary projector", sold_names,
                      "a sold asset is missing from the board's Disposed column")
        self.assertNotIn("Sanctuary projector", waiting_names,
                         "a sold asset is still shown as waiting to be sold")

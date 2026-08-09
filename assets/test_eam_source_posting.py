"""EAM — asset documents reach the general ledger when they happen.

Receipts, payments, refunds and cash transfers have always posted on save (see
ledger/signals.py). Assets did not: `post_acquisition`, `post_asset_transfer`
and `post_disposal` existed and were correct, but the only thing that ever
called them outside the tests was `posting.rebuild()` — a treasurer clicking
"Rebuild general ledger". Until someone did, an approved inter-fund transfer had
moved the asset on the register with no journal behind it, a disposal had left
the control accounts untouched, and a donated asset was on the register but
nowhere in the ledger.

Nothing warned about it either: the register↔ledger control compares the
FIXED_ASSETS / ACCUM_DEPRECIATION totals, and an inter-fund transfer is
equity-only, so the one control built to catch register/ledger drift is
structurally blind to a missing transfer posting. These tests are the alarm the
control cannot be.

The ledger only posts when it is in use (`chart_ready()`), which is why each
test builds the chart first — on a church that has never opened the ledger,
nothing posts and the first rebuild brings everything in.
"""
import datetime as dt
from decimal import Decimal
from unittest import mock

from django.contrib.auth.models import User, Group
from django.test import TestCase
from django.urls import reverse

from assets.models import Acquisition, AssetTransfer, FixedAsset
from core.roles import TREASURER
from departments.models import Department
from ledger.models import JournalEntry, JournalLine
from ledger.services import posting


def _entries(source_type, source_id=None):
    qs = JournalEntry.objects.filter(source_type=source_type)
    if source_id is not None:
        qs = qs.filter(source_id=source_id)
    return qs


def _bal(system_key, dept=None):
    from django.db.models import Sum
    qs = JournalLine.objects.filter(account__system_key=system_key)
    if dept is not None:
        qs = qs.filter(department=dept)
    agg = qs.aggregate(d=Sum("debit"), c=Sum("credit"))
    return (agg["d"] or Decimal(0)) - (agg["c"] or Decimal(0))


class SourcePostingBase(TestCase):
    def setUp(self):
        posting.ensure_chart()          # the ledger is in use on this church
        self.fund = Department.objects.create(name="General",
                                              fund_type=Department.FundType.LOCAL)
        self.other = Department.objects.create(name="Youth",
                                               fund_type=Department.FundType.LOCAL)
        self.tr = User.objects.create_user("src_tr", password="x")
        self.tr.groups.add(Group.objects.get_or_create(name=TREASURER)[0])
        self.tr2 = User.objects.create_user("src_tr2", password="x")
        self.tr2.groups.add(Group.objects.get_or_create(name=TREASURER)[0])
        self.client.force_login(self.tr)

    def _asset(self, **kw):
        defaults = dict(name="Van", category=FixedAsset.Category.VEHICLE,
                        cost=Decimal("100000"), salvage_value=Decimal(0),
                        acquired_on=dt.date(dt.date.today().year - 1, 1, 1),
                        in_service_on=dt.date(dt.date.today().year - 1, 1, 1),
                        method="NONE", rate=Decimal(0), department=self.fund,
                        status=FixedAsset.Status.IN_SERVICE)
        defaults.update(kw)
        return FixedAsset.objects.create(**defaults)

    def _request_transfer(self, asset, to_fund):
        self.client.post(reverse("asset_transfer", args=[asset.pk]),
                         {"to_fund": str(to_fund.pk)})
        return asset.transfers.first()

    def _approve(self, tr):
        self.client.force_login(self.tr2)      # never the requester
        return self.client.post(reverse("asset_transfer_decide", args=[tr.pk]),
                                {"decision": "approve"})


class TransferPostsOnApprovalTests(SourcePostingBase):
    def test_approving_a_fund_transfer_posts_its_equity_move_there_and_then(self):
        a = self._asset()
        tr = self._request_transfer(a, self.other)
        self._approve(tr)
        tr.refresh_from_db()
        self.assertEqual(tr.status, AssetTransfer.Status.APPROVED)
        self.assertEqual(_entries("asset_transfer", tr.pk).count(), 1,
                         "an approved fund transfer must post when it is approved, "
                         "not when someone next rebuilds the ledger")
        self.assertEqual(_bal("CAPITAL_FUND", self.fund), Decimal("100000"))    # value left
        self.assertEqual(_bal("CAPITAL_FUND", self.other), Decimal("-100000"))  # and arrived

    def test_a_pending_transfer_posts_nothing(self):
        a = self._asset()
        tr = self._request_transfer(a, self.other)
        self.assertEqual(tr.status, AssetTransfer.Status.PENDING)
        self.assertEqual(_entries("asset_transfer").count(), 0)

    def test_a_rejected_transfer_posts_nothing(self):
        a = self._asset()
        tr = self._request_transfer(a, self.other)
        self.client.force_login(self.tr2)
        self.client.post(reverse("asset_transfer_decide", args=[tr.pk]),
                         {"decision": "reject"})
        self.assertEqual(_entries("asset_transfer").count(), 0)

    def test_a_location_only_move_posts_nothing(self):
        from assets.models import Location
        a = self._asset()
        loc = Location.objects.create(name="Vestry")
        self.client.post(reverse("asset_transfer", args=[a.pk]),
                         {"to_location": str(loc.pk)})
        self._approve(a.transfers.first())
        self.assertEqual(_entries("asset_transfer").count(), 0)

    def test_a_later_rebuild_does_not_double_post_it(self):
        """Posting at the source and posting on rebuild must be the same act, not
        two — `post_asset_transfer` replaces its own entry, and this is the test
        that says we may rely on that."""
        a = self._asset()
        tr = self._request_transfer(a, self.other)
        self._approve(tr)
        posting.rebuild()
        self.assertEqual(_entries("asset_transfer", tr.pk).count(), 1)
        self.assertEqual(_bal("CAPITAL_FUND", self.other), Decimal("-100000"))

    def test_a_ledger_failure_leaves_the_transfer_unapproved(self):
        """The register must never say an asset moved while the ledger has no
        record of it, so if the posting cannot be written the approval itself is
        rolled back — visibly refused, rather than silently half-done."""
        a = self._asset()
        tr = self._request_transfer(a, self.other)
        with mock.patch("ledger.services.posting.post_asset_transfer",
                        side_effect=RuntimeError("ledger is down")):
            self._approve(tr)
        tr.refresh_from_db()
        a.refresh_from_db()
        self.assertEqual(tr.status, AssetTransfer.Status.PENDING,
                         "a transfer whose journal failed must not stay approved")
        self.assertEqual(a.department_id, self.fund.pk,
                         "and the asset must not have moved on the register")
        self.assertEqual(_entries("asset_transfer").count(), 0)


class DisposalPostsOnRecordingTests(SourcePostingBase):
    def test_recording_a_disposal_posts_its_journal_there_and_then(self):
        a = self._asset()
        self.client.post(reverse("asset_dispose", args=[a.pk]),
                         {"disposed_on": dt.date.today().isoformat(),
                          "proceeds": "130000", "method": "SOLD",
                          "fund": str(self.fund.pk)})
        a.refresh_from_db()
        self.assertTrue(a.disposed)
        self.assertEqual(_entries("asset_disposal", a.pk).count(), 1)
        self.assertEqual(_bal("ASSET_DISPOSAL_GAIN"), Decimal("-30000"))  # income = credit
        self.assertEqual(_bal("FIXED_ASSETS"), Decimal("-100000"),
                         "the asset's cost must leave the control account")

    def test_a_later_rebuild_does_not_double_post_the_disposal(self):
        a = self._asset()
        self.client.post(reverse("asset_dispose", args=[a.pk]),
                         {"disposed_on": dt.date.today().isoformat(),
                          "proceeds": "130000", "method": "SOLD",
                          "fund": str(self.fund.pk)})
        posting.rebuild()
        a.refresh_from_db()
        self.assertEqual(_entries("asset_disposal", a.pk).count(), 1)
        self.assertEqual(_bal("ASSET_DISPOSAL_GAIN"), Decimal("-30000"))

    def test_a_ledger_failure_leaves_the_asset_undisposed(self):
        a = self._asset()
        with mock.patch("ledger.services.posting.post_disposal",
                        side_effect=RuntimeError("ledger is down")):
            self.client.post(reverse("asset_dispose", args=[a.pk]),
                             {"disposed_on": dt.date.today().isoformat(),
                              "proceeds": "130000", "method": "SOLD",
                              "fund": str(self.fund.pk)})
        a.refresh_from_db()
        self.assertFalse(a.disposed, "a disposal whose journal failed must not stand")
        from giving.models import Transaction
        self.assertEqual(Transaction.objects.count(), 0,
                         "and the proceeds receipt must be rolled back with it")


class DonatedAcquisitionPostsOnRecordingTests(SourcePostingBase):
    """A donation is the one acquisition that posts a journal of its own (no cash
    moves, so no Expense carries it). It is created from three different places,
    so the rule lives on the model's save rather than in each of them."""

    def _donate(self, amount="250000"):
        asset = self._asset(name="Donated organ", category=FixedAsset.Category.EQUIPMENT,
                            cost=Decimal(amount), is_donated=True)
        return Acquisition.objects.create(
            asset=asset, source=Acquisition.Source.DONATION, date=asset.acquired_on,
            amount=asset.cost, fund=self.fund, donor_name="Anonymous member")

    def test_a_donated_asset_posts_its_acquisition_there_and_then(self):
        acq = self._donate()
        self.assertEqual(_entries("asset_acq", acq.pk).count(), 1,
                         "a gift in kind must reach the ledger when it is recorded")
        self.assertEqual(_bal("FIXED_ASSETS"), Decimal("250000"))
        self.assertEqual(_bal("CAPITAL_FUND"), Decimal("-250000"))   # equity = credit

    def test_a_purchase_acquisition_still_posts_nothing_of_its_own(self):
        """Its Expense already carries the cash side; a second entry would
        double-count the asset."""
        asset = self._asset(name="Bought van")
        Acquisition.objects.create(asset=asset, source=Acquisition.Source.PURCHASE,
                                   date=asset.acquired_on, amount=asset.cost,
                                   fund=self.fund)
        self.assertEqual(_entries("asset_acq").count(), 0)

    def test_a_later_rebuild_does_not_double_post_the_donation(self):
        acq = self._donate()
        posting.rebuild()
        self.assertEqual(_entries("asset_acq", acq.pk).count(), 1)
        self.assertEqual(_bal("CAPITAL_FUND"), Decimal("-250000"))

    def test_the_donation_form_posts_it_too(self):
        """The register's own "add an asset" form records the acquisition, so it
        must reach the ledger by the same route."""
        self.client.post(reverse("asset_create"), {
            "name": "Donated piano", "category": FixedAsset.Category.EQUIPMENT,
            "acquired_on": dt.date.today().isoformat(), "cost": "80000",
            "salvage_value": "0", "department": str(self.fund.pk),
            "acq_source": Acquisition.Source.DONATION, "donor_name": "A member",
            "status": FixedAsset.Status.IN_SERVICE})
        asset = FixedAsset.objects.filter(name="Donated piano").first()
        self.assertIsNotNone(asset, "the asset form should have created the asset")
        self.assertEqual(_entries("asset_acq", asset.acquisition.pk).count(), 1)


class LedgerNotInUseTests(TestCase):
    """A church that has never opened the general ledger has no chart of
    accounts. Posting must stay quiet there — exactly as ledger/signals.py does
    for receipts and payments — rather than fail the treasurer's action."""

    def test_recording_a_donation_without_a_chart_posts_nothing_and_raises_nothing(self):
        fund = Department.objects.create(name="General",
                                         fund_type=Department.FundType.LOCAL)
        asset = FixedAsset.objects.create(
            name="Donated organ", category=FixedAsset.Category.EQUIPMENT,
            cost=Decimal("250000"), salvage_value=Decimal(0),
            acquired_on=dt.date.today(), department=fund)
        Acquisition.objects.create(asset=asset, source=Acquisition.Source.DONATION,
                                   date=asset.acquired_on, amount=asset.cost, fund=fund)
        self.assertFalse(posting.chart_ready())
        self.assertEqual(JournalEntry.objects.count(), 0)

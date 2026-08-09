"""EAM — a recorded disposal is a fact, not a recalculation.

Two rules are under test here, both of them about the moment a disposal is
recorded through the disposal document flow:

* **The figures are frozen when the disposal is recorded.** The register stores
  the gain/(loss), and the Income & Expenditure statement reads that stored
  figure; the ledger's disposal journal used to RE-DERIVE the carrying value at
  posting time from whatever DepreciationRule happened to be in the database
  then. Changing a category's rate between recording a disposal and the next
  ledger rebuild — an ordinary thing for a treasurer to do — therefore made the
  statement and the ledger disagree about the same disposal, and could flip its
  sign. The register↔ledger control never caught it because it compares only the
  FIXED_ASSETS / ACCUM_DEPRECIATION totals, and the gain/loss accounts are not
  among them.
* **An asset still in someone's hands cannot be written off.** The lifecycle
  service has always refused to move an issued asset to "held for disposal", but
  the disposal flow — the only path that can actually set DISPOSED — never asked,
  so an asset could end up disposed AND checked out to someone indefinitely.
"""
import datetime as dt
from decimal import Decimal

from django.contrib.auth.models import User, Group
from django.test import TestCase
from django.urls import reverse

from assets.models import AssetAssignment, DepreciationRule, FixedAsset
from assets.services import lifecycle
from core.metrics import metrics
from core.roles import TREASURER
from departments.models import Department
from ledger.models import JournalLine
from ledger.services import posting


def _bal(system_key):
    from django.db.models import Sum
    agg = (JournalLine.objects.filter(account__system_key=system_key)
           .aggregate(d=Sum("debit"), c=Sum("credit")))
    return (agg["d"] or Decimal(0)) - (agg["c"] or Decimal(0))


class DisposalBase(TestCase):
    def setUp(self):
        self.fund = Department.objects.create(name="General",
                                              fund_type=Department.FundType.LOCAL)
        self.tr = User.objects.create_user("disp_tr", password="x")
        self.tr.groups.add(Group.objects.get_or_create(name=TREASURER)[0])
        self.client.force_login(self.tr)

    def _asset(self, **kw):
        # no per-asset method/rate: the CATEGORY rule decides, which is exactly
        # the policy a treasurer can change from /assets/depreciation-rules/
        defaults = dict(name="Sound desk", category=FixedAsset.Category.EQUIPMENT,
                        cost=Decimal("300000"), salvage_value=Decimal(0),
                        acquired_on=dt.date(2020, 1, 1), in_service_on=dt.date(2020, 1, 1),
                        department=self.fund, status=FixedAsset.Status.IN_SERVICE)
        defaults.update(kw)
        return FixedAsset.objects.create(**defaults)

    def _dispose(self, asset, proceeds, on=dt.date(2023, 8, 15)):
        return self.client.post(reverse("asset_dispose", args=[asset.pk]),
                                {"disposed_on": on.isoformat(), "proceeds": str(proceeds),
                                 "method": "SOLD", "fund": str(self.fund.pk)})


class DisposalSnapshotTests(DisposalBase):
    """44 months at 10% of 300,000 is 110,000 accumulated, so a 300,000 desk sold
    for 150,000 in August 2023 is a loss of 40,000. At 20% the same desk would
    have been a GAIN of 70,000 — which is what the ledger used to post if the
    rate was changed before the rebuild."""

    def setUp(self):
        super().setUp()
        DepreciationRule.objects.update_or_create(
            category=FixedAsset.Category.EQUIPMENT,
            defaults={"method": DepreciationRule.Method.STRAIGHT, "rate": Decimal("10")})

    def _raise_the_rate(self):
        DepreciationRule.objects.filter(category=FixedAsset.Category.EQUIPMENT).update(
            rate=Decimal("20"))

    def test_the_register_records_the_loss_as_at_the_disposal_date(self):
        a = self._asset()
        self._dispose(a, "150000")
        a.refresh_from_db()
        self.assertTrue(a.disposed)
        self.assertEqual(a.disposal_gain_loss, Decimal("-40000"))

    def test_a_later_rate_change_cannot_move_a_recorded_disposal(self):
        """The carrying value that the disposal was recorded against stays put
        even when the category's rate changes afterwards — otherwise the same
        disposal has two different values depending on when you ask."""
        a = self._asset()
        self._dispose(a, "150000")
        a.refresh_from_db()
        self._raise_the_rate()
        a = FixedAsset.objects.get(pk=a.pk)      # a fresh read, new rule in force
        self.assertEqual(a.accumulated_depreciation(a.disposed_on), Decimal("110000"))
        self.assertEqual(a.cost - a.accumulated_depreciation(a.disposed_on),
                         a.disposal_proceeds - a.disposal_gain_loss)

    def test_the_ledger_posts_the_loss_the_register_recorded_after_a_rate_change(self):
        """The bug in one line: record the disposal, change the rate, rebuild —
        and the ledger recognised a 70,000 gain for the 40,000 loss on the
        statement."""
        a = self._asset()
        self._dispose(a, "150000")
        self._raise_the_rate()
        posting.rebuild()
        self.assertEqual(_bal("ASSET_DISPOSAL_LOSS"), Decimal("40000"),
                         "the ledger must post the loss the register recorded")
        self.assertEqual(_bal("ASSET_DISPOSAL_GAIN"), Decimal("0"),
                         "a recorded loss must never come back as a gain")

    def test_the_statement_and_the_ledger_agree_on_the_same_disposal(self):
        """The two figures a reader can put side by side: the I&E line (from the
        register) and the disposal gain/loss accounts (from the ledger)."""
        a = self._asset()
        self._dispose(a, "150000")
        self._raise_the_rate()
        posting.rebuild()
        register = metrics.disposal_gain_loss(dt.date(2023, 1, 1), dt.date(2023, 12, 31))
        ledger = -_bal("ASSET_DISPOSAL_GAIN") - _bal("ASSET_DISPOSAL_LOSS")
        self.assertEqual(register, ledger,
                         f"register says {register}, ledger says {ledger}")

    def test_the_carrying_value_metric_reads_the_same_snapshot(self):
        """`disposed_carrying_value` feeds the movement in fixed assets; it too
        must read the frozen figure, not re-derive one."""
        a = self._asset()
        self._dispose(a, "150000")
        self._raise_the_rate()
        self.assertEqual(
            metrics.disposed_carrying_value(dt.date(2023, 1, 1), dt.date(2023, 12, 31)),
            Decimal("190000"))

    def test_an_untouched_rate_gives_the_same_answer_as_before(self):
        """The snapshot must not change the ordinary case — where nobody has
        touched the rule, it is the very figure the engine would compute."""
        a = self._asset()
        self._dispose(a, "150000")
        posting.rebuild()
        self.assertEqual(_bal("ASSET_DISPOSAL_LOSS"), Decimal("40000"))

    def test_a_disposal_recorded_without_a_snapshot_still_computes(self):
        """Rows written before the register carried a gain/loss (or by a fixture
        that sets `disposed` directly) have nothing to read back, so they must
        fall back to the engine rather than blow up or report zero."""
        a = self._asset()
        a.disposed = True
        a.disposed_on = dt.date(2023, 8, 15)
        a.save(update_fields=["disposed", "disposed_on"])
        self.assertEqual(a.accumulated_depreciation(a.disposed_on), Decimal("110000"))


class DisposalCustodyGuardTests(DisposalBase):
    def test_an_issued_asset_cannot_be_disposed_of(self):
        """lifecycle refuses to send an issued asset for disposal; the disposal
        document flow must refuse for the same reason, or the register ends up
        showing an asset that is both written off and still in someone's hands."""
        a = self._asset()
        asn = AssetAssignment.objects.create(asset=a, holder_name="Elder Musa",
                                             from_date=dt.date(2023, 1, 1))
        self._dispose(a, "150000")
        a.refresh_from_db()
        asn.refresh_from_db()
        self.assertFalse(a.disposed, "an asset still issued must not be disposed of")
        self.assertIsNone(asn.to_date, "and its assignment must be left open, not silently closed")
        self.assertIsNotNone(lifecycle.open_assignment(a))

    def test_checking_it_in_first_lets_the_disposal_through(self):
        a = self._asset()
        asn = AssetAssignment.objects.create(asset=a, holder_name="Elder Musa",
                                             from_date=dt.date(2023, 1, 1))
        asn.to_date = dt.date(2023, 8, 1)
        asn.save(update_fields=["to_date"])
        self._dispose(a, "150000")
        a.refresh_from_db()
        self.assertTrue(a.disposed)

    def test_the_refusal_is_the_lifecycle_service_speaking(self):
        """One rule, one place: the disposal flow asks the lifecycle service
        rather than carrying its own copy of the same check."""
        a = self._asset()
        AssetAssignment.objects.create(asset=a, holder_name="Elder Musa",
                                       from_date=dt.date(2023, 1, 1))
        with self.assertRaises(lifecycle.TransitionError):
            lifecycle.check_not_issued(a, "recording a disposal")

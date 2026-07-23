"""EAM Phase 2c — the asset lifecycle: guarded transitions, custody, transfers,
the profile and the board.

The rules that matter are the guards: a disposal can never be faked by setting a
status, an asset in someone's hands cannot be sent for disposal, a fund transfer
needs a second person, and moving an asset between funds reallocates equity
without disturbing the register/ledger reconciliation.
"""
import calendar
import datetime as dt
from decimal import Decimal

from django.contrib.auth.models import User, Group
from django.test import TestCase
from django.urls import reverse

from assets.models import (FixedAsset, AssetAssignment, AssetTransfer, AssetEvent,
                           DepreciationRun, Location)
from assets.services import lifecycle, runs
from core.metrics import metrics
from core.roles import TREASURER
from departments.models import Department
from ledger.models import JournalLine
from ledger.services import posting

S = FixedAsset.Status


def _bal(system_key, dept=None):
    from django.db.models import Sum
    qs = JournalLine.objects.filter(account__system_key=system_key)
    if dept is not None:
        qs = qs.filter(department=dept)
    agg = qs.aggregate(d=Sum("debit"), c=Sum("credit"))
    return (agg["d"] or Decimal(0)) - (agg["c"] or Decimal(0))


class LifecycleBase(TestCase):
    def setUp(self):
        self.fund = Department.objects.create(name="General", fund_type=Department.FundType.LOCAL)
        self.other = Department.objects.create(name="Youth", fund_type=Department.FundType.LOCAL)
        self.tr = User.objects.create_user("lc_tr", password="x")
        self.tr.groups.add(Group.objects.get_or_create(name=TREASURER)[0])
        self.tr2 = User.objects.create_user("lc_tr2", password="x")
        self.tr2.groups.add(Group.objects.get_or_create(name=TREASURER)[0])
        self.client.force_login(self.tr)

    def _asset(self, **kw):
        defaults = dict(
            name="Van", category="VEHICLE", cost=Decimal("100000"), salvage_value=Decimal(0),
            acquired_on=dt.date(dt.date.today().year - 1, 1, 1),
            in_service_on=dt.date(dt.date.today().year - 1, 1, 1),
            method="NONE", rate=Decimal(0), department=self.fund, status=S.IN_SERVICE)
        defaults.update(kw)
        return FixedAsset.objects.create(**defaults)


class TransitionGuardTests(LifecycleBase):
    def test_a_normal_move_is_allowed_and_logged(self):
        a = self._asset()
        lifecycle.transition(a, S.MAINTENANCE, user=self.tr, note="engine")
        a.refresh_from_db()
        self.assertEqual(a.status, S.MAINTENANCE)
        self.assertTrue(a.events.filter(kind=AssetEvent.Kind.STATUS).exists())

    def test_a_nonsense_move_is_refused(self):
        a = self._asset(status=S.PLANNED)
        with self.assertRaises(lifecycle.TransitionError):
            lifecycle.transition(a, S.IMPAIRED, user=self.tr)

    def test_disposal_cannot_be_faked_by_setting_the_status(self):
        """A disposal must record proceeds, method and fund, and post its journal."""
        a = self._asset(status=S.HELD_SALE)
        with self.assertRaises(lifecycle.TransitionError):
            lifecycle.transition(a, S.DISPOSED, user=self.tr)
        a.refresh_from_db()
        self.assertEqual(a.status, S.HELD_SALE)
        self.assertFalse(a.disposed)

    def test_an_issued_asset_cannot_be_held_for_disposal(self):
        a = self._asset()
        AssetAssignment.objects.create(asset=a, holder_name="Elder Musa",
                                       from_date=dt.date.today())
        with self.assertRaises(lifecycle.TransitionError):
            lifecycle.transition(a, S.HELD_SALE, user=self.tr)
        self.assertNotIn(S.HELD_SALE, lifecycle.allowed_transitions(a))

    def test_checking_in_releases_the_asset_for_disposal(self):
        a = self._asset()
        asn = AssetAssignment.objects.create(asset=a, holder_name="Elder Musa",
                                             from_date=dt.date.today())
        asn.to_date = dt.date.today()
        asn.save()
        self.assertIn(S.HELD_SALE, lifecycle.allowed_transitions(a))

    def test_a_disposed_asset_can_only_be_archived(self):
        a = self._asset(status=S.DISPOSED, disposed=True, disposed_on=dt.date.today())
        self.assertEqual(lifecycle.allowed_transitions(a), [S.ARCHIVED])
        with self.assertRaises(lifecycle.TransitionError):
            lifecycle.transition(a, S.IN_SERVICE, user=self.tr)

    def test_commissioning_sets_the_in_service_date(self):
        a = self._asset(status=S.IN_CWIP, in_service_on=None)
        lifecycle.transition(a, S.IN_SERVICE, user=self.tr, on=dt.date(2026, 5, 1))
        a.refresh_from_db()
        self.assertEqual(a.in_service_on, dt.date(2026, 5, 1))

    def test_the_view_reports_a_refusal_without_changing_anything(self):
        a = self._asset(status=S.HELD_SALE)
        self.client.post(reverse("asset_transition", args=[a.pk]), {"status": S.DISPOSED})
        a.refresh_from_db()
        self.assertEqual(a.status, S.HELD_SALE)


class CustodyTests(LifecycleBase):
    def test_issue_and_check_in_round_trip(self):
        a = self._asset()
        self.client.post(reverse("asset_assign", args=[a.pk]),
                         {"holder_name": "Elder Musa", "condition_out": "good"})
        a.refresh_from_db()
        self.assertIsNotNone(lifecycle.open_assignment(a))
        self.client.post(reverse("asset_checkin", args=[a.pk]), {"condition_in": "good"})
        a.refresh_from_db()
        self.assertIsNone(lifecycle.open_assignment(a))
        self.assertTrue(a.events.filter(kind=AssetEvent.Kind.RETURNED).exists())

    def test_an_asset_cannot_be_issued_twice(self):
        a = self._asset()
        self.client.post(reverse("asset_assign", args=[a.pk]), {"holder_name": "Elder Musa"})
        self.client.post(reverse("asset_assign", args=[a.pk]), {"holder_name": "Sister Ann"})
        self.assertEqual(a.assignments.filter(to_date__isnull=True).count(), 1)

    def test_issuing_needs_a_holder(self):
        a = self._asset()
        self.client.post(reverse("asset_assign", args=[a.pk]), {})
        self.assertEqual(a.assignments.count(), 0)


class TransferTests(LifecycleBase):
    def _request_transfer(self, a, to_fund=None, to_location=None):
        data = {}
        if to_fund:
            data["to_fund"] = str(to_fund.pk)
        if to_location:
            data["to_location"] = str(to_location.pk)
        self.client.post(reverse("asset_transfer", args=[a.pk]), data)
        return a.transfers.first()

    def test_a_transfer_starts_pending_and_changes_nothing_yet(self):
        a = self._asset()
        tr = self._request_transfer(a, to_fund=self.other)
        a.refresh_from_db()
        self.assertEqual(tr.status, AssetTransfer.Status.PENDING)
        self.assertEqual(a.department_id, self.fund.pk, "nothing moves before approval")

    def test_the_requester_cannot_approve_their_own_transfer(self):
        a = self._asset()
        tr = self._request_transfer(a, to_fund=self.other)
        self.client.post(reverse("asset_transfer_decide", args=[tr.pk]), {"decision": "approve"})
        tr.refresh_from_db()
        a.refresh_from_db()
        self.assertEqual(tr.status, AssetTransfer.Status.PENDING)
        self.assertEqual(a.department_id, self.fund.pk)

    def test_a_second_treasurer_can_approve_and_the_asset_moves(self):
        a = self._asset()
        tr = self._request_transfer(a, to_fund=self.other)
        self.client.force_login(self.tr2)
        self.client.post(reverse("asset_transfer_decide", args=[tr.pk]), {"decision": "approve"})
        tr.refresh_from_db()
        a.refresh_from_db()
        self.assertEqual(tr.status, AssetTransfer.Status.APPROVED)
        self.assertEqual(a.department_id, self.other.pk)
        self.assertTrue(a.events.filter(kind=AssetEvent.Kind.TRANSFERRED).exists())

    def test_rejecting_leaves_the_asset_where_it_was(self):
        a = self._asset()
        tr = self._request_transfer(a, to_fund=self.other)
        self.client.force_login(self.tr2)
        self.client.post(reverse("asset_transfer_decide", args=[tr.pk]), {"decision": "reject"})
        a.refresh_from_db()
        self.assertEqual(a.department_id, self.fund.pk)

    def test_a_fund_transfer_reallocates_equity_without_breaking_reconciliation(self):
        a = self._asset()
        tr = AssetTransfer.objects.create(
            asset=a, date=dt.date(dt.date.today().year, 4, 1), from_fund=self.fund,
            to_fund=self.other, status=AssetTransfer.Status.APPROVED)
        today = dt.date.today()
        DepreciationRun.objects.all().delete()
        posting.rebuild()
        for m in range(1, today.month + 1):
            runs.post_run(runs.generate_run(today.year, m))
        end = dt.date(today.year, today.month, calendar.monthrange(today.year, today.month)[1])
        rec = metrics.register_vs_ledger(end)
        self.assertTrue(all(v["diff"] == Decimal("0") for v in rec.values()),
                        f"an inter-fund transfer must not disturb the control accounts: {rec}")
        # value left the old fund and arrived in the new one
        self.assertEqual(_bal("CAPITAL_FUND", self.fund), Decimal("100000"))    # debit = reduction
        self.assertEqual(_bal("CAPITAL_FUND", self.other), Decimal("-100000"))  # credit = increase
        self.assertIsNotNone(tr)

    def test_a_transfer_into_a_fund_from_none_still_moves_the_value(self):
        """An asset with no owning fund carries its value in the general capital
        fund; giving it a fund is a real reallocation, not a no-op."""
        a = self._asset(department=None)
        AssetTransfer.objects.create(
            asset=a, date=dt.date(dt.date.today().year, 4, 1), from_fund=None,
            to_fund=self.other, status=AssetTransfer.Status.APPROVED)
        posting.rebuild()
        self.assertEqual(_bal("CAPITAL_FUND", self.other), Decimal("-100000"))

    def test_a_location_only_move_posts_nothing(self):
        a = self._asset()
        loc = Location.objects.create(name="Vestry")
        tr = AssetTransfer.objects.create(
            asset=a, date=dt.date.today(), from_fund=self.fund, to_fund=self.fund,
            to_location=loc, status=AssetTransfer.Status.APPROVED)
        posting.rebuild()
        self.assertEqual(JournalLine.objects.filter(
            entry__source_type="asset_transfer", entry__source_id=tr.pk).count(), 0)


class ProfileAndBoardTests(LifecycleBase):
    def test_the_profile_shows_the_lifecycle_sections(self):
        a = self._asset()
        lifecycle.transition(a, S.MAINTENANCE, user=self.tr)
        r = self.client.get(reverse("asset_detail", args=[a.pk]))
        self.assertEqual(r.status_code, 200)
        self.assertContains(r, "Custody")
        self.assertContains(r, "Timeline")
        self.assertContains(r, "Under maintenance")

    def test_the_board_groups_assets_by_stage(self):
        self._asset(name="In use van")
        self._asset(name="Store chairs", status=S.IDLE)
        r = self.client.get(reverse("asset_board"))
        self.assertEqual(r.status_code, 200)
        cols = {c["status"]: c for c in r.context["columns"]}
        self.assertEqual(cols[S.IN_SERVICE]["count"], 1)
        self.assertEqual(cols[S.IDLE]["count"], 1)
        self.assertContains(r, "Store chairs")

    def test_an_auditor_sees_the_board_but_gets_no_move_buttons(self):
        from core.roles import AUDITOR
        aud = User.objects.create_user("lc_aud", password="x")
        aud.groups.add(Group.objects.get_or_create(name=AUDITOR)[0])
        self._asset()
        self.client.force_login(aud)
        r = self.client.get(reverse("asset_board"))
        self.assertEqual(r.status_code, 200)
        self.assertNotContains(r, 'name="status"')

    def test_an_auditor_cannot_move_an_asset(self):
        from core.roles import AUDITOR
        aud = User.objects.create_user("lc_aud2", password="x")
        aud.groups.add(Group.objects.get_or_create(name=AUDITOR)[0])
        a = self._asset()
        self.client.force_login(aud)
        self.client.post(reverse("asset_transition", args=[a.pk]), {"status": S.IDLE})
        a.refresh_from_db()
        self.assertEqual(a.status, S.IN_SERVICE)

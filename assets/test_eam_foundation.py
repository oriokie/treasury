"""EAM Phase 0 — foundation contracts.

The flat fixed-asset register gained the EAM data foundation: configurable
AssetClass, hierarchical Location, an owning Organization (multi-church scaffold),
a lifecycle status, tags, custody and commissioning date — all non-breaking, and
the existing NBV figures now flow through the Financial Metrics Registry.

These tests pin: the seeding/backfill is complete and consistent, the Asset
alias works, and the registered asset metrics reconcile (cost − accumulated
depreciation == net book value) and match the register's own nbv_total.
"""
import datetime as dt
from decimal import Decimal

from django.test import TestCase

from assets.models import Asset, AssetClass, FixedAsset, Location, nbv_total
from core.metrics import metrics
from core.models import Organization


class FoundationSeedTests(TestCase):
    def test_asset_class_seeded_for_every_category(self):
        for code, _ in FixedAsset.Category.choices:
            self.assertTrue(AssetClass.objects.filter(code=code).exists(),
                            f"no AssetClass seeded for category {code}")

    def test_default_organization_exists_and_is_singular(self):
        self.assertEqual(Organization.objects.filter(is_default=True).count(), 1)
        self.assertIsNotNone(Organization.get_default())

    def test_asset_is_alias_of_fixedasset(self):
        self.assertIs(Asset, FixedAsset)

    def test_new_asset_defaults_are_sane(self):
        ac = AssetClass.objects.get(code="EQUIPMENT")
        a = FixedAsset.objects.create(
            name="Test PA system", category="EQUIPMENT", asset_class=ac,
            acquired_on=dt.date(2024, 1, 1), cost=Decimal("50000"))
        self.assertEqual(a.status, FixedAsset.Status.IN_SERVICE)
        self.assertFalse(a.is_heritage)

    def test_location_hierarchy_path(self):
        campus = Location.objects.create(name="Main Campus")
        room = Location.objects.create(name="Sanctuary", parent=campus)
        self.assertEqual(room.full_path(), "Main Campus › Sanctuary")


class AssetMetricsTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.ac = AssetClass.objects.get(code="EQUIPMENT")
        cls.today = dt.date.today()
        FixedAsset.objects.create(
            name="Mixer", category="EQUIPMENT", asset_class=cls.ac,
            acquired_on=dt.date(2022, 6, 1), cost=Decimal("120000"),
            salvage_value=Decimal("0"), method="STRAIGHT", rate=Decimal("10"))

    def test_registry_exposes_asset_metrics(self):
        for key in ("net_book_value", "fixed_assets_cost",
                    "accumulated_depreciation", "depreciation_expense",
                    "assets_by_class"):
            self.assertTrue(metrics.has(key), f"metric {key} not registered")

    def test_nbv_metric_matches_register(self):
        self.assertEqual(metrics.net_book_value(self.today), nbv_total(self.today))

    def test_cost_less_accdep_equals_nbv(self):
        cost = metrics.fixed_assets_cost(self.today)
        accdep = metrics.accumulated_depreciation(self.today)
        nbv = metrics.net_book_value(self.today)
        self.assertEqual((cost - accdep).quantize(Decimal("0.01")),
                         nbv.quantize(Decimal("0.01")))

    def test_disposed_assets_excluded_from_cost(self):
        before = metrics.fixed_assets_cost(self.today)
        a = FixedAsset.objects.create(
            name="Old bench", category="FURNITURE",
            asset_class=AssetClass.objects.get(code="FURNITURE"),
            acquired_on=dt.date(2020, 1, 1), cost=Decimal("9000"),
            disposed=True, disposed_on=dt.date(2023, 1, 1), status="DISPOSED")
        self.assertEqual(metrics.fixed_assets_cost(self.today), before,
                         "a disposed asset must not count toward cost")

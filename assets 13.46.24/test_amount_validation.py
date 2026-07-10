"""Same amount-positivity gap found and fixed in cashbook, closed here too:
FixedAsset.cost must be positive; salvage_value must be >= 0 (zero is a
legitimate salvage value, negative is not)."""
import datetime as dt
from decimal import Decimal
from django.core.exceptions import ValidationError
from django.test import TestCase
from assets.models import FixedAsset


class FixedAssetAmountValidationTests(TestCase):
    def test_negative_cost_rejected(self):
        with self.assertRaises(ValidationError):
            FixedAsset(name="Bad asset", acquired_on=dt.date(2024, 1, 1),
                cost=Decimal("-1000")).full_clean()

    def test_negative_salvage_value_rejected(self):
        with self.assertRaises(ValidationError):
            FixedAsset(name="Bad asset 2", acquired_on=dt.date(2024, 1, 1),
                cost=Decimal("1000"), salvage_value=Decimal("-1")).full_clean()

    def test_zero_salvage_value_still_valid(self):
        FixedAsset(name="No salvage", acquired_on=dt.date(2024, 1, 1),
            cost=Decimal("1000"), salvage_value=Decimal("0")).full_clean()

    def test_positive_cost_accepted(self):
        FixedAsset(name="Good asset", acquired_on=dt.date(2024, 1, 1),
            cost=Decimal("1000")).full_clean()

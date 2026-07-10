"""Same amount-positivity gap found and fixed in cashbook, closed here too:
EnvelopeLine.amount must be positive."""
import datetime as dt
from decimal import Decimal
from django.core.exceptions import ValidationError
from django.test import TestCase
from django.contrib.auth.models import User
from departments.models import Department
from envelopes.models import Envelope, EnvelopeLine


class EnvelopeLineAmountValidationTests(TestCase):
    def setUp(self):
        self.tr = User.objects.create_user("tr_env_amt", password="x")
        self.d = Department.objects.create(name="EnvAmtFund", fund_type="LOCAL",
            category="OFFERING")
        self.env = Envelope.objects.create(date=dt.date(2026, 6, 6),
            receipt_no="ENV-AMT-1", recorded_by=self.tr)

    def test_negative_amount_rejected(self):
        with self.assertRaises(ValidationError):
            EnvelopeLine(envelope=self.env, department=self.d,
                amount=Decimal("-500")).full_clean()

    def test_positive_amount_accepted(self):
        EnvelopeLine(envelope=self.env, department=self.d,
            amount=Decimal("500")).full_clean()

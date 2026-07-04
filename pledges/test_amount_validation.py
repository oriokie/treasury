"""Same amount-positivity gap found and fixed in cashbook, closed here too:
PledgePayment and PledgeMatchSuggestion amounts must be positive."""
import datetime as dt
from decimal import Decimal
from django.core.exceptions import ValidationError
from django.test import TestCase
from django.contrib.auth.models import User
from departments.models import Department
from members.models import Member
from giving.models import Transaction
from pledges.models import PledgeCampaign, Pledge, PledgePayment, PledgeMatchSuggestion


class PledgeAmountValidationTests(TestCase):
    def setUp(self):
        self.tr = User.objects.create_user("tr_pl_amt", password="x")
        self.d = Department.objects.create(name="PlAmtFund", fund_type="LOCAL",
            category="DEVELOPMENT")
        self.member = Member.objects.create(name="Amt Giver", phone="254700999888")
        self.camp = PledgeCampaign.objects.create(name="AmtDrive", target_department=self.d)
        self.pledge = Pledge.objects.create(campaign=self.camp, member=self.member,
            amount=Decimal("10000"), status="ACTIVE", start_date=dt.date(2026, 1, 1))
        self.txn = Transaction.objects.create(date=dt.date(2026, 6, 1), amount=Decimal("100"),
            direction="CREDIT", confirmed=True, channel="CASH",
            allocation_status="MANUAL", department=self.d)

    def test_pledge_payment_rejects_negative(self):
        with self.assertRaises(ValidationError):
            PledgePayment(pledge=self.pledge, transaction=self.txn,
                amount=Decimal("-100"), matched_by=self.tr).full_clean()

    def test_pledge_match_suggestion_rejects_negative(self):
        with self.assertRaises(ValidationError):
            PledgeMatchSuggestion(pledge=self.pledge, transaction=self.txn,
                amount=Decimal("-50")).full_clean()

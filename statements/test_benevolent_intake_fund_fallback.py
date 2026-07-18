"""A bank gift referencing a benevolent scheme by an ORDINARY fund-allocation
reference (giving.AllocationRule — "put this in the MSAMARIA fund") never
reached the benevolent intake queue, even after the fund allocation itself
worked correctly.

Root cause: `benevolent.services.allocation.detect_scheme` has two ways to
recognise a scheme — an active `ContributionRule` pattern match, or (its own
documented fallback) "the money landed on a fund only one scheme uses". The
statement importer called it exactly once, BEFORE the fund was known
(fund=None always), so the second fallback was structurally unreachable: a
church that had configured a giving-level fund rule but never a
benevolent-specific ContributionRule got the fund right and the intake queue
silently empty.

Fixed: the importer retries detect_scheme with the now-resolved fund once
ordinary allocation has determined it, so the sole-owner fallback gets a real
chance.
"""
import datetime as dt
from decimal import Decimal

from django.contrib.auth.models import Group, User
from django.test import TestCase

from core.roles import TREASURER
from departments.models import Department
from giving.models import AllocationRule, Transaction
from statements.models import BankAccount, StatementImport
from statements.services.importer import run_import

from benevolent.models import (BenevolentScheme, ContributionIntake,
                               SchemePolicy)
from benevolent.services import schemes as scheme_svc

TODAY = dt.date.today()


class BenevolentIntakeFundOnlyRuleFixture(TestCase):
    def setUp(self):
        self.treasurer = User.objects.create_user("t_intakefix", password="x")
        self.treasurer.groups.add(Group.objects.get_or_create(name=TREASURER)[0])
        self.account = BankAccount.objects.create(
            name="Main", is_default=True, active=True)
        self.fund = Department.objects.create(
            name="MSAMARIA Fund", slug="msamaria-fund",
            fund_type=Department.FundType.LOCAL)
        self.scheme = BenevolentScheme.objects.create(
            name="MSAMARIA", code="MSA", fund=self.fund, created_by=self.treasurer)
        policy = SchemePolicy.objects.create(
            scheme=self.scheme, effective_from=TODAY - dt.timedelta(days=100),
            membership_required=True, waiting_period_days=0,
            contribution_mode=SchemePolicy.ContributionMode.FIXED_PERIODIC,
            contribution_amount=Decimal("200"),
            benefit_mode=SchemePolicy.BenefitMode.FIXED, benefit_amount=Decimal("5000"),
            arrears_treatment=SchemePolicy.ArrearsTreatment.IGNORE,
            created_by=self.treasurer)
        scheme_svc.publish_policy(policy, user=self.treasurer)
        scheme_svc.activate_scheme(self.scheme, user=self.treasurer)
        # the church configured an ORDINARY fund rule — not a benevolent
        # ContributionRule — exactly the setup that silently dropped intake
        AllocationRule.objects.create(
            reference="msamaria", department=self.fund,
            source=AllocationRule.Source.SEED)

    def _import_csv(self, narration, amount="500"):
        content = (
            "Date,Narration,Credit,Debit,Balance\n"
            f"2026-07-16,{narration},{amount},,1000\n"
        ).encode()
        imp = StatementImport.objects.create(
            uploaded_by=self.treasurer, filename="s.csv", bank_account=self.account)
        run_import(imp, content, "s.csv")
        imp.refresh_from_db()
        return imp


class FundOnlyRuleStillReachesIntakeTests(BenevolentIntakeFundOnlyRuleFixture):
    def test_transaction_lands_in_the_right_fund(self):
        self._import_csv("MSAMARIA~441211#msamaria~254711222333~MPESAC2B~JANE DOE")
        txn = Transaction.objects.filter(department=self.fund).first()
        self.assertIsNotNone(txn)
        self.assertEqual(txn.amount, Decimal("500"))

    def test_a_contribution_intake_record_is_created(self):
        """The actual bug: before the fix, the transaction landed in the fund
        correctly but NO ContributionIntake was ever created — the money was
        invisible to the benevolent module entirely, not even queued."""
        self._import_csv("MSAMARIA~441211#msamaria~254711222333~MPESAC2B~JANE DOE")
        txn = Transaction.objects.filter(department=self.fund).first()
        intake = ContributionIntake.objects.filter(transaction=txn).first()
        self.assertIsNotNone(
            intake, "no ContributionIntake was created — the gift never reached "
                   "the benevolent intake queue despite landing in the right fund")
        self.assertEqual(intake.scheme_id, self.scheme.pk)

    def test_second_narration_also_reaches_intake(self):
        """Edwin's own report used two different narrations naming the same
        scheme — both must work, not just a lucky one."""
        self._import_csv(
            "MSAMC-2026-0004~441211#msamaria~254722333444~MPESAC2B~JOHN DOE",
            amount="300")
        txn = Transaction.objects.filter(department=self.fund).first()
        self.assertIsNotNone(txn)
        intake = ContributionIntake.objects.filter(transaction=txn).first()
        self.assertIsNotNone(intake)
        self.assertEqual(intake.scheme_id, self.scheme.pk)

    def test_does_not_regress_the_contribution_rule_path(self):
        """The pre-existing, already-working path — a real benevolent
        ContributionRule — must still take priority and still work."""
        from benevolent.models import ContributionRule
        ContributionRule.objects.create(
            pattern="msamaria", match_type=ContributionRule.MatchType.CONTAINS,
            scheme=self.scheme, priority=10)
        self._import_csv("MSAMARIA~441211#msamaria~254733444555~MPESAC2B~MARY DOE")
        txn = Transaction.objects.filter(department=self.fund).first()
        intake = ContributionIntake.objects.filter(transaction=txn).first()
        self.assertIsNotNone(intake)
        self.assertEqual(intake.scheme_id, self.scheme.pk)

    def test_fund_shared_by_two_schemes_is_not_falsely_claimed(self):
        """The sole-owner fallback must stay conservative: if TWO schemes
        share a fund, neither should be silently guessed."""
        scheme2 = BenevolentScheme.objects.create(
            name="MSAMARIA TWO", code="MSA2", fund=self.fund,
            created_by=self.treasurer)
        policy2 = SchemePolicy.objects.create(
            scheme=scheme2, effective_from=TODAY - dt.timedelta(days=100),
            membership_required=True, waiting_period_days=0,
            contribution_mode=SchemePolicy.ContributionMode.FIXED_PERIODIC,
            contribution_amount=Decimal("200"),
            benefit_mode=SchemePolicy.BenefitMode.FIXED, benefit_amount=Decimal("5000"),
            arrears_treatment=SchemePolicy.ArrearsTreatment.IGNORE,
            created_by=self.treasurer)
        scheme_svc.publish_policy(policy2, user=self.treasurer)
        scheme_svc.activate_scheme(scheme2, user=self.treasurer)

        self._import_csv("MSAMARIA~441211#msamaria~254744555666~MPESAC2B~AMBIGUOUS")
        txn = Transaction.objects.filter(department=self.fund).order_by("-id").first()
        self.assertIsNotNone(txn)
        # money is still safely in the fund either way
        intake = ContributionIntake.objects.filter(transaction=txn).first()
        self.assertIsNone(intake, "an ambiguous fund (two schemes) must not be "
                                  "silently guessed as either one")

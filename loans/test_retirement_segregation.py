"""Loan retirement — segregation of duties.

Conversion and write-off are the only loan actions that recognise income
without anyone paying the money back, and until this was fixed they were
gated by a role check alone: any treasurer could open a loan, receipt cash
against it and then, on their own, declare the balance a donation. Every
other money decision in the app already consults
SiteConfig.require_different_approver (expense approval, envelope batches,
benevolent cases); these tests pin the loan module to that same switch —
including the two things a copied rule usually gets wrong: staying silent
when the flag is off, and never firing on loans the bank importer opened
with no recorded creator.
"""
import datetime as dt
from decimal import Decimal

from django.contrib.auth.models import Group, User
from django.core.exceptions import ValidationError
from django.test import TestCase
from django.urls import reverse

from core.models import SiteConfig
from core.roles import TREASURER
from departments.models import Department
from giving.models import Transaction
from ledger.services import posting
from loans.models import Lender, Loan, LoanTransaction
from loans.services import loans as svc


def _user(name, role=TREASURER):
    u = User.objects.create_user(name, password="x")
    u.groups.add(Group.objects.get_or_create(name=role)[0])
    return u


def _require_different(on):
    cfg = SiteConfig.get()
    cfg.require_different_approver = on
    cfg.save()


class LoanRetirementApproverTests(TestCase):
    def setUp(self):
        posting.ensure_chart()
        self.author = _user("rs_tr1")           # records the loan
        self.other = _user("rs_tr2")            # the second pair of eyes
        self.fund = Department.objects.create(name="Development", fund_type="LOCAL")
        self.lender = Lender.objects.create(name="ACME SACCO", phone="0722000111")
        self.loan = Loan.objects.create(lender=self.lender, fund=self.fund,
                                        loan_date=dt.date(2026, 1, 5),
                                        created_by=self.author)
        svc.record_receipt(self.loan, date=dt.date(2026, 1, 5),
                           amount=Decimal("100000"), user=self.author)

    def test_with_the_flag_off_the_recorder_may_still_convert(self):
        """The switch is configuration, and it is off by default (plenty of
        installs have one active treasurer). Behaviour must be exactly what it
        always was until an administrator turns it on."""
        svc.convert_to_donation(self.loan, date=dt.date(2026, 2, 1),
                                user=self.author)
        self.assertEqual(self.loan.outstanding_principal, Decimal(0))
        self.assertEqual(self.loan.status, Loan.Status.CONVERTED)

    def test_the_treasurer_who_recorded_the_loan_cannot_convert_it_alone(self):
        """The abuse this exists to stop: record the loan, receipt the cash,
        then declare it a gift — all by one person, no second approval."""
        _require_different(True)
        with self.assertRaises(ValidationError) as ctx:
            svc.convert_to_donation(self.loan, date=dt.date(2026, 2, 1),
                                    user=self.author)
        self.assertIn("different treasurer", "; ".join(ctx.exception.messages))
        # and nothing was half-created: no income leg, no settlement expense
        self.assertEqual(self.loan.outstanding_principal, Decimal("100000"))
        self.assertFalse(LoanTransaction.objects.filter(
            loan=self.loan, kind=LoanTransaction.Kind.CONVERSION).exists())
        self.assertFalse(Transaction.objects.filter(
            reference__startswith="LOAN CONVERTED").exists())

    def test_write_off_is_held_to_the_same_rule(self):
        """Conversion and write-off share one implementation precisely so the
        control cannot be enforced on one and forgotten on the other."""
        _require_different(True)
        with self.assertRaises(ValidationError):
            svc.write_off(self.loan, date=dt.date(2026, 2, 1), user=self.author)
        self.assertEqual(self.loan.outstanding_principal, Decimal("100000"))

    def test_a_different_treasurer_may_retire_the_loan(self):
        """The rule asks for a second person, not for a veto — the point is
        lost if the loan can never be retired at all."""
        _require_different(True)
        svc.convert_to_donation(self.loan, date=dt.date(2026, 2, 1),
                                user=self.other)
        self.assertEqual(self.loan.outstanding_principal, Decimal(0))

    def test_a_loan_with_no_recorded_creator_is_never_blocked(self):
        """Loans opened straight from a bank narration have created_by=None.
        There is no one to be different from, so the check must stand aside —
        the same way the expense check never fires on a document with no
        recorded_by — otherwise auto-created loans could never be retired."""
        _require_different(True)
        auto = Loan.objects.create(lender=self.lender, fund=self.fund,
                                   loan_date=dt.date(2026, 1, 5))
        self.assertIsNone(auto.created_by_id)
        svc.record_receipt(auto, date=dt.date(2026, 1, 5),
                           amount=Decimal("5000"), user=self.author)
        svc.write_off(auto, date=dt.date(2026, 2, 1), user=self.author)
        self.assertEqual(auto.outstanding_principal, Decimal(0))

    def test_the_refusal_reaches_the_user_through_the_convert_page(self):
        """The guard lives in the service so it holds however the retirement
        is reached, but it still has to read as an ordinary form error rather
        than a crash when it is reached the usual way."""
        _require_different(True)
        self.client.force_login(self.author)
        r = self.client.post(reverse("loan_convert", args=[self.loan.pk]),
                             {"date": "2026-02-01", "amount": "100000"})
        self.assertEqual(r.status_code, 200)          # re-rendered, not redirected
        self.assertContains(r, "different treasurer")
        self.assertEqual(self.loan.outstanding_principal, Decimal("100000"))
        # the same post by the other treasurer goes through
        self.client.force_login(self.other)
        r = self.client.post(reverse("loan_convert", args=[self.loan.pk]),
                             {"date": "2026-02-01", "amount": "100000"})
        self.assertEqual(r.status_code, 302)
        self.assertEqual(self.loan.outstanding_principal, Decimal(0))

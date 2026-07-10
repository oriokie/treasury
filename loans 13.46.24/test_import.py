"""Loan module — bank statement / live-feed intake: narration detection order,
fully-automatic receipt intake (pattern names the fund), the never-guess path
(pattern without a fund -> review queue, no Member created), dedup on
re-import, and importer/webhook alignment."""
import datetime as dt
from decimal import Decimal

from django.contrib.auth.models import User
from django.test import TestCase

from departments.models import Department
from giving.models import Transaction
from loans.models import Lender, Loan, LoanNarrationPattern, LoanTransaction
from loans.services.narration import clear_pattern_cache, detect_loan
from statements.models import StatementImport
from statements.services.importer import run_import


def _csv(rows):
    body = "".join(f"{when},{narr},{amount}\n" for when, narr, amount in rows)
    return ("Completion Time,Details,Paid In\n" + body).encode("utf-8")


def _import(csv, fname="loan.csv"):
    u = (User.objects.filter(is_superuser=True).first()
         or User.objects.create_superuser("imp", password="x"))
    imp = StatementImport.objects.create(uploaded_by=u, filename=fname)
    run_import(imp, csv, fname)
    imp.refresh_from_db()
    return imp


class NarrationDetectionTests(TestCase):
    def setUp(self):
        clear_pattern_cache()

    def test_seeded_aliases_installed_and_detected(self):
        for ref, kind in [("LOAN", "RECEIPT"), ("Dev Loan", "RECEIPT"),
                          ("loan development", "RECEIPT"),
                          ("Loan Repayment", "REPAYMENT"),
                          ("REPAY LOAN", "REPAYMENT"),
                          ("loan int", "INTEREST"),
                          ("Convert Loan", "CONVERSION"),
                          ("loan forgive", "CONVERSION")]:
            hit = detect_loan(ref)
            self.assertIsNotNone(hit, ref)
            self.assertEqual(hit.kind, kind, ref)

    def test_specific_intent_beats_plain_loan(self):
        # 'loanrepayment' contains 'loan' — the receipt alias must never win
        self.assertEqual(detect_loan("loanrepayment").kind, "REPAYMENT")
        self.assertEqual(detect_loan("loan interest").kind, "INTEREST")

    def test_non_loan_references_pass_through(self):
        self.assertIsNone(detect_loan("tithe"))
        self.assertIsNone(detect_loan("grp12dev"))
        self.assertIsNone(detect_loan(""))

    def test_pattern_cache_invalidates_on_change(self):
        self.assertIsNone(detect_loan("sacco facility"))
        LoanNarrationPattern.objects.create(pattern="saccofacility",
                                            kind="RECEIPT", match_type="CONTAINS")
        self.assertIsNotNone(detect_loan("SACCO FACILITY"))


class ImporterIntakeTests(TestCase):
    def setUp(self):
        clear_pattern_cache()
        self.fund = Department.objects.create(name="Development", fund_type="LOCAL")
        # point the plain 'loan' receipt alias at the Development fund
        LoanNarrationPattern.objects.filter(pattern="loan", kind="RECEIPT") \
            .update(fund=self.fund)
        clear_pattern_cache()

    def test_receipt_with_fund_is_fully_automatic(self):
        from members.models import Member
        before_members = Member.objects.count()
        imp = _import(_csv([("02 May 2026",
                             "UERLN0001~loan~254722000111~ACME SACCO", "100000")]))
        self.assertEqual(imp.imported, 1)
        self.assertEqual(imp.queued_for_review, 0)
        # lender created (never a Member), loan opened, receipt indexed
        lender = Lender.objects.get(phone="254722000111")
        self.assertEqual(lender.source, Lender.Source.AUTO_BANK)
        self.assertEqual(Member.objects.count(), before_members)
        loan = Loan.objects.get(lender=lender)
        self.assertEqual(loan.fund_id, self.fund.id)
        self.assertEqual(loan.outstanding_principal, Decimal("100000"))
        txn = loan.transactions.get().receipt_transaction
        self.assertTrue(txn.excluded_from_income)
        self.assertEqual(txn.department_id, self.fund.id)
        self.assertIsNone(txn.member_id)

    def test_second_receipt_joins_the_open_loan(self):
        _import(_csv([("02 May 2026", "UERLN0A~loan~254722000111~ACME SACCO", "100000")]))
        _import(_csv([("09 May 2026", "UERLN0B~loan~254722000111~ACME SACCO", "50000")]))
        self.assertEqual(Lender.objects.filter(merged_into__isnull=True).count(), 1)
        loan = Loan.objects.get()
        self.assertEqual(loan.outstanding_principal, Decimal("150000"))
        self.assertEqual(loan.transactions.count(), 2)

    def test_reimport_dedups_on_core_ref(self):
        csv = _csv([("02 May 2026", "UERLN0C~loan~254722000111~ACME SACCO", "100000")])
        _import(csv)
        imp2 = _import(csv)
        self.assertEqual(imp2.duplicates_skipped, 1)
        self.assertEqual(Loan.objects.get().outstanding_principal, Decimal("100000"))

    def test_fundless_pattern_goes_to_review_never_guesses(self):
        from members.models import Member
        before_members = Member.objects.count()
        imp = _import(_csv([("02 May 2026",
                             "UERLN0D~member loan~254733000222~RUTH MOMANYI", "20000")]))
        self.assertEqual(imp.queued_for_review, 1)
        t = Transaction.objects.get(core_ref="UERLN0D")
        self.assertEqual(t.allocation_status, Transaction.Status.REVIEW)
        self.assertIsNone(t.department_id)
        self.assertFalse(hasattr(t, "loan_receipt"))
        # no lender, no member, no loan — resolution is a human decision
        self.assertEqual(Member.objects.count(), before_members)
        self.assertEqual(Lender.objects.count(), 0)
        self.assertEqual(Loan.objects.count(), 0)

    def test_ordinary_giving_untouched(self):
        tithe = Department.objects.create(name="Tithe", fund_type="TRUST")
        from giving.models import AllocationRule
        AllocationRule.objects.create(reference="tithe", department=tithe,
                                      source="SEED", match_type="EXACT")
        imp = _import(_csv([("02 May 2026",
                             "UEROR1~tithe~254790301470~KEVIN OGEGA", "2500")]))
        self.assertEqual(imp.imported, 1)
        t = Transaction.objects.get(core_ref="UEROR1")
        self.assertEqual(t.department_id, tithe.id)
        self.assertFalse(t.excluded_from_income)
        self.assertIsNotNone(t.member_id)       # ordinary givers still become members

    def test_webhook_ingest_matches_importer(self):
        """The live feed and the file importer must never drift: the same
        narration produces the same loan intake through ingest_event."""
        from statements.services.ingest import ingest_event
        txn, outcome = ingest_event(
            date=dt.date(2026, 5, 2), amount=Decimal("100000"),
            direction=Transaction.Direction.CREDIT, reference="loan",
            phone="254722000111", name="ACME SACCO",
            raw_narration="UERLN0E~loan~254722000111~ACME SACCO",
            core_ref="UERLN0E")
        self.assertEqual(outcome, "created")
        self.assertTrue(txn.excluded_from_income)
        loan = Loan.objects.get()
        self.assertEqual(loan.outstanding_principal, Decimal("100000"))
        # idempotent re-delivery
        txn2, outcome2 = ingest_event(
            date=dt.date(2026, 5, 2), amount=Decimal("100000"),
            direction=Transaction.Direction.CREDIT, reference="loan",
            phone="254722000111", name="ACME SACCO",
            raw_narration="UERLN0E~loan~254722000111~ACME SACCO",
            core_ref="UERLN0E")
        self.assertEqual(outcome2, "duplicate")
        self.assertEqual(loan.outstanding_principal, Decimal("100000"))

    def test_loan_receipts_never_match_pledges(self):
        """A member's giving pledge must never be 'fulfilled' by loan money —
        the importer's pledge pass skips excluded (loan) credits entirely."""
        from members.models import Member
        from pledges.models import Pledge, PledgeCampaign, PledgePayment
        m = Member.objects.create(name="ACME SACCO", phone="254722000111")
        camp = PledgeCampaign.objects.create(name="Building appeal",
                                             target_department=self.fund)
        Pledge.objects.create(campaign=camp, member=m,
                              amount=Decimal("100000"), status="ACTIVE")
        _import(_csv([("02 May 2026", "UERLN0F~loan~254722000111~ACME SACCO",
                       "100000")]))
        self.assertEqual(PledgePayment.objects.count(), 0)

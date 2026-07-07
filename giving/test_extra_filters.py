"""Additional filters for the Transactions page: Transaction Type, Amount
Range, Member, Bank Account, Imported By, Reversed Only, Receipted Only,
Manual Receipt Only. Added as a collapsible "More filters" section to keep
the primary filter bar uncluttered.

"Entered By" was deliberately NOT implemented: no existing field tracks who
manually recorded a cash entry (unlike "Imported By", which already has
StatementImport.uploaded_by to draw on) - adding it would need a new field,
a migration, and updating every manual-entry creation path to populate it,
a meaningfully larger change than the rest of this filter set. Documented
as a follow-up recommendation rather than half-implemented."""
import datetime as dt
from decimal import Decimal
from django.test import TestCase, Client
from django.contrib.auth.models import User, Group
from departments.models import Department
from giving.models import Transaction
from members.models import Member
from statements.models import BankAccount, StatementImport


def _tr():
    u = User.objects.create_user("tr_extrafilters", password="x", is_superuser=True)
    u.groups.add(Group.objects.get_or_create(name="Treasurer")[0])
    return u


class ExtraFiltersTests(TestCase):
    def setUp(self):
        self.tr = _tr()
        self.c = Client(); self.c.force_login(self.tr)
        self.d = Department.objects.create(name="ExtraFilterFund", fund_type="LOCAL",
            category="MINISTRY")

    def test_direction_filter(self):
        Transaction.objects.create(date=dt.date(2026, 6, 1), amount=Decimal("100"),
            direction="CREDIT", confirmed=True, channel="BANK", allocation_status="AUTO",
            department=self.d, reference="dirtest_credit")
        Transaction.objects.create(date=dt.date(2026, 6, 1), amount=Decimal("50"),
            direction="DEBIT", confirmed=True, channel="BANK", allocation_status="REVIEW",
            reference="dirtest_debit")
        b = self.c.get("/transactions/?direction=DEBIT").content.decode()
        self.assertIn("dirtest_debit", b)
        self.assertNotIn("dirtest_credit", b)

    def test_amount_range_filter(self):
        Transaction.objects.create(date=dt.date(2026, 6, 2), amount=Decimal("50"),
            direction="CREDIT", confirmed=True, channel="BANK", allocation_status="AUTO",
            department=self.d, reference="amttest_low")
        Transaction.objects.create(date=dt.date(2026, 6, 2), amount=Decimal("5000"),
            direction="CREDIT", confirmed=True, channel="BANK", allocation_status="AUTO",
            department=self.d, reference="amttest_high")
        b = self.c.get("/transactions/?amount_min=1000").content.decode()
        self.assertIn("amttest_high", b)
        self.assertNotIn("amttest_low", b)

    def test_amount_range_invalid_input_does_not_crash(self):
        r = self.c.get("/transactions/?amount_min=notanumber&amount_max=alsobad")
        self.assertEqual(r.status_code, 200)

    def test_member_filter(self):
        m = Member.objects.create(name="ALICE MEMBERTEST", phone="254700111222")
        Transaction.objects.create(date=dt.date(2026, 6, 3), amount=Decimal("200"),
            direction="CREDIT", confirmed=True, channel="BANK", allocation_status="AUTO",
            department=self.d, reference="membertest1", member=m)
        Transaction.objects.create(date=dt.date(2026, 6, 3), amount=Decimal("200"),
            direction="CREDIT", confirmed=True, channel="BANK", allocation_status="AUTO",
            department=self.d, reference="membertest2")
        b = self.c.get("/transactions/?member=ALICE").content.decode()
        self.assertIn("membertest1", b)
        self.assertNotIn("membertest2", b)

    def test_bank_account_filter(self):
        ba1 = BankAccount.objects.create(name="Account One", account_number="111", active=True)
        ba2 = BankAccount.objects.create(name="Account Two", account_number="222", active=True)
        Transaction.objects.create(date=dt.date(2026, 6, 4), amount=Decimal("300"),
            direction="CREDIT", confirmed=True, channel="BANK", allocation_status="AUTO",
            department=self.d, reference="banktest1", bank_account=ba1)
        Transaction.objects.create(date=dt.date(2026, 6, 4), amount=Decimal("300"),
            direction="CREDIT", confirmed=True, channel="BANK", allocation_status="AUTO",
            department=self.d, reference="banktest2", bank_account=ba2)
        b = self.c.get(f"/transactions/?bank_account={ba1.id}").content.decode()
        self.assertIn("banktest1", b)
        self.assertNotIn("banktest2", b)

    def test_imported_by_filter(self):
        other_tr = User.objects.create_user("tr_extrafilters_other", password="x",
            is_superuser=True)
        si1 = StatementImport.objects.create(uploaded_by=self.tr, filename="a.csv", status="DONE")
        si2 = StatementImport.objects.create(uploaded_by=other_tr, filename="b.csv", status="DONE")
        Transaction.objects.create(date=dt.date(2026, 6, 5), amount=Decimal("400"),
            direction="CREDIT", confirmed=True, channel="BANK", allocation_status="AUTO",
            department=self.d, reference="importtest1", statement_import=si1)
        Transaction.objects.create(date=dt.date(2026, 6, 5), amount=Decimal("400"),
            direction="CREDIT", confirmed=True, channel="BANK", allocation_status="AUTO",
            department=self.d, reference="importtest2", statement_import=si2)
        b = self.c.get(f"/transactions/?imported_by={self.tr.id}").content.decode()
        self.assertIn("importtest1", b)
        self.assertNotIn("importtest2", b)

    def test_reversed_only_filter(self):
        t = Transaction.objects.create(date=dt.date(2026, 6, 6), amount=Decimal("500"),
            direction="CREDIT", confirmed=True, channel="BANK", allocation_status="AUTO",
            department=self.d, reference="revonlytest1")
        Transaction.objects.create(date=dt.date(2026, 6, 6), amount=Decimal("500"),
            direction="CREDIT", confirmed=True, channel="BANK", allocation_status="AUTO",
            department=self.d, reference="revonlytest2")
        t.reverse(self.tr, reason="test")
        b = self.c.get("/transactions/?reversed_only=1").content.decode()
        self.assertIn("revonlytest1", b)
        self.assertNotIn("revonlytest2", b)

    def test_receipted_only_filter(self):
        Transaction.objects.create(date=dt.date(2026, 6, 7), amount=Decimal("500"),
            direction="CREDIT", confirmed=True, channel="BANK", allocation_status="AUTO",
            department=self.d, reference="receiptedtest1", manual_receipt=True)
        Transaction.objects.create(date=dt.date(2026, 6, 7), amount=Decimal("500"),
            direction="CREDIT", confirmed=True, channel="BANK", allocation_status="AUTO",
            department=self.d, reference="receiptedtest2")
        b = self.c.get("/transactions/?receipted_only=1").content.decode()
        self.assertIn("receiptedtest1", b)
        self.assertNotIn("receiptedtest2", b)

    def test_manual_receipt_only_excludes_envelope_receipted(self):
        Transaction.objects.create(date=dt.date(2026, 6, 8), amount=Decimal("500"),
            direction="CREDIT", confirmed=True, channel="BANK", allocation_status="AUTO",
            department=self.d, reference="manualonlytest1", manual_receipt=True)
        Transaction.objects.create(date=dt.date(2026, 6, 8), amount=Decimal("500"),
            direction="CREDIT", confirmed=True, channel="BANK", allocation_status="AUTO",
            department=self.d, reference="manualonlytest2", processed_via_envelope=True)
        b = self.c.get("/transactions/?manual_receipt_only=1").content.decode()
        self.assertIn("manualonlytest1", b)
        self.assertNotIn("manualonlytest2", b)

    def test_more_filters_section_present_in_page(self):
        b = self.c.get("/transactions/").content.decode()
        self.assertIn("More filters", b)
        self.assertIn("Amount from", b)
        self.assertIn("Bank account", b)
        self.assertIn("Imported by", b)

    def test_combining_multiple_new_filters(self):
        m = Member.objects.create(name="BOB COMBOTEST", phone="254700333444")
        Transaction.objects.create(date=dt.date(2026, 6, 9), amount=Decimal("1500"),
            direction="CREDIT", confirmed=True, channel="BANK", allocation_status="AUTO",
            department=self.d, reference="combotest1", member=m)
        b = self.c.get("/transactions/?direction=CREDIT&amount_min=1000&member=BOB").content.decode()
        self.assertIn("combotest1", b)

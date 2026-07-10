"""Tests for the general-ledger health check dashboard, the period-close
checklist, and immutable journal sequence numbering."""
import datetime as dt
from decimal import Decimal
from django.test import TestCase, Client
from django.contrib.auth.models import User, Group
from departments.models import Department
from cashbook.models import Expense
from giving.models import Transaction
from ledger.services.posting import ensure_chart
from ledger.models import JournalEntry, JournalEntryArchive, JournalSequence
from core.models import SiteConfig


def _tr(name="tr_health"):
    u = User.objects.create_user(name, password="x", is_superuser=True)
    u.groups.add(Group.objects.get_or_create(name="Treasurer")[0])
    return u


class HealthCheckTests(TestCase):
    def setUp(self):
        ensure_chart()
        self.tr = _tr()
        self.d = Department.objects.create(name="HealthFund", fund_type="LOCAL",
            category="MINISTRY")
        self.c = Client(); self.c.force_login(self.tr)

    def test_clean_ledger_is_all_clear(self):
        from ledger.services.health import run_health_check
        r = run_health_check()
        self.assertTrue(r["trial_balance_balanced"])
        self.assertFalse(r["unbalanced_journals"])
        self.assertFalse(r["orphan_journals"])
        self.assertFalse(r["duplicate_postings"])
        self.assertFalse(r["funds_out_of_balance"])

    def test_orphan_journal_detected(self):
        from ledger.services.health import orphan_journals
        from ledger.services.posting import _acct
        je = JournalEntry.objects.create(date=dt.date(2026, 6, 1), memo="orphan",
            source_type="transaction", source_id=999999)
        JournalLineModel = je.lines.model
        JournalLineModel.objects.create(entry=je, account=_acct("CASH"),
            debit=Decimal("100"), credit=0)
        JournalLineModel.objects.create(entry=je, account=_acct("INC_OFFERINGS"),
            debit=0, credit=Decimal("100"))
        orphans = orphan_journals()
        self.assertTrue(any(o.id == je.id for o in orphans))

    def test_missing_posting_detected(self):
        from ledger.services.health import missing_source_documents
        t = Transaction.objects.create(date=dt.date(2026, 6, 2), amount=Decimal("300"),
            direction="CREDIT", confirmed=True, channel="CASH",
            allocation_status="MANUAL", department=self.d)
        JournalEntry.objects.filter(source_type="transaction", source_id=t.id).delete()
        missing = missing_source_documents()
        self.assertTrue(any(m.id == t.id for m in missing["transactions"]))

    def test_health_page_renders(self):
        r = self.c.get("/ledger/health/")
        self.assertEqual(r.status_code, 200)
        self.assertIn("All clear", r.content.decode())

    def test_health_page_flags_orphan(self):
        from ledger.services.posting import _acct
        je = JournalEntry.objects.create(date=dt.date(2026, 6, 1), memo="orphan2",
            source_type="expense", source_id=888888)
        JournalLineModel = je.lines.model
        JournalLineModel.objects.create(entry=je, account=_acct("CASH"), debit=0,
            credit=Decimal("50"))
        JournalLineModel.objects.create(entry=je, account=_acct("EXP_OTHER"),
            debit=Decimal("50"), credit=0)
        b = self.c.get("/ledger/health/").content.decode()
        self.assertIn("Orphan journals", b)
        self.assertNotIn("All clear", b)


class PeriodCloseChecklistTests(TestCase):
    def setUp(self):
        ensure_chart()
        self.tr = _tr("tr_checklist")
        self.d = Department.objects.create(name="ChecklistFund", fund_type="LOCAL",
            category="MINISTRY")
        self.c = Client(); self.c.force_login(self.tr)

    def test_checklist_has_all_eight_items(self):
        from core.services.period_close import period_close_checklist
        items = period_close_checklist(2026, 6)
        self.assertEqual(len(items), 8)
        keys = {i["key"] for i in items}
        self.assertEqual(keys, {"bank_reconciliation", "petty_cash", "advances",
            "envelope_allocations", "pending_entries", "trial_balance",
            "fund_balances", "cashbook_equals_bank"})

    def test_pending_expense_flagged(self):
        from core.services.period_close import period_close_checklist
        Expense.objects.create(date=dt.date(2026, 6, 5), department=self.d,
            description="pending", amount=Decimal("200"), category="OTHER",
            status="PENDING", recorded_by=self.tr)
        items = period_close_checklist(2026, 6)
        pending_item = next(i for i in items if i["key"] == "pending_entries")
        self.assertFalse(pending_item["ok"])

    def test_balanced_reconciliation_clears_bank_item(self):
        from statements.models import BankReconciliation
        from core.services.period_close import period_close_checklist
        BankReconciliation.objects.create(statement_date=dt.date(2026, 6, 30),
            bank_balance=Decimal("1000"), book_balance=Decimal("1000"),
            created_by=self.tr)
        items = period_close_checklist(2026, 6)
        bank_item = next(i for i in items if i["key"] == "bank_reconciliation")
        self.assertTrue(bank_item["ok"])

    def test_controls_page_shows_checklist(self):
        r = self.c.get("/controls/?checklist_month=6")
        self.assertEqual(r.status_code, 200)
        self.assertIn("Period-close checklist", r.content.decode())

    def test_locked_month_hides_checklist(self):
        from core.models import PeriodLock
        PeriodLock.objects.create(year=2026, month=6, locked_by=self.tr)
        b = self.c.get("/controls/?checklist_month=6").content.decode()
        self.assertNotIn("Period-close checklist", b)


class JournalNumberingTests(TestCase):
    def setUp(self):
        ensure_chart()
        self.tr = _tr("tr_jvnum")
        self.d = Department.objects.create(name="JvNumFund", fund_type="LOCAL",
            category="MINISTRY")

    def test_new_entry_gets_a_number(self):
        exp = Expense.objects.create(date=dt.date(2026, 6, 5), department=self.d,
            description="jv test", amount=Decimal("100"), category="OTHER",
            status="PAID", recorded_by=self.tr, approved_by=self.tr)
        je = JournalEntry.objects.filter(source_type="expense", source_id=exp.id).first()
        self.assertRegex(je.number, r"^JV-2026-\d{6}$")

    def test_numbers_are_unique_and_sequential(self):
        numbers = []
        for i in range(3):
            exp = Expense.objects.create(date=dt.date(2026, 6, 5), department=self.d,
                description=f"jv seq {i}", amount=Decimal("10"), category="OTHER",
                status="PAID", recorded_by=self.tr, approved_by=self.tr)
            je = JournalEntry.objects.filter(source_type="expense", source_id=exp.id).first()
            numbers.append(int(je.number.split("-")[-1]))
        self.assertEqual(numbers, sorted(numbers))
        self.assertEqual(len(set(numbers)), 3)

    def test_correction_preserves_original_number_in_archive(self):
        cfg = SiteConfig.get(); cfg.archive_replaced_ledger_entries = True; cfg.save()
        exp = Expense.objects.create(date=dt.date(2026, 6, 5), department=self.d,
            description="jv correction", amount=Decimal("100"), category="OTHER",
            status="PAID", recorded_by=self.tr, approved_by=self.tr)
        je1 = JournalEntry.objects.filter(source_type="expense", source_id=exp.id).first()
        original_number = je1.number
        exp.amount = Decimal("200")
        exp.save()
        je2 = JournalEntry.objects.filter(source_type="expense", source_id=exp.id).first()
        self.assertNotEqual(je2.number, original_number)
        arch = JournalEntryArchive.objects.filter(source_type="expense", source_id=exp.id).first()
        self.assertEqual(arch.original_number, original_number)

    def test_number_never_reused(self):
        exp1 = Expense.objects.create(date=dt.date(2026, 6, 5), department=self.d,
            description="a", amount=Decimal("10"), category="OTHER",
            status="PAID", recorded_by=self.tr, approved_by=self.tr)
        je1 = JournalEntry.objects.filter(source_type="expense", source_id=exp1.id).first()
        n1 = je1.number
        exp1.delete()  # removes the journal entry entirely
        exp2 = Expense.objects.create(date=dt.date(2026, 6, 6), department=self.d,
            description="b", amount=Decimal("20"), category="OTHER",
            status="PAID", recorded_by=self.tr, approved_by=self.tr)
        je2 = JournalEntry.objects.filter(source_type="expense", source_id=exp2.id).first()
        self.assertNotEqual(je2.number, n1)

    def test_per_year_sequence(self):
        exp_2026 = Expense.objects.create(date=dt.date(2026, 6, 5), department=self.d,
            description="y2026", amount=Decimal("10"), category="OTHER",
            status="PAID", recorded_by=self.tr, approved_by=self.tr)
        exp_2025 = Expense.objects.create(date=dt.date(2025, 6, 5), department=self.d,
            description="y2025", amount=Decimal("10"), category="OTHER",
            status="PAID", recorded_by=self.tr, approved_by=self.tr)
        je_2026 = JournalEntry.objects.filter(source_type="expense", source_id=exp_2026.id).first()
        je_2025 = JournalEntry.objects.filter(source_type="expense", source_id=exp_2025.id).first()
        self.assertTrue(je_2026.number.startswith("JV-2026-"))
        self.assertTrue(je_2025.number.startswith("JV-2025-"))

    def test_journal_page_shows_number(self):
        exp = Expense.objects.create(date=dt.date(2026, 6, 5), department=self.d,
            description="visible", amount=Decimal("10"), category="OTHER",
            status="PAID", recorded_by=self.tr, approved_by=self.tr)
        je = JournalEntry.objects.filter(source_type="expense", source_id=exp.id).first()
        c = Client(); c.force_login(self.tr)
        b = c.get(f"/ledger/journal/?start=2026-06-01&end=2026-06-30").content.decode()
        self.assertIn(je.number, b)

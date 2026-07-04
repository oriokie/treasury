"""Regression tests for the customizable internal-controls implemented from
the accounting-integrity review's recommendations. Each control that involves
a policy trade-off is off (or unrestricted) by default, so existing
deployments see no behaviour change until a treasurer opts in."""
import datetime as dt
from decimal import Decimal
from django.test import TestCase, Client
from django.contrib.auth.models import User, Group
from departments.models import Department
from cashbook.models import Expense, StaffAdvance
from giving.models import Transaction
from ledger.services.posting import ensure_chart, UnbalancedEntryError, _entry, _acct
from ledger.models import JournalEntryArchive, JournalEntry
from core.models import SiteConfig


def _tr(name="tr_audit2"):
    u = User.objects.create_user(name, password="x", is_superuser=True)
    u.groups.add(Group.objects.get_or_create(name="Treasurer")[0])
    return u


class BalancedEntryValidationTests(TestCase):
    """F-4: nothing enforced that a journal entry balances; now validated at
    the single choke point every posting path goes through."""
    def setUp(self):
        ensure_chart()

    def test_unbalanced_entry_rejected(self):
        cash = _acct("CASH"); inc = _acct("INC_OFFERINGS")
        with self.assertRaises(UnbalancedEntryError):
            _entry(dt.date(2026, 6, 1), "test", "manual", None,
                   [(cash, Decimal("100"), Decimal(0)), (inc, Decimal(0), Decimal("90"))])

    def test_line_with_both_debit_and_credit_rejected(self):
        cash = _acct("CASH")
        with self.assertRaises(UnbalancedEntryError):
            _entry(dt.date(2026, 6, 1), "test", "manual", None,
                   [(cash, Decimal("100"), Decimal("50"))])

    def test_balanced_entry_still_works(self):
        cash = _acct("CASH"); inc = _acct("INC_OFFERINGS")
        je = _entry(dt.date(2026, 6, 1), "test", "manual", None,
                    [(cash, Decimal("100"), Decimal(0)), (inc, Decimal(0), Decimal("100"))])
        self.assertIsNotNone(je.id)


class LedgerArchiveTests(TestCase):
    """F-5: setting-controlled snapshot of a journal entry's detail before a
    correction replaces it."""
    def setUp(self):
        ensure_chart()
        self.tr = _tr("tr_archive")
        self.d = Department.objects.create(name="ArchiveFund", fund_type="LOCAL",
            category="MINISTRY")

    def test_archive_created_when_setting_on(self):
        cfg = SiteConfig.get(); cfg.archive_replaced_ledger_entries = True; cfg.save()
        exp = Expense.objects.create(date=dt.date(2026, 6, 5), department=self.d,
            description="Archive me", amount=Decimal("1000"), category="MATERIALS",
            status="PAID", recorded_by=self.tr, approved_by=self.tr)
        exp.amount = Decimal("1500")
        exp.save()
        self.assertTrue(JournalEntryArchive.objects.filter(
            source_type="expense", source_id=exp.id).exists())
        snap = JournalEntryArchive.objects.filter(source_type="expense", source_id=exp.id).first()
        self.assertTrue(any(l["debit"] == "1000.00" for l in snap.lines))

    def test_no_archive_when_setting_off(self):
        cfg = SiteConfig.get(); cfg.archive_replaced_ledger_entries = False; cfg.save()
        exp = Expense.objects.create(date=dt.date(2026, 6, 5), department=self.d,
            description="No archive", amount=Decimal("1000"), category="MATERIALS",
            status="PAID", recorded_by=self.tr, approved_by=self.tr)
        exp.amount = Decimal("2000")
        exp.save()
        self.assertFalse(JournalEntryArchive.objects.filter(
            source_type="expense", source_id=exp.id).exists())

    def test_current_ledger_always_reflects_the_latest_correction(self):
        cfg = SiteConfig.get(); cfg.archive_replaced_ledger_entries = True; cfg.save()
        exp = Expense.objects.create(date=dt.date(2026, 6, 5), department=self.d,
            description="Latest", amount=Decimal("1000"), category="MATERIALS",
            status="PAID", recorded_by=self.tr, approved_by=self.tr)
        exp.amount = Decimal("1500")
        exp.save()
        je = JournalEntry.objects.filter(source_type="expense", source_id=exp.id).first()
        line = je.lines.filter(debit__gt=0).first()
        self.assertEqual(line.debit, Decimal("1500"))

    def test_archive_viewer_page_renders(self):
        c = Client(); c.force_login(self.tr)
        r = c.get("/ledger/journal/archive/")
        self.assertEqual(r.status_code, 200)


class ReconciliationPeriodLinkTests(TestCase):
    """F-6: reconciliation sign-off can optionally auto-lock its period, and a
    non-blocking warning always appears when editing an entry in an already-
    reconciled period regardless of the setting."""
    def setUp(self):
        ensure_chart()
        self.tr = _tr("tr_recon_link")
        self.d = Department.objects.create(name="ReconLinkFund", fund_type="LOCAL",
            category="MINISTRY")
        self.c = Client(); self.c.force_login(self.tr)

    def test_auto_lock_off_by_default(self):
        from statements.models import BankReconciliation
        rec = BankReconciliation.objects.create(statement_date=dt.date(2026, 6, 30),
            bank_balance=Decimal("10000"), book_balance=Decimal("10000"), created_by=self.tr)
        self.c.post(f"/reconciliations/{rec.id}/", {"action": "recompute_book"})
        from core.models import period_locked
        self.assertIsNone(period_locked(dt.date(2026, 6, 15)))

    def test_auto_lock_when_enabled_and_balanced(self):
        cfg = SiteConfig.get(); cfg.auto_lock_on_reconciliation = True; cfg.save()
        from statements.models import BankReconciliation
        rec = BankReconciliation.objects.create(statement_date=dt.date(2026, 6, 30),
            bank_balance=Decimal("10000"), created_by=self.tr)
        self.c.post(f"/reconciliations/{rec.id}/",
            {"action": "set_book", "book_balance": "10000"})
        from core.models import period_locked
        self.assertIsNotNone(period_locked(dt.date(2026, 6, 15)))

    def test_warning_shown_editing_reconciled_period(self):
        from statements.models import BankReconciliation
        BankReconciliation.objects.create(statement_date=dt.date(2026, 6, 30),
            bank_balance=Decimal("5000"), book_balance=Decimal("5000"), created_by=self.tr)
        exp = Expense.objects.create(date=dt.date(2026, 6, 10), department=self.d,
            description="Edit me", amount=Decimal("100"), category="OTHER",
            status="PENDING", recorded_by=self.tr)
        r = self.c.post(f"/expenses/{exp.id}/edit/", {
            "date": "2026-06-11", "department": self.d.id, "description": "Edited",
            "amount": "150", "category": "OTHER", "method": "CASH"}, follow=True)
        b = r.content.decode()
        self.assertIn("already has a bank reconciliation", b)


class SelfApprovalControlTests(TestCase):
    """L-1: optional block on approving an expense you recorded yourself."""
    def setUp(self):
        ensure_chart()
        self.tr = _tr("tr_selfapprove")
        self.d = Department.objects.create(name="SelfApproveFund", fund_type="LOCAL",
            category="MINISTRY")
        self.c = Client(); self.c.force_login(self.tr)

    def test_self_approval_allowed_by_default(self):
        exp = Expense.objects.create(date=dt.date(2026, 6, 5), department=self.d,
            description="Self", amount=Decimal("500"), category="OTHER",
            status="PENDING", recorded_by=self.tr)
        self.c.post(f"/expenses/{exp.id}/approve/", {"action": "approve"})
        exp.refresh_from_db()
        self.assertEqual(exp.status, "APPROVED")

    def test_self_approval_blocked_when_enabled(self):
        cfg = SiteConfig.get(); cfg.require_different_approver = True; cfg.save()
        exp = Expense.objects.create(date=dt.date(2026, 6, 5), department=self.d,
            description="Self2", amount=Decimal("500"), category="OTHER",
            status="PENDING", recorded_by=self.tr)
        self.c.post(f"/expenses/{exp.id}/approve/", {"action": "approve"})
        exp.refresh_from_db()
        self.assertEqual(exp.status, "PENDING")

    def test_different_approver_still_works_when_enabled(self):
        cfg = SiteConfig.get(); cfg.require_different_approver = True; cfg.save()
        other = _tr("tr_selfapprove_other")
        exp = Expense.objects.create(date=dt.date(2026, 6, 5), department=self.d,
            description="Other", amount=Decimal("500"), category="OTHER",
            status="PENDING", recorded_by=other)
        self.c.post(f"/expenses/{exp.id}/approve/", {"action": "approve"})
        exp.refresh_from_db()
        self.assertEqual(exp.status, "APPROVED")


class IncomeAccountOverrideTests(TestCase):
    """L-2: explicit per-fund income-account override beats name-guessing."""
    def test_override_wins_over_name_guess(self):
        from ledger.services.posting import _income_key_for
        d = Department.objects.create(name="Ambiguous Fund Name", fund_type="LOCAL",
            category="MINISTRY", income_account="INC_DEVELOPMENT")
        self.assertEqual(_income_key_for(d), "INC_DEVELOPMENT")

    def test_blank_override_falls_back_to_name_guess(self):
        from ledger.services.posting import _income_key_for
        d = Department.objects.create(name="Tithe Fund", fund_type="LOCAL",
            category="MINISTRY")
        self.assertEqual(_income_key_for(d), "INC_TITHE")


class LeaderDeleteWindowAndReasonTests(TestCase):
    """F-3: a leader deleting their own posted advance line must give a
    reason, and can optionally be limited to a self-service time window."""
    def setUp(self):
        ensure_chart()
        self.tr = _tr("tr_delwindow")
        self.d = Department.objects.create(name="DelWindowFund", fund_type="LOCAL",
            category="MINISTRY")
        self.leader = User.objects.create_user("leader_delwindow", password="x")
        self.leader.groups.add(Group.objects.get_or_create(name="Leader")[0])
        from departments.models import DepartmentLeadership
        DepartmentLeadership.objects.create(department=self.d, user=self.leader)
        self.adv = StaffAdvance.objects.create(staff_name="Leader Advance",
            department=self.d, amount=Decimal("5000"), date_issued=dt.date(2026, 6, 1),
            purpose="x", method="CASH", from_petty_cash=False,
            issued_by=self.tr, status="ISSUED")
        self.c = Client(); self.c.force_login(self.leader)

    def test_delete_requires_a_reason(self):
        exp = Expense.objects.create(advance=self.adv, department=self.d,
            description="line", amount=Decimal("200"), category="OTHER",
            status="PAID", recorded_by=self.leader, date=dt.date(2026, 6, 5))
        self.c.post(f"/leader/advances/{self.adv.id}/",
            {"action": "delete_expense", "expense_id": exp.id})
        self.assertTrue(Expense.objects.filter(id=exp.id).exists())

    def test_delete_with_reason_succeeds_no_window_set(self):
        exp = Expense.objects.create(advance=self.adv, department=self.d,
            description="line2", amount=Decimal("200"), category="OTHER",
            status="PAID", recorded_by=self.leader, date=dt.date(2026, 6, 5))
        self.c.post(f"/leader/advances/{self.adv.id}/",
            {"action": "delete_expense", "expense_id": exp.id, "delete_reason": "mistake"})
        self.assertFalse(Expense.objects.filter(id=exp.id).exists())

    def test_delete_blocked_outside_window(self):
        cfg = SiteConfig.get(); cfg.leader_delete_window_days = 1; cfg.save()
        exp = Expense.objects.create(advance=self.adv, department=self.d,
            description="old line", amount=Decimal("200"), category="OTHER",
            status="PAID", recorded_by=self.leader, date=dt.date(2026, 6, 5))
        Expense.objects.filter(id=exp.id).update(
            created_at=dt.datetime.now() - dt.timedelta(days=5))
        self.c.post(f"/leader/advances/{self.adv.id}/",
            {"action": "delete_expense", "expense_id": exp.id, "delete_reason": "late"})
        self.assertTrue(Expense.objects.filter(id=exp.id).exists())

    def test_treasurer_notified_on_delete(self):
        from core.models import Notification
        exp = Expense.objects.create(advance=self.adv, department=self.d,
            description="line3", amount=Decimal("200"), category="OTHER",
            status="PAID", recorded_by=self.leader, date=dt.date(2026, 6, 5))
        before = Notification.objects.filter(recipient=self.tr).count()
        self.c.post(f"/leader/advances/{self.adv.id}/",
            {"action": "delete_expense", "expense_id": exp.id, "delete_reason": "mistake"})
        self.assertGreater(Notification.objects.filter(recipient=self.tr).count(), before)

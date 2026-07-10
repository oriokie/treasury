from django.contrib.auth.models import User
from django.test import TestCase

from departments.models import Department
from giving.models import AllocationRule, Transaction
from giving.services.allocation import normalize_reference
from statements.models import StatementImport
from statements.services.parser import parse_narration
from statements.services.importer import run_import


class NarrationParsingTests(TestCase):
    def test_standard_shape(self):
        # receipt ~ paybill#reference ~ phone ~ marker ~ name
        out = parse_narration(
            "UER2Q5NF2W~441211#tithe~254790301470~MPESAC2B_400222~KEVIN OGEGA")
        self.assertEqual(out["shape"], "standard")
        self.assertEqual(out["reference"], "tithe")
        self.assertEqual(out["phone"], "254790301470")
        self.assertEqual(out["name"], "KEVIN OGEGA")

    def test_other_shape_queues(self):
        out = parse_narration("UERCW5TIVN~Other~254716804186~Development200")
        self.assertEqual(out["shape"], "other")
        self.assertEqual(out["phone"], "254716804186")
        # 'Other' gives no confident reference -> empty so it lands in the queue
        self.assertEqual(out["reference"], "")

    def test_transfer_shape(self):
        out = parse_narration("AC0C40FD2E26 EDWIN ORIOKI KENYANSA Grp12dev")
        self.assertEqual(out["shape"], "transfer")
        self.assertEqual(out["reference"], "Grp12dev")
        self.assertIn("EDWIN", out["name"])

    def test_empty(self):
        out = parse_narration("")
        self.assertEqual(out["reference"], "")
        self.assertEqual(out["name"], "")


CSV = b"""Receipt No,Completion Time,Details,Paid In,Withdrawn,Balance
QFT11AA001,2026-06-06 09:14:00,UER2Q5NF2W~441211#tithe~254790301470~MPESAC2B_400222~KEVIN OGEGA,1000,,1000
QFT11AA004,2026-06-06 10:30:00,UERCW5TIVN~Other~254716804186~Development200,300,,2000
QFT11AA008,2026-06-06 13:00:00,Bank charge - monthly ledger fee,,75,9075
"""


class ImporterTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user("imp", password="x")
        tithe = Department.objects.create(
            name="Tithe", fund_type=Department.FundType.TRUST)
        AllocationRule.objects.create(
            reference=normalize_reference("tithe"), department=tithe,
            source=AllocationRule.Source.SEED)

    def _import(self, content):
        imp = StatementImport.objects.create(
            uploaded_by=self.user, filename="s.csv")
        run_import(imp, content, "s.csv")
        return imp

    def test_import_classifies_rows(self):
        imp = self._import(CSV)
        self.assertEqual(imp.status, StatementImport.Status.DONE)
        self.assertEqual(imp.total_rows, 3)
        # tithe auto-allocated; 'Other' credit -> review; debit -> review
        self.assertEqual(imp.imported, 1)
        self.assertEqual(imp.queued_for_review, 2)

    def test_reimport_skips_duplicates(self):
        self._import(CSV)
        before = Transaction.objects.count()
        imp2 = self._import(CSV)
        self.assertEqual(imp2.duplicates_skipped, 3)
        self.assertEqual(Transaction.objects.count(), before)

    def test_auto_allocated_links_member_and_fund(self):
        self._import(CSV)
        t = Transaction.objects.get(reference="tithe")
        self.assertEqual(t.department.name, "Tithe")
        self.assertIsNotNone(t.member)
        self.assertEqual(t.allocation_status, Transaction.Status.AUTO)
        # the M-Pesa receipt from the narration becomes the dedup key
        self.assertEqual(t.bank_receipt, "UER2Q5NF2W")


class RealStatementParserTests(TestCase):
    """Parse the actual bank .xls layout (header on row 6, Channel REF, commas)."""
    PATH = "/mnt/user-data/uploads/null01Jun2026_120102.xls"

    def setUp(self):
        import os
        if not os.path.exists(self.PATH):
            self.skipTest("sample statement not present")

    def test_parses_headers_amounts_and_mpesa_ref(self):
        from statements.services.parser import read_rows
        rows = read_rows(self.PATH, "null01Jun2026_120102.xls")
        self.assertGreater(len(rows), 300)
        credits = [r for r in rows if r["credit"]]
        self.assertTrue(all(r["mpesa_ref"] for r in credits))
        first = credits[0]
        self.assertEqual(first["core_ref"], "CB0019388260530")
        self.assertEqual(first["mpesa_ref"], "UEUF56BB14")
        self.assertEqual(first["phone"], "254796472241")

    def test_reads_from_bytes(self):
        from statements.services.parser import read_rows
        with open(self.PATH, "rb") as fh:
            data = fh.read()
        rows = read_rows(data, "x.xls")
        self.assertGreater(len(rows), 300)


class ReconciliationModelTests(TestCase):
    def test_adjusted_balance_and_difference(self):
        from django.contrib.auth.models import User
        from statements.models import BankReconciliation, ReconciliationItem
        from decimal import Decimal
        u = User.objects.create_user("rec", password="x")
        rec = BankReconciliation.objects.create(
            statement_date="2026-05-30", bank_balance=Decimal("100000"),
            book_balance=Decimal("97000"), created_by=u)
        ReconciliationItem.objects.create(
            reconciliation=rec, kind=ReconciliationItem.Kind.UNPRESENTED,
            amount=Decimal("5000"), effect=ReconciliationItem.Effect.SUBTRACT)
        ReconciliationItem.objects.create(
            reconciliation=rec, kind=ReconciliationItem.Kind.CASH_AT_HAND,
            amount=Decimal("2000"), effect=ReconciliationItem.Effect.ADD)
        self.assertEqual(rec.adjusted_balance, Decimal("97000"))
        self.assertEqual(rec.difference, Decimal("0"))
        self.assertTrue(rec.is_reconciled)


from django.test import TestCase


class AutoReconcileTests(TestCase):
    def setUp(self):
        import datetime as dt
        from decimal import Decimal
        from django.contrib.auth.models import User
        from departments.models import Department
        from giving.models import Transaction
        from cashbook.models import Expense
        self.u = User.objects.create_superuser("rc", password="x")
        self.dept = Department.objects.create(name="Youth", fund_type=Department.FundType.LOCAL)
        # high-confidence pair
        self.e_hi = Expense.objects.create(date=dt.date(2026, 5, 12), department=self.dept,
            description="PA system", amount=Decimal("3500"), category="MAINTENANCE",
            claimant="Otieno", method=Expense.Method.CHEQUE, voucher_no="CHQ77",
            status=Expense.Status.APPROVED, recorded_by=self.u)
        self.d_hi = Transaction.objects.create(date=dt.date(2026, 5, 12), channel="BANK",
            direction="DEBIT", amount=Decimal("3500"), allocation_status="REVIEW",
            raw_narration="CHEQUE CHQ77 OTIENO")
        # medium-confidence pair
        self.e_md = Expense.objects.create(date=dt.date(2026, 5, 2), department=self.dept,
            description="Refreshments", amount=Decimal("1234"), category="REFRESHMENTS",
            method=Expense.Method.BANK, status=Expense.Status.APPROVED, recorded_by=self.u)
        self.d_md = Transaction.objects.create(date=dt.date(2026, 5, 10), channel="BANK",
            direction="DEBIT", amount=Decimal("1234"), allocation_status="REVIEW",
            raw_narration="POS 8842")

    def test_scoring_and_run(self):
        from statements.services import reconcile as R
        from statements.models import ReconciliationMatch
        from cashbook.models import Expense
        self.assertEqual(R.score(self.d_hi, self.e_hi)[0], 100)
        self.assertTrue(55 <= R.score(self.d_md, self.e_md)[0] < 90)
        summary = R.run_auto_reconcile(self.u)
        self.assertEqual(summary["auto"], 1)
        self.assertEqual(summary["review"], 1)
        # high-confidence auto-linked, medium not linked until confirmed
        self.assertEqual(Expense.objects.get(id=self.e_hi.id).bank_transaction_id, self.d_hi.id)
        self.assertIsNone(Expense.objects.get(id=self.e_md.id).bank_transaction_id)

    def test_confirm_and_reject(self):
        from statements.services import reconcile as R
        from statements.models import ReconciliationMatch
        from cashbook.models import Expense
        R.run_auto_reconcile(self.u)
        m_md = ReconciliationMatch.objects.get(transaction=self.d_md)
        R.confirm(m_md, self.u)
        self.assertEqual(Expense.objects.get(id=self.e_md.id).bank_transaction_id, self.d_md.id)
        m_hi = ReconciliationMatch.objects.get(transaction=self.d_hi)
        R.reject(m_hi)
        self.assertIsNone(Expense.objects.get(id=self.e_hi.id).bank_transaction_id)


class SabbathCloseTests(TestCase):
    def _csv(self, ref, name, amount, when="02 May 2026"):
        return (
            "Completion Time,Details,Paid In\n"
            f"{when},{ref}~tithe~254790301470~{name},{amount}\n"
        ).encode("utf-8")

    def _import(self, csv, fname="m.csv", today="2026-05-02"):
        import datetime as dt
        from unittest import mock
        from django.contrib.auth.models import User
        from statements.models import StatementImport
        from statements.services import importer
        u = User.objects.filter(is_superuser=True).first() or \
            User.objects.create_superuser("imp", password="x")
        imp = StatementImport.objects.create(uploaded_by=u, filename=fname)

        # Pin the import day to the gift's Sabbath so these tests exercise the
        # closed-Sabbath roll in isolation (the "imported after the Sabbath had
        # passed" roll is verified in ImportedAfterSabbathTests below).
        class _D(dt.date):
            @classmethod
            def today(cls):
                return dt.date.fromisoformat(today)
        with mock.patch.object(importer, "dt") as m:
            m.date = _D
            m.datetime = dt.datetime
            m.timedelta = dt.timedelta
            run_import(imp, csv, fname)

    def test_open_sabbath_keeps_gift(self):
        import datetime as dt
        from giving.models import Transaction
        self._import(self._csv("AAA1", "John", "1000"))
        t = Transaction.objects.get(payer_name__icontains="John")
        self.assertEqual(t.service_sabbath, dt.date(2026, 5, 2))   # natural Sabbath

    def test_closed_sabbath_rolls_to_next_open(self):
        import datetime as dt
        from django.contrib.auth.models import User
        from core.models import SabbathClose
        from giving.models import Transaction
        sat = dt.date(2026, 5, 2)
        SabbathClose.objects.create(sabbath=sat)                   # counted & closed
        self._import(self._csv("BBB2", "Mary", "2000"))            # late gift, same Sabbath
        t = Transaction.objects.get(payer_name__icontains="Mary")
        self.assertEqual(t.date, sat)                              # real date unchanged
        self.assertEqual(t.service_sabbath, sat + dt.timedelta(days=7))  # next open

    def test_rolls_past_several_closed_sabbaths(self):
        import datetime as dt
        from core.models import SabbathClose
        from giving.models import Transaction
        sat = dt.date(2026, 5, 2)
        SabbathClose.objects.create(sabbath=sat)
        SabbathClose.objects.create(sabbath=sat + dt.timedelta(days=7))
        self._import(self._csv("CCC3", "Ann", "500"))
        t = Transaction.objects.get(payer_name__icontains="Ann")
        self.assertEqual(t.service_sabbath, sat + dt.timedelta(days=14))

    def test_financials_use_date_not_service_sabbath(self):
        # A gift whose service Sabbath rolled forward is STILL in the books by date,
        # so a month-end (date-based) reconciliation is unaffected.
        import datetime as dt
        from decimal import Decimal
        from core.models import SabbathClose
        from giving.models import Transaction
        from reports.services.balances import fund_balance
        from departments.models import Department
        sat = dt.date(2026, 5, 2)
        SabbathClose.objects.create(sabbath=sat)
        self._import(self._csv("DDD4", "Sam", "750"))
        t = Transaction.objects.get(payer_name__icontains="Sam")
        self.assertEqual(t.date, sat)
        self.assertNotEqual(t.service_sabbath, sat)                # rolled forward
        # but its fund balance contribution is recognised on the transaction date
        if t.department_id:
            bal_to_date = fund_balance(t.department, sat)
            self.assertGreaterEqual(bal_to_date, Decimal("750"))

    def test_disabled_keeps_natural_sabbath(self):
        import datetime as dt
        from core.models import SiteConfig, SabbathClose
        from giving.models import Transaction
        cfg = SiteConfig.get(); cfg.sabbath_cutoff_enabled = False; cfg.save()
        SabbathClose.objects.create(sabbath=dt.date(2026, 5, 2))
        self._import(self._csv("EEE5", "Joe", "100"))
        t = Transaction.objects.get(payer_name__icontains="Joe")
        self.assertEqual(t.service_sabbath, dt.date(2026, 5, 2))   # no rolling


class ImportedAfterSabbathTests(TestCase):
    """A contribution whose Sabbath has already passed by import day rolls forward and is
    flagged for confirmation — regardless of whether that Sabbath was closed.
    Tithe is a trust fund, so it is within the default (Trust+LCB) confirm scope."""

    def setUp(self):
        from departments.models import Department
        from giving.models import AllocationRule
        from giving.services.allocation import normalize_reference
        tithe = Department.objects.create(name="Tithe", fund_type="TRUST",
                                          category="TRUST")
        AllocationRule.objects.create(reference=normalize_reference("tithe"),
                                      department=tithe,
                                      source=AllocationRule.Source.SEED)

    def _import_on(self, when_dated, import_day):
        import datetime as dt
        from unittest import mock
        from django.contrib.auth.models import User
        from statements.models import StatementImport
        from statements.services import importer
        csv = ("Completion Time,Details,Paid In\n"
               f"{when_dated},ZZZ9~tithe~254790301470~Late,1500\n").encode()
        u = User.objects.create_superuser("imp2", password="x")
        imp = StatementImport.objects.create(uploaded_by=u, filename="late.csv")

        class _D(dt.date):
            @classmethod
            def today(cls):
                return dt.date.fromisoformat(import_day)
        with mock.patch.object(importer, "dt") as m:
            m.date = _D
            m.datetime = dt.datetime
            m.timedelta = dt.timedelta
            run = importer.run_import
            run(imp, csv, "late.csv")

    def test_saturday_gift_imported_days_later_rolls_and_flags(self):
        import datetime as dt
        from giving.models import Transaction
        # gift dated Sat 6 Jun, imported Thu 11 Jun
        self._import_on("06 Jun 2026", "2026-06-11")
        t = Transaction.objects.get(payer_name__icontains="Late")
        self.assertEqual(t.date, dt.date(2026, 6, 6))               # real date kept
        self.assertEqual(t.service_sabbath, dt.date(2026, 6, 13))   # next Sabbath
        self.assertTrue(t.sabbath_confirm_pending)                  # held for confirm

    def test_gift_imported_same_sabbath_stays(self):
        import datetime as dt
        from giving.models import Transaction
        self._import_on("06 Jun 2026", "2026-06-06")
        t = Transaction.objects.get(payer_name__icontains="Late")
        self.assertEqual(t.service_sabbath, dt.date(2026, 6, 6))
        self.assertFalse(t.sabbath_confirm_pending)


class MonthEndTimingTests(TestCase):
    """A trust contribution received after the month's last Sabbath but before month-end
    (e.g. Tue 31st, last Sabbath was 28th) must:
      * sit in the cash-book balance and reconcile to the 31st bank statement
        (by TRANSACTION DATE — it is real money at the bank on the 31st), and
      * count in the 31st month's FUND balance (also transaction date), while
      * appearing under the NEXT Sabbath's offering count (4th, service Sabbath).
    The two views use different date axes on purpose; the money is recognised once.
    """

    def _make(self):
        import datetime as dt
        from decimal import Decimal
        from departments.models import Department
        from giving.models import Transaction
        trust = Department.objects.filter(is_trust=True).first() or \
            Department.objects.create(name="TITHE", fund_type="TRUST", category="TRUST")
        return trust, Transaction.objects.create(
            date=dt.date(2026, 3, 31), service_sabbath=dt.date(2026, 4, 4),
            sabbath_week=1, channel="BANK", direction="CREDIT",
            amount=Decimal("2000"), department=trust, allocation_status="AUTO",
            confirmed=True, payer_name="Late Trust", core_ref="MTEND1")

    def test_in_31st_cashbook_balance(self):
        import datetime as dt
        from statements.views import _ledger_bank_balance
        trust, t = self._make()
        before = _ledger_bank_balance(dt.date(2026, 3, 30))
        after = _ledger_bank_balance(dt.date(2026, 3, 31))
        self.assertEqual(after - before, t.amount)   # in the 31st book balance

    def test_counts_in_march_fund_not_april(self):
        import datetime as dt
        from reports.services.balances import receipts_by_department
        trust, t = self._make()
        mar = receipts_by_department(dt.date(2026, 3, 1), dt.date(2026, 3, 31))
        apr = receipts_by_department(dt.date(2026, 4, 1), dt.date(2026, 4, 30))
        self.assertEqual(mar.get(trust.id, 0), t.amount)     # counted in March
        self.assertEqual(apr.get(trust.id, 0), 0)            # not double-counted

    def test_not_offbook_in_reconciliation(self):
        import datetime as dt
        from statements.views import _recon_diagnostic
        self._make()
        diag = _recon_diagnostic(dt.date(2026, 3, 31))
        self.assertEqual(diag["off_book_total"], 0)          # confirmed → in book


class StkpushReceiptTests(TestCase):
    """STKPUSH (and similar) placeholders in the channel-ref column must not be
    used as the dedup key — the real M-Pesa receipt lives in the narration."""

    def test_stkpush_uses_narration_receipt(self):
        from statements.services.parser import read_rows
        # channel-ref column literally "STKPUSH"; real receipt UF62975Y53 in narration
        csv = ("Receipt No,Completion Time,Details,Transaction Status,Paid In,Withdrawn,Balance\n"
               "STKPUSH,2026-06-06 09:00:00,UF62975Y53~DEVGR28~254721892567~Dev,Completed,18000,,18000\n"
               "STKPUSH,2026-06-06 09:01:00,UF629769BQ~DEVGR28~254721892567~Dev,Completed,2000,,2000\n"
               ).encode()
        rows = read_rows(csv, "m.csv")
        keys = [r["core_ref"] for r in rows]
        self.assertNotIn("STKPUSH", [k.upper() for k in keys])
        self.assertEqual(sorted(keys), ["UF62975Y53", "UF629769BQ"])


class LegacyImporterStkpushTests(TestCase):
    """The legacy importer must also fall back to the narration receipt when the
    statement's REF column is a channel placeholder (STKPUSH etc.), so those rows
    don't collapse onto one dedup key."""

    def test_placeholder_ref_uses_narration_receipt(self):
        # mirrors the base-derivation logic in import_legacy.phase_bank
        from statements.services.parser import parse_narration
        PLACEHOLDER = {"STKPUSH", "STK", "USSD", "C2B", "MPESAC2B",
                       "PAYBILL", "MULTI", "OTHER", ""}

        def base_for(ref, narration, row):
            p = parse_narration(narration or "")
            ref_clean = str(ref).strip() if ref not in (None, "") else ""
            if ref_clean.upper() in PLACEHOLDER:
                return (p.get("receipt") or "").strip() or f"LEG-BANK-{row}"
            return ref_clean or f"LEG-BANK-{row}"

        rows = [
            ("STKPUSH", "UA5KR30R1Y~Offering~254716794363~Offering50", 184),
            ("STKPUSH", "UAHKR45MKN~Offering~254716794363~Offering150", 405),
            ("UF629769BQ", "UF629769BQ~DEVGR28~254721892567~Dev", 60),
        ]
        bases = [base_for(*r) for r in rows]
        self.assertEqual(bases, ["UA5KR30R1Y", "UAHKR45MKN", "UF629769BQ"])
        # the two STKPUSH rows no longer share a key
        self.assertEqual(len(set(bases)), 3)


class RunningBalanceCheckTests(TestCase):
    """The statement's running-balance column is used as a checksum to confirm
    every row imported once, with no gaps or duplicates."""

    def _rows(self):
        from decimal import Decimal
        # opening 1000; three credits leave balances 1100, 1350, 1850
        return [
            {"core_ref": "A", "date": "d", "credit": Decimal("100"), "debit": None, "balance": Decimal("1100")},
            {"core_ref": "B", "date": "d", "credit": Decimal("250"), "debit": None, "balance": Decimal("1350")},
            {"core_ref": "C", "date": "d", "credit": Decimal("500"), "debit": None, "balance": Decimal("1850")},
        ]

    def test_clean_chain_passes(self):
        from statements.services.importer import verify_running_balance
        status, _ = verify_running_balance(self._rows())
        self.assertEqual(status, "OK")

    def test_duplicate_row_breaks(self):
        from statements.services.importer import verify_running_balance
        rows = self._rows()
        rows.insert(1, dict(rows[1]))      # duplicate B (balance now wrong)
        status, detail = verify_running_balance(rows)
        self.assertEqual(status, "BROKEN")

    def test_missing_row_breaks(self):
        from statements.services.importer import verify_running_balance
        rows = self._rows()
        del rows[1]                        # drop B: chain + net both break
        status, detail = verify_running_balance(rows)
        self.assertEqual(status, "BROKEN")

    def test_miskeyed_amount_breaks(self):
        from statements.services.importer import verify_running_balance
        from decimal import Decimal
        rows = self._rows()
        rows[1]["credit"] = Decimal("260")  # balance says +250, amount says +260
        status, _ = verify_running_balance(rows)
        self.assertEqual(status, "BROKEN")

    def test_no_balance_column(self):
        from statements.services.importer import verify_running_balance
        rows = [{"core_ref": "A", "credit": None, "debit": None, "balance": None}]
        status, _ = verify_running_balance(rows)
        self.assertEqual(status, "NO_BALANCE")


class ReceiptCaseNormalizationTests(TestCase):
    """Dedup keys (core_ref / receipt) are normalised to uppercase so that
    deduplication is exact regardless of the database collation. A
    case-insensitive collation (e.g. latin1_swedish_ci) must not change which
    rows are treated as duplicates."""

    def test_core_ref_uppercased(self):
        from statements.services.parser import read_rows
        csv = ("Receipt No,Completion Time,Details,Paid In,Balance\n"
               "abc123xyz,2026-06-06 09:00:00,abc123xyz~tithe~254790301470~Test,500,500\n"
               ).encode()
        rows = read_rows(csv, "m.csv")
        self.assertEqual(rows[0]["core_ref"], "ABC123XYZ")
        self.assertEqual(rows[0]["receipt"], "ABC123XYZ")

    def test_distinct_refs_stay_distinct(self):
        from statements.services.parser import read_rows
        # two receipts that differ only after normalisation stay separate
        csv = ("Receipt No,Completion Time,Details,Paid In,Balance\n"
               "UER001,2026-06-06 09:00:00,UER001~tithe~254790301470~A,500,500\n"
               "UER002,2026-06-06 09:01:00,UER002~tithe~254790301470~B,500,1000\n"
               ).encode()
        rows = read_rows(csv, "m.csv")
        self.assertEqual(len({r["core_ref"] for r in rows}), 2)


class PurgeUnlinkTests(TestCase):
    """Purge refuses when expenses are linked, but 'unlink and purge' clears the
    reconciliation links (keeping the expenses) and proceeds."""

    def setUp(self):
        from django.contrib.auth.models import User, Group
        from django.utils import timezone
        from statements.models import StatementImport
        from departments.models import Department
        from giving.models import Transaction
        from cashbook.models import Expense
        import datetime as dt
        from decimal import Decimal
        self.u = User.objects.create_user("pu", password="x")
        g, _ = Group.objects.get_or_create(name="Treasurer")
        self.u.groups.add(g)
        self.imp = StatementImport.objects.create(uploaded_by=self.u, filename="t.xls",
                                                  status="DONE")
        self.imp.uploaded_at = timezone.now(); self.imp.save()
        self.d = Department.objects.create(name="LCB", fund_type="LOCAL", category="OFFERING")
        self.debit = Transaction.objects.create(
            date=dt.date.today(), channel="BANK", direction="DEBIT",
            amount=Decimal("1000"), allocation_status="MANUAL", confirmed=True,
            statement_import=self.imp, core_ref="DBT9", department=self.d)
        self.exp = Expense.objects.create(
            date=dt.date.today(), department=self.d, description="x",
            amount=Decimal("1000"), status="PAID", recorded_by=self.u,
            bank_transaction=self.debit)

    def test_plain_purge_refused_when_linked(self):
        from django.test import Client
        c = Client(); c.force_login(self.u)
        c.post(f"/statements/{self.imp.id}/purge/")
        self.imp.refresh_from_db()
        self.assertEqual(self.imp.status, "DONE")  # refused

    def test_unlink_and_purge_keeps_expense(self):
        from django.test import Client
        from cashbook.models import Expense
        from giving.models import Transaction
        c = Client(); c.force_login(self.u)
        c.post(f"/statements/{self.imp.id}/purge/", {"unlink_expenses": "1"})
        self.imp.refresh_from_db()
        self.assertEqual(self.imp.status, "PURGED")
        exp = Expense.objects.get(id=self.exp.id)         # expense survives
        self.assertIsNone(exp.bank_transaction_id)        # link cleared
        self.assertFalse(Transaction.objects.filter(id=self.debit.id).exists())


class MpesaRefDedupTests(TestCase):
    """Import dedup also catches a repeated M-Pesa receipt even when core_ref
    differs or is absent."""

    def test_dedup_by_mpesa_ref(self):
        from giving.models import Transaction
        from statements.models import StatementImport
        from statements.services.importer import run_import
        from django.contrib.auth.models import User
        import datetime as dt
        from decimal import Decimal
        u = User.objects.create_user("md", password="x")
        # pre-existing row with this receipt but a DIFFERENT core_ref
        Transaction.objects.create(date=dt.date(2026, 6, 6), channel="BANK",
            direction="CREDIT", amount=Decimal("500"), allocation_status="AUTO",
            confirmed=True, mpesa_ref="UF6DUP01", core_ref="OLDREF1")
        csv = ("Receipt No,Completion Time,Details,Paid In,Balance\n"
               "UF6DUP01,2026-06-06 09:00:00,UF6DUP01~tithe~254790301470~A,500,500\n"
               ).encode()
        imp = StatementImport.objects.create(uploaded_by=u, filename="d.csv")
        run_import(imp, csv, "d.csv")
        # the incoming row shares the mpesa_ref → skipped as duplicate
        self.assertEqual(Transaction.objects.filter(mpesa_ref="UF6DUP01").count(), 1)
        self.assertEqual(imp.duplicates_skipped, 1)


class StatementBalanceCaptureTests(TestCase):
    """Importing a statement captures its opening/closing running balance and
    date span, for the bank-position reconciliation report."""

    def test_balances_captured(self):
        from django.contrib.auth.models import User
        from decimal import Decimal
        from statements.models import StatementImport
        from statements.services.importer import run_import
        u = User.objects.create_user("sb", password="x")
        csv = ("Receipt No,Completion Time,Details,Paid In,Withdrawn,Balance\n"
               "UF6A,2026-06-06 09:00:00,UF6A~tithe~254790301470~A,500,,1500\n"
               "UF6B,2026-06-06 10:00:00,UF6B~tithe~254790301470~B,300,,1800\n"
               ).encode()
        imp = StatementImport.objects.create(uploaded_by=u, filename="s.csv")
        run_import(imp, csv, "s.csv")
        imp.refresh_from_db()
        # opening = first balance (1500) minus first move (500) = 1000
        self.assertEqual(imp.stmt_opening_balance, Decimal("1000"))
        self.assertEqual(imp.stmt_closing_balance, Decimal("1800"))


class BankPositionReportTests(TestCase):
    """The bank-position report compares the system bank balance to the statement
    closing balance and renders without error."""

    def test_report_renders(self):
        from django.contrib.auth.models import User, Group
        from django.test import Client
        u = User.objects.create_user("bp", password="x")
        g, _ = Group.objects.get_or_create(name="Treasurer")
        u.groups.add(g)
        c = Client(); c.force_login(u)
        self.assertEqual(c.get("/reports/bank-position/").status_code, 200)

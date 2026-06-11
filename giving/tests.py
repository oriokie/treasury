from decimal import Decimal

from django.test import TestCase

from departments.models import Department, DevelopmentGroup
from giving.models import AllocationRule, Transaction
from giving.services.allocation import normalize_reference, allocate


class NormalizeReferenceTests(TestCase):
    def test_lowercases_and_strips_spaces(self):
        self.assertEqual(normalize_reference("  Sabbath School "), "sabbathschool")

    def test_handles_none(self):
        self.assertEqual(normalize_reference(None), "")


class AllocationEngineTests(TestCase):
    def setUp(self):
        self.tithe = Department.objects.create(
            name="Tithe", fund_type=Department.FundType.TRUST)
        AllocationRule.objects.create(
            reference="tithe", department=self.tithe,
            source=AllocationRule.Source.SEED)

    def test_fund_type_sets_is_trust(self):
        self.assertTrue(self.tithe.is_trust)

    def test_seeded_rule_resolves_auto(self):
        dept, status = allocate("tithe")
        self.assertEqual(dept, self.tithe)
        self.assertEqual(status, "AUTO")

    def test_unknown_reference_goes_to_review(self):
        resolver, status = allocate("something-unmapped")
        self.assertEqual(resolver, "UNALLOCATED")
        self.assertEqual(status, "REVIEW")

    def test_dev_group_reference_autodetected(self):
        resolver, status = allocate("Grp12dev")
        self.assertEqual(resolver, "DEV_GROUP_12")
        self.assertEqual(status, "AUTO")

    def test_messy_dev_references_resolve(self):
        # spellings seen in the real statement
        for ref, n in [("DEVGR7", 7), ("devg14", 14), ("DEVLOP GP14", 14),
                       ("dev grp5", 5), ("DEv Gp39", 39), ("DEVGRP3*", 3),
                       (" DEVGR26", 26), ("Devgrp11", 11)]:
            resolver, status = allocate(ref)
            self.assertEqual(resolver, f"DEV_GROUP_{n}", ref)
            self.assertEqual(status, "AUTO", ref)

    def test_dev_without_number_is_na(self):
        resolver, status = allocate("dev")
        self.assertEqual(resolver, "DEV_GROUP_NA")

    def test_resolver_token_maps_to_fund_and_group(self):
        from statements.services.importer import _resolve
        resolver, _ = allocate("Grp12dev")
        dept, grp = _resolve(resolver)
        self.assertEqual(dept.name.upper(), "DEVELOPMENT")
        self.assertEqual(grp.number, 12)
        self.assertTrue(DevelopmentGroup.objects.filter(number=12).exists())


class TransactionDedupTests(TestCase):
    def test_core_ref_is_unique(self):
        dept = Department.objects.create(name="Combined", fund_type=Department.FundType.LOCAL)
        Transaction.objects.create(
            date="2026-06-06", channel=Transaction.Channel.BANK,
            direction=Transaction.Direction.CREDIT, amount=Decimal("100"),
            department=dept, core_ref="UNIQUE123", allocation_status="AUTO")
        from django.db import IntegrityError, transaction
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                Transaction.objects.create(
                    date="2026-06-06", channel=Transaction.Channel.BANK,
                    direction=Transaction.Direction.CREDIT, amount=Decimal("100"),
                    department=dept, core_ref="UNIQUE123", allocation_status="AUTO")


class SplitFundTests(TestCase):
    def setUp(self):
        from giving.models import SplitFund, SplitComponent
        trust = Department.objects.create(name="Combined Trust",
                                          fund_type=Department.FundType.TRUST)
        local = Department.objects.create(name="Combined Local",
                                          fund_type=Department.FundType.LOCAL)
        self.sf = SplitFund.objects.create(name="Combined Offering")
        SplitComponent.objects.create(split_fund=self.sf, department=trust, percent=50)
        SplitComponent.objects.create(split_fund=self.sf, department=local, percent=50)

    def test_split_sums_exactly_with_rounding(self):
        parts = self.sf.split(Decimal("1001"))
        self.assertEqual(sum(a for _, a in parts), Decimal("1001"))
        self.assertEqual(parts[0][1], Decimal("500.50"))

    def test_allocate_returns_split_fund(self):
        from giving.models import AllocationRule
        AllocationRule.objects.create(reference="combined", split_fund=self.sf,
                                      source=AllocationRule.Source.SEED)
        resolver, status = allocate("combined")
        self.assertEqual(resolver, self.sf)
        self.assertEqual(status, "AUTO")


class DebitMultiMatchTests(TestCase):
    def setUp(self):
        import datetime as dt
        from decimal import Decimal
        from django.contrib.auth.models import User
        from departments.models import Department
        from giving.models import Transaction
        from cashbook.models import Expense
        self.u = User.objects.create_superuser("dm", password="x")
        self.dept = Department.objects.create(name="Tithe", fund_type=Department.FundType.TRUST)
        self.e1 = Expense.objects.create(date=dt.date(2026, 5, 10), department=self.dept,
            description="A", amount=Decimal("300"), status=Expense.Status.APPROVED, recorded_by=self.u)
        self.e2 = Expense.objects.create(date=dt.date(2026, 5, 10), department=self.dept,
            description="B", amount=Decimal("700"), status=Expense.Status.APPROVED, recorded_by=self.u)
        self.deb = Transaction.objects.create(date=dt.date(2026, 5, 10), channel="BANK",
            direction="DEBIT", amount=Decimal("1000"), allocation_status="REVIEW")
        self.client.login(username="dm", password="x")

    def test_multi_match_requires_equal_total(self):
        from django.urls import reverse
        # mismatch is rejected
        self.client.post(reverse("debit_resolve", args=[self.deb.id]),
                         {"kind": "match", "expense": [self.e1.id]})
        self.e1.refresh_from_db()
        self.assertIsNone(self.e1.bank_transaction_id)
        # matching total links both
        self.client.post(reverse("debit_resolve", args=[self.deb.id]),
                         {"kind": "match", "expense": [self.e1.id, self.e2.id]})
        self.e1.refresh_from_db(); self.e2.refresh_from_db()
        self.assertEqual(self.e1.bank_transaction_id, self.deb.id)
        self.assertEqual(self.e2.bank_transaction_id, self.deb.id)


class QueueImportTests(TestCase):
    def test_import_allocates(self):
        import datetime as dt
        from decimal import Decimal
        from django.contrib.auth.models import User
        from django.urls import reverse
        from django.core.files.uploadedfile import SimpleUploadedFile
        from departments.models import Department
        from giving.models import Transaction
        User.objects.create_superuser("qi", password="x")
        d = Department.objects.create(name="Tithe", fund_type=Department.FundType.TRUST)
        t = Transaction.objects.create(date=dt.date(2026, 5, 1), channel="BANK",
            direction="CREDIT", amount=Decimal("100"), allocation_status="REVIEW")
        self.client.login(username="qi", password="x")
        csv = f"id,date,amount,payer_name,payer_phone,reference,narration,allocate_to_fund\n{t.id},2026-05-01,100,X,,r,n,Tithe\n"
        self.client.post(reverse("queue_import"),
                         {"file": SimpleUploadedFile("q.csv", csv.encode())})
        t.refresh_from_db()
        self.assertEqual(t.allocation_status, "MANUAL")
        self.assertEqual(t.department_id, d.id)


class RuleMatchTypeTests(TestCase):
    def setUp(self):
        from departments.models import Department
        from giving.models import AllocationRule
        self.d = Department.objects.create(name="YOUTH", fund_type=Department.FundType.LOCAL)
        AllocationRule.objects.create(reference="potluck", department=self.d,
                                      match_type="CONTAINS", source="SEED")
        AllocationRule.objects.create(reference="yth", department=self.d,
                                      match_type="STARTS", source="SEED")

    def test_contains_and_starts(self):
        from giving.services.allocation import allocate
        self.assertEqual(allocate("abc potluck 99")[0], self.d)
        self.assertEqual(allocate("yth camp")[0], self.d)

    def test_exact_still_wins_review_otherwise(self):
        from giving.services.allocation import allocate
        target, status = allocate("totally-unknown-xyz")
        self.assertEqual(target, "UNALLOCATED")


class DeleteEntryTests(TestCase):
    def setUp(self):
        import datetime as dt
        from decimal import Decimal
        from django.contrib.auth.models import User, Group
        from departments.models import Department
        from giving.models import Transaction
        self.tre = User.objects.create_superuser("tre", password="x")
        self.asst = User.objects.create_user("asst", password="x")
        Group.objects.get_or_create(name="Assistant")[0].user_set.add(self.asst)
        d = Department.objects.create(name="Tithe", fund_type=Department.FundType.TRUST)
        self.t = Transaction.objects.create(date=dt.date(2026, 5, 1), channel="CASH",
            direction="CREDIT", amount=Decimal("100"), department=d, allocation_status="MANUAL")

    def test_treasurer_can_reverse(self):
        from django.urls import reverse
        from giving.models import Transaction
        self.client.login(username="tre", password="x")
        self.client.post(reverse("transaction_reverse", args=[self.t.id]))
        self.t.refresh_from_db()
        self.assertTrue(self.t.is_reversed)
        self.assertTrue(Transaction.objects.filter(id=self.t.id).exists())  # not deleted

    def test_assistant_cannot_reverse(self):
        from django.urls import reverse
        self.client.login(username="asst", password="x")
        self.client.post(reverse("transaction_reverse", args=[self.t.id]))
        self.t.refresh_from_db()
        self.assertFalse(self.t.is_reversed)


class ReversalTests(TestCase):
    def setUp(self):
        import datetime as dt
        from decimal import Decimal
        from django.contrib.auth.models import User
        from departments.models import Department
        from giving.models import Transaction
        self.u = User.objects.create_superuser("rv", password="x")
        d = Department.objects.create(name="Choir", fund_type=Department.FundType.LOCAL)
        self.t = Transaction.objects.create(date=dt.date(2026, 5, 1), channel="CASH",
            direction="CREDIT", amount=Decimal("300"), department=d, allocation_status="MANUAL")
        self.client.login(username="rv", password="x")

    def test_reverse_creates_contra_and_keeps_original(self):
        from django.urls import reverse
        from giving.models import Transaction, TransactionReversal
        self.client.post(reverse("transaction_reverse", args=[self.t.id]), {"reason": "dup"})
        self.t.refresh_from_db()
        self.assertTrue(self.t.is_reversed)
        self.assertTrue(Transaction.objects.filter(id=self.t.id).exists())  # never deleted
        contra = Transaction.objects.get(is_reversal=True)
        self.assertEqual(contra.amount, self.t.amount)   # positive (validator-safe)
        self.assertGreater(contra.amount, 0)
        self.assertTrue(TransactionReversal.objects.filter(original=self.t).exists())

    def test_cannot_reverse_twice(self):
        self.t.reverse(self.u)
        with self.assertRaises(ValueError):
            self.t.reverse(self.u)


class PeriodLockTests(TestCase):
    def test_lock_blocks_non_admin_create(self):
        import datetime as dt
        from django.contrib.auth.models import User, Group
        from django.urls import reverse
        from departments.models import Department
        from cashbook.models import Expense
        from core.models import PeriodLock
        admin = User.objects.create_superuser("adm", password="x")
        clerk = User.objects.create_user("clk", password="x")
        Group.objects.get_or_create(name="Assistant")[0].user_set.add(clerk)
        d = Department.objects.create(name="Choir", fund_type=Department.FundType.LOCAL)
        PeriodLock.objects.create(year=2026, month=5, locked_by=admin)
        self.client.login(username="clk", password="x")
        n = Expense.objects.count()
        self.client.post(reverse("expense_create"), {"date": "2026-05-10",
            "department": d.id, "description": "x", "amount": "20",
            "category": "OTHER", "claimant": "A", "method": "CASH", "voucher_no": ""})
        self.assertEqual(Expense.objects.count(), n)  # blocked


class TransactionDateFilterTests(TestCase):
    def setUp(self):
        import datetime as dt
        from django.contrib.auth.models import User
        from departments.models import Department
        from giving.models import Transaction
        self.u = User.objects.create_superuser("tf", password="x")
        self.fund = Department.objects.create(name="LCB", fund_type=Department.FundType.LOCAL)
        for d in [dt.date(2026, 1, 5), dt.date(2026, 3, 5), dt.date(2026, 6, 5)]:
            Transaction.objects.create(date=d, channel="CASH", direction="CREDIT",
                amount=100, department=self.fund, allocation_status="AUTO")
        self.client.login(username="tf", password="x")

    def test_date_range_filters(self):
        from django.urls import reverse
        url = reverse("transaction_list")
        r = self.client.get(url, {"date_from": "2026-02-01", "date_to": "2026-04-30"})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(len(r.context["transactions"]), 1)  # only the March one
        r2 = self.client.get(url, {"date_from": "2026-01-01"})
        self.assertEqual(len(r2.context["transactions"]), 3)


class CashDuplicateTests(TestCase):
    def setUp(self):
        from django.contrib.auth.models import User
        from departments.models import Department
        self.u = User.objects.create_superuser("cd", password="x")
        self.fund = Department.objects.create(name="LCB", fund_type=Department.FundType.LOCAL)
        self.client.login(username="cd", password="x")

    def test_duplicate_requires_confirmation(self):
        import datetime as dt
        from decimal import Decimal
        from django.urls import reverse
        from giving.models import Transaction
        Transaction.objects.create(date=dt.date(2026, 5, 2), channel="CASH",
            direction="CREDIT", amount=Decimal("2000"), department=self.fund,
            allocation_status="MANUAL", payer_name="Mary Wanjiku")
        # second entry with same amount AND a similar name -> blocked without confirm
        self.client.post(reverse("cash_new"), {
            "date": "2026-05-02", "department": self.fund.id, "amount": "2000",
            "channel": "CASH", "reference": "", "payer_name": "Wanjiku Mary"})
        self.assertEqual(Transaction.objects.filter(amount=Decimal("2000")).count(), 1)
        # with confirmation -> saved
        self.client.post(reverse("cash_new"), {
            "date": "2026-05-02", "department": self.fund.id, "amount": "2000",
            "channel": "CASH", "reference": "", "payer_name": "Wanjiku Mary",
            "confirm_duplicate": "1"})
        self.assertEqual(Transaction.objects.filter(amount=Decimal("2000")).count(), 2)

    def test_negative_amount_rejected_by_model(self):
        import datetime as dt
        from decimal import Decimal
        from django.core.exceptions import ValidationError
        from giving.models import Transaction
        t = Transaction(date=dt.date(2026, 5, 2), channel="CASH", direction="CREDIT",
                        amount=Decimal("-5"), department=self.fund, allocation_status="MANUAL")
        with self.assertRaises(ValidationError):
            t.full_clean()


class ReviewVsDebitQueueTests(TestCase):
    def setUp(self):
        import datetime as dt
        from decimal import Decimal
        from django.contrib.auth.models import User
        from departments.models import Department
        from giving.models import Transaction
        self.u = User.objects.create_superuser("rq", password="x")
        self.fund = Department.objects.create(name="LCB", fund_type=Department.FundType.LOCAL)
        # a credit awaiting allocation -> review queue
        self.credit = Transaction.objects.create(date=dt.date(2026, 5, 2),
            channel="BANK", direction="CREDIT", amount=Decimal("500"),
            allocation_status="REVIEW", raw_narration="unmatched giving")
        # a bank debit awaiting classification -> debit queue, NOT review queue
        self.debit = Transaction.objects.create(date=dt.date(2026, 5, 3),
            channel="BANK", direction="DEBIT", amount=Decimal("900"),
            allocation_status="REVIEW", raw_narration="LEDGER FEE")
        self.client.login(username="rq", password="x")

    def test_debit_not_in_review_queue(self):
        from django.urls import reverse
        ids = [t.id for t in self.client.get(reverse("queue")).context["items"]]
        self.assertIn(self.credit.id, ids)
        self.assertNotIn(self.debit.id, ids)

    def test_debit_in_debit_queue(self):
        from django.urls import reverse
        ids = [t.id for t in self.client.get(reverse("debit_queue")).context["debits"]]
        self.assertIn(self.debit.id, ids)
        self.assertNotIn(self.credit.id, ids)


class BulkReceiptsTests(TestCase):
    def setUp(self):
        import datetime as dt
        from decimal import Decimal
        from django.contrib.auth.models import User
        from departments.models import Department
        from envelopes.models import Envelope, EnvelopeLine
        self.u = User.objects.create_superuser("br", password="x")
        d = Department.objects.create(name="Tithe", fund_type=Department.FundType.TRUST)
        sab = dt.date(2026, 5, 2)  # a Saturday
        for i in range(3):
            e = Envelope.objects.create(receipt_no=f"R-{i}", contributor_name=f"Giver {i}",
                date=sab, total=Decimal("1000"), recorded_by=self.u)
            EnvelopeLine.objects.create(envelope=e, department=d, amount=Decimal("1000"))
        self.sab = sab
        self.client.login(username="br", password="x")

    def test_bulk_a4_lists_all_receipts(self):
        from django.urls import reverse
        r = self.client.get(reverse("envelope_receipts_bulk") + f"?date={self.sab.isoformat()}")
        self.assertEqual(r.status_code, 200)
        body = r.content.decode()
        self.assertEqual(body.count('class="rcpt"'), 3)
        self.assertIn("a4-grid", body)

    def test_bulk_etr_format(self):
        from django.urls import reverse
        r = self.client.get(reverse("envelope_receipts_bulk")
                            + f"?date={self.sab.isoformat()}&format=etr")
        self.assertEqual(r.status_code, 200)
        self.assertIn('class="etr"', r.content.decode())


class AllocationPeriodTests(TestCase):
    def setUp(self):
        from departments.models import Department
        from giving.models import AllocationRule
        self.permanent = Department.objects.create(name="General", fund_type="LOCAL")
        self.camp = Department.objects.create(name="Camp Meeting", fund_type="LOCAL")
        import datetime as dt
        AllocationRule.objects.create(reference="campoffer", department=self.permanent, source="SEED")
        AllocationRule.objects.create(reference="campoffer", department=self.camp, source="SEED",
            valid_from=dt.date(2026, 12, 1), valid_to=dt.date(2026, 12, 31))

    def test_period_rule_supersedes_permanent_in_range(self):
        import datetime as dt
        from giving.services.allocation import allocate
        r, _ = allocate("campoffer", dt.date(2026, 12, 15))
        self.assertEqual(r, self.camp)

    def test_permanent_used_outside_period(self):
        import datetime as dt
        from giving.services.allocation import allocate
        r, _ = allocate("campoffer", dt.date(2026, 6, 1))
        self.assertEqual(r, self.permanent)


class CashDuplicateNameTests(TestCase):
    def setUp(self):
        import datetime as dt
        from decimal import Decimal
        from departments.models import Department
        from giving.models import Transaction
        self.fund = Department.objects.create(name="LCB", fund_type="LOCAL")
        Transaction.objects.create(date=dt.date(2026, 5, 2), channel="CASH",
            direction="CREDIT", amount=Decimal("500"), department=self.fund,
            allocation_status="MANUAL", payer_name="Ruth Momanyi")

    def test_same_amount_different_name_not_duplicate(self):
        import datetime as dt
        from decimal import Decimal
        from giving.views import _cash_duplicate
        self.assertFalse(_cash_duplicate(dt.date(2026, 5, 2), self.fund,
                                         Decimal("500"), "John Otieno"))

    def test_same_amount_similar_name_is_duplicate(self):
        import datetime as dt
        from decimal import Decimal
        from giving.views import _cash_duplicate
        self.assertTrue(_cash_duplicate(dt.date(2026, 5, 2), self.fund,
                                        Decimal("500"), "Momanyi Ruth"))


class ImportConfirmationTests(TestCase):
    def setUp(self):
        import datetime as dt
        from decimal import Decimal
        from django.contrib.auth.models import User
        from departments.models import Department
        from giving.models import Transaction
        from statements.models import StatementImport
        self.u = User.objects.create_superuser("ic", password="x")
        self.fund = Department.objects.create(name="LCB", fund_type="LOCAL")
        self.imp = StatementImport.objects.create(uploaded_by=self.u, filename="x.csv")
        self.t = Transaction.objects.create(date=dt.date(2026, 5, 2), channel="BANK",
            direction="CREDIT", amount=Decimal("1000"), department=self.fund,
            allocation_status="AUTO", confirmed=False, statement_import=self.imp)
        self.client.login(username="ic", password="x")

    def test_unconfirmed_excluded_from_balances(self):
        from decimal import Decimal
        from reports.services import balances
        rows = {r["department"].id: r["closing"]
                for r in balances.department_summary(None, None, consolidated=False)}
        self.assertEqual(rows.get(self.fund.id, Decimal(0)), Decimal(0))

    def test_confirm_makes_it_count(self):
        from decimal import Decimal
        from django.urls import reverse
        from reports.services import balances
        self.client.post(reverse("statement_auto_review", args=[self.imp.id]),
                         {"confirm_all": "1", f"dept_{self.t.id}": str(self.fund.id)})
        self.t.refresh_from_db()
        self.assertTrue(self.t.confirmed)
        rows = {r["department"].id: r["closing"]
                for r in balances.department_summary(None, None, consolidated=False)}
        self.assertEqual(rows.get(self.fund.id, Decimal(0)), Decimal("1000"))

    def test_excel_export(self):
        from django.urls import reverse
        r = self.client.get(reverse("statement_auto_excel", args=[self.imp.id]))
        self.assertEqual(r.status_code, 200)
        self.assertIn("spreadsheet", r["Content-Type"])


class RuleDeleteAndConfigTests(TestCase):
    def setUp(self):
        from django.contrib.auth.models import User, Group
        from core.roles import TREASURER
        from departments.models import Department
        from giving.models import AllocationRule
        u = User.objects.create_user("rd", password="x")
        u.groups.add(Group.objects.get_or_create(name=TREASURER)[0])
        self.client.login(username="rd", password="x")
        self.fund = Department.objects.create(name="LCB", fund_type="LOCAL")
        self.rule = AllocationRule.objects.create(reference="tithe", department=self.fund,
                                                  source="SEED")

    def test_rule_delete_redirects_and_removes(self):
        from django.urls import reverse
        from giving.models import AllocationRule
        r = self.client.post(reverse("rule_delete", args=[self.rule.id]))
        self.assertEqual(r.status_code, 302)
        self.assertFalse(AllocationRule.objects.filter(id=self.rule.id).exists())

    def test_configurable_dev_prefix(self):
        import datetime as dt
        from core.models import SiteConfig
        from giving.services.allocation import allocate
        cfg = SiteConfig.get(); cfg.dev_group_extra_prefixes = "project, phase"; cfg.save()
        r, status = allocate("project7", dt.date(2026, 5, 2))
        self.assertEqual(r, "DEV_GROUP_7")


class ExpectedCashTests(TestCase):
    def test_expected_cash_includes_envelopes_excludes_bank_less_disbursed(self):
        import datetime as dt
        from decimal import Decimal
        from django.contrib.auth.models import User
        from departments.models import Department
        from giving.models import Transaction
        from cashbook.models import Expense
        from core.utils import sabbath_of
        from envelopes.views import CountSessionCreate
        u = User.objects.create_superuser("ec", password="x")
        fund = Department.objects.create(name="Loose", fund_type="LOCAL")
        sab = sabbath_of(dt.date(2026, 5, 2))
        Transaction.objects.create(date=sab, channel="CASH", direction="CREDIT",
            amount=Decimal("1000"), department=fund, allocation_status="MANUAL", confirmed=True)
        Transaction.objects.create(date=sab, channel="ENVELOPE", direction="CREDIT",
            amount=Decimal("2000"), department=fund, allocation_status="MANUAL", confirmed=True)
        Transaction.objects.create(date=sab, channel="BANK", direction="CREDIT",
            amount=Decimal("5000"), department=fund, allocation_status="MANUAL", confirmed=True)
        Expense.objects.create(date=sab, department=fund, description="Cash out",
            amount=Decimal("300"), category="OTHER", method="CASH", status="PAID",
            recorded_by=u, approved_by=u)
        self.assertEqual(CountSessionCreate()._expected(sab), Decimal("2700"))

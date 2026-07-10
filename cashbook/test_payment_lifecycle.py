"""Payment instrument lifecycle & reconciliation tests — the Part 15 matrix:
full cheque lifecycle with audited events; THE cleared-date reconciliation
case (issued 5 Jul, cleared 19 Jul, reconciliation dated 10 Jul); cancelled +
re-issued cheques; EFT covering several vouchers; loan repayments by cheque;
debit-queue integration (auto-clear, one-click clear, no duplicates);
immutability of cleared instruments; accounting invariance (instruments never
post); granular permissions and leader scoping; legacy-row fallbacks.
"""
import datetime as dt
from decimal import Decimal

from django.contrib.auth.models import Group, User
from django.core.exceptions import ValidationError
from django.test import TestCase
from django.urls import reverse

from cashbook.models import Expense, PaymentEvent, PaymentInstrument
from cashbook.services.payments import (apply_event, clear_for_bank_debit,
                                        reissue, suggest_instrument_for_debit)
from cashbook.views import unpresented_cheques_total
from core.roles import ASSISTANT, AUDITOR, LEADER, TREASURER
from departments.models import Department, DepartmentLeadership
from giving.models import Transaction
from ledger.models import JournalEntry
from ledger.services import posting


def _user(name, role):
    u = User.objects.create_user(name, password="x")
    u.groups.add(Group.objects.get_or_create(name=role)[0])
    return u


def _expense(dept, user, amount="5000", **kw):
    return Expense.objects.create(
        date=kw.pop("date", dt.date(2026, 7, 1)), department=dept,
        description=kw.pop("description", "supplies"), amount=Decimal(amount),
        category=kw.pop("category", Expense.Category.OTHER),
        method=Expense.Method.CHEQUE,
        status=kw.pop("status", Expense.Status.APPROVED),
        recorded_by=user, approved_by=user, **kw)


class LifecycleTests(TestCase):
    def setUp(self):
        self.tr = _user("pl_tr", TREASURER)
        self.dept = Department.objects.create(name="Development", fund_type="LOCAL")
        self.exp = _expense(self.dept, self.tr)
        self.inst = PaymentInstrument.objects.create(
            method="CHEQUE", instrument_number="000101", payee="ACME LTD",
            amount=Decimal("5000"), source_kind="EXPENSE", expense=self.exp,
            recorded_by=self.tr, status="DRAFT")

    def test_full_lifecycle_with_audited_events(self):
        apply_event(self.inst, "APPROVE", self.tr)
        apply_event(self.inst, "PREPARE", self.tr, on=dt.date(2026, 7, 2))
        apply_event(self.inst, "ISSUE", self.tr, on=dt.date(2026, 7, 5))
        apply_event(self.inst, "PRESENT", self.tr, on=dt.date(2026, 7, 15))
        apply_event(self.inst, "CLEAR", self.tr, on=dt.date(2026, 7, 19),
                    comment="July statement line 42")
        self.inst.refresh_from_db()
        self.assertEqual(self.inst.status, "CLEARED")
        # each event kept its own business date — none overwritten
        self.assertEqual(self.inst.date_prepared, dt.date(2026, 7, 2))
        self.assertEqual(self.inst.date_issued, dt.date(2026, 7, 5))
        self.assertEqual(self.inst.date_presented, dt.date(2026, 7, 15))
        self.assertEqual(self.inst.date_cleared, dt.date(2026, 7, 19))
        self.assertEqual(self.inst.clearance_days, 14)
        # the timeline: user, transition, comment all audited
        evs = list(self.inst.events.values_list("event", "from_status", "to_status"))
        self.assertEqual([e[0] for e in evs],
                         ["APPROVE", "PREPARE", "ISSUE", "PRESENT", "CLEAR"])
        self.assertEqual(evs[-1][1:], ("PRESENTED", "CLEARED"))
        last = self.inst.events.last()
        self.assertEqual(last.user, self.tr)
        self.assertEqual(last.comment, "July statement line 42")

    def test_cleared_is_immutable_except_reverse(self):
        apply_event(self.inst, "ISSUE", self.tr, on=dt.date(2026, 7, 5))
        apply_event(self.inst, "CLEAR", self.tr, on=dt.date(2026, 7, 9))
        for verb in ("ISSUE", "CANCEL", "VOID", "APPROVE"):
            with self.assertRaises(ValidationError):
                apply_event(self.inst, verb, self.tr)
        apply_event(self.inst, "REVERSE", self.tr, on=dt.date(2026, 7, 20),
                    comment="Bank recalled the payment")
        self.inst.refresh_from_db()
        self.assertEqual(self.inst.status, "REVERSED")
        self.assertEqual(self.inst.date_reversed, dt.date(2026, 7, 20))

    def test_clear_before_issue_refused(self):
        apply_event(self.inst, "ISSUE", self.tr, on=dt.date(2026, 7, 5))
        with self.assertRaises(ValidationError):
            apply_event(self.inst, "CLEAR", self.tr, on=dt.date(2026, 7, 1))
        with self.assertRaises(ValidationError):        # never issued
            fresh = PaymentInstrument.objects.create(
                method="EFT", amount=Decimal("10"), source_kind="MANUAL",
                recorded_by=self.tr)
            apply_event(fresh, "CLEAR", self.tr)

    def test_cancel_and_reissue_flow(self):
        apply_event(self.inst, "ISSUE", self.tr, on=dt.date(2026, 7, 5))
        copy = reissue(self.inst, self.tr, number="000102",
                       comment="Cheque lost in the post")
        self.inst.refresh_from_db()
        self.assertEqual(self.inst.status, "CANCELLED")
        self.assertEqual(self.inst.date_cancelled, dt.date.today())
        self.assertEqual(copy.status, "DRAFT")
        self.assertEqual(copy.instrument_number, "000102")
        self.assertEqual(copy.expense_id, self.exp.pk)
        self.assertEqual(copy.amount, self.inst.amount)
        # both sides audited
        self.assertTrue(self.inst.events.filter(event="CANCEL").exists())
        self.assertTrue(copy.events.filter(event="REISSUE").exists())

    def test_reissue_of_cleared_refused(self):
        apply_event(self.inst, "ISSUE", self.tr, on=dt.date(2026, 7, 5))
        apply_event(self.inst, "CLEAR", self.tr, on=dt.date(2026, 7, 9))
        with self.assertRaises(ValidationError):
            reissue(self.inst, self.tr)


class ReconciliationDateTests(TestCase):
    """THE core fix: outstanding-as-at is judged on event dates, never today's
    status."""

    def setUp(self):
        self.tr = _user("rd_tr", TREASURER)
        self.dept = Department.objects.create(name="Development", fund_type="LOCAL")

    def _cheque(self, number, amount, issued, cleared=None):
        exp = _expense(self.dept, self.tr, amount)
        inst = PaymentInstrument.objects.create(
            method="CHEQUE", instrument_number=number, payee="P",
            amount=Decimal(amount), source_kind="EXPENSE", expense=exp,
            recorded_by=self.tr, status="DRAFT")
        apply_event(inst, "ISSUE", self.tr, on=issued)
        if cleared:
            apply_event(inst, "CLEAR", self.tr, on=cleared)
        return inst

    def test_spec_example_issued_jul5_cleared_jul19(self):
        """Issued 5 Jul, cleared 19 Jul: outstanding on a 10 Jul
        reconciliation, cleared on a 31 Jul one — regardless of when the
        reconciliation is run (the instrument's status is CLEARED today)."""
        inst = self._cheque("000201", "8000",
                            dt.date(2026, 7, 5), dt.date(2026, 7, 19))
        self.assertEqual(inst.status, "CLEARED")           # today's status
        out_jul10 = PaymentInstrument.outstanding_asof(dt.date(2026, 7, 10))
        self.assertIn(inst, out_jul10)                     # but outstanding then
        out_jul31 = PaymentInstrument.outstanding_asof(dt.date(2026, 7, 31))
        self.assertNotIn(inst, out_jul31)
        # and the reconciliation total moves with it
        self.assertEqual(unpresented_cheques_total(dt.date(2026, 7, 10)),
                         Decimal("8000"))
        self.assertEqual(unpresented_cheques_total(dt.date(2026, 7, 31)),
                         Decimal(0))

    def test_not_outstanding_before_issue(self):
        self._cheque("000202", "100", dt.date(2026, 7, 5))
        self.assertEqual(unpresented_cheques_total(dt.date(2026, 7, 4)),
                         Decimal(0))

    def test_cancelled_cheque_respects_cancellation_date(self):
        inst = self._cheque("000203", "500", dt.date(2026, 7, 1))
        apply_event(inst, "CANCEL", self.tr, on=dt.date(2026, 7, 20))
        # outstanding at 10 Jul (cancelled later), gone at 31 Jul
        self.assertIn(inst, PaymentInstrument.outstanding_asof(dt.date(2026, 7, 10)))
        self.assertNotIn(inst, PaymentInstrument.outstanding_asof(dt.date(2026, 7, 31)))

    def test_eft_and_mpesa_now_count_as_unpresented(self):
        exp = _expense(self.dept, self.tr, "3000")
        inst = PaymentInstrument.objects.create(
            method="EFT", instrument_number="EFT-9", amount=Decimal("3000"),
            source_kind="EXPENSE", expense=exp, recorded_by=self.tr,
            status="DRAFT")
        apply_event(inst, "ISSUE", self.tr, on=dt.date(2026, 7, 5))
        self.assertEqual(unpresented_cheques_total(dt.date(2026, 7, 10)),
                         Decimal("3000"))

    def test_legacy_cleared_without_date_stays_cleared(self):
        """Rows recorded before per-event dates existed keep their totals:
        a CLEARED instrument with no cleared date is treated as always
        cleared, never resurrected into old reconciliations."""
        inst = PaymentInstrument.objects.create(
            method="CHEQUE", instrument_number="LEGACY1", amount=Decimal("999"),
            source_kind="MANUAL", recorded_by=self.tr,
            status="CLEARED", date_issued=dt.date(2026, 1, 5))
        self.assertNotIn(inst, PaymentInstrument.outstanding_asof(dt.date(2026, 2, 1)))

    def test_legacy_voided_without_date_never_outstanding(self):
        inst = PaymentInstrument.objects.create(
            method="CHEQUE", instrument_number="LEGACY2", amount=Decimal("50"),
            source_kind="MANUAL", recorded_by=self.tr,
            status="VOIDED", date_issued=dt.date(2026, 1, 5))
        self.assertNotIn(inst, PaymentInstrument.outstanding_asof(dt.date(2026, 2, 1)))


class DebitQueueIntegrationTests(TestCase):
    def setUp(self):
        posting.ensure_chart()
        self.tr = _user("dq_tr", TREASURER)
        self.client.force_login(self.tr)
        self.dept = Department.objects.create(name="Development", fund_type="LOCAL")
        self.exp = _expense(self.dept, self.tr, "6000")
        self.inst = PaymentInstrument.objects.create(
            method="CHEQUE", instrument_number="000301", payee="ACME",
            amount=Decimal("6000"), source_kind="EXPENSE", expense=self.exp,
            recorded_by=self.tr, status="DRAFT")
        apply_event(self.inst, "ISSUE", self.tr, on=dt.date(2026, 7, 5))
        self.debit = Transaction.objects.create(
            date=dt.date(2026, 7, 18), channel="BANK", direction="DEBIT",
            amount=Decimal("6000"), allocation_status="REVIEW",
            core_ref="DB1", raw_narration="CHQ 000301 ACME LTD")

    def test_matching_debit_autoclears_instrument_on_debit_date(self):
        r = self.client.post(reverse("debit_resolve", args=[self.debit.pk]),
                             {"kind": "match", "expense": [self.exp.pk]})
        self.assertEqual(r.status_code, 302)
        self.inst.refresh_from_db()
        self.assertEqual(self.inst.status, "CLEARED")
        self.assertEqual(self.inst.date_cleared, dt.date(2026, 7, 18))
        self.assertEqual(self.inst.bank_transaction_id, self.debit.pk)
        ev = self.inst.events.filter(event="CLEAR").first()
        self.assertIsNotNone(ev)
        self.assertEqual(ev.on, dt.date(2026, 7, 18))

    def test_same_debit_never_clears_twice(self):
        clear_for_bank_debit(self.debit, self.tr, [self.exp])
        # a second run finds nothing to clear — no duplicates
        self.assertEqual(clear_for_bank_debit(self.debit, self.tr, [self.exp]), [])
        self.assertEqual(self.inst.events.filter(event="CLEAR").count(), 1)

    def test_suggestion_by_number_in_narration(self):
        inst, how = suggest_instrument_for_debit(self.debit)
        self.assertEqual(inst.pk, self.inst.pk)
        self.assertEqual(how, "number")

    def test_suggestion_by_unique_amount(self):
        other = Transaction.objects.create(
            date=dt.date(2026, 7, 18), channel="BANK", direction="DEBIT",
            amount=Decimal("6000"), allocation_status="REVIEW",
            core_ref="DB2", raw_narration="EFT NO REF")
        inst, how = suggest_instrument_for_debit(other)
        self.assertEqual(inst.pk, self.inst.pk)
        self.assertEqual(how, "amount")

    def test_one_click_clear_instrument(self):
        r = self.client.post(reverse("debit_resolve", args=[self.debit.pk]),
                             {"kind": "clear_instrument",
                              "instrument": self.inst.pk})
        self.assertEqual(r.status_code, 302)
        self.inst.refresh_from_db()
        self.exp.refresh_from_db()
        self.assertEqual(self.inst.status, "CLEARED")
        self.assertEqual(self.inst.date_cleared, self.debit.date)
        self.assertEqual(self.exp.status, Expense.Status.PAID)
        self.assertEqual(self.exp.bank_transaction_id, self.debit.pk)
        # the same instrument cannot be cleared by another debit
        d2 = Transaction.objects.create(
            date=dt.date(2026, 7, 19), channel="BANK", direction="DEBIT",
            amount=Decimal("6000"), allocation_status="REVIEW", core_ref="DB3")
        self.client.post(reverse("debit_resolve", args=[d2.pk]),
                         {"kind": "clear_instrument", "instrument": self.inst.pk})
        self.inst.refresh_from_db()
        self.assertEqual(self.inst.bank_transaction_id, self.debit.pk)

    def test_amount_mismatch_refused(self):
        d = Transaction.objects.create(
            date=dt.date(2026, 7, 19), channel="BANK", direction="DEBIT",
            amount=Decimal("5999"), allocation_status="REVIEW", core_ref="DB4")
        self.client.post(reverse("debit_resolve", args=[d.pk]),
                         {"kind": "clear_instrument", "instrument": self.inst.pk})
        self.inst.refresh_from_db()
        self.assertNotEqual(self.inst.status, "CLEARED")


class MultiExpenseAndLoanTests(TestCase):
    def setUp(self):
        posting.ensure_chart()
        self.tr = _user("me_tr", TREASURER)
        self.client.force_login(self.tr)
        self.dept = Department.objects.create(name="Development", fund_type="LOCAL")
        self.youth = Department.objects.create(name="Youth", fund_type="LOCAL")

    def test_one_eft_covering_multiple_expenses(self):
        e1 = _expense(self.dept, self.tr, "3000")
        e2 = _expense(self.youth, self.tr, "2000", description="youth camp")
        r = self.client.post(reverse("payment_register"), {
            "action": "add", "method": "EFT", "instrument_number": "EFT-77",
            "payee": "SUPPLIER LTD", "amount": "5000",
            "source_kind": "EXPENSE", "source_id": f"{e1.pk},{e2.pk}"})
        self.assertEqual(r.status_code, 302)
        inst = PaymentInstrument.objects.get(instrument_number="EFT-77")
        self.assertEqual(inst.expense_id, e1.pk)
        self.assertEqual(list(inst.extra_expenses.values_list("id", flat=True)),
                         [e2.pk])
        self.assertIn("Development", inst.fund_names)
        self.assertIn("Youth", inst.fund_names)
        # once issued, clearing via a matched debit settles the one instrument
        # covering both vouchers (found via either expense), on the debit date
        apply_event(inst, "ISSUE", self.tr, on=dt.date(2026, 7, 10))
        debit = Transaction.objects.create(
            date=dt.date(2026, 7, 20), channel="BANK", direction="DEBIT",
            amount=Decimal("5000"), allocation_status="REVIEW", core_ref="DB9")
        cleared = clear_for_bank_debit(debit, self.tr, [e1, e2])
        self.assertEqual([c.pk for c in cleared], [inst.pk])
        inst.refresh_from_db()
        self.assertEqual(inst.date_cleared, dt.date(2026, 7, 20))

    def test_multi_expense_total_must_match(self):
        e1 = _expense(self.dept, self.tr, "3000")
        e2 = _expense(self.youth, self.tr, "2000")
        self.client.post(reverse("payment_register"), {
            "action": "add", "method": "EFT", "instrument_number": "EFT-78",
            "payee": "X", "amount": "4999",
            "source_kind": "EXPENSE", "source_id": f"{e1.pk},{e2.pk}"})
        self.assertFalse(PaymentInstrument.objects.filter(
            instrument_number="EFT-78").exists())

    def test_loan_repayment_by_cheque(self):
        from loans.models import Lender, Loan
        from loans.services import loans as loan_svc
        lender = Lender.objects.create(name="ACME SACCO")
        loan = Loan.objects.create(lender=lender, fund=self.dept,
                                   loan_date=dt.date(2026, 1, 1))
        loan_svc.record_receipt(loan, date=dt.date(2026, 1, 1),
                                amount=Decimal("50000"), user=self.tr)
        lt = loan_svc.record_repayment(loan, date=dt.date(2026, 7, 1),
                                       amount=Decimal("20000"), user=self.tr,
                                       voucher_no="CHQ-500")
        inst = PaymentInstrument.objects.create(
            method="CHEQUE", instrument_number="000500",
            payee=lender.name, amount=Decimal("20000"),
            source_kind="EXPENSE", expense=lt.expense,
            recorded_by=self.tr, status="DRAFT")
        apply_event(inst, "ISSUE", self.tr, on=dt.date(2026, 7, 1))
        self.assertIn("Loan repayment", inst.source_label)
        apply_event(inst, "CLEAR", self.tr, on=dt.date(2026, 7, 9))
        self.assertEqual(inst.clearance_days, 8)


class AccountingInvarianceTests(TestCase):
    """Instruments track HOW money moved — they must never post."""

    def setUp(self):
        posting.ensure_chart()
        self.tr = _user("ai_tr2", TREASURER)
        self.dept = Department.objects.create(name="Development", fund_type="LOCAL")

    def test_full_lifecycle_posts_nothing(self):
        exp = _expense(self.dept, self.tr, "5000", status=Expense.Status.PAID,
                       paid_date=dt.date(2026, 7, 1))
        before = JournalEntry.objects.count()
        inst = PaymentInstrument.objects.create(
            method="CHEQUE", instrument_number="000900", amount=Decimal("5000"),
            source_kind="EXPENSE", expense=exp, recorded_by=self.tr)
        apply_event(inst, "ISSUE", self.tr, on=dt.date(2026, 7, 2))
        apply_event(inst, "CLEAR", self.tr, on=dt.date(2026, 7, 9))
        apply_event(inst, "REVERSE", self.tr, on=dt.date(2026, 7, 12))
        self.assertEqual(JournalEntry.objects.count(), before)
        rows, totals = posting.trial_balance()
        self.assertEqual(totals["debit"], totals["credit"])


class PermissionTests(TestCase):
    def setUp(self):
        self.tr = _user("pp_tr", TREASURER)
        self.asst = _user("pp_as", ASSISTANT)
        self.aud = _user("pp_au", AUDITOR)
        self.leader = _user("pp_ld", LEADER)
        self.dept = Department.objects.create(name="Development", fund_type="LOCAL")
        self.other = Department.objects.create(name="Youth", fund_type="LOCAL")
        DepartmentLeadership.objects.create(user=self.leader, department=self.dept)
        exp = _expense(self.dept, self.tr, "1000")
        oexp = _expense(self.other, self.tr, "2222", description="youth thing")
        self.inst = PaymentInstrument.objects.create(
            method="CHEQUE", instrument_number="PRM1", amount=Decimal("1000"),
            source_kind="EXPENSE", expense=exp, recorded_by=self.tr, status="DRAFT")
        self.oinst = PaymentInstrument.objects.create(
            method="CHEQUE", instrument_number="PRM2", amount=Decimal("2222"),
            source_kind="EXPENSE", expense=oexp, recorded_by=self.tr, status="DRAFT")

    def test_assistant_can_create_and_clear_not_approve_or_void(self):
        self.client.force_login(self.asst)
        apply_event(self.inst, "ISSUE", self.tr, on=dt.date(2026, 7, 1))
        r = self.client.post(reverse("payment_register"),
                             {"action": "approve", "pk": self.inst.pk})
        self.inst.refresh_from_db()
        self.assertNotEqual(self.inst.status, "APPROVED")
        self.client.post(reverse("payment_register"),
                         {"action": "clear", "pk": self.inst.pk,
                          "on": "2026-07-09"})
        self.inst.refresh_from_db()
        self.assertEqual(self.inst.status, "CLEARED")
        r = self.client.post(reverse("payment_register"),
                             {"action": "reverse", "pk": self.inst.pk})
        self.inst.refresh_from_db()
        self.assertEqual(self.inst.status, "CLEARED")      # void right needed

    def test_auditor_views_but_cannot_act(self):
        self.client.force_login(self.aud)
        self.assertEqual(self.client.get(reverse("payment_register")).status_code, 200)
        self.client.post(reverse("payment_register"),
                         {"action": "issue", "pk": self.inst.pk})
        self.inst.refresh_from_db()
        self.assertEqual(self.inst.status, "DRAFT")

    def test_leader_scoped_to_own_funds(self):
        self.client.force_login(self.leader)
        r = self.client.get(reverse("payment_register"))
        self.assertEqual(r.status_code, 200)
        self.assertContains(r, "PRM1")
        self.assertNotContains(r, "PRM2")


class RegisterSearchExportTests(TestCase):
    def setUp(self):
        self.tr = _user("rs_tr", TREASURER)
        self.client.force_login(self.tr)
        self.dept = Department.objects.create(name="Development", fund_type="LOCAL")
        exp = _expense(self.dept, self.tr, "1234", voucher_no="V-88")
        self.inst = PaymentInstrument.objects.create(
            method="CHEQUE", instrument_number="SRCH1", payee="UNIQUE PAYEE",
            amount=Decimal("1234"), source_kind="EXPENSE", expense=exp,
            recorded_by=self.tr, status="DRAFT")
        apply_event(self.inst, "ISSUE", self.tr, on=dt.date(2026, 7, 1))

    def test_search_by_payee_number_expense_and_amount(self):
        url = reverse("payment_register")
        for q in ("UNIQUE PAYEE", "SRCH1", "V-88", "1234"):
            r = self.client.get(url, {"q": q})
            self.assertContains(r, "SRCH1", msg_prefix=q)
        # a non-matching search empties the table (the global stats header may
        # still name the oldest outstanding instrument, so check the body)
        r = self.client.get(url, {"q": "NO SUCH THING"})
        self.assertContains(r, "No payments recorded yet")

    def test_outstanding_filter_and_exports(self):
        url = reverse("payment_register")
        r = self.client.get(url, {"status": "_outstanding"})
        self.assertContains(r, "SRCH1")
        r = self.client.get(url + "?export=csv")
        body = r.content.decode()
        self.assertIn("SRCH1", body)
        self.assertIn("Development", body)                  # fund column
        r = self.client.get(url + "?export=xlsx")
        self.assertIn("spreadsheetml", r["Content-Type"])

    def test_outstanding_report_asof_and_analysis(self):
        apply_event(self.inst, "CLEAR", self.tr, on=dt.date(2026, 7, 19))
        r = self.client.get(reverse("payment_outstanding") + "?as_of=2026-07-10")
        self.assertContains(r, "SRCH1")                     # outstanding then
        r = self.client.get(reverse("payment_outstanding") + "?as_of=2026-07-31")
        self.assertNotContains(r, "SRCH1")                  # cleared by then
        r = self.client.get(reverse("payment_analysis")
                            + "?start=2026-07-01&end=2026-07-31&group=fund")
        self.assertContains(r, "Development")

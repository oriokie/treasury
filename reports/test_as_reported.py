"""The "as reported" basis: the position as it stood on a date.

The case these tests exist for: a bank credit lands on 25 July and is receipted
on 1 August. A report for 30 July, run in August, must be able to say the money
was sitting unreceipted — otherwise the suspense line reads nil, the fund shows
money it had not yet been given, and the bank reconciliation has nothing to
explain the gap with.
"""
import datetime as dt
from decimal import Decimal

from django.contrib.auth.models import Group, User
from django.test import TestCase
from django.urls import reverse

from cashbook.models import Expense
from core.reporting import ReportContext, registry
from core.roles import TREASURER
from departments.models import Department
from giving.models import Transaction
from reports.services import asat, balances

AS_AT = dt.date(2026, 7, 30)


def _treasurer(username="asat_tr"):
    u = User.objects.create_user(username, password="x")
    u.groups.add(Group.objects.get_or_create(name=TREASURER)[0])
    return u


def _aware(*args):
    from django.utils import timezone
    return timezone.make_aware(dt.datetime(*args), timezone.get_current_timezone())


def _entered_on(obj, *when):
    """Backdate the row's latest history entry.

    ``simple_history`` stamps the wall clock, so a fixture built today exists
    only as of today — which would make every as-at test trivially empty. These
    helpers put the entry when the story says it happened, which is also what
    real data looks like: a July entry carries a July ``history_date``.
    """
    latest = obj.history.order_by("-history_date", "-history_id").first()
    type(obj).history.filter(history_id=latest.history_id).update(
        history_date=_aware(*when))


class _Seed(TestCase):
    """A credit banked and keyed in on 25 July, receipted on 1 August."""

    def setUp(self):
        self.u = User.objects.create_user("asat_seed", password="x",
                                          is_superuser=True)
        self.fund = Department.objects.create(name="Building", fund_type="LOCAL")
        self.txn = Transaction.objects.create(
            date=dt.date(2026, 7, 25), channel="BANK", direction="CREDIT",
            amount=Decimal("5000"), department=None, confirmed=True,
            allocation_status="REVIEW")
        _entered_on(self.txn, 2026, 7, 25, 10, 0)

    def receipt_it(self):
        """What the treasurer did on 1 August: allocated it to a fund.

        This is the plain allocation route, and it sets NEITHER receipt flag.
        Say so out loud, because for a long time it was the only route any test
        in this file used, and ``balances.receipted_after`` — the half of
        ``pending_receipts_total`` these tests are meant to be watching — reads
        nothing except ``manual_receipt`` and ``processed_via_envelope``. So it
        returned zero in every case exercised here, and the assertions below
        were passing against a function they had never once caused to run.
        For the two routes that do set a flag, use ``receipt_via_envelope`` or
        ``receipt_on_paper``.
        """
        self.txn.department = self.fund
        self.txn.allocation_status = "MANUAL"
        self.txn.save()
        _entered_on(self.txn, 2026, 8, 1, 9, 0)

    def receipt_via_envelope(self):
        """Receipted through the app on 1 August — the envelope pull and the
        receipt-this-contribution action both do this.

        No second posting is made: the envelope is attached to the bank row and
        the bank row keeps the money, so once the fund is set the bank row IS
        the fund's income (see Transaction.is_bank_memo, which excludes exactly
        this case from the memo treatment).
        """
        self.txn.department = self.fund
        self.txn.allocation_status = "MANUAL"
        self.txn.processed_via_envelope = True
        self.txn.save()
        _entered_on(self.txn, 2026, 8, 1, 9, 0)

    def receipt_on_paper(self):
        """Receipted by hand on 1 August — ``mark_manual_receipt``.

        The mirror image of the envelope route: the bank row is detached from
        every fund and turned into a zero-cash memo, and the income is keyed as
        a separate envelope entry dated the Sabbath it was given — 1 August,
        which is after the reporting date. On 30 July the money is at the bank
        and in no fund at all, so suspense is the only line that can carry it.
        """
        self.txn.mark_manual_receipt(True)
        _entered_on(self.txn, 2026, 8, 1, 9, 0)
        env = Transaction.objects.create(
            date=dt.date(2026, 8, 1), channel="ENVELOPE", direction="CREDIT",
            amount=Decimal("5000"), department=self.fund, confirmed=True,
            allocation_status="MANUAL")
        _entered_on(env, 2026, 8, 1, 9, 5)
        return env


class MomentTests(TestCase):
    def test_as_at_a_date_means_the_end_of_that_day(self):
        m = asat.moment_for(dt.date(2026, 7, 30))
        self.assertEqual(m.date(), dt.date(2026, 7, 30))
        self.assertEqual((m.hour, m.minute), (23, 59))

    def test_basis_is_off_by_default_and_restores_itself(self):
        self.assertIsNone(asat.active())
        with asat.as_reported(AS_AT):
            self.assertTrue(asat.is_active())
        self.assertIsNone(asat.active())

    def test_a_null_date_is_a_no_op(self):
        with asat.as_reported(None):
            self.assertFalse(asat.is_active())

    def test_cache_key_separates_the_two_bases(self):
        default = balances._k(dt.date(2026, 1, 1), AS_AT, True)
        with asat.as_reported(AS_AT):
            reported = balances._k(dt.date(2026, 1, 1), AS_AT, True)
        self.assertNotEqual(default, reported)


class PendingReceiptsTests(_Seed):
    def test_restated_basis_loses_the_item_once_it_is_receipted(self):
        """Today's behaviour, stated so a change to it is deliberate."""
        self.assertEqual(balances.pending_receipts_total(AS_AT), Decimal("5000"))
        self.receipt_it()
        self.assertEqual(balances.pending_receipts_total(AS_AT), Decimal("0"))

    def test_as_reported_basis_still_shows_it_pending(self):
        self.receipt_it()
        with asat.as_reported(AS_AT):
            self.assertEqual(balances.pending_receipts_total(AS_AT),
                             Decimal("5000"))

    def test_the_fund_does_not_hold_it_yet(self):
        self.receipt_it()
        with asat.as_reported(AS_AT):
            rows = balances.department_summary(dt.date(2026, 1, 1), AS_AT)
            building = next(r for r in rows
                            if r["department"].name == "Building")
            self.assertEqual(building["closing"], Decimal("0"))

    def _assert_counted_exactly_once(self):
        """Whichever line ends up holding it, the fund balances and suspense
        must add to the 5,000 that actually came in — on BOTH bases. 10,000
        here is the same shilling reported twice; it nets off against itself in
        net assets, so the bottom of the statement looks right while total
        assets, total liabilities and the fund-balances bridge are all wrong."""
        for label, ctx_mgr in (("restated", asat.restated()),
                               ("as reported", asat.as_reported(AS_AT))):
            with self.subTest(label), ctx_mgr:
                rows = balances.department_summary(dt.date(2026, 1, 1), AS_AT)
                fund_cash = sum((r["closing"] for r in rows), Decimal(0))
                pending = balances.pending_receipts_total(AS_AT)
                self.assertEqual(fund_cash + pending, Decimal("5000"))

    def test_suspense_and_fund_balances_move_together(self):
        """The failure this basis exists to prevent: the same 5,000 counted
        both in the fund and in suspense."""
        self.receipt_it()
        self._assert_counted_exactly_once()

    def test_an_envelope_receipting_is_in_the_fund_or_in_suspense_never_both(self):
        """The routine 'banked Friday, receipted Sabbath' flow, on the DEFAULT
        basis — which is the basis every ordinary caller runs on: the standalone
        Statement of Financial Position, the board pack's position sections, the
        fund-balances bridge, the treasurer's report and the health checks.

        None of them enters an ``as_reported`` block, and the add-back in
        ``pending_receipts_total`` was applied whether or not one had been
        entered. So a credit that had since been allocated and receipted was
        counted in its fund AND added back to suspense: 5,000 in, 10,000 out."""
        self.receipt_via_envelope()
        self._assert_counted_exactly_once()
        # the bank row is the money and it now has a fund, so the fund holds it
        # and suspense is empty — not the other way round, and not both
        self.assertEqual(balances.pending_receipts_total(AS_AT), Decimal(0))
        rows = balances.department_summary(dt.date(2026, 1, 1), AS_AT)
        building = next(r for r in rows if r["department"].id == self.fund.id)
        self.assertEqual(building["closing"], Decimal("5000"))

    def test_a_paper_receipting_is_still_carried_as_suspense(self):
        """The opposite case, and the one the add-back exists for — pinned so
        that fixing the double-count above cannot quietly delete it. The memo
        row is in no fund on 30 July and its envelope is dated 1 August, so
        without the add-back the 5,000 would be reported nowhere at all and a
        30 July reconciliation would be short by exactly that much."""
        self.receipt_on_paper()
        self._assert_counted_exactly_once()
        self.assertEqual(balances.pending_receipts_total(AS_AT), Decimal("5000"))


class LaterEntryTests(_Seed):
    def test_an_entry_made_after_the_date_is_not_in_the_position(self):
        """Back-dating is the same problem wearing a different hat: a credit
        keyed in on 5 August but dated 28 July was not there on 30 July."""
        late = Transaction.objects.create(
            date=dt.date(2026, 7, 28), channel="BANK", direction="CREDIT",
            amount=Decimal("1200"), department=self.fund, confirmed=True,
            allocation_status="MANUAL")
        _entered_on(late, 2026, 8, 5, 11, 0)        # keyed in on 5 August
        restated = balances.receipts_by_department(dt.date(2026, 1, 1), AS_AT)
        self.assertEqual(restated.get(self.fund.id), Decimal("1200"))
        with asat.as_reported(AS_AT):
            reported = balances.receipts_by_department(dt.date(2026, 1, 1), AS_AT)
        self.assertIsNone(reported.get(self.fund.id))


class ExpenseTests(_Seed):
    def test_a_claim_approved_later_was_only_a_claim_on_the_day(self):
        e = Expense.objects.create(
            date=dt.date(2026, 7, 28), department=self.fund,
            description="Repairs", amount=Decimal("900"),
            category="MAINTENANCE", status="PENDING", recorded_by=self.u)
        _entered_on(e, 2026, 7, 28, 14, 0)          # claimed on 28 July
        e.status = "APPROVED"
        e.save()
        _entered_on(e, 2026, 8, 3, 10, 0)           # approved on 3 August
        self.assertEqual(
            balances.operating_expense_total(dt.date(2026, 1, 1), AS_AT),
            Decimal("900"))
        with asat.as_reported(AS_AT):
            self.assertEqual(
                balances.operating_expense_total(dt.date(2026, 1, 1), AS_AT),
                Decimal("0"))


class CoherenceTests(_Seed):
    """Every figure in a pack must be drawn from the same moment. These pin the
    aggregates that are easiest to leave behind — income, transfers, refunds and
    the single-fund balance all reach the database by different routes."""

    def test_income_follows_the_basis_with_the_fund_balances(self):
        """Total income and fund receipts reach the database by different
        routes — ``core.metrics.income_credits`` and
        ``balances.receipts_by_department``. A late entry must drop out of
        both, or the pack's headline income would not match its own funds."""
        late = Transaction.objects.create(
            date=dt.date(2026, 7, 26), channel="CASH", direction="CREDIT",
            amount=Decimal("700"), department=self.fund, confirmed=True,
            allocation_status="MANUAL")
        _entered_on(late, 2026, 8, 4, 9, 0)          # keyed in on 4 August

        restated_income = balances.income_by_channel(dt.date(2026, 1, 1), AS_AT)
        restated_rows = balances.department_summary(dt.date(2026, 1, 1), AS_AT)
        self.assertEqual(sum((c["total"] for c in restated_income), Decimal(0)),
                         Decimal("5700"))
        self.assertEqual(sum((r["receipts"] for r in restated_rows), Decimal(0)),
                         Decimal("700"))

        with asat.as_reported(AS_AT):
            income = balances.income_by_channel(dt.date(2026, 1, 1), AS_AT)
            rows = balances.department_summary(dt.date(2026, 1, 1), AS_AT)
        # the 700 is gone from both; the 5,000 remains, unallocated, so it is
        # income but belongs to no fund yet — which is what suspense is for
        self.assertEqual(sum((c["total"] for c in income), Decimal(0)),
                         Decimal("5000"))
        self.assertEqual(sum((r["receipts"] for r in rows), Decimal(0)),
                         Decimal("0"))
        with asat.as_reported(AS_AT):
            self.assertEqual(balances.pending_receipts_total(AS_AT),
                             Decimal("5000"))

    def test_a_transfer_made_later_is_not_in_the_earlier_position(self):
        from cashbook.models import FundTransfer
        other = Department.objects.create(name="Youth", fund_type="LOCAL")
        tr = FundTransfer.objects.create(
            date=dt.date(2026, 7, 29), source=self.fund, destination=other,
            amount=Decimal("400"), recorded_by=self.u)
        _entered_on(tr, 2026, 8, 6, 9, 0)
        self.assertEqual(
            balances.transfers_in_by_department(None, AS_AT).get(other.id),
            Decimal("400"))
        with asat.as_reported(AS_AT):
            self.assertIsNone(
                balances.transfers_in_by_department(None, AS_AT).get(other.id))

    def test_the_single_fund_balance_agrees_with_the_statement(self):
        self.receipt_it()
        with asat.as_reported(AS_AT):
            rows = balances.department_summary(dt.date(2026, 1, 1), AS_AT)
            statement = next(r for r in rows
                             if r["department"].id == self.fund.id)["closing"]
            single = balances.fund_balance_parts(self.fund, AS_AT)
        self.assertEqual(statement, Decimal("0"))
        self.assertEqual(
            single["opening"] + single["receipts"] - single["spent"]
            + single["refunded"] + single["transfers_in"]
            - single["transfers_out"], Decimal("0"))


class CollectionsTests(_Seed):
    def test_the_collections_table_follows_the_same_basis(self):
        """A pack cannot have its collections on one basis and its statements
        on another."""
        from reports.services import monthly
        self.receipt_it()
        restated = monthly.collections_summary_period(dt.date(2026, 7, 1), AS_AT)
        with asat.as_reported(AS_AT):
            reported = monthly.collections_summary_period(dt.date(2026, 7, 1),
                                                          AS_AT)
        self.assertEqual(restated["totals"]["collections"], Decimal("5000"))
        self.assertEqual(reported["totals"]["collections"], Decimal("5000"))
        # the credit existed either way; what changed is only where it sat


class ReportTests(_Seed):
    def test_the_board_pack_offers_the_basis_as_a_filter(self):
        names = [f.name for f in registry.get("board_report_v2").filters]
        self.assertIn("as_reported", names)

    def test_off_by_default(self):
        self.receipt_it()
        self.client.force_login(_treasurer())
        r = self.client.get(reverse("engine_report", args=["board_report_v2"]),
                            {"start": "2026-07-01", "end": "2026-07-30"})
        self.assertEqual(r.status_code, 200)
        self.assertNotContains(r, "Position as it stood on 30 July 2026")

    def test_the_flag_switches_the_basis_and_says_so_on_the_page(self):
        self.receipt_it()
        self.client.force_login(_treasurer("asat_tr2"))
        r = self.client.get(reverse("engine_report", args=["board_report_v2"]),
                            {"start": "2026-07-01", "end": "2026-07-30",
                             "as_reported": "1"})
        self.assertEqual(r.status_code, 200)
        self.assertContains(r, "Position as it stood on 30 July 2026")

    def test_the_basis_does_not_leak_out_of_the_render(self):
        self.client.force_login(_treasurer("asat_tr3"))
        self.client.get(reverse("engine_report", args=["board_report_v2"]),
                        {"start": "2026-07-01", "end": "2026-07-30",
                         "as_reported": "1"})
        self.assertIsNone(asat.active())

    def test_a_report_without_the_filter_ignores_the_parameter(self):
        """The parameter cannot quietly change the basis of a report that was
        never designed for it."""
        self.receipt_it()
        self.client.force_login(_treasurer("asat_tr4"))
        r = self.client.get(reverse("engine_report", args=["trial_balance_v2"]),
                            {"start": "2026-07-01", "end": "2026-07-30",
                             "as_reported": "1"})
        self.assertEqual(r.status_code, 200)

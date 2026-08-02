"""Tests for the minimal Board / Treasurer's Report pack.

Covers the two new period-aware collection tables, the revisions to the fund,
position and cash-flow statements, the bank reconciliation, and the editable
per-section commentary. Every assertion about a figure checks it against the
registry metric the rest of the system reads, so the board pack cannot quietly
diverge from the statements it is drawn from.
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
from reports.board_sections import (BankReconciliationComponent,
                                    CollectionsSummaryComponent,
                                    TrustFundSummaryComponent)
from reports.financial_statements import (CashFlowStatementSection,
                                          FinancialPositionStatementSection,
                                          FinancialPositionSummarySection,
                                          FundBalancesStatementSection)


def _ctx(start=dt.date(2026, 1, 1), end=dt.date(2026, 12, 31)):
    return ReportContext.for_period(start, end)


def _treasurer(username="bpm_tr"):
    u, created = User.objects.get_or_create(username=username)
    if created:
        u.set_password("x")
        u.save()
    u.groups.add(Group.objects.get_or_create(name=TREASURER)[0])
    return u


def _credit(date, dept, amount):
    return Transaction.objects.create(
        date=date, channel="CASH", direction="CREDIT", amount=Decimal(amount),
        department=dept, allocation_status="AUTO", confirmed=True)


class _Seed(TestCase):
    """Two months of movement across a local fund and two trust funds."""

    def setUp(self):
        self.u = User.objects.create_user("bpm_seed", password="x",
                                          is_superuser=True)
        self.local = Department.objects.create(name="Building", fund_type="LOCAL")
        self.tithe = Department.objects.create(name="Tithe", fund_type="TRUST")
        self.camp = Department.objects.create(name="Camp Meeting",
                                              fund_type="TRUST")
        # a fund that has never seen a shilling
        self.dormant = Department.objects.create(name="Dormant Fund",
                                                 fund_type="LOCAL")
        _credit(dt.date(2026, 3, 4), self.local, "40000")
        _credit(dt.date(2026, 3, 4), self.tithe, "18000")
        _credit(dt.date(2026, 4, 6), self.local, "10000")
        _credit(dt.date(2026, 4, 6), self.camp, "2000")
        Expense.objects.create(
            date=dt.date(2026, 3, 10), department=self.local,
            description="Repairs", amount=Decimal("6000"),
            category="MAINTENANCE", status="PAID", recorded_by=self.u)


# ===========================================================================
# Collections summary
# ===========================================================================

class CollectionsSummaryTests(_Seed):
    def test_one_row_per_month_over_a_multi_month_period(self):
        data = CollectionsSummaryComponent().render(
            _ctx(dt.date(2026, 3, 1), dt.date(2026, 4, 30)), {})
        self.assertEqual([r.cells["period"] for r in data.rows],
                         ["Mar 2026", "Apr 2026"])
        self.assertIsNotNone(data.total)

    def test_single_month_period_is_one_line_with_no_total(self):
        data = CollectionsSummaryComponent().render(
            _ctx(dt.date(2026, 3, 1), dt.date(2026, 3, 31)), {})
        self.assertEqual(len(data.rows), 1)
        self.assertIsNone(data.total)

    def test_columns_are_the_ones_the_board_asked_for(self):
        data = CollectionsSummaryComponent().render(_ctx(), {})
        self.assertEqual([c.key for c in data.columns],
                         ["period", "collections", "trust", "local",
                          "expenditure", "net"])

    def test_trust_and_local_split_and_net(self):
        data = CollectionsSummaryComponent().render(
            _ctx(dt.date(2026, 3, 1), dt.date(2026, 3, 31)), {})
        row = data.rows[0].cells
        self.assertEqual(row["collections"], Decimal("58000"))
        self.assertEqual(row["trust"], Decimal("18000"))
        self.assertEqual(row["local"], Decimal("40000"))
        self.assertEqual(row["expenditure"], Decimal("6000"))
        self.assertEqual(row["net"], Decimal("52000"))

    def test_ties_to_the_standalone_collections_summary_report(self):
        """Same dates, same definitions, same figures — the board pack and the
        Collections Summary report can never disagree."""
        from reports.services import monthly
        legacy = monthly.collections_summary(2026)
        period = monthly.collections_summary_period(dt.date(2026, 1, 1),
                                                    dt.date(2026, 12, 31))
        for key in ("collections", "trust", "local", "expenditure", "net"):
            self.assertEqual(period["totals"][key], legacy[f"tot_{key}"], key)


# ===========================================================================
# Trust fund summary
# ===========================================================================

class TrustFundSummaryTests(_Seed):
    def test_month_columns_over_a_multi_month_period(self):
        data = TrustFundSummaryComponent().render(
            _ctx(dt.date(2026, 3, 1), dt.date(2026, 4, 30)), {})
        self.assertEqual([c.label for c in data.columns],
                         ["Trust fund", "Mar", "Apr", "Total"])

    def test_single_month_collapses_to_one_figure_column(self):
        data = TrustFundSummaryComponent().render(
            _ctx(dt.date(2026, 3, 1), dt.date(2026, 3, 31)), {})
        self.assertEqual([c.label for c in data.columns],
                         ["Trust fund", "Collected"])

    def test_funds_with_nothing_collected_are_omitted(self):
        data = TrustFundSummaryComponent().render(
            _ctx(dt.date(2026, 3, 1), dt.date(2026, 3, 31)), {})
        names = [r.cells["fund"] for r in data.rows]
        self.assertIn("Tithe", names)
        self.assertNotIn("Camp Meeting", names)     # collected in April only

    def test_largest_fund_first(self):
        data = TrustFundSummaryComponent().render(_ctx(), {})
        totals = [r.cells["total"] for r in data.rows]
        self.assertEqual(totals, sorted(totals, reverse=True))

    def test_grand_total_ties_to_the_collections_summary_trust_column(self):
        ctx = _ctx()
        trust = TrustFundSummaryComponent().render(ctx, {})
        collections = CollectionsSummaryComponent().render(ctx, {})
        self.assertEqual(trust.total.cells["total"],
                         collections.total.cells["trust"])

    def test_no_trust_collections_renders_nothing(self):
        data = TrustFundSummaryComponent().render(
            _ctx(dt.date(2025, 1, 1), dt.date(2025, 12, 31)), {})
        self.assertIsNone(data)


# ===========================================================================
# Statement of fund balances
# ===========================================================================

class FundBalancesStatementTests(_Seed):
    def _render(self, ctx=None):
        return FundBalancesStatementSection().render(ctx or _ctx(),
                                                     {"consolidated": True})

    def test_dormant_funds_are_dropped(self):
        names = [r.cells["fund"].strip() for r in self._render().rows]
        self.assertIn("Building", names)
        self.assertNotIn("Dormant Fund", names)

    def test_sorted_by_closing_amount_descending(self):
        data = self._render()
        local = []
        seen_heading = False
        for r in data.rows:
            label = r.cells["fund"].strip()
            if label == "Local funds":
                seen_heading = True
                continue
            if label.startswith("Total") or label == "Trust funds":
                break
            if seen_heading:
                local.append(r.cells["closing"])
        self.assertEqual(local, sorted(local, reverse=True))

    def test_transfers_column_is_dropped_when_nothing_was_transferred(self):
        data = self._render()
        self.assertNotIn("net_transfer", [c.key for c in data.columns])
        self.assertIn("No transfers", data.note)

    def test_transfers_column_appears_when_a_transfer_exists(self):
        from cashbook.models import FundTransfer
        other = Department.objects.create(name="Youth", fund_type="LOCAL")
        FundTransfer.objects.create(
            date=dt.date(2026, 5, 1), source=self.local, destination=other,
            amount=Decimal("1000"), reason="Test", recorded_by=self.u)
        data = self._render()
        self.assertIn("net_transfer", [c.key for c in data.columns])
        self.assertNotIn("No transfers", data.note)

    def test_total_still_reconciles_to_the_fund_summary_metric(self):
        ctx = _ctx()
        data = FundBalancesStatementSection().render(ctx, {"consolidated": True})
        expected = sum((r["closing"] or Decimal(0)
                        for r in ctx.fund_summary()), Decimal(0))
        self.assertEqual(data.total.cells["closing"], expected)


# ===========================================================================
# Statement of financial position
# ===========================================================================

class FinancialPositionTests(_Seed):
    def _labels(self, **kw):
        data = FinancialPositionSummarySection(**kw).render(_ctx(), {})
        return data, [r.cells["label"] for r in data.rows]

    def test_zero_lines_are_not_printed_in_the_board_pack(self):
        _data, labels = self._labels(hide_nil_lines=True)
        # nothing borrowed in this fixture, so the loans line has no business
        # on the board's statement
        self.assertNotIn("Outstanding loans", labels)

    def test_the_standalone_statement_still_shows_every_line(self):
        """The opt-in stays opt-in: an accounting document says "nil", it does
        not go quiet."""
        _data, labels = self._labels()
        self.assertIn("Outstanding loans", labels)

    def test_the_board_pack_asks_for_the_suppression(self):
        report = registry.get("board_report_v2")
        section = next(s for s in report.sections
                       if s.key == "financial_position_statement")
        self.assertTrue(section.hide_nil_lines)

    def test_headings_subtotals_and_net_assets_always_stand(self):
        _data, labels = self._labels(hide_nil_lines=True)
        for required in ("Assets", "Total assets", "Liabilities",
                         "Total liabilities", "Net assets"):
            self.assertIn(required, labels)

    def test_net_assets_is_still_assets_less_liabilities(self):
        for kw in ({}, {"hide_nil_lines": True}):
            data, _labels = self._labels(**kw)
            v = {r.cells["label"]: r.cells["value"] for r in data.rows}
            self.assertEqual(v["Net assets"],
                             v["Total assets"] - v["Total liabilities"], kw)


class FullFinancialPositionTests(_Seed):
    """The board pack carries the statement itself, not a précis of it."""

    def _rows(self, **kw):
        data = FinancialPositionStatementSection(**kw).render(_ctx(), {})
        return data, {r.cells["label"]: r.cells["value"] for r in data.rows}

    def test_it_separates_current_assets_from_fixed(self):
        _data, v = self._rows()
        self.assertIn("Total current assets", v)
        self.assertIn("Net book value", v)
        self.assertIn("TOTAL ASSETS", v)

    def test_it_splits_the_trust_liability_by_whether_it_was_receipted(self):
        _data, v = self._rows()
        self.assertIn("Trust funds payable — receipted", v)
        self.assertIn("Trust funds payable — not yet receipted", v)

    def test_it_splits_borrowings_current_against_long_term(self):
        _data, v = self._rows()
        self.assertIn("Loans payable — current", v)
        self.assertIn("Loans payable — long term", v)

    def test_it_shows_what_the_net_assets_consist_of(self):
        _data, v = self._rows()
        for line in ("Financed by", "Unallocated (general) funds",
                     "Allocated (board-designated) funds",
                     "Invested in property", "TOTAL FUNDS"):
            self.assertIn(line, v)

    def test_the_statement_balances(self):
        _data, v = self._rows()
        self.assertEqual(v["NET ASSETS"],
                         v["TOTAL ASSETS"] - v["TOTAL LIABILITIES"])
        self.assertEqual(v["TOTAL FUNDS"], v["NET ASSETS"])

    def test_the_trust_split_sums_to_the_trust_payable(self):
        ctx = _ctx()
        _data, v = self._rows()
        rows = ctx.fund_summary()
        payable = sum((r["closing"] or Decimal(0)
                       for r in rows if r.get("is_trust")), Decimal(0))
        self.assertEqual(v["Trust funds payable — receipted"]
                         + v["Trust funds payable — not yet receipted"], payable)

    def test_nil_lines_drop_only_when_asked(self):
        _full, v_full = self._rows()
        _lean, v_lean = self._rows(hide_nil_lines=True)
        self.assertIn("Loans payable — current", v_full)
        self.assertNotIn("Loans payable — current", v_lean)
        # the structure always stands
        for required in ("Assets", "TOTAL ASSETS", "TOTAL LIABILITIES",
                         "NET ASSETS", "TOTAL FUNDS"):
            self.assertIn(required, v_lean)

    def test_it_carries_a_generated_explanation(self):
        from reports.services import narratives
        ctx = _ctx()
        section = FinancialPositionStatementSection().render(ctx, {})
        self.assertTrue(narratives.generate(section, ctx))


# ===========================================================================
# Statement of cash flows — including loans converted to donations
# ===========================================================================

class CashFlowStatementTests(_Seed):
    def _lines(self, ctx=None, **kw):
        kw.setdefault("hide_nil_lines", True)
        data = CashFlowStatementSection(**kw).render(ctx or _ctx(), {})
        return data, {r.cells["line"]: r.cells["amount"] for r in data.rows}

    def test_empty_activity_blocks_drop_out_in_the_board_pack(self):
        _data, lines = self._lines()
        self.assertNotIn("Cash flows from financing activities", lines)
        self.assertIn("Cash flows from operating activities", lines)

    def test_the_statutory_statement_keeps_its_full_shape(self):
        _data, lines = self._lines(hide_nil_lines=False)
        self.assertIn("Cash flows from financing activities", lines)
        self.assertIn("Loan receipts (borrowings)", lines)

    def test_the_board_pack_asks_for_the_suppression(self):
        report = registry.get("board_report_v2")
        section = next(s for s in report.sections
                       if s.key == "cash_flow_statement")
        self.assertTrue(section.hide_nil_lines)

    def test_reconciles_to_the_movement_in_fund_cash(self):
        ctx = _ctx()
        data, lines = self._lines(ctx)
        rows = ctx.fund_summary()
        closing = sum((r["closing"] or Decimal(0) for r in rows), Decimal(0))
        self.assertEqual(lines["Cash & bank at end of period"], closing)
        self.assertIn("Reconciles", data.note)


class LoanConversionCashFlowTests(TestCase):
    """A loan converted to a donation moves no money. It must not be counted as
    an operating receipt, must NOT be netted against loan receipts, must leave
    the statement reconciling, and must be disclosed."""

    def setUp(self):
        from loans.models import Lender, Loan
        from loans.services import loans as loan_svc
        self.u = User.objects.create_user("bpm_loan", password="x",
                                          is_superuser=True)
        self.fund = Department.objects.create(name="Building", fund_type="LOCAL")
        lender = Lender.objects.create(name="A Benefactor")
        self.loan = Loan.objects.create(
            lender=lender, fund=self.fund, loan_date=dt.date(2026, 2, 1),
            principal_amount=Decimal("100000"), status=Loan.Status.ACTIVE)
        # 100,000 borrowed in February; 40,000 of it gifted back in May
        loan_svc.record_receipt(self.loan, date=dt.date(2026, 2, 1),
                                amount=Decimal("100000"), user=self.u)
        loan_svc.convert_to_donation(self.loan, date=dt.date(2026, 5, 1),
                                     amount=Decimal("40000"), user=self.u)

    def _lines(self):
        """Rendered as the board pack does, so a nil line genuinely means the
        statement had nothing to say rather than that it printed a zero."""
        ctx = _ctx()
        data = CashFlowStatementSection(hide_nil_lines=True).render(ctx, {})
        return ctx, data, {r.cells["line"]: r.cells["amount"] for r in data.rows}

    def test_loan_receipts_are_not_reduced_by_the_conversion(self):
        ctx, _data, lines = self._lines()
        borrowed = ctx.metric("financing_activity")["receipts"]
        self.assertEqual(borrowed, Decimal("100000"))
        self.assertEqual(lines["Loan receipts (borrowings)"], Decimal("100000"))

    def test_conversion_is_not_an_operating_receipt(self):
        ctx, _data, lines = self._lines()
        # the fund received 100,000 of loan cash and 40,000 of gift income;
        # neither is an operating receipt, so the line is nil and drops out
        self.assertNotIn("Local offerings & income received", lines)

    def test_conversion_is_disclosed_as_a_non_cash_memo(self):
        _ctx_, data, lines = self._lines()
        self.assertIn("Loans converted to donations / written off", lines)
        self.assertEqual(lines["Loans converted to donations / written off"],
                         Decimal("40000"))
        self.assertIn("moved no money", data.note)

    def test_statement_still_reconciles_to_fund_cash(self):
        ctx, data, lines = self._lines()
        closing = sum((r["closing"] or Decimal(0)
                       for r in ctx.fund_summary()), Decimal(0))
        self.assertEqual(lines["Cash & bank at end of period"], closing)
        self.assertIn("Reconciles", data.note)


# ===========================================================================
# Bank reconciliation
# ===========================================================================

class BankReconciliationTests(_Seed):
    def test_says_so_plainly_when_no_statement_covers_the_period_end(self):
        data = BankReconciliationComponent().render(_ctx(), {})
        labels = [r.cells["label"] for r in data.rows]
        self.assertEqual(len(labels), 1)
        self.assertIn("Not reconciled", labels[0])
        # and prints no figure it cannot stand behind
        self.assertIsNone(data.rows[0].cells["value"])

    def _worksheet(self, when=dt.date(2026, 12, 31), bank="52000",
                   book="50000"):
        from statements.models import BankReconciliation, ReconciliationItem
        rec = BankReconciliation.objects.create(
            statement_date=when, bank_balance=Decimal(bank),
            book_balance=Decimal(book), created_by=self.u)
        ReconciliationItem.objects.create(
            reconciliation=rec, kind=ReconciliationItem.Kind.UNPRESENTED,
            description="Cheque 041 not yet cleared", amount=Decimal("2000"),
            effect=ReconciliationItem.Effect.SUBTRACT)
        return rec

    def test_it_shows_the_worksheet_the_treasurer_prepared(self):
        """Not a second reconciliation of its own — the one that was signed
        off, with its own reconciling items."""
        self._worksheet()
        data = BankReconciliationComponent().render(_ctx(), {})
        v = {r.cells["label"]: r.cells["value"] for r in data.rows}
        self.assertEqual(v["Balance per bank statement at 31 Dec 2026"],
                         Decimal("52000"))
        self.assertEqual(v["  Less: Cheque 041 not yet cleared"],
                         Decimal("-2000"))
        self.assertEqual(v["Adjusted bank balance"], Decimal("50000"))
        self.assertEqual(v["Balance per cash book"], Decimal("50000"))
        self.assertEqual(v["Unreconciled difference"], Decimal(0))
        self.assertIn("Reconciled", data.note)

    def test_an_unresolved_difference_is_called_out(self):
        self._worksheet(book="49000")
        data = BankReconciliationComponent().render(_ctx(), {})
        v = {r.cells["label"]: r.cells["value"] for r in data.rows}
        self.assertEqual(v["Unreconciled difference"], Decimal("1000"))
        self.assertIn("unexplained", data.note)

    def test_the_newest_worksheet_on_or_before_the_period_end_is_used(self):
        self._worksheet(when=dt.date(2026, 6, 30), bank="10000", book="10000")
        self._worksheet(when=dt.date(2026, 9, 30), bank="33000", book="33000")
        # a later one must not be pulled into an earlier period
        self._worksheet(when=dt.date(2027, 1, 31), bank="99000", book="99000")
        data = BankReconciliationComponent().render(
            _ctx(dt.date(2026, 1, 1), dt.date(2026, 12, 31)), {})
        labels = [r.cells["label"] for r in data.rows]
        self.assertIn("Balance per bank statement at 30 Sep 2026", labels)

    def test_a_stale_worksheet_says_the_period_end_is_unreconciled(self):
        self._worksheet(when=dt.date(2026, 6, 30), bank="10000", book="10000")
        data = BankReconciliationComponent().render(
            _ctx(dt.date(2026, 1, 1), dt.date(2026, 12, 31)), {})
        self.assertIn("has not itself", data.note)


# ===========================================================================
# The report itself
# ===========================================================================

class BoardPackCompositionTests(_Seed):
    def test_registered_with_the_minimal_presentation_template(self):
        report = registry.get("board_report_v2")
        self.assertIsNotNone(report)
        self.assertEqual(report.html_template, "reports/board_pack_min.html")

    def test_carries_every_section_the_board_asked_for(self):
        report = registry.get("board_report_v2")
        keys = [s.key for s in report.sections]
        for expected in ("collections_summary", "trust_fund_summary",
                         "fund_balances_statement",
                         "financial_position_statement", "cash_flow_statement",
                         "trial_balance", "bank_reconciliation"):
            self.assertIn(expected, keys, expected)

    def test_the_position_is_the_full_statement_not_the_summary(self):
        """A board adopting accounts is handed the statement, not a précis."""
        report = registry.get("board_report_v2")
        keys = [s.key for s in report.sections]
        self.assertNotIn("financial_position_summary", keys)

    def test_renders_for_a_treasurer(self):
        self.client.force_login(_treasurer())
        r = self.client.get(reverse("engine_report", args=["board_report_v2"]),
                            {"start": "2026-01-01", "end": "2026-12-31"})
        self.assertEqual(r.status_code, 200)
        body = r.content.decode()
        self.assertIn("Collections summary", body)
        self.assertIn("Trust fund summary", body)
        self.assertIn("Trial balance", body)

    def test_every_export_format_still_renders(self):
        self.client.force_login(_treasurer("bpm_exp"))
        url = reverse("engine_report", args=["board_report_v2"])
        for fmt in ("csv", "xlsx", "pdf", "docx"):
            r = self.client.get(url, {"export": fmt, "start": "2026-01-01",
                                      "end": "2026-12-31"})
            self.assertEqual(r.status_code, 200, fmt)

    def test_declared_metrics_are_all_registered(self):
        from core.metrics import metrics
        for comp in (CollectionsSummaryComponent(), TrustFundSummaryComponent(),
                     BankReconciliationComponent()):
            for m in comp.declared_metrics:
                self.assertIn(m, metrics.registry, m)

    def test_components_are_in_the_component_library(self):
        from core.reporting import component_registry
        for key in ("collections_summary", "trust_fund_summary",
                    "bank_reconciliation"):
            self.assertTrue(component_registry.has(key), key)
            self.assertIsNotNone(component_registry.create(key))


# ===========================================================================
# Editable commentary
# ===========================================================================

class NarrativeTests(_Seed):
    def _rendered(self):
        from django.test import RequestFactory
        report = registry.get("board_report_v2")
        req = RequestFactory().get("/", {"start": "2026-01-01",
                                         "end": "2026-12-31"})
        req.user = _treasurer("bpm_nar")
        return report.render(req)

    def test_every_table_section_gets_a_generated_explanation(self):
        from reports.services import narratives
        rendered = narratives.annotate(self._rendered(), "board_report_v2")
        by_key = {s.key: s for s in rendered.sections}
        for key in ("collections_summary", "trust_fund_summary",
                    "fund_balances_statement", "financial_position_statement",
                    "cash_flow_statement", "trial_balance",
                    "bank_reconciliation"):
            self.assertTrue(by_key[key].extra.get("explanation"), key)
            self.assertEqual(by_key[key].extra["explanation_source"], "AUTO")

    def test_generation_is_deterministic(self):
        from reports.services import narratives
        a = narratives.annotate(self._rendered(), "board_report_v2")
        b = narratives.annotate(self._rendered(), "board_report_v2")
        self.assertEqual(
            {s.key: s.extra["explanation"] for s in a.sections},
            {s.key: s.extra["explanation"] for s in b.sections})

    def test_explanations_quote_the_section_s_own_figures(self):
        from reports.services import narratives
        ctx = _ctx()
        text = narratives.generate(
            CollectionsSummaryComponent().render(ctx, {}), ctx)
        self.assertIn("20,000", text)          # the trust collections
        self.assertIn("50,000", text)          # the local collections
        self.assertIn("70,000", text)          # and their sum

    def test_an_edit_replaces_the_generated_text_for_that_period_only(self):
        from reports.services import narratives
        narratives.save("board_report_v2", "collections_summary",
                        dt.date(2026, 1, 1), dt.date(2026, 12, 31),
                        "The board should note the April shortfall.",
                        _treasurer("bpm_ed"))
        rendered = narratives.annotate(self._rendered(), "board_report_v2")
        section = next(s for s in rendered.sections
                       if s.key == "collections_summary")
        self.assertEqual(section.extra["explanation"],
                         "The board should note the April shortfall.")
        self.assertTrue(section.extra["explanation_edited"])
        # a different period is untouched
        from django.test import RequestFactory
        req = RequestFactory().get("/", {"start": "2025-01-01",
                                         "end": "2025-12-31"})
        req.user = _treasurer("bpm_ed2")
        other = narratives.annotate(
            registry.get("board_report_v2").render(req), "board_report_v2")
        other_section = next((s for s in other.sections
                              if s.key == "collections_summary"), None)
        if other_section is not None:
            self.assertNotEqual(other_section.extra["explanation"],
                                "The board should note the April shortfall.")

    def test_clearing_an_edit_restores_the_generated_text(self):
        from reports.models import ReportNarrative
        from reports.services import narratives
        user = _treasurer("bpm_clr")
        args = ("board_report_v2", "trial_balance", dt.date(2026, 1, 1),
                dt.date(2026, 12, 31))
        narratives.save(*args, "Custom.", user)
        self.assertTrue(ReportNarrative.objects.exists())
        narratives.save(*args, "   ", user)
        self.assertFalse(ReportNarrative.objects.exists())

    def test_save_endpoint_stores_and_returns_the_text(self):
        self.client.force_login(_treasurer("bpm_post"))
        r = self.client.post(reverse("report_narrative_save"), {
            "report_key": "board_report_v2", "section_key": "trial_balance",
            "start": "2026-01-01", "end": "2026-12-31", "text": "Noted."})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json(), {"ok": True, "edited": True,
                                    "text": "Noted.", "source": "MANUAL"})

    def test_save_endpoint_is_closed_to_non_treasurers(self):
        u = User.objects.create_user("bpm_reader", password="x")
        self.client.force_login(u)
        r = self.client.post(reverse("report_narrative_save"), {
            "report_key": "board_report_v2", "section_key": "trial_balance",
            "text": "Nope."})
        self.assertIn(r.status_code, (302, 403))

    def test_ai_endpoint_explains_itself_when_the_assistant_is_off(self):
        self.client.force_login(_treasurer("bpm_ai"))
        r = self.client.post(reverse("report_narrative_ai"), {
            "report_key": "board_report_v2", "section_key": "trial_balance",
            "start": "2026-01-01", "end": "2026-12-31"})
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertFalse(body["ok"])
        self.assertIn("switched off", body["error"])

    def test_ai_draft_is_stored_against_the_section(self):
        from unittest.mock import patch
        from core.models import SiteConfig
        cfg = SiteConfig.get()
        cfg.llm_enabled = True
        cfg.llm_api_key = "test-key"
        cfg.save()
        self.client.force_login(_treasurer("bpm_ai2"))
        with patch("core.services.assistant._llm_call",
                   return_value=("Cash is tight; act on the LCB fund.", None)):
            r = self.client.post(reverse("report_narrative_ai"), {
                "report_key": "board_report_v2",
                "section_key": "collections_summary",
                "start": "2026-01-01", "end": "2026-12-31"})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["text"], "Cash is tight; act on the LCB fund.")
        from reports.models import ReportNarrative
        stored = ReportNarrative.objects.get(section_key="collections_summary")
        self.assertEqual(stored.source, "AI")
        # stored against the period the reader was looking at, which arrives on
        # the POST — not whatever period the engine would default to
        self.assertEqual(stored.period_start, dt.date(2026, 1, 1))
        self.assertEqual(stored.period_end, dt.date(2026, 12, 31))

    def test_ai_writes_about_the_period_on_screen(self):
        from unittest.mock import patch
        from core.models import SiteConfig
        cfg = SiteConfig.get()
        cfg.llm_enabled = True
        cfg.llm_api_key = "test-key"
        cfg.save()
        self.client.force_login(_treasurer("bpm_ai3"))
        with patch("core.services.assistant._llm_call",
                   return_value=("ok", None)) as call:
            self.client.post(reverse("report_narrative_ai"), {
                "report_key": "board_report_v2",
                "section_key": "collections_summary",
                "start": "2026-03-01", "end": "2026-03-31"})
        context = call.call_args.kwargs["context"]
        self.assertIn("01 Mar 2026 to 31 Mar 2026", context)

    def test_ai_only_ever_sees_the_section_it_is_writing_about(self):
        from unittest.mock import patch
        from core.models import SiteConfig
        from reports.services import narratives
        cfg = SiteConfig.get()
        cfg.llm_enabled = True
        cfg.llm_api_key = "test-key"
        cfg.save()
        ctx = _ctx()
        section = CollectionsSummaryComponent().render(ctx, {})
        with patch("core.services.assistant._llm_call",
                   return_value=("ok", None)) as call:
            narratives.ai_explain(section, ctx)
        context = call.call_args.kwargs["context"]
        self.assertIn("Collections summary", context)
        self.assertNotIn("Trial balance", context)

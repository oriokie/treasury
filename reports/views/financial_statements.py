"""Split from reports/views.py (P1-2). Behaviour identical; the
package __init__ reproduces the original module namespace."""
from decimal import Decimal
from django.db.models import Sum, Count, Q
from django.views.generic import TemplateView
from core.permissions import (ReportAccessMixin, TreasurerRequiredMixin,
                              RightRequiredMixin, ReportAccessMixin)
from cashbook.models import Expense
from ..services import balances
import datetime as _dt
from core.models import SiteConfig
from ._shared import PeriodMixin
from .summaries import _export


class IncomeStatementView(PeriodMixin, TemplateView):
    """Statement of income & expenditure on a LOCAL (operating) basis: trust
    collections and their remittances are excluded, since trust money is held on
    behalf of the field rather than being the church's own income."""
    template_name = "reports/income_statement.html"

    def get(self, request, *args, **kwargs):
        ctx = self.get_context_data(**kwargs)
        s, e = ctx["start"], ctx["end"]
        rows = balances.department_summary(s, e)
        income = [{"name": r["department"].name, "amount": r["receipts"]}
                  for r in rows if not r["is_trust"] and r["receipts"]]
        income.sort(key=lambda x: -x["amount"])
        total_income = sum((r["amount"] for r in income), Decimal(0))
        trust_collected = sum((r["receipts"] for r in rows if r["is_trust"]), Decimal(0))
        # expenditure by category, excluding trust remittances, split recurrent/capital
        eff = Q(status__in=[Expense.Status.APPROVED, Expense.Status.PAID],
                date__gte=s, date__lte=e)
        cats = dict(Expense.Category.choices)
        base = Expense.objects.filter(eff).exclude(doc_class=Expense.DocClass.LIABILITY)

        def _by_cat(qs):
            rows = [{"name": cats.get(r["category"], r["category"]), "amount": r["t"]}
                    for r in qs.values("category").annotate(t=Sum("amount")).order_by("-t")]
            return rows, sum((r["amount"] for r in rows), Decimal(0))

        recurrent, total_recurrent = _by_cat(
            base.filter(expenditure_type=Expense.ExpenditureType.RECURRENT))
        capital, total_capital = _by_cat(
            base.filter(expenditure_type=Expense.ExpenditureType.CAPITAL))
        total_exp = total_recurrent + total_capital
        operating = total_income - total_recurrent
        surplus = operating - total_capital
        # Change in net assets, FUND BASIS: the money held in the funds. This is
        # deliberately NOT the net assets on the Statement of Financial Position,
        # which also carries the fixed assets at their written-down value. The two
        # differ by the asset register, and by the depreciation charged since —
        # neither of which passes through the funds. `non_cash` below states that
        # difference rather than leaving the reader to wonder why the statements
        # disagree.
        na_open = sum((r["opening"] for r in rows if not r["is_trust"]), Decimal(0))
        net_transfers = sum((r["net_transfer"] for r in rows if not r["is_trust"]), Decimal(0))
        na_close = na_open + surplus + net_transfers
        from core.metrics import metrics as _metrics
        non_cash = _metrics.non_cash_items(s, e)
        as_at = min(e, _dt.date.today())
        nbv_close = _metrics.net_book_value(as_at)
        # Bridge from the FUND balances above to the net assets reported on the
        # Statement of Financial Position. Both come from the registry's
        # `net_assets` metric — the same figures that statement itself now reads —
        # so this reconciliation cannot drift from the statement it reconciles to.
        _na = _metrics.net_assets(as_at)
        na_bridge = [
            ("Fixed assets at written-down value", _na["fixed_assets"]),
            ("Prepayments not yet expired", _na["prepayments"]),
            ("Less amounts owed to suppliers", -_na["payables"]),
            ("Less expenses accrued", -_na["accruals"]),
            ("Less loans still to repay", -_na["loans_payable"]),
        ]
        na_total = _na["total"]
        # The funds figure this statement built for itself must equal the one the
        # metric used; if it ever does not, say so rather than hide it.
        na_unexplained = na_close - _na["local_funds"]
        if request.GET.get("export") in ("csv", "xlsx"):
            header = ["Section", "Line", "Amount"]
            data = [["Revenue", r["name"], r["amount"]] for r in income]
            data.append(["Revenue", "TOTAL REVENUE", total_income])
            data += [["Operating (recurrent) expenditure", r["name"], r["amount"]] for r in recurrent]
            data.append(["Operating (recurrent) expenditure", "TOTAL RECURRENT", total_recurrent])
            data.append(["Result", "OPERATING SURPLUS/(DEFICIT)", operating])
            data += [["Capital expenditure", r["name"], r["amount"]] for r in capital]
            data.append(["Capital expenditure", "TOTAL CAPITAL", total_capital])
            data.append(["Result", "NET SURPLUS/(DEFICIT)", surplus])
            data += [
                ["Net assets", "Net assets brought forward", na_open],
                ["Net assets", "Net surplus/(deficit) for the period", surplus],
                ["Net assets", "Net inter-fund transfers", net_transfers],
                ["Net assets", "NET ASSETS CARRIED FORWARD (funds)", na_close],
                ["Non-cash", "Depreciation charged (not in the result above)",
                 -non_cash["depreciation"]],
                ["Non-cash", "Assets donated in kind", non_cash["donated_assets"]],
                ["Non-cash", "Gain/(loss) on disposals", non_cash["disposal_gain_loss"]],
            ] + [["Net assets", label, amount] for label, amount in na_bridge if amount] + [
                ["Net assets", "TOTAL NET ASSETS (per Statement of Financial Position)",
                 na_total],
            ]
            ex = _export(request, f"income_statement_{s}_{e}", header, data,
                         "Statement of Financial Activity")
            if ex:
                return ex
        ctx.update({"income": income, "total_income": total_income,
                    "recurrent": recurrent, "total_recurrent": total_recurrent,
                    "capital": capital, "total_capital": total_capital,
                    "operating": operating, "total_exp": total_exp,
                    "surplus": surplus, "trust_collected": trust_collected,
                    "na_open": na_open, "net_transfers": net_transfers,
                    "na_close": na_close, "non_cash": non_cash,
                    "nbv_close": nbv_close, "na_bridge": na_bridge,
                    "na_total": na_total, "na_unexplained": na_unexplained,
                    "church": SiteConfig.get().church_name})
        return self.render_to_response(ctx)

class FinancialPositionView(ReportAccessMixin, TemplateView):
    """Statement of Financial Position (balance sheet) on a fund-accounting basis,
    as at a date. Assets = cash/bank (fund balances) + fixed assets (NBV);
    financed by trust funds payable, accumulated local funds, and a capital fund
    matching the carrying value of fixed assets."""
    template_name = "reports/financial_position.html"

    def get(self, request, *args, **kwargs):
        try:
            as_of = _dt.date.fromisoformat(request.GET.get("as_of", ""))
        except ValueError:
            as_of = _dt.date.today()
        rows = balances.department_summary(None, as_of)
        cash = sum((r["closing"] for r in rows), Decimal(0))
        trust_payable = sum((r["closing"] for r in rows if r["is_trust"]), Decimal(0))
        # Split the trust liability into RECEIPTED (firmly due to remit) vs
        # not-yet-receipted (trust money allocated to a trust fund but without a
        # formal receipt). The receipted figure comes from the trust summary
        # (opening + receipted − remitted); the remainder of the closing balance
        # is the unreceipted portion, so the two always sum to the trust payable
        # that ties the balance sheet. Bank money not yet allocated to any fund is
        # a DIFFERENT thing (suspense) and is shown on its own line below.
        _tsum = balances.trust_summary(None, as_of)
        trust_receipted = sum((r["to_remit"] for r in _tsum), Decimal(0))
        trust_unreceipted = trust_payable - trust_receipted
        local_rows = [r for r in rows if not r["is_trust"]]
        local_funds = sum((r["closing"] for r in local_rows), Decimal(0))
        from assets.models import nbv_total
        nbv = nbv_total(as_of)
        # Net assets, classified per the SDA framework: Board-designated funds
        # (development/projects) are "Allocated"; the rest are "Unallocated"; the
        # carrying value of property is held as "Invested in property".
        allocated = sum((r["closing"] for r in local_rows
                         if r["department"].category == "DEVELOPMENT"), Decimal(0))
        unallocated = local_funds - allocated
        # Accrual overlay (memoranda): credit purchases owed, expenses accrued, and
        # amounts prepaid. These adjust the cash-basis position to an accrual view.
        from cashbook.views import (open_payables_total, open_accruals_total,
                                     unexpired_prepayments_total,
                                     outstanding_advances_total)
        payables = open_payables_total(as_of)
        accruals = open_accruals_total(as_of)
        prepaid = unexpired_prepayments_total(as_of)
        # An unspent staff advance is cash that has physically left but not yet been
        # expensed — a receivable. Reclassify it out of cash so each is shown
        # correctly; totals are unchanged (it is still inside the cash figure).
        advances = outstanding_advances_total(as_of)
        # The petty-cash float is the same story in the other direction: cash
        # physically held in the petty box rather than at the bank. It is inside
        # the fund cash figure (petty disbursements are real fund expenses;
        # top-ups merely move cash between locations), so it is reclassified out
        # of "Cash & bank" onto its own line — exactly the staff-advance
        # treatment. Totals are unchanged.
        from cashbook.services.treasury_position import petty_balance_asof
        petty = petty_balance_asof(as_of)
        cash_on_hand = cash - advances - petty
        # Bank money received but not yet receipted/allocated to a fund — shown as
        # cash held in suspense with a matching "pending allocation" liability, so
        # the statement ties to the bank and the money is never invisible.
        pending = balances.pending_receipts_total(as_of)
        # Loans payable: the outstanding loan principal is a real liability, split
        # into current (≤12 months / on demand) and long-term. The loan cash is
        # already inside the `cash` asset figure (a loan receipt raises the fund's
        # balance), so recognising the matching liability here is exactly what
        # keeps the statement in balance once loans exist. This total ties to the
        # LOANS_PAYABLE ledger account by construction.
        from loans.services import reporting as loan_rep
        loan_liab = loan_rep.outstanding_liability(as_of)
        loans_current = loan_liab["current"]
        loans_long_term = loan_liab["long_term"]
        loans_payable = loan_liab["total"]
        accrual_adj = prepaid - payables - accruals
        # Loans payable is a liability the church must settle from its own cash,
        # so it reduces net assets (unlike trust payable, which sits against
        # trust cash held on the field's behalf). Deducting it here keeps
        # Assets = Liabilities + Net assets true.
        # Net assets come from the registry, so this statement and anything that
        # reconciles to it are reading one definition rather than two.
        from core.metrics import metrics as _metrics
        _na = _metrics.net_assets(as_of)
        net_assets = _na["total"]
        total_assets = cash_on_hand + petty + advances + pending + nbv + prepaid
        total_liabilities = (trust_payable + payables + accruals + pending
                             + loans_payable)
        total_liab_and_na = total_liabilities + net_assets
        # committed-but-unpaid vouchers (memorandum)
        unpaid = (Expense.objects.filter(status__in=[Expense.Status.PENDING,
                  Expense.Status.APPROVED], date__lte=as_of)
                  .aggregate(t=Sum("amount"))["t"] or Decimal(0))
        ctx = self.get_context_data(**kwargs)
        _cfg = SiteConfig.get()
        if request.GET.get("export") in ("csv", "xlsx"):
            header = ["Section", "Line", "Amount"]
            data = [
                ["Assets", "Bank (current)", cash_on_hand],
                ["Assets", "Petty cash float", petty],
                ["Assets", "Staff advances (receivable)", advances],
                ["Assets", "Bank receipts pending allocation", pending],
                ["Assets", "Prepayments", prepaid],
                ["Assets", "Property, plant & equipment (non-current)", nbv],
                ["Assets", "TOTAL ASSETS", total_assets],
                ["Liabilities", "Trust funds payable to the field", trust_payable],
                ["Liabilities", "Payables", payables],
                ["Liabilities", "Accruals", accruals],
                ["Liabilities", "Receipts pending allocation", pending],
                ["Liabilities", "Loans payable — current", loans_current],
                ["Liabilities", "Loans payable — long-term", loans_long_term],
                ["Liabilities", "TOTAL LIABILITIES", total_liabilities],
                ["Net assets", "General net assets", unallocated],
                ["Net assets", "Designated development funds", allocated],
                ["Net assets", "Invested in property", nbv],
                ["Net assets", "Accrual adjustment", accrual_adj],
                ["Net assets", "TOTAL NET ASSETS", net_assets],
                ["", "TOTAL LIABILITIES & NET ASSETS", total_liab_and_na],
            ]
            ex = _export(request, f"financial_position_{as_of}", header, data,
                         "Statement of Financial Position")
            if ex:
                return ex
        ctx.update({"as_of": as_of, "cash": cash, "nbv": nbv,
                    "cash_on_hand": cash_on_hand, "advances": advances,
                    "petty": petty,
                    "pending": pending,
                    "trust_payable": trust_payable, "local_funds": local_funds,
                    "trust_receipted": trust_receipted,
                    "trust_unreceipted": trust_unreceipted,
                    "trust_total_payable": trust_payable,
                    "unallocated": unallocated, "allocated": allocated,
                    "net_assets": net_assets, "total_assets": total_assets,
                    "total_liab_and_na": total_liab_and_na,
                    "loans_payable": loans_payable, "loans_current": loans_current,
                    "loans_long_term": loans_long_term,
                    "payables": payables, "accruals": accruals, "prepaid": prepaid,
                    "accrual_adj": accrual_adj, "total_liabilities": total_liabilities,
                    "balanced": total_assets == total_liab_and_na,
                    "unpaid": unpaid, "trust_rows": [r for r in rows if r["is_trust"]],
                    "local_rows": [r for r in local_rows if r["closing"]],
                    "opening_bank": _cfg.opening_bank_balance,
                    "opening_cash_on_hand": _cfg.opening_cash_on_hand,
                    "opening_unremitted_trust": _cfg.opening_unremitted_trust,
                    "opening_total": (_cfg.opening_bank_balance
                                      + _cfg.opening_cash_on_hand
                                      - _cfg.opening_unremitted_trust),
                    "church": SiteConfig.get().church_name})
        return self.render_to_response(ctx)

class ChangesInNetAssetsView(PeriodMixin, TemplateView):
    """Statement of Changes in Net Assets — how each class of net assets moved over
    the period: opening + surplus/(deficit) +/- capital reclassification +/-
    transfers = closing. Classified into Unallocated, Allocated (Board-designated)
    and Invested in property, and ties to the Statement of Financial Position."""
    template_name = "reports/changes_in_net_assets.html"

    def get(self, request, *args, **kwargs):
        ctx = self.get_context_data(**kwargs)
        s, e = ctx["start"], ctx["end"]
        rows = balances.department_summary(s, e)
        local = [r for r in rows if not r["is_trust"]]

        def is_alloc(r):
            return r["department"].category == "DEVELOPMENT"

        eff = Q(status__in=[Expense.Status.APPROVED, Expense.Status.PAID],
                date__gte=s, date__lte=e)

        def cap_for(alloc):
            qs = (Expense.objects.filter(eff, expenditure_type=Expense.ExpenditureType.CAPITAL,
                  department__fund_type="LOCAL").exclude(doc_class=Expense.DocClass.LIABILITY))
            tot = Decimal(0)
            for x in qs.select_related("department"):
                if (x.department.category == "DEVELOPMENT") == alloc:
                    tot += x.amount
            return tot

        def col(alloc):
            sub = [r for r in local if is_alloc(r) == alloc]
            opening = sum((r["opening"] for r in sub), Decimal(0))
            receipts = sum((r["receipts"] for r in sub), Decimal(0))
            expenses = sum((r["expenses"] for r in sub), Decimal(0))
            transfers = sum((r["net_transfer"] for r in sub), Decimal(0))
            closing = sum((r["closing"] for r in sub), Decimal(0))
            capital = cap_for(alloc)
            op_surplus = receipts - (expenses - capital)   # surplus before capital
            return {"opening": opening, "op_surplus": op_surplus, "capital": capital,
                    "transfers": transfers, "closing": closing}

        un, al = col(False), col(True)
        cap_total = un["capital"] + al["capital"]
        day_before = s - _dt.timedelta(days=1)
        from core.metrics import metrics as _m
        # For a period that has not finished, every as-at asset figure is stated
        # at today, not at the period end — otherwise closing net book value
        # would carry a full year of depreciation while the income and
        # expenditure beside it show only what has actually happened.
        as_at = min(e, _dt.date.today())
        nbv_open = _m.net_book_value(day_before)
        nbv_close = _m.net_book_value(as_at)
        # Depreciation is a posted figure now, so read it rather than deriving it.
        # It used to be the balancing number (closing NBV less opening less
        # additions), which silently swept up disposals AND donated assets — a
        # donation raised NBV and so read as negative depreciation.
        # depreciation is only charged up to today: for a period that has not
        # finished, projecting the remaining months would overstate it against
        # the income and expenditure beside it, which are actuals
        donated = _m.donated_assets(s, as_at)
        # what actually joined the register — capital spending still held as work
        # in progress has not, so it is not an addition
        additions = _m.asset_additions_at_cost(s, as_at)
        # the charge runs from the opening position (the day before the period),
        # because that is the date the opening net book value is stated at
        depr = _m.depreciation_expense(day_before, as_at) if as_at >= s else Decimal(0)
        disposals = _m.disposed_carrying_value(s, as_at)
        # every line is now a real figure, so the movement is a genuine check
        # rather than one line absorbing whatever is left over
        unexplained = nbv_open + additions - depr - disposals - nbv_close
        prop = {"opening": nbv_open, "additions": additions, "capital": cap_total,
                "donated": donated, "depr": depr, "disposals": disposals,
                # deductions, negative so money_acct brackets them in the house style
                "depr_charge": -depr, "disposals_out": -disposals,
                "unexplained": unexplained, "closing": nbv_close}

        t_open = un["opening"] + al["opening"] + nbv_open
        t_opsurplus = un["op_surplus"] + al["op_surplus"]
        t_transfers = un["transfers"] + al["transfers"]
        t_close = un["closing"] + al["closing"] + nbv_close

        if request.GET.get("export") in ("csv", "xlsx"):
            header = ["Line", "General net assets", "Designated development funds", "Invested in property", "Total"]
            data = [
                ["Net assets, beginning", un["opening"], al["opening"], nbv_open, t_open],
                ["Surplus/(deficit) from operations", un["op_surplus"], al["op_surplus"], 0, t_opsurplus],
                ["Capital expenditure (property acquired)", -un["capital"], -al["capital"], cap_total, 0],
                ["Donated assets received", 0, 0, donated, donated],
                ["Depreciation", 0, 0, -depr, -depr],
                ["Assets disposed of (carrying value)", 0, 0, -disposals, -disposals],
                ["Net inter-fund transfers", un["transfers"], al["transfers"], 0, t_transfers],
                ["Net assets, end", un["closing"], al["closing"], nbv_close, t_close],
            ]
            ex = _export(request, f"changes_in_net_assets_{s}_{e}", header, data,
                         "Statement of Changes in Net Assets")
            if ex:
                return ex
        ctx.update({"un": un, "al": al, "prop": prop, "cap_total": cap_total,
                    "donated": donated, "disposals": disposals,
                    "depr_charge": -depr, "disposals_out": -disposals,
                    "t_open": t_open, "t_opsurplus": t_opsurplus, "depr": depr,
                    "t_transfers": t_transfers, "t_close": t_close,
                    "church": SiteConfig.get().church_name})
        return self.render_to_response(ctx)

class StatementOfCashFlowsView(PeriodMixin, TemplateView):
    """Statement of Cash Flows on the SDA three-category basis (operating, investing,
    financing). Reconciles the movement in total cash & bank over the period."""
    template_name = "reports/cash_flows.html"

    def get(self, request, *args, **kwargs):
        ctx = self.get_context_data(**kwargs)
        s, e = ctx["start"], ctx["end"]
        rows = balances.department_summary(s, e)
        cash_open = sum((r["opening"] for r in rows), Decimal(0))
        cash_close = sum((r["closing"] for r in rows), Decimal(0))
        local_receipts = sum((r["receipts"] for r in rows if not r["is_trust"]), Decimal(0))
        trust_receipts = sum((r["receipts"] for r in rows if r["is_trust"]), Decimal(0))
        eff = Q(status__in=[Expense.Status.APPROVED, Expense.Status.PAID],
                date__gte=s, date__lte=e)

        def _sum(qs):
            return qs.aggregate(t=Sum("amount"))["t"] or Decimal(0)

        remittances = _sum(Expense.objects.filter(eff, category=Expense.Category.REMITTANCE))
        nonremit = Expense.objects.filter(eff).exclude(doc_class=Expense.DocClass.LIABILITY)
        total_nonremit = _sum(nonremit)
        capital = _sum(nonremit.filter(expenditure_type=Expense.ExpenditureType.CAPITAL))
        # everything non-remittance that isn't explicitly capital is operating —
        # this way the three buckets always sum to total expenses, so the
        # statement reconciles even if some rows have no expenditure type set.
        operating_exp = total_nonremit - capital

        # Financing activities: loan receipts (cash in) and principal repayments
        # (cash out) belong here, never in operating.
        #  * `local_receipts` (from department_summary/receipts_by_department)
        #    INCLUDES loan receipts as fund cash, so they must be SUBTRACTED
        #    out of operating and shown in financing instead (not added twice).
        #  * principal repayments were excluded from `nonremit`, so they never
        #    hit operating expenses; they reduce cash only here in financing.
        #  * interest paid stays inside operating expenses per system policy
        #    (an ordinary voucher on the fund) — no adjustment needed.
        from loans.services import reporting as loan_rep
        fin = loan_rep.financing_activity(s, e)
        loan_receipts = fin["receipts"]
        loan_repayments = fin["repayments"]
        # Loan conversions / write-offs recognise income with NO cash movement
        # (a liability is reclassified to income). That income leg is a normal,
        # non-excluded contribution credit, so it is inside `local_receipts` —
        # remove it here so operating cash receipts reflect only real cash in.
        # (Its contra LOAN_REPAYMENT leg was already excluded from operating
        # expenses, so removing this keeps the statement reconciling.)
        loan_noncash_income = loan_rep.retirement_income(s, e)
        local_operating_receipts = local_receipts - loan_receipts - loan_noncash_income

        net_operating = local_operating_receipts + trust_receipts - operating_exp - remittances
        net_investing = -capital
        net_financing = loan_receipts - loan_repayments
        net_change = net_operating + net_investing + net_financing

        if request.GET.get("export") in ("csv", "xlsx"):
            header = ["Section", "Line", "Amount"]
            data = [
                ["Operating", "Local offerings & income received", local_operating_receipts],
                ["Operating", "Tithe & trust offerings received (held for the field)", trust_receipts],
                ["Operating", "Operating (recurrent) expenses paid", -operating_exp],
                ["Operating", "Remittances to the field paid", -remittances],
                ["Operating", "Net cash from operating activities", net_operating],
                ["Investing", "Purchase of property & equipment", -capital],
                ["Investing", "Net cash used in investing activities", net_investing],
                ["Financing", "Loan receipts (borrowings)", loan_receipts],
                ["Financing", "Loan principal repayments", -loan_repayments],
                ["Financing", "Net cash from financing activities", net_financing],
                ["Summary", "Net increase/(decrease) in cash", net_change],
                ["Summary", "Cash & bank at beginning of period", cash_open],
                ["Summary", "Cash & bank at end of period", cash_open + net_change],
            ]
            ex = _export(request, f"cash_flows_{s}_{e}", header, data,
                         "Statement of Cash Flows")
            if ex:
                return ex
        ctx.update({"local_receipts": local_operating_receipts, "trust_receipts": trust_receipts,
                    "operating_exp": operating_exp, "remittances": remittances,
                    "capital": capital, "net_operating": net_operating,
                    "loan_receipts": loan_receipts, "loan_repayments": loan_repayments,
                    "net_investing": net_investing, "net_financing": net_financing,
                    "net_change": net_change, "cash_open": cash_open,
                    "cash_close": cash_close, "cash_end_calc": cash_open + net_change,
                    "ties": (cash_open + net_change) == cash_close,
                    "church": SiteConfig.get().church_name})
        return self.render_to_response(ctx)

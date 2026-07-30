"""Split from reports/views.py (P1-2). Behaviour identical; the
package __init__ reproduces the original module namespace."""
from decimal import Decimal
from django.contrib import messages
from django.db.models import Sum, Count, Q
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.views.generic import TemplateView
from core.permissions import (ReportAccessMixin, TreasurerRequiredMixin,
                              RightRequiredMixin, ReportAccessMixin)
from cashbook.models import Expense
from departments.models import Department
from giving.models import Transaction
from members.models import Member
from ..exports import csv_response
import datetime as dt
from core.models import SiteConfig
from ..exports import xlsx_response
from ._shared import PeriodMixin


class FundLedgerView(PeriodMixin, TemplateView):
    template_name = "reports/fund_ledger.html"

    def get(self, request, *args, **kwargs):
        if request.GET.get("export") in ("xlsx", "csv", "subgroups", "subgroups-csv"):
            return self._export(request, *args, **kwargs)
        return super().get(request, *args, **kwargs)

    def _export(self, request, *args, **kwargs):
        from reports.exports import csv_response, xlsx_response
        from core.models import SiteConfig
        ctx = self.get_context_data(**kwargs)
        dept = ctx["department"]
        mode = request.GET.get("export")

        # subgroup breakdown export (sub-accounts beneath this fund)
        if mode in ("subgroups", "subgroups-csv"):
            show_pay = ctx.get("sub_show_payments", True)
            header = (["ID", "Subgroup", "Type", "Opening", "Receipts", "Payments", "Closing balance"]
                      if show_pay else
                      ["ID", "Subgroup", "Type", "Opening", "Receipts", "Closing balance"])
            rows = []
            for r in ctx["subgroups"]:
                sub = r["sub"]
                row = [sub.id, sub.name, "Trust" if sub.is_trust else "Local",
                       float(r["opening"]), float(r["receipts"])]
                if show_pay:
                    row.append(float(r["payments"]))
                row.append(float(r["closing"]))
                rows.append(row)
            for r in ctx.get("dev_rows", []):
                g = r["group"]
                row = [getattr(g, "id", ""), g.name, "Local", "", float(r["receipts"])]
                if show_pay:
                    row.append("")
                row.append(float(r["receipts"]))
                rows.append(row)
            total_row = ["", "TOTAL", "", float(ctx["combined_opening"]), ""]
            if show_pay:
                total_row.append("")
            total_row.append(float(ctx["subgroup_total"]))
            rows.append(total_row)
            fname = f"fund-{dept.slug or dept.id}-subgroups-{ctx['start']}-{ctx['end']}"
            if mode == "subgroups-csv":
                return csv_response(fname + ".csv", header, rows)
            return xlsx_response(fname + ".xlsx", header, rows,
                                 title=f"{dept.name} — sub-accounts ({ctx['start']} to {ctx['end']})",
                                 church=SiteConfig.get().church_name)

        header = ["ID", "Type", "Date", "Description", "Debit", "Credit", "Balance"]
        rows = [["", "", "", "Opening balance", "", "", float(ctx["opening"])]]
        for en in ctx["entries"]:
            rows.append([en.get("ref_id", ""), en.get("src", ""),
                         en["date"].isoformat(), en["desc"],
                         float(en["debit"]) if en["debit"] else "",
                         float(en["credit"]) if en["credit"] else "",
                         float(en["balance"])])
        rows.append(["", "", "", "Closing balance", "", "", float(ctx["closing"])])
        fname = f"fund-{dept.slug or dept.id}-{ctx['start']}-{ctx['end']}"
        if request.GET["export"] == "csv":
            return csv_response(fname + ".csv", header, rows)
        return xlsx_response(fname + ".xlsx", header, rows,
                             title=f"{dept.name} ledger ({ctx['start']} to {ctx['end']})",
                             church=SiteConfig.get().church_name)

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        dept = get_object_or_404(Department, pk=kwargs["pk"])
        s, e = ctx["start"], ctx["end"]
        # ALL confirmed, non-reversed credits — including excluded_from_income
        # rows (loan receipts, asset-disposal proceeds and other non-income
        # cash). The fund ledger is a CASH statement of the fund: that money
        # genuinely entered the fund and the fund's opening/closing balances
        # (brought_forward / department_summary / fund_balance) include it, so
        # omitting the rows made the ledger irreconcilable with its own closing
        # balance whenever a loan was received in the period. They are shown,
        # clearly labelled as financing, and remain excluded from every INCOME
        # report (total_income / Income & Expenditure), which is the correct
        # accounting split: cash yes, income no.
        receipts = (Transaction.objects.confirmed_credits().filter(
            department=dept, date__gte=s, date__lte=e).order_by("date"))
        payments = Expense.objects.filter(
            department=dept, date__gte=s, date__lte=e,
            status__in=[Expense.Status.APPROVED, Expense.Status.PAID]).order_by("date")
        entries = []
        for t in receipts:
            if t.excluded_from_income:
                base = t.payer_name or t.reference or "Receipt"
                entries.append({"date": t.date,
                                "desc": f"{base} — loan / financing receipt "
                                        "(not income)",
                                "credit": t.amount, "debit": None,
                                "src": "Financing", "ref_id": t.id})
            else:
                entries.append({"date": t.date,
                                "desc": t.payer_name or t.reference or "Receipt",
                                "credit": t.amount, "debit": None,
                                "src": "Receipt", "ref_id": t.id})
        for x in payments:
            entries.append({"date": x.date, "desc": x.description,
                            "credit": None, "debit": x.amount, "src": "Expense", "ref_id": x.id})
        # expense refunds are contra-entries: cash returned to the fund. The
        # fund's balances net them against expenses (expenses_by_department /
        # fund_balance), so the ledger must show them or its closing balance
        # cannot tie whenever a refund exists in the period.
        from cashbook.models import ExpenseRefund
        for rf in (ExpenseRefund.objects.filter(
                expense__department=dept, date__gte=s, date__lte=e,
                expense__status__in=[Expense.Status.APPROVED, Expense.Status.PAID])
                .select_related("expense")):
            entries.append({"date": rf.date,
                            "desc": f"Refund — {rf.expense.description}",
                            "credit": rf.amount, "debit": None,
                            "src": "Refund", "ref_id": rf.id})
        from cashbook.models import FundTransfer
        for tr in FundTransfer.objects.filter(destination=dept, date__gte=s, date__lte=e):
            entries.append({"date": tr.date, "desc": f"Transfer from {tr.source.name}"
                            + (f" — {tr.reason}" if tr.reason else ""),
                            "credit": tr.amount, "debit": None, "src": "Transfer", "ref_id": tr.id})
        for tr in FundTransfer.objects.filter(source=dept, date__gte=s, date__lte=e):
            entries.append({"date": tr.date, "desc": f"Transfer to {tr.destination.name}"
                            + (f" — {tr.reason}" if tr.reason else ""),
                            "credit": None, "debit": tr.amount, "src": "Transfer", "ref_id": tr.id})
        entries.sort(key=lambda r: r["date"])
        # opening = founding opening_balance + all net movement before `s` (not
        # just the raw founding field), so a fund with real prior-period activity
        # shows its true brought-forward balance rather than zero.
        from reports.services.balances import brought_forward, brought_forward_map
        opening_bf = brought_forward(dept, s)
        running = opening_bf
        for en in entries:
            running += (en["credit"] or 0) - (en["debit"] or 0)
            en["balance"] = running
        ctx["department"] = dept
        ctx["entries"] = entries
        ctx["opening"] = opening_bf
        ctx["closing"] = running

        # roll up any sub-accounts beneath this fund (two grouped queries, not 2/sub)
        subs = list(dept.subgroups.all())
        sub_ids = [x.id for x in subs]
        sub_rec = {r["department"]: (r["t"] or Decimal(0)) for r in
                   Transaction.objects.filter(
                       department_id__in=sub_ids,
                       direction=Transaction.Direction.CREDIT,
                       date__gte=s, date__lte=e)
                   .values("department").annotate(t=Sum("amount"))}
        sub_pay = {r["department"]: (r["t"] or Decimal(0)) for r in
                   Expense.objects.filter(
                       department_id__in=sub_ids, date__gte=s, date__lte=e,
                       status__in=[Expense.Status.APPROVED, Expense.Status.PAID])
                   .values("department").annotate(t=Sum("amount"))}
        subs_rows = []
        sub_total = Decimal(0)
        sub_bf = brought_forward_map(sub_ids, s) if sub_ids else {}
        for sub in subs:
            r = sub_rec.get(sub.id, Decimal(0))
            p = sub_pay.get(sub.id, Decimal(0))
            opening = sub_bf.get(sub.id, Decimal(0))
            closing = opening + r - p
            subs_rows.append({"sub": sub, "opening": opening, "receipts": r,
                              "payments": p, "closing": closing})
            sub_total += closing
        subs_rows.sort(key=lambda x: x["closing"], reverse=True)   # largest balance first
        subs = subs_rows

        # development groups are sub-accounts of the Development fund (one query)
        from departments.models import DevelopmentGroup
        dev_rows = []
        if dept.name.lower() == "development":
            dev_map = {r["dev_group"]: (r["t"] or Decimal(0)) for r in
                       Transaction.objects.filter(
                           dev_group__isnull=False,
                           direction=Transaction.Direction.CREDIT,
                           date__gte=s, date__lte=e)
                       .values("dev_group").annotate(t=Sum("amount"))}
            for grp in DevelopmentGroup.objects.filter(active=True):
                r = dev_map.get(grp.id, Decimal(0))
                dev_rows.append({"group": grp, "receipts": r})
                sub_total += r
            dev_rows.sort(key=lambda x: x["receipts"], reverse=True)
        ctx["dev_rows"] = dev_rows
        ctx["subgroups"] = subs
        ctx["subgroup_total"] = sub_total
        ctx["combined_closing"] = running + sub_total

        # combined (parent + sub-accounts) figures for the top cards, since the
        # sub-accounts are part of the parent fund.
        parent_receipts = sum((en["credit"] or Decimal(0)) for en in entries)
        parent_payments = sum((en["debit"] or Decimal(0)) for en in entries)
        sub_receipts_total = sum(sub_rec.values(), Decimal(0)) + sum(
            (r["receipts"] for r in dev_rows), Decimal(0))
        sub_payments_total = sum(sub_pay.values(), Decimal(0))
        sub_opening_total = sum((r["opening"] for r in subs_rows), Decimal(0))
        ctx["has_subaccounts"] = bool(subs_rows or dev_rows)
        ctx["combined_opening"] = opening_bf + sub_opening_total
        ctx["combined_receipts"] = parent_receipts + sub_receipts_total
        ctx["combined_payments"] = parent_payments + sub_payments_total
        ctx["parent"] = dept.parent
        # collection-only funds never take expenses/payments; hide that column in
        # the sub-accounts table when this fund and every sub-account shown are
        # collection-only, so the summary reads opening/receipts/closing only.
        _cols = [dept] + [r["sub"] for r in subs_rows]
        ctx["sub_show_payments"] = not all(
            getattr(d, "collection_only", False) for d in _cols) if _cols else True
        return ctx

class FundMembersView(PeriodMixin, TemplateView):
    """Aggregated giving for a fund and all its sub-accounts, grouped by member,
    so leaders/treasurers can see how much each person has given. Complements the
    chronological fund ledger (which this links to and from)."""
    template_name = "reports/fund_members.html"

    def get(self, request, *args, **kwargs):
        if request.GET.get("export") in ("xlsx", "csv"):
            return self._export(request, *args, **kwargs)
        return super().get(request, *args, **kwargs)

    def _fund_ids(self, dept):
        # the fund itself plus every descendant sub-account
        ids, frontier = {dept.id}, [dept]
        while frontier:
            nxt = []
            for d in frontier:
                for sub in d.subgroups.all():
                    if sub.id not in ids:
                        ids.add(sub.id); nxt.append(sub)
            frontier = nxt
        return ids

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        dept = get_object_or_404(Department, pk=kwargs["pk"])
        s, e = ctx["start"], ctx["end"]
        fund_ids = self._fund_ids(dept)
        qs = (Transaction.objects.confirmed_credits().filter(
            department_id__in=fund_ids, date__gte=s, date__lte=e,
            excluded_from_income=False))
        # group by member (named) and, separately, anonymous/loose giving
        rows = {}
        anon_total, anon_count = Decimal(0), 0
        for r in (qs.values("member", "member__name", "payer_name")
                    .annotate(total=Sum("amount"), n=Count("id"))):
            mid = r["member"]
            if mid:
                key, name = mid, r["member__name"]
            elif (r["payer_name"] or "").strip():
                key, name = f"p:{r['payer_name'].strip().lower()}", r["payer_name"].strip()
            else:
                anon_total += r["total"] or Decimal(0); anon_count += r["n"]; continue
            if key in rows:
                rows[key]["total"] += r["total"] or Decimal(0)
                rows[key]["n"] += r["n"]
            else:
                rows[key] = {"member_id": mid, "name": name,
                             "total": r["total"] or Decimal(0), "n": r["n"]}
        members = sorted(rows.values(), key=lambda x: x["total"], reverse=True)
        named_total = sum((m["total"] for m in members), Decimal(0))
        ctx.update({
            "department": dept, "members": members,
            "named_total": named_total, "anon_total": anon_total,
            "anon_count": anon_count, "grand_total": named_total + anon_total,
            "giver_count": len(members), "subaccount_count": len(fund_ids) - 1,
        })
        return ctx

    def _export(self, request, *args, **kwargs):
        from reports.exports import csv_response, xlsx_response
        from core.models import SiteConfig
        ctx = self.get_context_data(**kwargs)
        dept = ctx["department"]
        header = ["Member", "Gifts", "Total"]
        rows = [[m["name"], m["n"], float(m["total"])] for m in ctx["members"]]
        if ctx["anon_total"]:
            rows.append(["(unnamed / loose)", ctx["anon_count"], float(ctx["anon_total"])])
        rows.append(["TOTAL", "", float(ctx["grand_total"])])
        fname = f"fund-{dept.slug or dept.id}-by-member-{ctx['start']}-{ctx['end']}"
        if request.GET["export"] == "csv":
            return csv_response(fname + ".csv", header, rows)
        return xlsx_response(fname + ".xlsx", header, rows,
                             title=f"{dept.name} — giving by member ({ctx['start']} to {ctx['end']})",
                             church=SiteConfig.get().church_name)

class FundThankSmsView(ReportAccessMixin, TemplateView):
    """Thank contributors to a fund (and its sub-accounts) for a period by SMS.

    Lumps each member's total giving across the fund and its sub-accounts within
    the selected period; the message is a customizable template. Treasurer only
    for sending; the preview is read-access."""
    template_name = "reports/fund_thank_sms.html"

    DEFAULT_TEMPLATE = ("Dear {name}, thank you for your contribution of KES {amount} "
                        "to {fund} ({period}). May God bless you. - {church}")

    def _period(self, request):
        import datetime as dt
        def _d(name, default):
            raw = request.GET.get(name) or request.POST.get(name)
            try:
                return dt.date.fromisoformat(raw) if raw else default
            except ValueError:
                return default
        today = dt.date.today()
        start = _d("start", today.replace(day=1))
        end = _d("end", today)
        return start, end

    def _recipients(self, dept, start, end):
        """[(member, total)] for members who gave to this fund or its sub-accounts
        in the period and have a phone on file."""
        from django.db.models import Sum
        from members.models import Member
        ids = [dept.id] + list(dept.subgroups.values_list("id", flat=True))
        rows = (Transaction.objects.filter(
                    department_id__in=ids, direction=Transaction.Direction.CREDIT,
                    confirmed=True, is_reversal=False, is_reversed=False,
                    excluded_from_income=False, member__isnull=False,
                    date__gte=start, date__lte=end)
                .values("member").annotate(t=Sum("amount")))
        totals = {r["member"]: r["t"] or Decimal(0) for r in rows}
        members = {m.id: m for m in Member.objects.filter(id__in=totals)}
        out = []
        for mid, total in totals.items():
            m = members.get(mid)
            if m and m.phone:
                out.append((m, total))
        out.sort(key=lambda x: x[1], reverse=True)
        return out

    def get_context_data(self, **kwargs):
        from core.models import SiteConfig
        from core.roles import is_treasurer
        ctx = super().get_context_data(**kwargs)
        dept = get_object_or_404(Department, pk=kwargs["pk"])
        start, end = self._period(self.request)
        recips = self._recipients(dept, start, end)
        ctx.update({
            "department": dept, "start": start, "end": end,
            "recipients": recips, "recipient_count": len(recips),
            "total": sum((t for _, t in recips), Decimal(0)),
            "template": self.DEFAULT_TEMPLATE,
            "church": SiteConfig.get().church_name or "",
            "can_send": is_treasurer(self.request.user) and SiteConfig.get().sms_enabled,
            "sms_enabled": SiteConfig.get().sms_enabled,
        })
        return ctx

    def post(self, request, *args, **kwargs):
        from core.roles import is_treasurer
        from core.models import SiteConfig
        from core.services.sms import send_sms, _format
        if not is_treasurer(request.user):
            messages.error(request, "Only a treasurer can send the thank-you messages.")
            return redirect("report_fund", pk=kwargs["pk"])
        dept = get_object_or_404(Department, pk=kwargs["pk"])
        start, end = self._period(request)
        template = request.POST.get("template") or self.DEFAULT_TEMPLATE
        church = SiteConfig.get().church_name or ""
        period_str = f"{start:%d %b %Y} – {end:%d %b %Y}"
        sent = failed = 0
        for member, total in self._recipients(dept, start, end):
            msg = _format(template, name=member.name.split()[0] if member.name else "member",
                          amount=f"{total:,.0f}", fund=dept.name,
                          period=period_str, church=church)
            log = send_sms(member.phone, msg)
            if getattr(log, "status", "") == "SENT":
                sent += 1
            else:
                failed += 1
        if sent:
            messages.success(request, f"Thank-you SMS sent to {sent} contributor(s)"
                                      + (f"; {failed} failed." if failed else "."))
        else:
            messages.error(request, "No messages were sent. "
                                    + ("Check SMS settings." if failed else "No recipients with a phone."))
        return redirect(f"{reverse('report_fund', kwargs={'pk': dept.id})}?start={start}&end={end}")

class BankPositionView(ReportAccessMixin, TemplateView):
    """Bank reconciliation: does the system's bank balance agree with the bank?

    The system's bank position = opening bank balance + every confirmed BANK
    credit − every confirmed BANK debit (expenses paid from the bank appear as
    debit rows). The bank's own figure is the closing running balance of the most
    recent imported statement. If the two differ, an entry is on the statement but
    not in the app (or vice versa) — exactly the un-entered-entry case. We show the
    gap and list the most likely culprits so the treasurer can chase them.
    """
    template_name = "reports/bank_position.html"

    def get_context_data(self, **kwargs):
        from decimal import Decimal
        ctx = super().get_context_data(**kwargs)
        # the calculation lives in reports.services.balances.bank_position (the
        # bank_position registry metric) — this view only presents it
        from reports.services.balances import bank_position
        pos = bank_position()
        stmt = pos["stmt"]
        ctx["stmt"] = stmt
        ctx["opening"] = pos["opening"]
        ctx["bank_credits"] = pos["bank_credits"]
        ctx["bank_debits"] = pos["bank_debits"]
        ctx["bank_expenses"] = pos["bank_expenses"]
        ctx["system_balance"] = pos["system_balance"]
        ctx["statement_balance"] = pos["statement_balance"]
        # Where the bank's figure came from and how old it is. Copied through
        # rather than recomputed: the service decides which source wins, and a
        # view that worked it out again could disagree with the number it is
        # printing beside.
        for key in ("balance_source", "balance_stale_days", "balance_note",
                    "cleared_balance", "statement_date",
                    "register_balance", "register_as_at",
                    "live_balance", "live_as_at"):
            ctx[key] = pos.get(key)
        ctx["difference"] = pos["difference"]

        # real-time cleared balance from the CBS feed (independent of an imported
        # statement) — often more current than the last uploaded statement.
        from statements.services.importer import latest_cleared_balance
        live = latest_cleared_balance()
        ctx["live_balance"] = live
        ctx["live_difference"] = ((live["balance"] - pos["system_balance"])
                                  if live else None)

        # if there is a gap, surface candidates: recent bank rows that look
        # suspicious (unallocated, in review, or unconfirmed) which often explain
        # a difference, plus a note about the statement's own integrity check.
        ctx["suspects"] = []
        if stmt and ctx["difference"] and abs(ctx["difference"]) > Decimal("0.01"):
            suspects = (Transaction.objects.filter(
                channel=Transaction.Channel.BANK)
                .filter(Q(confirmed=False) | Q(allocation_status="REVIEW")
                        | Q(department__isnull=True))
                .order_by("-date")[:50])
            ctx["suspects"] = [{
                "date": t.date, "payer": t.payer_name or "—",
                "amount": t.amount if t.direction == "CREDIT" else -t.amount,
                "ref": t.mpesa_ref or t.core_ref or t.reference or "",
                "why": ("not confirmed" if not t.confirmed
                        else "in review" if t.allocation_status == "REVIEW"
                        else "no fund"),
                "id": t.id} for t in suspects]
            ctx["stmt_integrity"] = stmt.balance_check
            ctx["stmt_integrity_detail"] = stmt.balance_detail
        return ctx

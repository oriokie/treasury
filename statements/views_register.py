"""Bank Statement Register views — the bank's own record, kept separately."""
import datetime as _dt
from decimal import Decimal

from django.contrib import messages
from django.core.paginator import Paginator
from django.db.models import Q
from django.shortcuts import get_object_or_404, redirect, render
from django.views import View

from core.permissions import DataEntryRequiredMixin, ReadAccessMixin
from core.utils import default_to_current_month

from .models import BankAccount
from .models_register import RegisterException, StatementLine, StatementRegisterImport
from .services import register as reg_svc


def _account(request):
    """The account being viewed. Most churches have one; the picker only
    matters where there are several."""
    acc_id = request.GET.get("account") or request.POST.get("account")
    if acc_id:
        return BankAccount.objects.filter(pk=acc_id).first()
    return BankAccount.objects.order_by("id").first()


class RegisterView(ReadAccessMixin, View):
    """The running bank statement: every line the bank ever sent, in order,
    with a running balance beside the bank's own."""
    template_name = "statements/register.html"

    def get(self, request):
        account = _account(request)
        if account is None:
            return render(request, self.template_name, {"no_account": True})

        start, end = default_to_current_month(request, from_param="start",
                                              to_param="end")
        data = reg_svc.running(account, start=start, end=end)

        q = (request.GET.get("q") or "").strip()
        rows = data["rows"]
        if q:
            ql = q.lower()
            rows = [r for r in rows
                    if ql in (r["line"].raw_narration or "").lower()
                    or ql in (r["line"].payer_name or "").lower()
                    or ql in (r["line"].dedup_key or "").lower()
                    or ql in (r["line"].reference or "").lower()]

        export = request.GET.get("export")
        if export in ("csv", "xlsx"):
            return self._export(account, rows, data, export, start, end)

        page = Paginator(rows, 100).get_page(request.GET.get("page"))
        return render(request, self.template_name, {
            "account": account,
            "accounts": BankAccount.objects.all(),
            "opening": data["opening"],
            "closing": data["closing"],
            "page_obj": page, "rows": page.object_list,
            "row_count": len(rows),
            "summary": reg_svc.summary(account),
            "start": start, "end": end, "q": q,
            "date_default_applied": not request.GET,
        })

    def _export(self, account, rows, data, fmt, start, end):
        """Download the register — the whole filtered window, not just the page
        being looked at, because someone exporting a statement wants the
        statement, not a screenshot of it."""
        from reports.exports import csv_response, xlsx_response

        header = ["Date", "Narration", "Payer", "Phone", "Reference",
                  "In", "Out", "Running balance", "Bank's own balance", "Drift"]
        out = []
        for r in rows:
            ln = r["line"]
            out.append([
                ln.date.isoformat(),
                ln.raw_narration or "",
                ln.payer_name or "",
                ln.payer_phone or "",
                ln.dedup_key,
                ln.credit or "",
                ln.debit or "",
                r["running"],
                ln.bank_balance if ln.bank_balance is not None else "",
                r["drift"] if r["drift"] else "",
            ])
        # the opening balance is part of the statement, not decoration — a
        # register exported without it cannot be checked by anyone
        out.insert(0, [start.isoformat() if start else "", "OPENING BALANCE", "", "", "",
                       "", "", data["opening"], "", ""])
        out.append(["", "CLOSING BALANCE", "", "", "", "", "", data["closing"], "", ""])

        span = ""
        if start and end:
            span = f"_{start:%Y%m%d}-{end:%Y%m%d}"
        stem = f"bank_register_{account.name.replace(' ', '_')}{span}"

        if fmt == "csv":
            return csv_response(f"{stem}.csv", header, out)
        from core.models import SiteConfig
        return xlsx_response(
            f"{stem}.xlsx", header, out,
            title=f"Bank statement register — {account.name}"
                  + (f" ({start:%d %b %Y} – {end:%d %b %Y})" if start and end else ""),
            church=SiteConfig.get().church_name)


class RegisterImportView(DataEntryRequiredMixin, View):
    """Import a bank file into the register.

    Deliberately its OWN import, not a reuse of the ledger importer's screen —
    because they do genuinely different things and conflating them is how
    someone double-posts a month. This one asserts nothing about the money, so
    it is safe to re-run over any period, as often as you like: re-importing
    January every month adds only what is new.
    """
    template_name = "statements/register_import.html"

    def get(self, request):
        return render(request, self.template_name, {
            "accounts": BankAccount.objects.all(),
            "account": _account(request),
            "recent": StatementRegisterImport.objects.select_related(
                "account", "uploaded_by")[:10],
        })

    def post(self, request):
        account = _account(request)
        f = request.FILES.get("file")
        if account is None:
            messages.error(request, "Add a bank account first.")
            return redirect("bank_register_import")
        if not f:
            messages.error(request, "Choose a statement file (.csv, .xls or .xlsx).")
            return redirect("bank_register_import")
        try:
            imp = reg_svc.import_file(
                account, path_or_bytes=f.read(), filename=f.name,
                user=request.user, notes=(request.POST.get("notes") or ""))
        except ValueError as e:
            messages.error(request, str(e))
            return redirect("bank_register_import")
        except Exception:  # noqa: BLE001
            from core.utils import log_exception
            log_exception("statements/views_register.py")
            messages.error(request, "Could not read that file.")
            return redirect("bank_register_import")

        msg = (f"{imp.lines_added} new line(s) added"
               f"{f' from {imp.period_start:%d %b %Y} to {imp.period_end:%d %b %Y}' if imp.period_start else ''}. "
               f"{imp.duplicates_skipped} already in the register (skipped).")
        if imp.rows_failed:
            msg += f" {imp.rows_failed} row(s) could not be read."
        messages.success(request, msg)

        # Importing new bank lines is exactly the moment the exception picture
        # changes — so re-check straight away rather than making someone
        # remember to.
        result = reg_svc.recheck(account)
        if result["opened"] or result["auto_closed"]:
            messages.info(
                request,
                f"Exceptions re-checked: {result['opened']} new, "
                f"{result['auto_closed']} closed automatically, "
                f"{result['open']} still open.")
        return redirect(f"/bank-register/?account={account.pk}")


class RegisterExceptionsView(ReadAccessMixin, View):
    """Where the bank's record and ours disagree."""
    template_name = "statements/register_exceptions.html"

    def get(self, request):
        account = _account(request)
        if account is None:
            return render(request, self.template_name, {"no_account": True})

        f_kind = request.GET.get("kind") or ""
        f_status = request.GET.get("status") or RegisterException.Status.OPEN
        qs = (RegisterException.objects.filter(account=account)
              .select_related("line", "transaction", "transaction__department",
                              "resolved_by"))
        if f_kind:
            qs = qs.filter(kind=f_kind)
        if f_status:
            qs = qs.filter(status=f_status)

        page = Paginator(qs, 50).get_page(request.GET.get("page"))
        return render(request, self.template_name, {
            "account": account,
            "accounts": BankAccount.objects.all(),
            "page_obj": page, "exceptions": page.object_list,
            "kinds": RegisterException.Kind.choices,
            "statuses": RegisterException.Status.choices,
            "f_kind": f_kind, "f_status": f_status,
            "summary": reg_svc.summary(account),
            "unverifiable": reg_svc.unverifiable(account)[:50],
        })

    def post(self, request):
        account = _account(request)
        if request.POST.get("recheck"):
            result = reg_svc.recheck(account)
            messages.success(
                request,
                f"Checked. {result['matched']} statement line(s) matched a "
                f"transaction. {result['opened']} new exception(s), "
                f"{result['auto_closed']} closed automatically, "
                f"{result['open']} open now.")
            return redirect(f"/bank-register/exceptions/?account={account.pk}")

        exc_id = request.POST.get("resolve")
        if exc_id:
            exc = get_object_or_404(RegisterException, pk=exc_id, account=account)
            reason = (request.POST.get("resolution") or "").strip()
            if not reason:
                messages.error(request, "Say what the explanation is — an exception "
                                        "closed without one teaches the next person "
                                        "nothing.")
            else:
                reg_svc.resolve(exc, user=request.user, resolution=reason,
                                ignore=bool(request.POST.get("ignore")))
                messages.success(request, "Exception closed.")
        return redirect(f"/bank-register/exceptions/?account={account.pk}")

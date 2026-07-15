"""Bank Statement Register views — the bank's own record, kept separately."""
import datetime as _dt
from decimal import Decimal

from django.contrib import messages
from django.core.exceptions import ValidationError
from django.core.paginator import Paginator
from django.db.models import Q
from django.shortcuts import get_object_or_404, redirect, render
from django.views import View

from core.permissions import DataEntryRequiredMixin, ReadAccessMixin
from core.roles import can_enter_data
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

        # Can the opening balance be derived from the bank's own figures? If the
        # statements carry a running-balance column we already know the opening
        # and nobody needs to type one — so we only ASK when we genuinely cannot
        # work it out, rather than demanding a number the bank has already given
        # us and which a person could only get wrong.
        first_line = (StatementLine.objects.filter(account=account)
                      .order_by("date", "occurred_at", "id").first())
        needs_opening = (first_line is not None
                         and first_line.bank_balance is None
                         and account.register_opening_balance is None)

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
            "needs_opening": needs_opening,
            "date_default_applied": not request.GET,
        })

    def post(self, request):
        """Set the register's opening balance."""
        account = _account(request)
        if account is None:
            return redirect("bank_register")
        raw = (request.POST.get("register_opening_balance") or "").strip()
        raw_date = (request.POST.get("register_opening_date") or "").strip()
        from decimal import InvalidOperation
        from django.utils.dateparse import parse_date
        try:
            account.register_opening_balance = Decimal(raw) if raw else None
        except InvalidOperation:
            messages.error(request, "That opening balance isn't a number.")
            return redirect(f"/bank-register/?account={account.pk}")
        account.register_opening_date = parse_date(raw_date) if raw_date else None
        account.save(update_fields=["register_opening_balance",
                                    "register_opening_date"])
        messages.success(
            request, "Opening balance saved. The running balance now starts from it.")
        return redirect(f"/bank-register/?account={account.pk}")

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

        # purge a same-day register upload (a wrong file / wrong account)
        purge_id = request.POST.get("purge")
        if purge_id:
            imp = get_object_or_404(StatementRegisterImport, pk=purge_id)
            try:
                result = reg_svc.purge_import(imp, user=request.user)
                messages.success(
                    request,
                    f"Upload undone — {result['lines_removed']} line(s) removed"
                    + (f", {result['exceptions_removed']} exception(s) cleared"
                       if result['exceptions_removed'] else "") + ".")
                # the exception picture changed; re-check
                if imp.account_id:
                    reg_svc.recheck(imp.account)
            except ValidationError as e:
                messages.error(request, "; ".join(e.messages))
            back = f"?account={imp.account_id}" if imp.account_id else ""
            return redirect(f"/bank-register/import/{back}")

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
        from departments.models import Department
        from statements.services import exceptions_intake as ei
        # precompute which dispositions fit each exception, for the per-row UI
        rows = []
        for exc in page.object_list:
            rows.append({"exc": exc,
                         "dispositions": [(d, ei.DISPOSITIONS[d])
                                          for d in ei.applicable_dispositions(exc)]})
        return render(request, self.template_name, {
            "account": account,
            "accounts": BankAccount.objects.all(),
            "page_obj": page, "exceptions": page.object_list,
            "rows": rows,
            "kinds": RegisterException.Kind.choices,
            "statuses": RegisterException.Status.choices,
            "f_kind": f_kind, "f_status": f_status,
            "summary": reg_svc.summary(account),
            "unverifiable": reg_svc.unverifiable(account)[:50],
            "funds": Department.objects.filter(active=True).order_by("name"),
            "dispositions": ei.DISPOSITIONS,
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

        # take a SINGLE exception to the books with a chosen disposition
        take_id = request.POST.get("take_to_books")
        if take_id:
            return self._take_single(request, account, take_id)

        # bulk: one disposition applied to several selected exceptions
        if request.POST.get("bulk_take"):
            return self._bulk_take(request, account)

        return redirect(f"/bank-register/exceptions/?account={account.pk}")

    def _take_single(self, request, account, take_id):
        if not can_enter_data(request.user):
            messages.error(request, "You do not have permission to take entries to "
                                    "the books.")
            return redirect(f"/bank-register/exceptions/?account={account.pk}")
        from statements.services import exceptions_intake as ei
        from departments.models import Department
        exc = get_object_or_404(RegisterException, pk=take_id, account=account)
        disposition = request.POST.get("disposition") or ""
        note = (request.POST.get("note") or "").strip()
        dept = None
        if request.POST.get("department"):
            dept = Department.objects.filter(pk=request.POST["department"]).first()
        linked = request.POST.getlist("linked_transaction_ids")
        try:
            result = ei.take_to_books(
                exc, disposition=disposition, user=request.user, account=account,
                note=note, department=dept, linked_transaction_ids=linked)
            messages.success(request, result["message"])
        except ei.DispositionNotApplicable as e:
            messages.error(request, "; ".join(e.messages))
        except ValidationError as e:
            messages.error(request, "; ".join(e.messages))
        return redirect(f"/bank-register/exceptions/?account={account.pk}")

    def _bulk_take(self, request, account):
        if not can_enter_data(request.user):
            messages.error(request, "You do not have permission to take entries to "
                                    "the books.")
            return redirect(f"/bank-register/exceptions/?account={account.pk}")
        from statements.services import exceptions_intake as ei
        from departments.models import Department
        ids = request.POST.getlist("selected")
        disposition = request.POST.get("bulk_disposition") or ""
        note = (request.POST.get("bulk_note") or "").strip()
        dept = None
        if request.POST.get("bulk_department"):
            dept = Department.objects.filter(pk=request.POST["bulk_department"]).first()
        if not ids:
            messages.error(request, "Select at least one exception to act on.")
            return redirect(f"/bank-register/exceptions/?account={account.pk}")
        if disposition not in ei.DISPOSITIONS:
            messages.error(request, "Choose what to do with the selected exceptions.")
            return redirect(f"/bank-register/exceptions/?account={account.pk}")
        if disposition == ei.BANK_CHARGE and dept is None:
            messages.error(request, "Choose the fund the bank charges should be "
                                    "posted to.")
            return redirect(f"/bank-register/exceptions/?account={account.pk}")

        exceptions = list(RegisterException.objects.filter(
            pk__in=ids, account=account))
        outcome = ei.bulk_take_to_books(
            exceptions, disposition=disposition, user=request.user,
            account=account, note=note, department=dept)

        done, skipped = outcome["done"], outcome["skipped"]
        if done:
            messages.success(
                request, f"{len(done)} exception(s) taken to the books as "
                         f"'{ei.DISPOSITIONS[disposition].split(' — ')[0]}'.")
        if skipped:
            detail = "; ".join(
                f"{e.date} {e.amount:,.2f} — {reason}" for e, reason in skipped[:10])
            more = "" if len(skipped) <= 10 else f" (+{len(skipped)-10} more)"
            messages.warning(
                request, f"{len(skipped)} skipped because the disposition did not "
                         f"fit: {detail}{more}.")
        if not done and not skipped:
            messages.info(request, "Nothing to do.")
        return redirect(f"/bank-register/exceptions/?account={account.pk}")

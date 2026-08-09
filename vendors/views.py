"""Supplier register and account view."""
import datetime as dt
from urllib.parse import urlencode

from django.contrib import messages
from django.core.exceptions import ValidationError
from django.core.paginator import Paginator
from django.db.models import Count, Q
from django.shortcuts import get_object_or_404, redirect
from django.urls import reverse
from django.views import View
from django.views.generic import TemplateView

from core.permissions import DataEntryRequiredMixin, ReadAccessMixin, TreasurerRequiredMixin

from .models import (Vendor, VendorAddress, VendorBankAccount, VendorCategory,
                     VendorContact, VendorDocument, VendorNote, VendorTag)
from .services import accounts as account_svc


class VendorListView(ReadAccessMixin, TemplateView):
    """The supplier register."""
    template_name = "vendors/vendor_list.html"

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        q = (self.request.GET.get("q") or "").strip()
        status = self.request.GET.get("status") or ""
        category = self.request.GET.get("category") or ""
        tag = self.request.GET.get("tag") or ""
        show_archived = status == Vendor.Status.ARCHIVED

        rows = account_svc.search(q, include_archived=show_archived or bool(q))
        if status:
            rows = rows.filter(status=status)
        if category.isdigit():
            rows = rows.filter(category_id=int(category))
        if tag.isdigit():
            rows = rows.filter(tags__id=int(tag))
        rows = (rows.select_related("category").prefetch_related("tags")
                .annotate(open_bills=Count("payables",
                                           filter=Q(payables__settled=False)))
                .order_by("name"))

        paginator = Paginator(rows, 40)
        page = paginator.get_page(self.request.GET.get("page"))
        # Balances are computed for the page only — the register lists every
        # supplier the church has ever used, and totalling all of them on every
        # page load would be work nobody asked for.
        ctx["rows"] = [{"vendor": v, "summary": account_svc.account_summary(v)}
                       for v in page]
        ctx.update({
            "page": page, "q": q, "status": status,
            "category_id": category, "tag_id": tag,
            "statuses": Vendor.Status.choices,
            "categories": VendorCategory.objects.filter(active=True),
            "tags": VendorTag.objects.all(),
            "counts": {s: Vendor.objects.filter(status=s).count()
                       for s, _ in Vendor.Status.choices},
            "duplicates": account_svc.possible_duplicates().count(),
        })
        return ctx


class VendorDetailView(ReadAccessMixin, TemplateView):
    """The supplier account — the whole profile on one page."""
    template_name = "vendors/vendor_detail.html"

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        vendor = get_object_or_404(
            Vendor.objects.select_related("category").prefetch_related(
                "tags", "contacts", "addresses", "bank_accounts", "documents",
                "note_entries"),
            pk=self.kwargs["pk"])
        ctx.update({
            "vendor": vendor,
            "summary": account_svc.account_summary(vendor),
            "ageing": account_svc.ageing(vendor),
            "transactions": account_svc.transactions(vendor, limit=100),
            "timeline": account_svc.timeline(vendor),
            "duplicates": account_svc.possible_duplicates(vendor),
            "contact_kinds": VendorContact.Kind.choices,
            "address_kinds": VendorAddress.Kind.choices,
            "bank_kinds": VendorBankAccount.Kind.choices,
            "document_kinds": VendorDocument.Kind.choices,
            "can_edit_bank": "manage_vendor_bank_details" in _rights(self.request.user),
            "expiring": [d for d in vendor.documents.all()
                         if d.expires_on and d.expires_on <= dt.date.today()
                         + dt.timedelta(days=60)],
        })
        return ctx


class VendorSaveView(DataEntryRequiredMixin, View):
    """Create or edit a supplier, and everything hanging off it.

    One POST endpoint with an `action`, rather than a dozen tiny views. The
    supplier profile is a page of small forms and a view per form would be a
    dozen near-identical classes; the branch here is flat and readable.
    """

    def post(self, request, pk=None):
        vendor = get_object_or_404(Vendor, pk=pk) if pk else None
        action = request.POST.get("action") or "save"
        p = request.POST

        try:
            if action == "save":
                vendor = self._save_vendor(request, vendor)
                messages.success(request, f"{vendor.name} saved.")
                return redirect("vendor_detail", pk=vendor.pk)

            if action == "contact":
                contact = (get_object_or_404(VendorContact, pk=p["contact_id"],
                                             vendor=vendor)
                           if p.get("contact_id") else VendorContact(vendor=vendor))
                contact.name = p.get("name", "").strip()[:120]
                contact.role = p.get("role", "").strip()[:80]
                contact.kind = p.get("kind") or VendorContact.Kind.GENERAL
                contact.phone = p.get("phone", "").strip()[:32]
                contact.email = p.get("email", "").strip()
                contact.is_primary = bool(p.get("is_primary"))
                contact.full_clean()
                contact.save()
                messages.success(request, "Contact saved.")

            elif action == "address":
                address = VendorAddress(vendor=vendor)
                address.kind = p.get("kind") or VendorAddress.Kind.PHYSICAL
                address.line1 = p.get("line1", "")[:160]
                address.line2 = p.get("line2", "")[:160]
                address.town = p.get("town", "")[:80]
                address.county = p.get("county", "")[:80]
                address.postal_code = p.get("postal_code", "")[:20]
                address.country = p.get("country", "Kenya")[:60]
                address.is_primary = bool(p.get("is_primary"))
                address.save()
                messages.success(request, "Address saved.")

            elif action in ("bank", "verify_bank") and not self._may_edit_bank(request):
                messages.error(
                    request,
                    "Changing where a supplier is paid needs the payment-details "
                    "right. Ask a treasurer to make this change — and confirm the "
                    "new details by phoning a number you already had.")

            elif action == "bank":
                bank = VendorBankAccount(vendor=vendor)
                bank.kind = p.get("kind") or VendorBankAccount.Kind.BANK
                bank.account_name = p.get("account_name", "")[:160]
                bank.bank_name = p.get("bank_name", "")[:120]
                bank.branch = p.get("branch", "")[:120]
                bank.account_number = p.get("account_number", "")[:40]
                bank.paybill_or_till = p.get("paybill_or_till", "")[:20]
                bank.is_primary = bool(p.get("is_primary"))
                bank.save()
                messages.success(
                    request,
                    "Payment details saved. Confirm them with the supplier "
                    "directly before paying — a letter announcing changed bank "
                    "details is the commonest way churches are defrauded.")

            elif action == "verify_bank":
                bank = get_object_or_404(VendorBankAccount,
                                         pk=p.get("bank_id"), vendor=vendor)
                bank.verified_by = request.user
                bank.verified_on = dt.date.today()
                bank.save(update_fields=["verified_by", "verified_on"])
                messages.success(request, "Payment details marked as verified.")

            elif action == "note":
                body = (p.get("body") or "").strip()
                if not body:
                    messages.error(request, "Write something first.")
                else:
                    VendorNote.objects.create(
                        vendor=vendor, body=body, author=request.user,
                        pinned=bool(p.get("pinned")))
                    messages.success(request, "Note added.")

            elif action == "document":
                upload = request.FILES.get("file")
                if not upload:
                    messages.error(request, "Choose a file to upload.")
                else:
                    VendorDocument.objects.create(
                        vendor=vendor, kind=p.get("kind") or "OTHER",
                        label=p.get("label", "")[:140], file=upload,
                        original_name=upload.name[:200],
                        expires_on=_date(p.get("expires_on")),
                        uploaded_by=request.user)
                    messages.success(request, "Document uploaded.")
            else:
                messages.error(request, "Unknown action.")
        except ValidationError as exc:
            messages.error(request, "; ".join(
                m for msgs in getattr(exc, "message_dict", {"": exc.messages}).values()
                for m in msgs))

        # `vendor` is only a saved record on the edit path and after a create
        # that succeeded; every other exit lands here holding None.
        if vendor is None or vendor.pk is None:
            return self._back_to_register(request, action)
        return redirect("vendor_detail", pk=vendor.pk)

    @staticmethod
    def _back_to_register(request, action):
        """Where a rejected POST goes when no supplier account exists yet.

        `vendor_detail` is the right destination for everything the profile
        page posts, and for a create that worked. It is exactly wrong for a
        create that did not: `_save_vendor` assigns `vendor` only on its last
        line, so a record the model refused — a duplicate name, or the blank
        one you get by pressing Save on the empty form — left `vendor` None and
        this view's single exit redirect died on `None.pk`. The treasurer met a
        500 in place of the sentence `Vendor.clean()` wrote for them, so the
        one guard against an eleventh spelling of "Mwangi Hardware" was, from
        the register's form, indistinguishable from the server falling over.

        The register is where the add form lives, so returning there IS the
        form re-rendered, with the flash sitting above it.

        The rejected name is carried back as the register's search term, which
        is what turns the message into the offer it was written to be:
        `accounts.search` matches on `name_key`, the same normalisation
        `Vendor.clean()` used to decide the two names are one supplier, so the
        record it named is on the page as a link the treasurer can click. The
        matching rule is not restated here — it is that one call. Only the
        supplier form has a `name` worth searching for; the profile's smaller
        forms send a contact's or an account's name under the same key, and
        filtering the register by one of those would be a lie.
        """
        rejected = request.POST.get("name", "").strip() if action == "save" else ""
        query = f"?{urlencode({'q': rejected})}" if rejected else ""
        return redirect(f"{reverse('vendor_list')}{query}")

    @staticmethod
    def _may_edit_bank(request):
        """Bank details are gated separately from the rest of the record.

        Deliberately not folded into data-entry: a church can let the office
        maintain suppliers while reserving payment details to the treasurer,
        which is the control that actually blocks invoice-redirection fraud.
        """
        from core import rights
        return "manage_vendor_bank_details" in rights.user_rights(request.user)

    def _save_vendor(self, request, vendor):
        p = request.POST
        vendor = vendor or Vendor(created_by=request.user)
        vendor.name = p.get("name", "").strip()[:160]
        vendor.code = p.get("code", "").strip()[:20]
        vendor.category_id = (int(p["category"]) if p.get("category", "").isdigit()
                              else None)
        vendor.status = p.get("status") or Vendor.Status.ACTIVE
        vendor.payment_terms = p.get("payment_terms") or Vendor.Terms.IMMEDIATE
        vendor.terms_note = p.get("terms_note", "")[:200]
        vendor.credit_limit = p.get("credit_limit") or None
        vendor.tax_pin = p.get("tax_pin", "").strip()[:20]
        vendor.tax_exempt = bool(p.get("tax_exempt"))
        vendor.withholding_rate = p.get("withholding_rate") or None
        vendor.phone = p.get("phone", "").strip()[:32]
        vendor.email = p.get("email", "").strip()
        vendor.website = p.get("website", "").strip()
        vendor.notes = p.get("notes", "")
        vendor.full_clean(exclude=["name_key"])
        vendor.save()
        tag_ids = [int(t) for t in p.getlist("tags") if str(t).isdigit()]
        vendor.tags.set(VendorTag.objects.filter(id__in=tag_ids))
        return vendor


class VendorArchiveView(TreasurerRequiredMixin, View):
    def post(self, request, pk):
        vendor = get_object_or_404(Vendor, pk=pk)
        if request.POST.get("action") == "restore":
            account_svc.restore(vendor, user=request.user)
            messages.success(request, f"{vendor.name} restored.")
        else:
            account_svc.archive(vendor, user=request.user,
                                reason=request.POST.get("reason", ""))
            messages.success(request, f"{vendor.name} archived. Nothing has been "
                                      f"deleted — the history is intact.")
        return redirect("vendor_detail", pk=vendor.pk)


class VendorMergeView(TreasurerRequiredMixin, View):
    def post(self, request, pk):
        source = get_object_or_404(Vendor, pk=pk)
        target = get_object_or_404(Vendor, pk=request.POST.get("target"))
        try:
            account_svc.merge(source, target, user=request.user)
        except ValueError as exc:
            messages.error(request, str(exc))
            return redirect("vendor_detail", pk=source.pk)
        messages.success(request, f"“{source.name}” merged into “{target.name}”.")
        return redirect("vendor_detail", pk=target.pk)


class VendorLookupView(ReadAccessMixin, View):
    """JSON lookup for supplier pickers elsewhere in the app.

    A plain JsonResponse rather than a REST framework: this application has no
    DRF dependency and adding one for a single autocomplete would be a large
    decision taken for a small reason. The same shape as the existing member
    lookup, so the front end already knows how to consume it.
    """

    def get(self, request):
        from django.http import JsonResponse
        term = (request.GET.get("q") or "").strip()
        if len(term) < 2:
            return JsonResponse({"results": []})
        rows = account_svc.search(term)[:10]
        return JsonResponse({"results": [
            {"id": v.pk, "name": v.name, "code": v.code,
             "phone": v.phone, "terms": v.get_payment_terms_display(),
             "outstanding": float(account_svc.outstanding(v)),
             "status": v.status}
            for v in rows]})


def _rights(user):
    from core import rights
    return rights.user_rights(user)


def _date(value):
    if not value:
        return None
    try:
        return dt.date.fromisoformat(str(value)[:10])
    except ValueError:
        return None

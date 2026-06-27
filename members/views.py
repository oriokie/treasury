from decimal import Decimal

from core.utils import PrefPaginationMixin

from django.contrib import messages
from django.db.models import Q, Sum
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse_lazy
from django.views.generic import ListView, DetailView, CreateView, UpdateView, View

from core.permissions import DataEntryRequiredMixin, ReadAccessMixin
from giving.models import Transaction
from .forms import MemberForm
from .models import Member, PossibleDuplicate
from .services.matching import merge_members


class MemberListView(PrefPaginationMixin, ReadAccessMixin, ListView):
    model = Member
    template_name = "members/list.html"
    context_object_name = "members"
    paginate_by = 50

    def get_queryset(self):
        qs = Member.objects.select_related("dev_group").order_by("name")
        q = self.request.GET.get("q")
        group = self.request.GET.get("group")
        source = self.request.GET.get("source")
        if q:
            qs = qs.filter(Q(name__icontains=q) | Q(phone__icontains=q))
        if group:
            qs = qs.filter(group=group)
        if source:
            qs = qs.filter(source=source)
        return qs

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx["groups"] = Member.Group.choices
        ctx["member_types"] = Member.MemberType.choices
        ctx["sources"] = Member.Source.choices
        ctx["filters"] = self.request.GET
        ctx["dup_count"] = PossibleDuplicate.objects.filter(resolved=False).count()
        return ctx


class MemberDetailView(ReadAccessMixin, DetailView):
    model = Member
    template_name = "members/detail.html"
    context_object_name = "member"

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        txns = (Transaction.objects.filter(member=self.object,
                direction=Transaction.Direction.CREDIT)
                .select_related("department").order_by("-date"))
        ctx["transactions"] = txns[:100]
        ctx["total_given"] = txns.aggregate(t=Sum("amount"))["t"] or Decimal(0)
        ctx["aliases"] = self.object.aliases.all()
        return ctx


class MemberCreateView(DataEntryRequiredMixin, CreateView):
    model = Member
    form_class = MemberForm
    template_name = "members/form.html"

    def get_success_url(self):
        messages.success(self.request, "Member saved.")
        return reverse_lazy("member_detail", args=[self.object.pk])


class MemberUpdateView(DataEntryRequiredMixin, UpdateView):
    model = Member
    form_class = MemberForm
    template_name = "members/form.html"

    def get_success_url(self):
        messages.success(self.request, "Member updated.")
        return reverse_lazy("member_detail", args=[self.object.pk])


class DuplicateReviewView(ReadAccessMixin, ListView):
    template_name = "members/duplicates.html"
    context_object_name = "dups"

    def get_queryset(self):
        return (PossibleDuplicate.objects.filter(resolved=False)
                .select_related("member"))

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        # for each flagged member, find same-name candidates to merge with
        data = []
        single = 0
        for d in ctx["dups"]:
            candidates = list(Member.objects.filter(
                name_key=d.member.name_key).exclude(pk=d.member.pk))
            row = {"flag": d, "member": d.member, "candidates": candidates,
                   "single": len(candidates) == 1}
            if row["single"]:
                single += 1
            data.append(row)
        ctx["rows"] = data
        ctx["single_count"] = single        # how many can be auto-merged in bulk
        return ctx


class BulkMergeView(DataEntryRequiredMixin, View):
    """Merge every duplicate that has exactly one candidate, in one click.
    Ambiguous ones (more than one candidate) are left for manual review."""

    def post(self, request):
        merged = 0
        for d in PossibleDuplicate.objects.filter(resolved=False).select_related("member"):
            cands = list(Member.objects.filter(
                name_key=d.member.name_key).exclude(pk=d.member.pk))
            if len(cands) == 1:
                # keep the older / manually-entered record where possible
                keep, absorb = cands[0], d.member
                if (keep.source == Member.Source.AUTO_BANK
                        and absorb.source != Member.Source.AUTO_BANK):
                    keep, absorb = absorb, keep
                merge_members(keep, absorb)
                merged += 1
        if merged:
            messages.success(request, f"Merged {merged} unambiguous duplicate(s). "
                             "Any remaining ones have more than one candidate and "
                             "need a manual choice.")
        else:
            messages.info(request, "No unambiguous duplicates to merge.")
        return redirect("member_duplicates")


class MergeMembersView(DataEntryRequiredMixin, View):
    def post(self, request):
        keep = get_object_or_404(Member, pk=request.POST.get("keep"))
        absorb = get_object_or_404(Member, pk=request.POST.get("absorb"))
        if keep.pk == absorb.pk:
            messages.error(request, "Choose two different members.")
            return redirect("member_duplicates")
        merge_members(keep, absorb)
        messages.success(request, f"Merged into {keep.name}.")
        return redirect("member_duplicates")


import csv
import io
from django.http import HttpResponse
from departments.models import DevelopmentGroup


class MemberBulkView(DataEntryRequiredMixin, View):
    """Bulk-edit selected members: set group, member type, or active flag."""

    def post(self, request):
        ids = request.POST.getlist("ids")
        field = request.POST.get("field")
        value = request.POST.get("value")
        if not ids:
            messages.warning(request, "Select at least one member.")
            return redirect("member_list")
        qs = Member.objects.filter(pk__in=ids)
        n = qs.count()
        if field == "group":
            qs.update(group=value or None)
            label = "group"
        elif field == "member_type":
            qs.update(member_type=value or None)
            label = "member type"
        elif field == "active":
            qs.update(active=(value == "1"))
            label = "status"
        elif field == "dev_group":
            grp = DevelopmentGroup.objects.filter(pk=value).first() if value else None
            qs.update(dev_group=grp)
            label = "development group"
        else:
            messages.error(request, "Unknown bulk action.")
            return redirect("member_list")
        messages.success(request, f"Updated {label} for {n} member(s).")
        return redirect("member_list")


class MemberExportView(ReadAccessMixin, View):
    """Download all members as CSV for offline bulk editing / re-import."""

    def get(self, request):
        resp = HttpResponse(content_type="text/csv")
        resp["Content-Disposition"] = 'attachment; filename="members.csv"'
        w = csv.writer(resp)
        w.writerow(["id", "name", "phone", "group", "member_type", "dev_group_number", "active"])
        for m in Member.objects.select_related("dev_group").order_by("name"):
            w.writerow([m.id, m.name, m.phone or "", m.group or "",
                        m.member_type or "",
                        m.dev_group.number if m.dev_group else "",
                        "1" if m.active else "0"])
        return resp


class MemberImportView(DataEntryRequiredMixin, View):
    """Upload an edited members CSV. Rows with an id update that member; rows
    without an id create a new member. Matches the export columns."""
    template_name = "members/import.html"

    def get(self, request):
        return render(request, self.template_name, {})

    def post(self, request):
        f = request.FILES.get("file")
        if not f:
            messages.error(request, "Choose a CSV file.")
            return redirect("member_import")
        try:
            text = io.TextIOWrapper(f.file, encoding="utf-8-sig")
            reader = csv.DictReader(text)
        except Exception:
            from core.utils import log_exception as _lx; _lx('members/views.py')
            messages.error(request, "Could not read the CSV.")
            return redirect("member_import")
        groups = {c[0] for c in Member.Group.choices}
        types = {c[0] for c in Member.MemberType.choices}
        updated = created = 0
        for row in reader:
            name = (row.get("name") or "").strip()
            if not name:
                continue
            data = {
                "name": name,
                "phone": (row.get("phone") or "").strip() or None,
                "group": (row.get("group") or "").strip() or None,
                "member_type": (row.get("member_type") or "").strip() or None,
                "active": (row.get("active") or "1").strip() != "0",
            }
            if data["group"] and data["group"] not in groups:
                data["group"] = None
            if data["member_type"] and data["member_type"] not in types:
                data["member_type"] = None
            devnum = (row.get("dev_group_number") or "").strip()
            dev = DevelopmentGroup.objects.filter(number=devnum).first() if devnum.isdigit() else None
            data["dev_group"] = dev
            mid = (row.get("id") or "").strip()
            obj = Member.objects.filter(pk=mid).first() if mid.isdigit() else None
            if obj is None and data["phone"]:
                obj = Member.objects.filter(phone=data["phone"]).first()
            if obj is None:
                obj = Member.objects.filter(name__iexact=name).first()
            if obj:
                for k, v in data.items():
                    setattr(obj, k, v)
                obj.save()
                updated += 1
            else:
                Member.objects.create(**data)
                created += 1
        messages.success(request, f"Import complete — {updated} updated, {created} created.")
        return redirect("member_list")

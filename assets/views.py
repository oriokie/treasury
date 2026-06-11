import datetime as dt
from decimal import Decimal

from django.contrib import messages
from django.shortcuts import render, redirect, get_object_or_404
from django.urls import reverse_lazy
from django.views import View
from django.views.generic import ListView, CreateView, UpdateView

from core.permissions import ReadAccessMixin, TreasurerRequiredMixin, DataEntryRequiredMixin
from .models import FixedAsset, DepreciationRule
from .forms import FixedAssetForm, DepreciationRuleForm


def _as_of(request):
    try:
        return dt.date.fromisoformat(request.GET.get("as_of", ""))
    except ValueError:
        return dt.date.today()


class AssetListView(ReadAccessMixin, ListView):
    model = FixedAsset
    template_name = "assets/list.html"
    context_object_name = "assets"

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        as_of = _as_of(self.request)
        rows, t_cost, t_acc, t_nbv = [], Decimal(0), Decimal(0), Decimal(0)
        for a in self.object_list:
            acc = a.accumulated_depreciation(as_of)
            nbv = a.net_book_value(as_of)
            rows.append({"a": a, "acc": acc, "nbv": nbv})
            t_cost += a.cost
            t_acc += acc
            t_nbv += nbv
        ctx.update({"rows": rows, "as_of": as_of, "t_cost": t_cost,
                    "t_acc": t_acc, "t_nbv": t_nbv})
        return ctx


class AssetCreate(TreasurerRequiredMixin, CreateView):
    model = FixedAsset
    form_class = FixedAssetForm
    template_name = "assets/form.html"
    success_url = reverse_lazy("asset_list")

    def form_valid(self, form):
        messages.success(self.request, "Asset added to the register.")
        return super().form_valid(form)


class AssetUpdate(TreasurerRequiredMixin, UpdateView):
    model = FixedAsset
    form_class = FixedAssetForm
    template_name = "assets/form.html"
    success_url = reverse_lazy("asset_list")

    def form_valid(self, form):
        messages.success(self.request, "Asset updated.")
        return super().form_valid(form)


class AssetDisposeView(TreasurerRequiredMixin, View):
    def post(self, request, pk):
        from giving.models import Transaction
        from departments.models import Department
        a = get_object_or_404(FixedAsset, pk=pk)
        if a.disposed:
            messages.info(request, f"{a.name} is already disposed.")
            return redirect("asset_detail", pk=pk)
        try:
            on = dt.date.fromisoformat(request.POST.get("disposed_on", ""))
        except ValueError:
            on = dt.date.today()
        try:
            proceeds = Decimal(request.POST.get("proceeds") or "0")
        except Exception:
            proceeds = Decimal("0")
        method = request.POST.get("method") or FixedAsset.DisposalMethod.SOLD
        fund_id = request.POST.get("fund")
        nbv = a.net_book_value(on)
        gain_loss = proceeds - nbv
        a.disposed = True
        a.disposed_on = on
        a.disposal_proceeds = proceeds
        a.disposal_method = method
        a.disposal_gain_loss = gain_loss
        fund = None
        if fund_id:
            fund = Department.objects.filter(pk=fund_id).first()
            a.disposal_fund = fund
        a.save()
        # Record the cash proceeds as a receipt into the nominated fund so the cash,
        # ledger and reports reflect it. Removing the asset's NBV plus this receipt
        # moves net assets by exactly the gain/(loss).
        if proceeds and fund:
            Transaction.objects.create(
                date=on, channel=Transaction.Channel.BANK, direction=Transaction.Direction.CREDIT,
                amount=proceeds, department=fund, allocation_status=Transaction.Status.MANUAL,
                excluded_from_income=True,
                reference=f"Disposal of {a.name}"[:60],
                payer_name=f"Asset disposal ({a.get_disposal_method_display()})"[:120])
        verb = {"SOLD": "sold", "SCRAPPED": "scrapped", "DONATED": "donated",
                "LOST": "written off"}.get(method, "disposed")
        gl = (f"gain of {gain_loss}" if gain_loss > 0 else
              (f"loss of {-gain_loss}" if gain_loss < 0 else "no gain or loss"))
        messages.success(request, f"{a.name} {verb}. Net book value {nbv}, proceeds {proceeds} "
                                  f"— {gl}.")
        return redirect("asset_detail", pk=pk)


class DepreciationRulesView(TreasurerRequiredMixin, View):
    """Manage per-category depreciation rules (linked from settings)."""
    template_name = "assets/depreciation_rules.html"

    def get(self, request):
        from core.models import SiteConfig
        rules = {r.category: r for r in DepreciationRule.objects.all()}
        cats = [{"value": v, "label": l, "rule": rules.get(v)}
                for v, l in FixedAsset.Category.choices]
        return render(request, self.template_name,
                      {"cats": cats, "methods": DepreciationRule.Method.choices,
                       "cfg": SiteConfig.get()})

    def post(self, request):
        for v, _ in FixedAsset.Category.choices:
            method = request.POST.get(f"method_{v}")
            rate = request.POST.get(f"rate_{v}")
            if method is None:
                continue
            try:
                rate_val = Decimal(rate) if rate else Decimal(0)
            except Exception:
                rate_val = Decimal(0)
            DepreciationRule.objects.update_or_create(
                category=v, defaults={"method": method, "rate": rate_val})
        messages.success(request, "Depreciation rules saved.")
        return redirect("depreciation_rules")


from django.views.generic import DetailView
from .models import AssetAttachment


class AssetDetailView(ReadAccessMixin, DetailView):
    model = FixedAsset
    template_name = "assets/asset_detail.html"
    context_object_name = "asset"

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        a = self.object
        ctx["nbv"] = a.net_book_value()
        ctx["accum"] = a.accumulated_depreciation()
        from departments.models import Department
        ctx["funds"] = Department.objects.filter(active=True, is_trust=False).order_by("name")
        ctx["today"] = dt.date.today()
        return ctx


class AssetAttachmentUpload(DataEntryRequiredMixin, View):
    def post(self, request, pk):
        a = get_object_or_404(FixedAsset, pk=pk)
        f = request.FILES.get("file")
        if f:
            AssetAttachment.objects.create(asset=a, file=f,
                label=request.POST.get("label", "")[:120], uploaded_by=request.user)
            messages.success(request, "Document attached.")
        else:
            messages.error(request, "Please choose a file to upload.")
        return redirect("asset_detail", pk=pk)


class AssetAttachmentDelete(DataEntryRequiredMixin, View):
    def post(self, request, pk, att):
        x = get_object_or_404(AssetAttachment, pk=att, asset_id=pk)
        x.file.delete(save=False)
        x.delete()
        messages.success(request, "Attachment removed.")
        return redirect("asset_detail", pk=pk)

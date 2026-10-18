"""Fund Leaders directory — contacts under Funds & setup who receive period
fund statements by SMS and email."""
from django.contrib import messages
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse, reverse_lazy
from django.views import View
from django.views.generic import CreateView, DeleteView, ListView, UpdateView

from core.permissions import TreasurerRequiredMixin
from core.models import SiteConfig
from .forms import FundLeaderForm, FundLeaderSendForm
from .models import FundLeader
from .services import fund_leaders as svc


class FundLeaderListView(TreasurerRequiredMixin, ListView):
    model = FundLeader
    template_name = "departments/fund_leaders_list.html"
    context_object_name = "leaders"

    def get_queryset(self):
        return (FundLeader.objects
                .prefetch_related("departments", "groups")
                .order_by("name"))

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        cfg = SiteConfig.get()
        start, end = svc.default_period()
        ctx.update({
            "sms_enabled": cfg.sms_enabled,
            "email_enabled": bool(cfg.email_enabled and cfg.email_host
                                  and cfg.email_from),
            "default_start": start.isoformat(),
            "default_end": end.isoformat(),
            "sms_ready": sum(1 for l in ctx["leaders"]
                             if l.active and l.has_valid_phone
                             and l.departments.exists()),
            "email_ready": sum(1 for l in ctx["leaders"]
                               if l.active and l.has_valid_email
                               and l.departments.exists()),
        })
        return ctx


class FundLeaderCreateView(TreasurerRequiredMixin, CreateView):
    model = FundLeader
    form_class = FundLeaderForm
    template_name = "departments/fund_leader_form.html"
    success_url = reverse_lazy("fund_leader_list")

    def form_valid(self, form):
        messages.success(self.request, f"Saved leader {form.instance.name}.")
        return super().form_valid(form)


class FundLeaderUpdateView(TreasurerRequiredMixin, UpdateView):
    model = FundLeader
    form_class = FundLeaderForm
    template_name = "departments/fund_leader_form.html"
    success_url = reverse_lazy("fund_leader_list")

    def form_valid(self, form):
        messages.success(self.request, f"Updated {form.instance.name}.")
        return super().form_valid(form)


class FundLeaderDeleteView(TreasurerRequiredMixin, DeleteView):
    model = FundLeader
    template_name = "departments/fund_leader_confirm_delete.html"
    success_url = reverse_lazy("fund_leader_list")

    def form_valid(self, form):
        messages.success(self.request, f"Removed {self.object.name}.")
        return super().form_valid(form)


class FundLeaderSendView(TreasurerRequiredMixin, View):
    """Preview + send SMS or email statements for a period."""
    template_name = "departments/fund_leaders_send.html"

    def _channel(self, request):
        ch = (request.GET.get("channel") or request.POST.get("channel")
              or "sms").lower()
        return ch if ch in ("sms", "email") else "sms"

    def _leaders_for(self, channel):
        qs = (FundLeader.objects.filter(active=True)
              .prefetch_related("departments", "groups")
              .order_by("name"))
        out = []
        for l in qs:
            if not l.departments.exists():
                continue
            if channel == "sms" and l.has_valid_phone:
                out.append(l)
            elif channel == "email" and l.has_valid_email:
                out.append(l)
        return out

    def get(self, request):
        channel = self._channel(request)
        leaders = self._leaders_for(channel)
        start, end = svc.default_period()
        form = FundLeaderSendForm(
            initial={
                "start": start, "end": end,
                "leader_ids": [str(l.pk) for l in leaders],
            },
            leaders=leaders)
        return self._render(request, form, channel, leaders)

    def post(self, request):
        channel = self._channel(request)
        leaders = self._leaders_for(channel)
        form = FundLeaderSendForm(request.POST, leaders=leaders)
        if not form.is_valid():
            return self._render(request, form, channel, leaders)

        selected = list(form.selected_leaders())
        # Re-filter to those still eligible for this channel.
        if channel == "sms":
            selected = [l for l in selected if l.has_valid_phone]
        else:
            selected = [l for l in selected if l.has_valid_email]
        selected = [l for l in selected if l.departments.exists()]

        if not selected:
            messages.warning(
                request,
                "No eligible leaders selected — need a linked fund and a valid "
                + ("phone." if channel == "sms" else "email."))
            return redirect(f"{reverse('fund_leader_send')}?channel={channel}")

        start = form.cleaned_data["start"]
        end = form.cleaned_data["end"]
        rows = svc.statement_rows_by_id(start, end)
        cfg = SiteConfig.get()
        sent = skipped = failed = 0
        for leader in selected:
            if channel == "sms":
                s, sk, f = svc.send_sms_to_leader(
                    leader, start, end, cfg=cfg, rows_by_id=rows)
            else:
                s, sk, f = svc.send_email_to_leader(
                    leader, start, end, cfg=cfg, rows_by_id=rows)
            sent += s
            skipped += sk
            failed += f

        parts = [f"{sent} {channel.upper()} message(s) sent"]
        if skipped:
            parts.append(f"{skipped} skipped (no valid contact or fund)")
        if failed:
            parts.append(f"{failed} failed")
        (messages.success if sent else messages.warning)(
            request, "; ".join(parts) + ".")
        return redirect("fund_leader_list")

    def _render(self, request, form, channel, leaders):
        cfg = SiteConfig.get()
        return render(request, self.template_name, {
            "form": form,
            "channel": channel,
            "leaders": leaders,
            "sms_enabled": cfg.sms_enabled,
            "email_enabled": bool(cfg.email_enabled and cfg.email_host
                                  and cfg.email_from),
        })


class FundLeaderSendOneView(TreasurerRequiredMixin, View):
    """Quick send for one leader from the list (period = month to date)."""

    def post(self, request, pk):
        leader = get_object_or_404(FundLeader, pk=pk, active=True)
        channel = (request.POST.get("channel") or "sms").lower()
        start, end = svc.default_period()
        if request.POST.get("start") and request.POST.get("end"):
            try:
                import datetime as dt
                start = dt.date.fromisoformat(request.POST["start"])
                end = dt.date.fromisoformat(request.POST["end"])
            except ValueError:
                pass
        cfg = SiteConfig.get()
        if channel == "email":
            if not leader.has_valid_email:
                messages.error(request, f"{leader.name} has no valid email.")
                return redirect("fund_leader_list")
            sent, skipped, failed = svc.send_email_to_leader(
                leader, start, end, cfg=cfg)
        else:
            if not leader.has_valid_phone:
                messages.error(request, f"{leader.name} has no valid phone.")
                return redirect("fund_leader_list")
            sent, skipped, failed = svc.send_sms_to_leader(
                leader, start, end, cfg=cfg)
        if sent:
            messages.success(
                request,
                f"Sent {sent} {channel.upper()} message(s) to {leader.name}.")
        elif skipped:
            messages.warning(
                request,
                f"Nothing sent to {leader.name} — link at least one fund.")
        else:
            messages.error(
                request,
                f"Could not send to {leader.name}"
                + (f" ({failed} failed)." if failed else "."))
        return redirect("fund_leader_list")

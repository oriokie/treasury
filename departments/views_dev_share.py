"""Public development codes directory + live collection board.

Church-wide share links (SiteConfig tokens). Staff manage enable / period /
rotate from DevShareControlView.
"""
import datetime as dt
from decimal import Decimal

from django.contrib import messages
from django.contrib.auth.decorators import login_not_required
from django.db.models import Count, Q, Sum
from django.http import Http404, JsonResponse
from django.shortcuts import redirect, render
from django.utils.decorators import method_decorator
from django.views import View
from django.views.decorators.csrf import csrf_protect

from core.models import SiteConfig
from core.permissions import TreasurerRequiredMixin


def _cfg():
    return SiteConfig.get()


def _dev_contributor_members():
    """Active members with a phone who have confirmed Development credits."""
    from departments.models import Department
    from members.models import Member

    return (
        Member.objects.filter(active=True)
        .filter(Q(phone__gt="") | Q(phones__number__gt=""))
        .filter(
            transaction__direction="CREDIT",
            transaction__confirmed=True,
            transaction__is_reversed=False,
            transaction__is_reversal=False,
            transaction__department__category=Department.Category.DEVELOPMENT,
        )
        .select_related("dev_group")
        .distinct()
        .order_by("name")
    )


def _live_totals(cfg):
    """Confirmed Development-fund credits in the configured board period."""
    from departments.models import Department
    from giving.models import Transaction

    start = cfg.dev_live_period_start
    end = cfg.dev_live_period_end
    if not start or not end:
        return Decimal("0"), 0, start, end
    qs = Transaction.objects.filter(
        direction=Transaction.Direction.CREDIT,
        confirmed=True,
        is_reversed=False,
        is_reversal=False,
        department__category=Department.Category.DEVELOPMENT,
        date__gte=start,
        date__lte=end,
    )
    agg = qs.aggregate(t=Sum("amount"), n=Count("id"))
    return agg["t"] or Decimal("0"), int(agg["n"] or 0), start, end


def _gift_dev_group_by_member(member_ids):
    """Map member_id → DevelopmentGroup from stamped Development gifts.

    Many register members never have ``Member.dev_group`` set even though their
    gifts are tagged to a group. For the public codes directory we fall back to
    the group they have given the most confirmed Development KES to.
    """
    from departments.models import Department, DevelopmentGroup
    from giving.models import Transaction

    if not member_ids:
        return {}
    rows = list(
        Transaction.objects.filter(
            member_id__in=member_ids,
            direction=Transaction.Direction.CREDIT,
            confirmed=True,
            is_reversed=False,
            is_reversal=False,
            department__category=Department.Category.DEVELOPMENT,
            dev_group__isnull=False,
        )
        .values("member_id", "dev_group")
        .annotate(total=Sum("amount"), n=Count("id"))
        .order_by("member_id", "-total", "-n", "dev_group")
    )
    best = {}
    for row in rows:
        mid = row["member_id"]
        if mid not in best:
            best[mid] = row["dev_group"]
    if not best:
        return {}
    groups = {
        g.id: g
        for g in DevelopmentGroup.objects.filter(pk__in=set(best.values()))
    }
    return {mid: groups.get(gid) for mid, gid in best.items() if groups.get(gid)}


def _directory_dev_group(member, gift_groups):
    """Home group first, else the member's strongest tagged Development group."""
    if getattr(member, "dev_group_id", None):
        return member.dev_group
    return gift_groups.get(member.id)


class DevShareControlView(TreasurerRequiredMixin, View):
    """Enable / rotate share links and set the live board period."""
    template_name = "departments/dev_share_control.html"

    def get(self, request):
        cfg = _cfg()
        return render(request, self.template_name, {
            "cfg": cfg,
            "codes_url": self._codes_url(request, cfg),
            "live_url": self._live_url(request, cfg),
            "contributor_count": _dev_contributor_members().count(),
        })

    def post(self, request):
        cfg = _cfg()
        action = (request.POST.get("action") or "").strip()

        if action == "enable_codes":
            cfg.dev_codes_public_enabled = True
            cfg.ensure_dev_codes_share_token(save=False)
            cfg.save(update_fields=["dev_codes_public_enabled",
                                    "dev_codes_share_token"])
            messages.success(request, "Development codes page is live.")
        elif action == "disable_codes":
            cfg.dev_codes_public_enabled = False
            cfg.save(update_fields=["dev_codes_public_enabled"])
            messages.success(request, "Development codes page turned off.")
        elif action == "rotate_codes":
            cfg.ensure_dev_codes_share_token(save=True, rotate=True)
            messages.success(request, "Codes link rotated — old links stop working.")
        elif action == "enable_live":
            start = request.POST.get("start") or ""
            end = request.POST.get("end") or ""
            try:
                start_d = dt.date.fromisoformat(start)
                end_d = dt.date.fromisoformat(end)
            except ValueError:
                messages.error(request, "Choose a valid start and end date.")
                return redirect("dev_share_control")
            if end_d < start_d:
                messages.error(request, "End date must be on or after the start.")
                return redirect("dev_share_control")
            cfg.dev_live_period_start = start_d
            cfg.dev_live_period_end = end_d
            cfg.dev_live_public_enabled = True
            cfg.ensure_dev_live_share_token(save=False)
            cfg.save(update_fields=[
                "dev_live_public_enabled", "dev_live_share_token",
                "dev_live_period_start", "dev_live_period_end"])
            messages.success(request, "Live Development board is on.")
        elif action == "update_live_period":
            start = request.POST.get("start") or ""
            end = request.POST.get("end") or ""
            try:
                start_d = dt.date.fromisoformat(start)
                end_d = dt.date.fromisoformat(end)
            except ValueError:
                messages.error(request, "Choose a valid start and end date.")
                return redirect("dev_share_control")
            if end_d < start_d:
                messages.error(request, "End date must be on or after the start.")
                return redirect("dev_share_control")
            cfg.dev_live_period_start = start_d
            cfg.dev_live_period_end = end_d
            cfg.save(update_fields=["dev_live_period_start", "dev_live_period_end"])
            messages.success(request, "Live board period updated.")
        elif action == "disable_live":
            cfg.dev_live_public_enabled = False
            cfg.save(update_fields=["dev_live_public_enabled"])
            messages.success(request, "Live Development board turned off.")
        elif action == "rotate_live":
            cfg.ensure_dev_live_share_token(save=True, rotate=True)
            messages.success(request, "Live board link rotated — old links stop working.")
        else:
            messages.error(request, "Unknown action.")
        return redirect("dev_share_control")

    @staticmethod
    def _codes_url(request, cfg):
        if not (cfg.dev_codes_public_enabled and cfg.dev_codes_share_token):
            return ""
        from django.urls import reverse
        return request.build_absolute_uri(
            reverse("dev_codes_public", args=[cfg.dev_codes_share_token]))

    @staticmethod
    def _live_url(request, cfg):
        if not (cfg.dev_live_public_enabled and cfg.dev_live_share_token):
            return ""
        from django.urls import reverse
        return request.build_absolute_uri(
            reverse("dev_live_public", args=[cfg.dev_live_share_token]))


@method_decorator(csrf_protect, name="dispatch")
@method_decorator(login_not_required, name="dispatch")
class DevCodesPublicView(View):
    """Shareable directory: MB… codes for development contributors."""
    template_name = "departments/dev_codes_public.html"

    def get(self, request, token):
        from members.models import mask_phone

        cfg = _cfg()
        if (not cfg.dev_codes_public_enabled
                or not cfg.dev_codes_share_token
                or cfg.dev_codes_share_token != token):
            raise Http404()
        # Group by development group № so leaders can spot mismatches.
        # Prefer home ``Member.dev_group``; if blank, use the group stamped on
        # their Development gifts (common when home group was never set).
        # Only the masked phone ever leaves the server — never put the full
        # number in HTML/JS (no reveal button / data-full attributes).
        members = []
        for m in _dev_contributor_members():
            phone = m.receipt_phone or m.phone or ""
            if not phone:
                continue
            if not m.match_code:
                m.save()  # Member.save assigns MB… when blank
            members.append(m)
        gift_groups = _gift_dev_group_by_member(
            [m.id for m in members if not m.dev_group_id])
        buckets = {}
        for m in members:
            phone = m.receipt_phone or m.phone or ""
            dg = _directory_dev_group(m, gift_groups)
            number = dg.number if dg else None
            label = dg.label if dg else ""
            key = number if number is not None else -1
            buckets.setdefault(key, {
                "number": number,
                "label": label,
                "rows": [],
            })
            buckets[key]["rows"].append({
                "name": m.name,
                "code": m.match_code,
                "phone_masked": mask_phone(phone, 3),
                "group_number": number,
                "group_label": label,
            })
        groups = sorted(
            buckets.values(),
            key=lambda g: (g["number"] is None, g["number"] or 0, g["label"] or ""),
        )
        total = sum(len(g["rows"]) for g in groups)
        return render(request, self.template_name, {
            "groups": groups,
            "total_rows": total,
            "church": cfg.church_name or "Church",
            "currency": cfg.currency_symbol or "KES",
        })


@method_decorator(login_not_required, name="dispatch")
class DevLivePublicView(View):
    """Presentation board: live Development fund total + gift count."""
    template_name = "departments/dev_live_public.html"

    def get(self, request, token):
        cfg = _cfg()
        if (not cfg.dev_live_public_enabled
                or not cfg.dev_live_share_token
                or cfg.dev_live_share_token != token):
            raise Http404()
        total, count, start, end = _live_totals(cfg)
        return render(request, self.template_name, {
            "token": token,
            "total": total,
            "total_display": f"{total:,.0f}",
            "count": count,
            "start": start,
            "end": end,
            "church": cfg.church_name or "Church",
            "currency": cfg.currency_symbol or "KES",
            "poll_url": request.path.rstrip("/") + "/poll/",
        })


@method_decorator(login_not_required, name="dispatch")
class DevLivePollView(View):
    """JSON poll for the live board."""

    def get(self, request, token):
        cfg = _cfg()
        if (not cfg.dev_live_public_enabled
                or not cfg.dev_live_share_token
                or cfg.dev_live_share_token != token):
            raise Http404()
        total, count, start, end = _live_totals(cfg)
        return JsonResponse({
            "total": float(total),
            "total_display": f"{total:,.0f}",
            "count": count,
            "start": start.isoformat() if start else "",
            "end": end.isoformat() if end else "",
        })

"""Public contribution link, the SMS that shares it, and the follow-up list."""
import json

from django.contrib import messages
from django.contrib.auth.decorators import login_not_required
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.utils.decorators import method_decorator
from django.views import View
from django.views.decorators.csrf import csrf_exempt

from core.models import SiteConfig
from core.permissions import TreasurerRequiredMixin
from core.services.sms import send_sms
from giving.models import ContributionAttempt
from giving.services.contrib_link import (
    apply_callback, callback_url, contribution_url, display_name, member_for_code,
    parse_amount, payment_ready, public_message, quick_amounts, refresh_from_daraja,
    render_sms, start_prompt,
)
from members.models import Member


def _page_context(member, cfg, request):
    ready, reason = payment_ready(cfg)
    return {
        "member": member,
        "display_name": display_name(member.name),
        "initial": (display_name(member.name)[:1] or "•"),
        "cfg": cfg,
        "church": cfg.church_name or "Church",
        "title": (cfg.contrib_title or "").strip() or "Development Fund",
        "tagline": (cfg.contrib_tagline or "").strip(),
        "verse": (cfg.contrib_verse or "").strip(),
        "verse_ref": (cfg.contrib_verse_ref or "").strip(),
        "quick": quick_amounts(cfg),
        "ready": ready,
        "not_ready": reason,
        "shortcode": cfg.daraja_shortcode,
        "till": cfg.daraja_txn_type == "TILL",
        "share_url": contribution_url(member, request, cfg),
    }


@method_decorator(login_not_required, name="dispatch")
class ContributionLinkView(View):
    """The page a member shares. Anyone with the code can give; the gift is
    credited to the member the code belongs to."""
    template_name = "giving/contrib_public.html"

    def _member(self, code):
        if not SiteConfig.get().contrib_links_enabled:
            return None
        return member_for_code(code)

    def get(self, request, code):
        member = self._member(code)
        if member is None:
            return render(request, "giving/contrib_closed.html", status=404)
        cfg = SiteConfig.get()
        ctx = _page_context(member, cfg, request)
        token = (request.GET.get("t") or "").strip()
        if token:
            attempt = ContributionAttempt.objects.filter(
                token=token, member=member).first()
            if attempt and attempt.status == ContributionAttempt.Status.SENT:
                attempt = refresh_from_daraja(attempt)
            if attempt:
                ctx["attempt"] = attempt
                ctx["status_message"] = public_message(attempt)
        return render(request, self.template_name, ctx)

    def post(self, request, code):
        member = self._member(code)
        if member is None:
            return render(request, "giving/contrib_closed.html", status=404)
        cfg = SiteConfig.get()
        amount, amount_err = parse_amount(request.POST.get("amount"))
        phone = request.POST.get("phone") or ""
        ctx = _page_context(member, cfg, request)
        ctx["phone_value"] = phone
        ctx["amount_value"] = request.POST.get("amount") or ""
        if amount_err:
            ctx["error"] = amount_err
            return render(request, self.template_name, ctx, status=400)
        attempt, err = start_prompt(member, phone, amount, request, cfg)
        if err and attempt is None:
            ctx["error"] = err
            return render(request, self.template_name, ctx, status=400)
        return redirect(f"{request.path}?t={attempt.token}")


@method_decorator(login_not_required, name="dispatch")
class ContributionLinkStatusView(View):
    """Polled by the public page. Returns only the outcome, never a phone
    number or anyone else's attempt."""

    def get(self, request, token):
        attempt = ContributionAttempt.objects.filter(token=token).first()
        if attempt is None:
            return JsonResponse({"status": "UNKNOWN", "message": "Not found."}, status=404)
        if attempt.status == ContributionAttempt.Status.SENT:
            attempt = refresh_from_daraja(attempt)
        return JsonResponse({
            "status": attempt.status,
            "message": public_message(attempt),
        })


@method_decorator(csrf_exempt, name="dispatch")
@method_decorator(login_not_required, name="dispatch")
class MpesaStkCallbackView(View):
    """Safaricom posts the PIN result here. There is no session; the checkout
    id must match a prompt this app sent."""

    def post(self, request):
        try:
            payload = json.loads(request.body.decode() or "{}")
        except (json.JSONDecodeError, UnicodeDecodeError):
            payload = {}
        apply_callback(payload)
        return JsonResponse({"ResultCode": 0, "ResultDesc": "Accepted"})


class ContributionAttemptsView(TreasurerRequiredMixin, View):
    """Prompts that were started and did not become a gift, for follow-up."""
    template_name = "giving/contrib_attempts.html"

    def _qs(self, which):
        qs = (ContributionAttempt.objects
              .select_related("member", "fund", "transaction")
              .order_by("-created_at"))
        if which == "done":
            return qs.filter(status=ContributionAttempt.Status.SUCCESS)
        if which == "all":
            return qs
        return qs.exclude(status=ContributionAttempt.Status.SUCCESS).filter(followed_up=False)

    def get(self, request):
        which = request.GET.get("show") or "open"
        if which not in ("open", "done", "all"):
            which = "open"
        rows = list(self._qs(which)[:200])
        open_count = (ContributionAttempt.objects
                      .exclude(status=ContributionAttempt.Status.SUCCESS)
                      .filter(followed_up=False).count())
        return render(request, self.template_name, {
            "rows": rows, "show": which, "open_count": open_count,
        })

    def post(self, request):
        attempt = get_object_or_404(ContributionAttempt, pk=request.POST.get("id"))
        action = request.POST.get("action")
        if action == "query":
            refresh_from_daraja(attempt)
            messages.info(request, "Asked M-Pesa for the latest result.")
        elif action == "follow":
            attempt.followed_up = True
            attempt.followed_up_at = timezone.now()
            attempt.save(update_fields=["followed_up", "followed_up_at", "updated_at"])
            messages.success(request, f"Marked {attempt.code} as followed up.")
        elif action == "reopen":
            attempt.followed_up = False
            attempt.followed_up_at = None
            attempt.save(update_fields=["followed_up", "followed_up_at", "updated_at"])
        show = request.POST.get("show") or "open"
        return redirect(f"{request.path}?show={show}")


class ContributionLinkSmsView(TreasurerRequiredMixin, View):
    """Text members their personal link, so they can give and pass it on."""
    template_name = "giving/contrib_sms.html"

    def _recipients(self):
        return (Member.objects.filter(active=True)
                .exclude(phone__isnull=True).exclude(phone="")
                .exclude(match_code__isnull=True).exclude(match_code="")
                .order_by("name"))

    def get(self, request):
        cfg = SiteConfig.get()
        recips = list(self._recipients()[:500])
        sample = recips[0] if recips else None
        return render(request, self.template_name, {
            "recipients": recips,
            "recipient_count": self._recipients().count(),
            "sms_enabled": cfg.sms_enabled,
            "links_enabled": cfg.contrib_links_enabled,
            "preview": render_sms(sample, request, cfg) if sample else "",
            "template": (cfg.contrib_sms_template or "").strip(),
        })

    def post(self, request):
        cfg = SiteConfig.get()
        if not cfg.sms_enabled:
            messages.error(request, "SMS is switched off. Turn it on under Settings → SMS.")
            return redirect("member_contrib_sms")
        sent = failed = 0
        for member in self._recipients():
            log = send_sms(member.receipt_phone or member.phone,
                           render_sms(member, request, cfg), cfg)
            if getattr(log, "status", "") == "SENT":
                sent += 1
            else:
                failed += 1
        if sent:
            messages.success(request, f"Contribution link sent to {sent} member(s)"
                             + (f"; {failed} could not be sent." if failed else "."))
        else:
            messages.error(request, "No messages were sent. Check SMS settings.")
        return redirect("member_contrib_sms")


class ContributionLinkSmsOneView(TreasurerRequiredMixin, View):
    """Text one member their own link, from their page."""

    def post(self, request, pk):
        member = get_object_or_404(Member, pk=pk)
        cfg = SiteConfig.get()
        phone = member.receipt_phone
        if not phone:
            messages.error(request, f"{member.name} has no phone number on file.")
            return redirect("member_detail", pk=pk)
        if not cfg.sms_enabled:
            messages.error(request, "SMS is switched off. Turn it on under Settings → SMS.")
            return redirect("member_detail", pk=pk)
        log = send_sms(phone, render_sms(member, request, cfg), cfg)
        if getattr(log, "status", "") == "SENT":
            messages.success(request, f"Sent {display_name(member.name)} their contribution link.")
        else:
            messages.error(request, "The text could not be sent. Check SMS settings.")
        return redirect("member_detail", pk=pk)

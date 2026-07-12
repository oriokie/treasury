"""Phase 7 views — configurable templates, and the delivery/history log."""
import datetime as dt

from django.contrib import messages
from django.core.paginator import Paginator
from django.db.models import Q
from django.shortcuts import get_object_or_404, redirect, render
from django.views import View

from core.permissions import BenevolentSettingsMixin, BenevolentViewMixin

from .forms import NotificationTemplateForm
from .models import (BenevolentNotification, BenevolentScheme, NotificationEvent,
                     NotificationTemplate)
from .services import notify as notify_svc


class NotificationTemplateListView(BenevolentSettingsMixin, View):
    """Every configurable message, grouped by event — what the brief calls
    "editable templates... with placeholders"."""

    def get(self, request):
        notify_svc.install_default_templates()   # idempotent; nothing to lose by trying
        events = []
        for event_key, event_label in NotificationEvent.choices:
            templates = list(NotificationTemplate.objects.filter(event=event_key))
            events.append({
                "key": event_key, "label": event_label,
                "templates": templates,
                "placeholders": _PLACEHOLDER_HELP.get(event_key, []),
            })
        return render(request, "benevolent/notification_templates.html", {
            "events": events,
        })


class NotificationTemplateEditView(BenevolentSettingsMixin, View):
    def get(self, request, pk):
        tpl = get_object_or_404(NotificationTemplate, pk=pk)
        return render(request, "benevolent/notification_template_edit.html", {
            "tpl": tpl, "form": NotificationTemplateForm(instance=tpl),
            "placeholders": _PLACEHOLDER_HELP.get(tpl.event, []),
        })

    def post(self, request, pk):
        tpl = get_object_or_404(NotificationTemplate, pk=pk)
        form = NotificationTemplateForm(request.POST, instance=tpl)
        if form.is_valid():
            obj = form.save(commit=False)
            obj.updated_by = request.user
            obj.save()
            messages.success(request, "Template saved.")
            return redirect("benevolent_notification_templates")
        messages.error(request, "Check the template.")
        return render(request, "benevolent/notification_template_edit.html", {
            "tpl": tpl, "form": form, "placeholders": _PLACEHOLDER_HELP.get(tpl.event, [])})


class NotificationHistoryView(BenevolentViewMixin, View):
    """Every delivery attempt: who was told what, when, how, and whether it
    worked — notification history and delivery tracking, in one filterable
    screen."""

    def get(self, request):
        f_status = request.GET.get("status") or ""
        f_event = request.GET.get("event") or ""
        f_scheme = request.GET.get("scheme") or ""

        qs = (BenevolentNotification.objects
              .select_related("membership__member", "membership__scheme", "case", "user"))
        if f_status:
            qs = qs.filter(status=f_status)
        if f_event:
            qs = qs.filter(event=f_event)
        if f_scheme:
            qs = qs.filter(Q(membership__scheme_id=f_scheme) | Q(case__scheme_id=f_scheme))

        page = Paginator(qs.order_by("-created_at"), 50).get_page(request.GET.get("page"))

        return render(request, "benevolent/notification_history.html", {
            "page_obj": page, "items": page.object_list,
            "statuses": BenevolentNotification.Status.choices,
            "events": NotificationEvent.choices,
            "schemes": BenevolentScheme.objects.all(),
            "f_status": f_status, "f_event": f_event, "f_scheme": f_scheme,
        })


# Placeholders each event's context actually provides (see services/notify.py
# ::_context) — shown next to the edit box so a treasurer never has to guess
# or read the source to find out what {tokens} are available.
_PLACEHOLDER_HELP = {
    NotificationEvent.REGISTRATION_CONFIRMED: [
        "church", "member_name", "scheme", "membership_number", "joined_on"],
    NotificationEvent.RENEWAL_REMINDER: [
        "church", "member_name", "scheme", "membership_number", "renewal_due"],
    NotificationEvent.RENEWAL_CONFIRMED: [
        "church", "member_name", "scheme", "membership_number", "renewed_until"],
    NotificationEvent.ARREARS_REMINDER: [
        "church", "member_name", "scheme", "membership_number", "amount"],
    NotificationEvent.CASE_RECEIVED: [
        "church", "member_name", "scheme", "case_number"],
    NotificationEvent.CASE_DECIDED: [
        "church", "member_name", "scheme", "case_number", "decision", "amount_clause"],
    NotificationEvent.PAYOUT_MADE: [
        "church", "member_name", "scheme", "case_number", "amount"],
    NotificationEvent.MEMBERSHIP_STATUS_CHANGED: [
        "church", "member_name", "scheme", "membership_number", "status", "status_note"],
    NotificationEvent.COMMITTEE_VOTE_NEEDED: [
        "church", "user_name", "scheme", "case_number", "beneficiary"],
}

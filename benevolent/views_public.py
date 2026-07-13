"""The public benevolent application form, and the review that turns one into
a real membership.

The public half follows the security model the public pledge form established
(see `pledges/views.py`), for the same reasons:

  * Off unless explicitly enabled.
  * Write-only. It never reads or exposes any member data — no autocomplete, no
    lookup, no roll. The applicant types their own details as free text, and a
    reviewer links them to the real church record afterwards.
  * A submission touches no ledger, no fund, no balance, and creates no cover.
  * Honeypot, minimum fill time, and a per-session throttle against bots.
"""
import datetime as _dt
import time

from django.contrib import messages
from django.core.exceptions import ValidationError
from django.core.paginator import Paginator
from django.db import transaction as db_tx
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.utils.decorators import method_decorator
from django.views import View
from django.views.decorators.csrf import csrf_protect

from core.permissions import BenevolentRegistrationMixin

from .models import (ApplicationDependant, BenevolentApplication, BenevolentScheme,
                     BenevolentSettings, SchemeDependant)
from .services import registry as reg_svc

MIN_SECONDS = 3          # a form filled faster than this is a bot
MAX_DEPENDANTS = 12      # a sanity ceiling, not a policy


@method_decorator(csrf_protect, name="dispatch")
class PublicApplicationView(View):
    """The public form. No login."""
    template_name = "benevolent/public_apply.html"

    def _cfg(self):
        return BenevolentSettings.get()

    def _schemes(self, cfg):
        if cfg.public_form_scheme_id:
            return BenevolentScheme.objects.filter(pk=cfg.public_form_scheme_id,
                                                   status=BenevolentScheme.Status.ACTIVE)
        return BenevolentScheme.objects.filter(status=BenevolentScheme.Status.ACTIVE)

    def get(self, request):
        cfg = self._cfg()
        if not cfg.public_form_enabled:
            return render(request, "benevolent/public_disabled.html", status=404)
        request.session["ben_apply_ts"] = time.time()
        return render(request, self.template_name, {
            "cfg": cfg, "schemes": self._schemes(cfg),
            "standings": BenevolentApplication.Standing.choices,
        })

    def post(self, request):
        cfg = self._cfg()
        if not cfg.public_form_enabled:
            return render(request, "benevolent/public_disabled.html", status=404)
        schemes = self._schemes(cfg)

        def fail(msg):
            return render(request, self.template_name, {
                "cfg": cfg, "schemes": schemes,
                "standings": BenevolentApplication.Standing.choices,
                "error": msg, "form": request.POST})

        # 1) honeypot — a hidden field a real person never sees, let alone fills
        if (request.POST.get("website") or "").strip():
            return fail("Something went wrong. Please try again.")

        # 2) minimum fill time
        ts = request.session.get("ben_apply_ts")
        if not ts or (time.time() - float(ts)) < MIN_SECONDS:
            return fail("That was submitted very quickly — please take a moment and "
                        "try again.")

        # 3) simple per-session throttle
        last = request.session.get("ben_apply_last")
        if last and (time.time() - float(last)) < 30:
            return fail("An application was just submitted from this browser. If that "
                        "was you, it has been received.")

        scheme_id = request.POST.get("scheme")
        scheme = schemes.filter(pk=scheme_id).first() if scheme_id else schemes.first()
        if scheme is None:
            return fail("No scheme is open for applications at the moment.")

        name = (request.POST.get("full_name") or "").strip()
        phone = (request.POST.get("phone") or "").strip()
        if len(name) < 3:
            return fail("Please give your full name.")
        if len(phone) < 7:
            return fail("Please give a phone number we can reach you on.")

        standing = request.POST.get("standing")
        if standing not in BenevolentApplication.Standing.values:
            standing = BenevolentApplication.Standing.VISITOR

        def _date(raw):
            from django.utils.dateparse import parse_date
            return parse_date(raw) if raw else None

        with db_tx.atomic():
            app = BenevolentApplication.objects.create(
                scheme=scheme, full_name=name[:120], phone=phone[:32],
                email=(request.POST.get("email") or "").strip()[:254],
                standing=standing,
                date_of_birth=_date(request.POST.get("date_of_birth")),
                national_id=(request.POST.get("national_id") or "").strip()[:32],
                notes=(request.POST.get("notes") or "").strip()[:2000],
                submitted_ip=(request.META.get("REMOTE_ADDR") or None))

            # Dependants arrive in three named sections — spouse, children,
            # parents — because that is how a family is described out loud. A
            # single undifferentiated list makes an applicant guess where their
            # mother goes.
            added = 0
            for rel, prefix in (("SPOUSE", "spouse"), ("CHILD", "child"),
                                ("PARENT", "parent"), ("OTHER", "other")):
                names = request.POST.getlist(f"{prefix}_name")
                phones = request.POST.getlist(f"{prefix}_phone")
                dobs = request.POST.getlist(f"{prefix}_dob")
                for i, dn in enumerate(names):
                    dn = (dn or "").strip()
                    if not dn or added >= MAX_DEPENDANTS:
                        continue
                    ApplicationDependant.objects.create(
                        application=app, relationship=rel, full_name=dn[:120],
                        phone=(phones[i] if i < len(phones) else "").strip()[:32],
                        date_of_birth=_date(dobs[i] if i < len(dobs) else ""))
                    added += 1

        request.session["ben_apply_last"] = time.time()
        return render(request, "benevolent/public_thanks.html",
                      {"application": app, "cfg": cfg})


# ===========================================================================
# Review — where an application becomes a membership (or does not)
# ===========================================================================

class ApplicationListView(BenevolentRegistrationMixin, View):
    template_name = "benevolent/application_list.html"

    def get(self, request):
        f_status = request.GET.get("status") or BenevolentApplication.Status.PENDING
        qs = (BenevolentApplication.objects
              .select_related("scheme", "matched_member", "membership")
              .prefetch_related("dependants"))
        if f_status:
            qs = qs.filter(status=f_status)
        page = Paginator(qs, 30).get_page(request.GET.get("page"))
        return render(request, self.template_name, {
            "page_obj": page, "applications": page.object_list,
            "statuses": BenevolentApplication.Status.choices,
            "f_status": f_status,
            "pending": BenevolentApplication.objects.filter(
                status=BenevolentApplication.Status.PENDING).count(),
        })


class ApplicationDetailView(BenevolentRegistrationMixin, View):
    template_name = "benevolent/application_detail.html"

    def get(self, request, pk):
        app = get_object_or_404(
            BenevolentApplication.objects.select_related("scheme").prefetch_related(
                "dependants"), pk=pk)
        return render(request, self.template_name, {
            "app": app, "candidates": self._candidates(app)})

    def _candidates(self, app):
        """Church members this application MIGHT be. Matched on phone, because
        that is the one thing an applicant types that the church also holds —
        and shown to a reviewer, never to the applicant, who cannot search the
        roll at all."""
        from django.db.models import Q
        from members.models import Member
        digits = "".join(ch for ch in (app.phone or "") if ch.isdigit())
        if len(digits) < 7:
            return Member.objects.none()
        tail = digits[-9:]
        return (Member.objects.filter(active=True)
                .filter(Q(phone__contains=tail) | Q(phones__number__contains=tail)
                       | Q(name__icontains=app.full_name.split()[0]))
                .distinct()[:6])

    @db_tx.atomic
    def post(self, request, pk):
        app = get_object_or_404(BenevolentApplication, pk=pk)
        action = request.POST.get("action")

        if action == "reject":
            app.status = BenevolentApplication.Status.REJECTED
            app.reviewed_by = request.user
            app.reviewed_at = timezone.now()
            app.review_note = (request.POST.get("note") or "")[:255]
            app.save()
            messages.success(request, f"{app.full_name}'s application was not accepted.")
            return redirect("benevolent_applications")

        if action != "approve":
            return redirect("benevolent_application_detail", pk=pk)

        if not app.is_pending:
            messages.error(request, "This application has already been reviewed.")
            return redirect("benevolent_application_detail", pk=pk)

        # Link to the church roll: either an existing member the reviewer picked,
        # or a new record created from what the applicant typed. Either way it
        # goes through the SAME matcher every bank import uses, so a person who
        # is already known is found rather than duplicated.
        from members.services.matching import match_or_create_member
        chosen = request.POST.get("member")
        if chosen:
            from members.models import Member
            member = get_object_or_404(Member, pk=chosen)
        else:
            member, _how = match_or_create_member(app.full_name, app.phone)

        dependants = [
            {"name": d.full_name, "relationship": d.relationship, "phone": d.phone}
            for d in app.dependants.all()]

        try:
            membership = reg_svc.register(
                app.scheme, member,
                joined_on=_dt.date.today(), user=request.user,
                registration_type=("HOUSEHOLD" if dependants else "INDIVIDUAL"),
                date_of_birth=app.date_of_birth,
                notes=f"From a public application submitted "
                      f"{app.submitted_at:%d %b %Y}."[:200],
                dependants=dependants)
        except ValidationError as e:
            messages.error(request, "; ".join(e.messages))
            return redirect("benevolent_application_detail", pk=pk)

        app.status = BenevolentApplication.Status.APPROVED
        app.reviewed_by = request.user
        app.reviewed_at = timezone.now()
        app.review_note = (request.POST.get("note") or "")[:255]
        app.matched_member = member
        app.membership = membership
        app.save()

        messages.success(
            request,
            f"{member.name} registered as {membership.number}"
            + (f" with {len(dependants)} dependant(s)." if dependants else "."))
        return redirect("benevolent_membership_detail", pk=membership.pk)

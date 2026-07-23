"""The office side of the member portal: who has access, and what they have sent.

Two screens and a decision page. The decision page is where the portal earns
its keep or fails: an officer reads a member's request, and either declines it
with a reason the member can read, or approves it — at which point
``services.portal.approve_request`` calls the service that owns the change.
Nothing on this page writes to a case, a dependant or a member record itself.

Permissions are layered deliberately. Opening the queue needs only scheme
administration (``PortalAdminMixin``); *deciding* an item needs the right that
owns the change it would make — registration for household and profile changes,
cases for assistance and deaths. A church that has split those roles keeps them
split here, rather than the portal quietly becoming a way round the split.
"""
from django.contrib import messages
from django.core.exceptions import ValidationError
from django.core.paginator import Paginator
from django.shortcuts import get_object_or_404, redirect
from django.views.generic import TemplateView

from core import roles
from core.permissions import PortalAdminMixin

from .models import MemberAccount, PortalRequest
from .services import portal as portal_svc


def _may_decide(user, req):
    """The right that owns the change this request would make.

    Approving an assistance request raises a case; approving a household change
    edits cover. Those are different powers and this module already has
    different rights for them, so the portal asks the same question the direct
    screens ask rather than inventing a blanket "portal approver".
    """
    if req.kind in {PortalRequest.Kind.ASSISTANCE, PortalRequest.Kind.DEATH}:
        return roles.can_manage_benevolent_cases(user)
    if req.kind in {PortalRequest.Kind.HOUSEHOLD, PortalRequest.Kind.PROFILE}:
        return roles.can_register_benevolent_members(user)
    # a correction is an accepted point, not a record change (see _apply_noop)
    return roles.can_manage_benevolent(user)


class PortalAccountListView(PortalAdminMixin, TemplateView):
    """Who can sign in, and who cannot yet."""
    template_name = "benevolent/portal/admin_accounts.html"

    def get_context_data(self, **kwargs):
        from members.models import Member
        ctx = super().get_context_data(**kwargs)
        rows = (MemberAccount.objects
                .select_related("member", "user", "invited_by")
                .order_by("member__name"))
        status = self.request.GET.get("status") or ""
        if status in MemberAccount.Status.values:
            rows = rows.filter(status=status)
        q = (self.request.GET.get("q") or "").strip()
        if q:
            rows = rows.filter(member__name__icontains=q)

        ctx["status"], ctx["q"] = status, q
        ctx["statuses"] = MemberAccount.Status.choices
        paginator = Paginator(rows, 40)
        ctx["page"] = paginator.get_page(self.request.GET.get("page"))
        # A list of (label, count) rather than a dict: the template can iterate
        # it directly, instead of needing a dictionary-lookup filter that would
        # exist for this one page.
        ctx["counts"] = [
            (label, MemberAccount.objects.filter(status=value).count())
            for value, label in MemberAccount.Status.choices]
        # members enrolled in a scheme who have no portal account yet
        ctx["uninvited"] = (Member.objects
                            .filter(scheme_memberships__isnull=False,
                                    portal_account__isnull=True)
                            .distinct().order_by("name")[:50])
        return ctx

    def post(self, request, *args, **kwargs):
        from members.models import Member
        action = request.POST.get("action")
        try:
            if action == "invite":
                member = get_object_or_404(Member, pk=request.POST.get("member"))
                account = portal_svc.invite(
                    member, actor=request.user,
                    email=(request.POST.get("email") or "").strip(),
                    phone=(request.POST.get("phone") or "").strip())
                messages.success(
                    request,
                    f"{member.name} can now sign in as “{account.user.username}”. "
                    f"They set their own password using “forgot password”.")
            else:
                account = get_object_or_404(MemberAccount,
                                            pk=request.POST.get("account"))
                if action == "suspend":
                    portal_svc.suspend(account, actor=request.user,
                                       reason=request.POST.get("reason", ""))
                    messages.success(request, f"Portal access suspended for "
                                              f"{account.member.name}.")
                elif action == "restore":
                    portal_svc.restore(account, actor=request.user)
                    messages.success(request, f"Portal access restored for "
                                              f"{account.member.name}.")
                elif action == "close":
                    portal_svc.close(account, actor=request.user,
                                     reason=request.POST.get("reason", ""))
                    messages.success(request, f"Portal account closed for "
                                              f"{account.member.name}.")
                else:
                    messages.error(request, "Unknown action.")
        except ValidationError as exc:
            messages.error(request, "; ".join(exc.messages))
        return redirect("portal_admin_accounts")


class PortalRequestQueueView(PortalAdminMixin, TemplateView):
    """Everything members have sent in."""
    template_name = "benevolent/portal/admin_queue.html"

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        rows = (PortalRequest.objects
                .select_related("account", "account__member", "membership",
                                "membership__scheme", "event_type", "case")
                .order_by("submitted_at", "-created_at"))
        status = self.request.GET.get("status") or "open"
        if status == "open":
            rows = rows.filter(status__in=list(PortalRequest.OPEN_STATUSES))
        elif status in PortalRequest.Status.values:
            rows = rows.filter(status=status)
        kind = self.request.GET.get("kind") or ""
        if kind in PortalRequest.Kind.values:
            rows = rows.filter(kind=kind)

        ctx["status"], ctx["kind"] = status, kind
        ctx["statuses"] = PortalRequest.Status.choices
        ctx["kinds"] = PortalRequest.Kind.choices
        paginator = Paginator(rows, 30)
        ctx["page"] = paginator.get_page(self.request.GET.get("page"))
        ctx["awaiting"] = PortalRequest.objects.filter(
            status__in=[PortalRequest.Status.SUBMITTED,
                        PortalRequest.Status.UNDER_REVIEW]).count()
        return ctx


class PortalRequestReviewView(PortalAdminMixin, TemplateView):
    """Read one request and decide it."""
    template_name = "benevolent/portal/admin_review.html"

    def _request_obj(self):
        return get_object_or_404(
            PortalRequest.objects.select_related(
                "account", "account__member", "membership", "membership__scheme",
                "event_type", "dependant", "case"),
            pk=self.kwargs["pk"])

    def get_context_data(self, **kwargs):
        from .models import BenevolentEventType
        ctx = super().get_context_data(**kwargs)
        req = self._request_obj()
        ctx["req"] = req
        ctx["documents"] = req.documents.filter(withdrawn_at__isnull=True)
        ctx["thread"] = req.messages.select_related("author")
        ctx["may_decide"] = _may_decide(self.request.user, req)
        ctx["history"] = (portal_svc.scope(req.account).requests()
                          .exclude(pk=req.pk)[:8])
        if req.membership_id:
            ctx["event_types"] = BenevolentEventType.objects.filter(
                scheme=req.membership.scheme, active=True).order_by("name")
        return ctx

    def post(self, request, *args, **kwargs):
        req = self._request_obj()
        action = request.POST.get("action")

        if action in {"approve", "decline"} and not _may_decide(request.user, req):
            messages.error(
                request,
                "Deciding this request needs the right that owns the change it "
                "would make. Someone with that role must action it.")
            return redirect("portal_admin_review", pk=req.pk)

        try:
            if action == "take":
                portal_svc.take_for_review(req, user=request.user)
                messages.success(request, f"{req.reference} is now under review.")
            elif action == "info":
                portal_svc.request_more_information(
                    req, user=request.user, message=request.POST.get("message", ""))
                messages.success(request, "The member has been asked for more detail.")
            elif action == "note":
                req.internal_note = request.POST.get("internal_note", "")[:4000]
                req.save(update_fields=["internal_note", "updated_at"])
                messages.success(request, "Office note saved.")
            elif action == "decline":
                portal_svc.decline_request(
                    req, user=request.user, reason=request.POST.get("reason", ""))
                messages.success(request, f"{req.reference} declined.")
            elif action == "approve":
                # An assistance request needs the event type chosen before it can
                # become a case; let the officer set it here rather than sending
                # them away to edit the request first.
                if request.POST.get("event_type") and req.membership_id:
                    from .models import BenevolentEventType
                    event_type = BenevolentEventType.objects.filter(
                        pk=request.POST["event_type"],
                        scheme=req.membership.scheme).first()
                    if event_type:
                        req.event_type = event_type
                        req.save(update_fields=["event_type", "updated_at"])
                amount = request.POST.get("amount") or None
                portal_svc.approve_request(
                    req, user=request.user,
                    note=request.POST.get("decision_note", ""),
                    **({"amount": amount} if req.kind == PortalRequest.Kind.ASSISTANCE
                       else {}))
                req.refresh_from_db()
                if req.case_id:
                    messages.success(
                        request,
                        f"{req.reference} approved — case {req.case.number} raised. "
                        f"It still has to be assessed and approved on its own merits.")
                else:
                    messages.success(request, f"{req.reference} approved.")
            else:
                messages.error(request, "Unknown action.")
        except ValidationError as exc:
            messages.error(request, "; ".join(exc.messages))
        return redirect("portal_admin_review", pk=req.pk)

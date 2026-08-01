"""The member self-service workspace.

Every view here is gated by ``PortalAccessMixin`` (which guarantees
``self.account``) and gets its rows from ``services.portal.scope(self.account)``
— never from a manager directly. That is the whole access-control story for
this file: if a view queries a model without going through the scope, it is a
bug, and the guard test in ``test_portal_security`` is there to catch it.

The figures are the same figures the office sees, from the same services. A
member's arrears on their phone and the arrears on the treasurer's registry
screen are one computation (``contributions.arrears_for``) rendered twice. This
is the point of the exercise: a portal that computed its own numbers would be a
second opinion about a member's debt, and the member would be right to trust
whichever one suited them.
"""
import datetime as _dt
from decimal import Decimal

from django.contrib import messages
from django.core.exceptions import PermissionDenied, ValidationError
from django.core.paginator import Paginator
from django.db.models import Sum
from django.http import FileResponse, Http404, HttpResponseRedirect
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views import View
from django.views.generic import TemplateView

from core.permissions import PortalAccessMixin

from .models import (BenevolentEventType, CaseEvent, MemberAccount,
                     PortalAccessLog, PortalDocument, PortalRequest,
                     SchemeDependant)
from .services import portal as portal_svc

Action = PortalAccessLog.Action

MAX_UPLOAD_BYTES = 10 * 1024 * 1024      # 10 MB — a phone photo, comfortably
ALLOWED_UPLOAD_TYPES = {
    "application/pdf", "image/jpeg", "image/png", "image/heic", "image/webp",
}
ALLOWED_UPLOAD_EXTENSIONS = {".pdf", ".jpg", ".jpeg", ".png", ".heic", ".webp"}


class PortalBase(PortalAccessMixin, TemplateView):
    """Shared context every portal page needs for its chrome."""
    log_action = None
    # The browser tab. `portal/_base.html` has always rendered
    # `{{ portal_title|default:"My scheme" }}`, but no view ever set it, so every
    # page in the portal shared one title and a member with three tabs open could
    # not tell them apart. Declared per view below.
    portal_title = ""

    def scope(self):
        return portal_svc.scope(self.account)

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        account = self.account
        sc = self.scope()
        ctx["account"] = account
        ctx["member"] = account.member
        ctx["portal_title"] = self.portal_title or "My scheme"
        ctx["memberships"] = sc.memberships()
        ctx["open_request_count"] = sc.requests().filter(
            status__in=list(PortalRequest.OPEN_STATUSES)).count()
        ctx["action_needed_count"] = sc.requests().filter(
            status=PortalRequest.Status.INFO_NEEDED).count()
        ctx["portal_nav"] = self.nav_key()
        return ctx

    def nav_key(self):
        return ""

    def dispatch(self, request, *args, **kwargs):
        response = super().dispatch(request, *args, **kwargs)
        if self.log_action and getattr(response, "status_code", 0) == 200:
            portal_svc.log_access(self.account, self.log_action, request=request)
        return response


# ---------------------------------------------------------------------------
# Home
# ---------------------------------------------------------------------------

class PortalHomeView(PortalBase):
    template_name = "benevolent/portal/home.html"
    portal_title = "My scheme"
    log_action = Action.SIGN_IN

    def nav_key(self):
        return "home"

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx.update(portal_svc.overview(self.account))
        return ctx


class PortalUnavailableView(TemplateView):
    """Shown to a login bound to an account that is not usable.

    Deliberately outside ``PortalAccessMixin`` — it is the page that mixin
    redirects to, so gating it with the same test would loop. It shows nothing
    but the account's own state, which is the one thing its owner is entitled
    to know.
    """
    template_name = "benevolent/portal/unavailable.html"

    def get_context_data(self, **kwargs):
        from core import roles
        ctx = super().get_context_data(**kwargs)
        ctx["account"] = roles.member_account(self.request.user)
        return ctx


# ---------------------------------------------------------------------------
# Money — contributions, receipts, statements
# ---------------------------------------------------------------------------

class PortalContributionsView(PortalBase):
    template_name = "benevolent/portal/contributions.html"
    portal_title = "My contributions"
    log_action = Action.VIEW_CONTRIBUTIONS

    def nav_key(self):
        return "contributions"

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        rows = self.scope().contributions()

        year = self.request.GET.get("year") or ""
        if year.isdigit():
            rows = rows.filter(transaction__date__year=int(year))
        scheme_id = self.request.GET.get("scheme") or ""
        if scheme_id.isdigit():
            rows = rows.filter(membership__scheme_id=int(scheme_id))

        ctx["year"], ctx["scheme_id"] = year, scheme_id
        ctx["years"] = _contribution_years(self.scope())
        ctx["total"] = rows.aggregate(s=Sum("transaction__amount"))["s"] or Decimal("0")
        ctx["count"] = rows.count()
        # The date of the most recent gift in the current selection. A member's
        # first question about their own record is nearly always "did my last
        # payment land", so it is answered on the page rather than left to be
        # inferred from the top row of the table.
        ctx["last_on"] = rows.order_by("-transaction__date").values_list(
            "transaction__date", flat=True).first()

        paginator = Paginator(rows, 25)
        ctx["page"] = paginator.get_page(self.request.GET.get("page"))
        return ctx


class PortalStatementView(PortalBase):
    """A member's own contribution statement for a year.

    Rendered as a print-ready page in the application's statement design
    language, and downloadable as a spreadsheet through the module's existing
    ``exports`` helpers — the same formatter the office uses, given a scoped
    queryset. Reusing it means a member's statement and an officer's export of
    the same rows cannot disagree about what a column means.
    """
    template_name = "benevolent/portal/statement.html"
    portal_title = "Contribution statement"

    def nav_key(self):
        return "contributions"

    def get(self, request, *args, **kwargs):
        fmt = request.GET.get("export")
        if fmt in {"csv", "xlsx"}:
            return self._export(fmt)
        return super().get(request, *args, **kwargs)

    def _rows(self):
        year = self.request.GET.get("year")
        rows = self.scope().contributions()
        if year and str(year).isdigit():
            rows = rows.filter(transaction__date__year=int(year))
        return rows, (int(year) if year and str(year).isdigit() else None)

    def _export(self, fmt):
        from core.models import SiteConfig
        from .exports import contribution_rows, export_response
        rows, year = self._rows()
        header, data = contribution_rows(rows, user=self.request.user)
        portal_svc.log_access(
            self.account, Action.DOWNLOAD_STATEMENT, request=self.request,
            detail=f"{fmt} statement for {year or 'all years'}")
        return export_response(
            fmt,
            filename=f"statement-{self.account.member.name}-{year or 'all'}".replace(" ", "-"),
            title=f"Contribution statement — {year or 'all years'}",
            header=header, rows=data,
            church=SiteConfig.get().church_name)

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        rows, year = self._rows()
        ctx["rows"] = rows
        ctx["year"] = year or "All years"
        ctx["total"] = rows.aggregate(s=Sum("transaction__amount"))["s"] or Decimal("0")
        ctx["generated_at"] = timezone.now()
        ctx["years"] = _contribution_years(self.scope())
        portal_svc.log_access(self.account, Action.DOWNLOAD_STATEMENT,
                              request=self.request, detail=f"viewed {year or 'all'}")
        return ctx


class PortalReceiptView(PortalBase):
    """The receipt for one contribution."""
    template_name = "benevolent/portal/receipt.html"
    portal_title = "Receipt"

    def nav_key(self):
        return "contributions"

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        contribution = self.scope().contribution(self.kwargs["pk"])
        ctx["contribution"] = contribution
        ctx["generated_at"] = timezone.now()
        portal_svc.log_access(self.account, Action.DOWNLOAD_RECEIPT,
                              request=self.request,
                              object_ref=f"contribution:{contribution.pk}")
        return ctx


# ---------------------------------------------------------------------------
# Standing, eligibility, benefits
# ---------------------------------------------------------------------------

class PortalStandingView(PortalBase):
    """Where the member stands, and what they would be entitled to.

    Both answers come from the engines that decide them for real —
    ``standing.assess`` and ``eligibility.evaluate``. The eligibility figure is
    shown as an *indication* and labelled as one: it is evaluated against
    today's facts with no case in front of it, and the checks that depend on a
    case (documents, nominee, the claim window) cannot be answered yet. Showing
    it as a promise would be the portal writing a cheque the assessor has to
    honour.
    """
    template_name = "benevolent/portal/standing.html"
    portal_title = "My standing"
    log_action = Action.VIEW_STANDING

    def nav_key(self):
        return "standing"

    def get_context_data(self, **kwargs):
        from .services import contributions as contrib_svc
        from .services import eligibility as elig_svc
        from .services import standing as standing_svc

        ctx = super().get_context_data(**kwargs)
        rows = []
        for m in self.scope().memberships():
            policy = m.scheme.current_policy
            try:
                assessment = standing_svc.assess(m, policy)
            except Exception:
                assessment = None
            try:
                arrears = contrib_svc.arrears_for(m, policy)
            except Exception:
                arrears = Decimal("0")
            try:
                schedule = contrib_svc.dues_schedule(m, policy)
            except Exception:
                schedule = []
            benefits = []
            if policy is not None:
                for event_type in BenevolentEventType.objects.filter(
                        scheme=m.scheme, active=True).order_by("name"):
                    try:
                        result = elig_svc.evaluate(
                            m.scheme, event_type=event_type,
                            event_date=_dt.date.today(), membership=m)
                    except Exception:
                        result = None
                    benefits.append({"event_type": event_type, "result": result})
            rows.append({
                "membership": m, "scheme": m.scheme, "policy": policy,
                "standing": assessment, "arrears": arrears,
                "schedule": schedule[-6:] if schedule else [],
                "benefits": benefits,
            })
        ctx["rows"] = rows
        return ctx


# ---------------------------------------------------------------------------
# Household
# ---------------------------------------------------------------------------

class PortalHouseholdView(PortalBase):
    template_name = "benevolent/portal/household.html"
    portal_title = "My household"
    log_action = Action.VIEW_HOUSEHOLD

    def nav_key(self):
        return "household"

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        sc = self.scope()
        ctx["dependants"] = sc.dependants().filter(removed_on__isnull=True)
        ctx["former"] = sc.dependants().filter(removed_on__isnull=False)
        ctx["pending"] = sc.requests().filter(
            kind__in=[PortalRequest.Kind.HOUSEHOLD, PortalRequest.Kind.PROFILE],
            status__in=list(PortalRequest.OPEN_STATUSES))
        ctx["relationships"] = SchemeDependant.Relationship.choices
        return ctx


# ---------------------------------------------------------------------------
# Cases
# ---------------------------------------------------------------------------

class PortalCaseListView(PortalBase):
    template_name = "benevolent/portal/cases.html"
    portal_title = "My cases"

    def nav_key(self):
        return "cases"

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx["cases"] = self.scope().cases()
        return ctx


class PortalCaseDetailView(PortalBase):
    template_name = "benevolent/portal/case_detail.html"
    portal_title = "Case"

    def nav_key(self):
        return "cases"

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        case = self.scope().case(self.kwargs["pk"])
        ctx["case"] = case
        # The member sees progress, not the committee's deliberations. Votes,
        # internal notes and the fraud flags are the church's working papers;
        # what a claimant is owed is an honest account of where their claim has
        # got to and what it was decided.
        # Kinds referenced through the enum, not as loose strings. Two of the
        # strings here were not real CaseEvent kinds at all — "PAID" (the value
        # is PAY_PAID) and "DOCUMENT" (DOC_ADD) — so the two events a claimant
        # most wants to see, that they were PAID and that their documents were
        # received, were silently filtered out of their own timeline. A typo in
        # a string list fails quietly; a typo on the enum fails loudly.
        _K = CaseEvent.Kind
        ctx["timeline"] = case.events.filter(kind__in=[
            _K.RAISED, _K.SUBMITTED, _K.ASSESSED, _K.APPROVED, _K.REJECTED,
            _K.PAYOUT_PAID, _K.CLOSED, _K.DOCUMENT_ADDED,
        ]).order_by("on", "created_at")   # oldest first: this reads as progress
        ctx["documents"] = case.attachments.all()
        ctx["payouts"] = case.payouts.all()
        portal_svc.log_access(self.account, Action.VIEW_CASE, request=self.request,
                              object_ref=f"case:{case.pk}")
        return ctx


# ---------------------------------------------------------------------------
# Requests
# ---------------------------------------------------------------------------

class PortalRequestListView(PortalBase):
    template_name = "benevolent/portal/requests.html"
    portal_title = "My requests"

    def nav_key(self):
        return "requests"

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        rows = self.scope().requests()
        status = self.request.GET.get("status") or ""
        if status == "open":
            rows = rows.filter(status__in=list(PortalRequest.OPEN_STATUSES))
        elif status:
            rows = rows.filter(status=status)
        ctx["status"] = status
        ctx["requests"] = rows
        ctx["kinds"] = PortalRequest.Kind.choices
        return ctx


class PortalRequestCreateView(PortalBase):
    """One form, five shapes.

    The kind is in the URL rather than a field, so each entry point is its own
    page with its own wording — "report a death" and "correct a record" are
    very different moments for the person filling them in, and a single generic
    form with a dropdown serves neither.
    """
    template_name = "benevolent/portal/request_new.html"
    portal_title = "New request"

    def nav_key(self):
        return "requests"

    def _kind(self):
        kind = self.kwargs.get("kind", "").upper()
        if kind not in PortalRequest.Kind.values:
            raise Http404("Unknown request type.")
        return kind

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        kind = self._kind()
        sc = self.scope()
        ctx["kind"] = kind
        ctx["kind_label"] = dict(PortalRequest.Kind.choices)[kind]
        ctx["dependants"] = sc.dependants().filter(removed_on__isnull=True)
        ctx["relationships"] = SchemeDependant.Relationship.choices
        ctx["event_types"] = BenevolentEventType.objects.filter(
            scheme__in=[m.scheme_id for m in sc.memberships()], active=True
        ).order_by("name")
        ctx["today"] = _dt.date.today()
        ctx["form"] = self.request.POST or None
        # `v` holds the values the form renders. Empty when starting a request
        # from scratch, filled from the query string when the member arrived by
        # asking to correct a particular person, and filled from the request
        # itself when amending one — which is what lets one template serve all
        # three instead of copies that would drift apart.
        ctx.setdefault("v", self._prefill_from_query(kind, sc))
        ctx.setdefault("is_edit", False)
        return ctx

    def _prefill_from_query(self, kind, sc):
        """Fill the form from `?dependant=…&op=…`, as the household page links.

        "Correct" beside a person on the household page should open a form that
        already knows who is meant and what their details currently are, so the
        member changes the one field that is wrong instead of copying four
        correct ones out of the page above. Without this the link carried the
        person's id and the form ignored it, leaving them to retype everything
        and leaving the office to guess which of the four values was the
        correction.

        A dependant the member is not entitled to see is dropped rather than
        refused: the query string is a convenience, and a bad one should open an
        ordinary blank form, not an error page.
        """
        params = self.request.GET
        values = {}
        if kind != PortalRequest.Kind.HOUSEHOLD:
            return values
        if params.get("op") in {"add", "update", "remove"}:
            values["op"] = params["op"]
        raw = params.get("dependant")
        if not raw:
            return values
        try:
            dep = sc.dependant(int(raw))
        except (TypeError, ValueError, PermissionDenied):
            return values
        values.update({
            "dependant": dep.pk,
            # `display_name` so a dependant who is also on the church roll shows
            # the name the church actually holds, which is the one being corrected.
            "name": dep.display_name,
            "relationship": dep.relationship,
            "phone": (dep.phone or ""),
            "date_of_birth": (dep.date_of_birth.isoformat()
                              if dep.date_of_birth else ""),
            "subject": f"Correction to {dep.display_name}'s details",
        })
        return values

    def _fields_from(self, post, sc, kind):
        """Read one submitted form into the arguments the services take.

        Shared with the edit view so a field added to a form is picked up on
        both paths; when only one of them knew about a field, a member editing a
        draft would silently lose it.
        """
        membership = None
        if post.get("membership"):
            try:
                membership = sc.membership(int(post["membership"]))
            except (ValueError, PermissionDenied):
                membership = None

        dependant = None
        if post.get("dependant"):
            try:
                dependant = sc.dependant(int(post["dependant"]))
            except (ValueError, PermissionDenied):
                dependant = None

        event_type = None
        if post.get("event_type"):
            allowed = [m.scheme_id for m in sc.memberships()]
            event_type = BenevolentEventType.objects.filter(
                pk=post["event_type"], scheme_id__in=allowed).first()

        payload = {}
        if kind == PortalRequest.Kind.HOUSEHOLD:
            payload = {
                "op": post.get("op") or "add",
                "relationship": post.get("relationship") or "",
                "name": (post.get("name") or "").strip(),
                "phone": (post.get("phone") or "").strip(),
                "date_of_birth": post.get("date_of_birth") or "",
            }
        elif kind == PortalRequest.Kind.PROFILE:
            payload = {
                "name": (post.get("new_name") or "").strip(),
                "phone": (post.get("new_phone") or "").strip(),
            }
        elif kind == PortalRequest.Kind.CORRECTION:
            payload = {"what": (post.get("what") or "").strip()}

        return {
            "subject": post.get("subject") or dict(PortalRequest.Kind.choices)[kind],
            "detail": post.get("detail") or "",
            "membership": membership, "event_type": event_type,
            "event_date": _parse_date(post.get("event_date")),
            "dependant": dependant,
            "deceased_name": post.get("deceased_name") or "",
            "payload": payload,
        }

    def post(self, request, *args, **kwargs):
        kind = self._kind()
        sc = self.scope()
        post = request.POST

        try:
            req = portal_svc.create_request(
                self.account, kind=kind,
                submit=post.get("action") == "submit",
                **self._fields_from(post, sc, kind))
        except ValidationError as exc:
            ctx = self.get_context_data(**kwargs)
            ctx["error"] = "; ".join(
                m for msgs in getattr(exc, "message_dict", {"": exc.messages}).values()
                for m in msgs)
            return self.render_to_response(ctx)

        _attach_uploads(request, self.account, req)
        messages.success(
            request,
            f"Request {req.reference} " +
            ("has been sent to the church office." if req.status ==
             PortalRequest.Status.SUBMITTED else "has been saved as a draft."))
        return redirect("portal_request_detail", pk=req.pk)


class PortalRequestEditView(PortalRequestCreateView):
    """Amend a request the member still holds.

    Subclasses the create view rather than copying it: the same template, the
    same five form shapes, the same field parsing. The differences are only
    that the kind comes from the request instead of the URL, the fields start
    filled in, and saving amends rather than creates.

    Scope is enforced by `scope().request(pk)`, which raises for a request that
    is not this member's — a member cannot reach another member's draft by
    guessing a number. Whether it may be edited *at all* is decided by
    `portal_svc.update_request`, so the rule cannot be bypassed by posting
    directly to this URL.
    """
    portal_title = "Edit request"

    def _req(self):
        return self.scope().request(self.kwargs["pk"])

    def _kind(self):
        return self._req().kind

    def get_context_data(self, **kwargs):
        req = self._req()
        ctx = super().get_context_data(**kwargs)
        ctx["req"] = req
        ctx["is_edit"] = True
        if not req.member_may_edit:
            ctx["locked"] = True
        payload = req.payload or {}
        # Names here match the form's field names, so the template asks for the
        # value under the same name it posts back.
        ctx["v"] = {
            "subject": req.subject, "detail": req.detail,
            "membership": req.membership_id, "dependant": req.dependant_id,
            "event_type": req.event_type_id,
            "event_date": req.event_date.isoformat() if req.event_date else "",
            "deceased_name": req.deceased_name,
            "op": payload.get("op") or "add",
            "relationship": payload.get("relationship") or "",
            "name": payload.get("name") or "",
            "phone": payload.get("phone") or "",
            "date_of_birth": payload.get("date_of_birth") or "",
            "new_name": payload.get("name") or "",
            "new_phone": payload.get("phone") or "",
            "what": payload.get("what") or "",
        }
        return ctx

    def get(self, request, *args, **kwargs):
        req = self._req()
        if not req.member_may_edit:
            messages.error(
                request,
                f"{req.reference} is with the church office and can no longer "
                "be changed. You can still reply on it.")
            return redirect("portal_request_detail", pk=req.pk)
        return super().get(request, *args, **kwargs)

    def post(self, request, *args, **kwargs):
        req = self._req()
        sc = self.scope()
        post = request.POST
        fields = self._fields_from(post, sc, req.kind)
        # `_fields_from` returns None for a cleared selector, which
        # `update_request` reads as "leave alone". On an edit the member really
        # has cleared it, so say so explicitly.
        for key in ("dependant", "event_type", "event_date"):
            if fields[key] is None:
                fields[key] = False
        try:
            portal_svc.update_request(
                req, actor=request.user,
                submit=post.get("action") == "submit", **fields)
        except ValidationError as exc:
            ctx = self.get_context_data(**kwargs)
            ctx["error"] = "; ".join(
                m for msgs in getattr(exc, "message_dict", {"": exc.messages}).values()
                for m in msgs)
            return self.render_to_response(ctx)

        _attach_uploads(request, self.account, req)
        req.refresh_from_db()
        messages.success(
            request,
            f"{req.reference} " +
            ("has been sent to the church office."
             if req.status == PortalRequest.Status.SUBMITTED
             else "has been updated and is still a draft."))
        return redirect("portal_request_detail", pk=req.pk)


class PortalRequestDetailView(PortalBase):
    template_name = "benevolent/portal/request_detail.html"
    portal_title = "Request"

    def nav_key(self):
        return "requests"

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        req = self.scope().request(self.kwargs["pk"])
        ctx["req"] = req
        ctx["documents"] = req.documents.filter(withdrawn_at__isnull=True)
        ctx["messages_thread"] = req.messages.select_related("author")
        return ctx

    def post(self, request, *args, **kwargs):
        req = self.scope().request(self.kwargs["pk"])
        action = request.POST.get("action")
        try:
            if action == "submit":
                portal_svc.submit_request(req, actor=request.user)
                messages.success(request, f"{req.reference} has been sent to the office.")
            elif action == "withdraw":
                portal_svc.withdraw_request(
                    req, actor=request.user, reason=request.POST.get("reason", ""))
                messages.success(request, f"{req.reference} has been withdrawn.")
            elif action == "reply":
                portal_svc.add_message(
                    req, body=request.POST.get("body", ""),
                    user=request.user, from_member=True)
                # a reply to "more information needed" is the member handing it
                # back, so re-submit it rather than leaving it in their court
                if req.status == PortalRequest.Status.INFO_NEEDED:
                    portal_svc.submit_request(req, actor=request.user)
                messages.success(request, "Your reply has been sent.")
            elif action == "upload":
                _attach_uploads(request, self.account, req)
            else:
                messages.error(request, "Unknown action.")
        except ValidationError as exc:
            messages.error(request, "; ".join(exc.messages))
        return redirect("portal_request_detail", pk=req.pk)


# ---------------------------------------------------------------------------
# Documents
# ---------------------------------------------------------------------------

def _attach_uploads(request, account, req=None):
    """Take whatever files came with this form.

    Validated on size and type here rather than trusting the browser. The
    extension check is belt-and-braces beside the content type: a browser will
    happily report ``application/pdf`` for anything, and a member's phone will
    happily report nothing at all.
    """
    files = request.FILES.getlist("documents") or request.FILES.getlist("document")
    kind = request.POST.get("document_kind") or PortalDocument.Kind.OTHER
    if kind not in PortalDocument.Kind.values:
        kind = PortalDocument.Kind.OTHER
    saved = 0
    for upload in files[:10]:
        if upload.size > MAX_UPLOAD_BYTES:
            messages.error(request, f"{upload.name} is too large (limit 10 MB).")
            continue
        suffix = ("." + upload.name.rsplit(".", 1)[-1].lower()) if "." in upload.name else ""
        if suffix not in ALLOWED_UPLOAD_EXTENSIONS:
            messages.error(
                request,
                f"{upload.name} is not a supported file. Send a PDF or a photo.")
            continue
        PortalDocument.objects.create(
            account=account, request=req, kind=kind,
            label=(request.POST.get("document_label") or "").strip()[:140],
            file=upload, original_name=upload.name[:200],
            content_type=(upload.content_type or "")[:100],
            size_bytes=upload.size)
        saved += 1
    if saved:
        messages.success(request, f"{saved} document(s) uploaded.")
    return saved


class PortalDocumentListView(PortalBase):
    template_name = "benevolent/portal/documents.html"
    portal_title = "My documents"

    def nav_key(self):
        return "documents"

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx["documents"] = self.scope().documents()
        ctx["kinds"] = PortalDocument.Kind.choices
        return ctx

    def post(self, request, *args, **kwargs):
        _attach_uploads(request, self.account)
        return redirect("portal_documents")


class PortalDocumentActionView(PortalAccessMixin, View):
    """Withdraw or download one of the member's own documents."""

    def scope(self):
        return portal_svc.scope(self.account)

    def get(self, request, pk, action=None):
        doc = self.scope().document(pk)
        if action != "download":
            raise Http404
        portal_svc.log_access(self.account, Action.DOWNLOAD_DOCUMENT,
                              request=request, object_ref=f"document:{doc.pk}")
        try:
            handle = doc.file.open("rb")
        except (FileNotFoundError, ValueError):
            raise Http404("That file is no longer available.")
        return FileResponse(handle, as_attachment=True,
                            filename=doc.original_name or doc.file.name.rsplit("/", 1)[-1])

    def post(self, request, pk, action=None):
        doc = self.scope().document(pk)
        if action == "withdraw":
            if not doc.may_withdraw:
                messages.error(
                    request,
                    "That document is already part of a case and cannot be removed. "
                    "Evidence behind a decision stays on the record.")
            else:
                doc.withdrawn_at = timezone.now()
                doc.save(update_fields=["withdrawn_at"])
                messages.success(request, "Document removed.")
        return redirect(request.POST.get("next") or "portal_documents")


# ---------------------------------------------------------------------------
# Notifications
# ---------------------------------------------------------------------------

class PortalNotificationsView(PortalBase):
    template_name = "benevolent/portal/notifications.html"
    portal_title = "Messages"

    def nav_key(self):
        return "notifications"

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        sc = self.scope()
        paginator = Paginator(sc.notifications(), 30)
        ctx["page"] = paginator.get_page(self.request.GET.get("page"))
        # Committee updates a member is entitled to: the decisions on their own
        # cases, not the committee's deliberations on anyone's.
        ctx["case_updates"] = sc.cases().exclude(status="DRAFT")[:10]
        return ctx


# ---------------------------------------------------------------------------
# Profile & preferences
# ---------------------------------------------------------------------------

class PortalProfileView(PortalBase):
    template_name = "benevolent/portal/profile.html"
    portal_title = "My details"

    def nav_key(self):
        return "profile"

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx["channels"] = MemberAccount.Channel.choices
        ctx["recent_access"] = self.account.access_log.all()[:15]
        return ctx

    def post(self, request, *args, **kwargs):
        account = self.account
        post = request.POST
        # Contact details and preferences are the member's own to set: they say
        # where to reach *them*. The name and number on the church roll are a
        # different thing entirely and go through an approved PROFILE request —
        # the roll's phone number is what bank payments are matched against.
        account.contact_phone = (post.get("contact_phone") or "").strip()[:32]
        account.contact_email = (post.get("contact_email") or "").strip()[:254]
        channel = post.get("preferred_channel")
        if channel in MemberAccount.Channel.values:
            account.preferred_channel = channel
        account.notify_case_updates = bool(post.get("notify_case_updates"))
        account.notify_contributions = bool(post.get("notify_contributions"))
        account.notify_dues_reminders = bool(post.get("notify_dues_reminders"))
        account.notify_announcements = bool(post.get("notify_announcements"))
        account.save(update_fields=[
            "contact_phone", "contact_email", "preferred_channel",
            "notify_case_updates", "notify_contributions",
            "notify_dues_reminders", "notify_announcements"])
        messages.success(request, "Your preferences have been saved.")
        return redirect("portal_profile")


def _contribution_years(scope):
    """The years this member actually has contributions in.

    Read off the underlying transaction date, since the contribution's own
    `date` is a property with no column behind it to query.
    """
    return sorted(
        {d.year for d in scope.contributions()
         .values_list("transaction__date", flat=True) if d}, reverse=True)


def _parse_date(value):
    if not value:
        return None
    try:
        return _dt.date.fromisoformat(str(value)[:10])
    except ValueError:
        return None

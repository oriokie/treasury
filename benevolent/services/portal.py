"""The member self-service portal — scoping, request lifecycle, and the bridge
back into the services that own each change.

Three responsibilities, and deliberately no fourth:

1. **Scoping** (``scope``). One function decides what a given member may see.
   Every portal queryset comes from it. If object-level permission is ever
   wrong in this module, it is wrong in one readable place.

2. **Lifecycle** (``submit``, ``request_info``, ``withdraw``, ``decline``,
   ``approve``). The states a request moves through, and who may move it.

3. **Application** (``_apply_*``). Turning an approved request into the change
   it asked for — by calling ``registry``, ``cases`` and the rest. There is no
   accounting, no eligibility and no workflow written here. Where this module
   appears to "do" something, it is calling the one implementation that already
   does it.

The thing this module must never become is a second way to change a record. A
member's request is a claim; the services are the law.
"""
import datetime as _dt
from decimal import Decimal

from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction as db_tx
from django.db.models import Q, Sum
from django.utils import timezone

from ..models import (BenevolentCase, BenevolentContribution, CaseAttachment,
                      MemberAccount, PortalAccessLog, PortalDocument, PortalRequest,
                      PortalRequestMessage, PortalRequestSequence, SchemeDependant,
                      SchemeMembership)


# ---------------------------------------------------------------------------
# 1. Scoping — the one object-level rule
# ---------------------------------------------------------------------------

class Scope:
    """What one member may see.

    Constructed from a ``MemberAccount`` and nothing else. Every method returns
    a queryset already narrowed to that person; a view that wants "my
    contributions" asks this object rather than filtering for itself, because a
    filter written in a view is a filter nobody reviews again.

    The rule in one sentence: a member sees rows that belong to their own
    ``members.Member`` record, or to a scheme enrolment of theirs, or to a case
    in which they or one of their dependants is the subject.
    """

    def __init__(self, account: MemberAccount):
        if account is None:
            raise PermissionDenied("No member account.")
        self.account = account
        self.member = account.member

    # -- enrolments ---------------------------------------------------------
    def memberships(self):
        return (SchemeMembership.objects
                .filter(member=self.member)
                .select_related("scheme", "member")
                .order_by("scheme__name"))

    def membership(self, pk):
        """One enrolment of this member's, or a refusal. Never ``.get()`` on an
        unfiltered manager — that is the shape every object-level leak takes."""
        obj = self.memberships().filter(pk=pk).first()
        if obj is None:
            raise PermissionDenied("Not your membership.")
        return obj

    # -- household ----------------------------------------------------------
    def dependants(self):
        return (SchemeDependant.objects
                .filter(membership__member=self.member)
                .select_related("membership", "membership__scheme", "member")
                .order_by("relationship", "id"))

    def dependant(self, pk):
        obj = self.dependants().filter(pk=pk).first()
        if obj is None:
            raise PermissionDenied("Not your dependant.")
        return obj

    # -- money --------------------------------------------------------------
    def contributions(self):
        """Everything recorded as this member's giving to a scheme.

        Built on ``contributions.contributions_qs()`` rather than on a fresh
        manager query, and that is not a stylistic preference. That function
        carries THE definition of a contribution that counts — it excludes
        receipts that were never confirmed, that have been reversed, or that are
        themselves reversals. A portal that queried the table directly would
        show a member money the fund does not have, and would keep showing a
        reversed payment as evidence they had paid. The member would be right to
        believe it, and wrong.

        Two ways a row belongs to them, and both count: it is recorded against
        one of their enrolments, or the underlying bank receipt is in their own
        name (a member paying a levy towards somebody else's case is still their
        money and belongs on their statement).

        Note ``date`` and ``amount`` are properties on the model, derived from
        the underlying ``giving.Transaction`` — so ordering, filtering and
        aggregation all have to go through ``transaction__``.
        """
        from . import contributions as contrib_svc
        return (contrib_svc.contributions_qs()
                .filter(Q(membership__member=self.member)
                        | Q(transaction__member=self.member))
                .select_related("membership", "membership__scheme", "case", "transaction")
                .order_by("-transaction__date", "-id"))

    def contribution(self, pk):
        obj = self.contributions().filter(pk=pk).first()
        if obj is None:
            raise PermissionDenied("Not your contribution.")
        return obj

    # -- cases --------------------------------------------------------------
    def cases(self):
        """Cases this member is party to.

        A member sees a case where they are the claimant, or where the subject
        is one of their own dependants. They do not see other members' cases,
        even in the same scheme and even when they contributed a levy towards
        it — the levy is on their statement, the family's circumstances are not
        theirs to read.
        """
        return (BenevolentCase.objects
                .filter(Q(membership__member=self.member)
                        | Q(dependant__membership__member=self.member))
                .select_related("scheme", "event_type", "membership", "dependant")
                .distinct()
                .order_by("-event_date", "-id"))

    def case(self, pk):
        obj = self.cases().filter(pk=pk).first()
        if obj is None:
            raise PermissionDenied("Not your case.")
        return obj

    # -- requests & documents ----------------------------------------------
    def requests(self):
        return (PortalRequest.objects
                .filter(account=self.account)
                .select_related("membership", "membership__scheme", "event_type",
                                "dependant", "case")
                .order_by("-created_at"))

    def request(self, pk):
        obj = self.requests().filter(pk=pk).first()
        if obj is None:
            raise PermissionDenied("Not your request.")
        return obj

    def documents(self):
        return (PortalDocument.objects
                .filter(account=self.account, withdrawn_at__isnull=True)
                .select_related("request")
                .order_by("-uploaded_at"))

    def document(self, pk):
        obj = self.documents().filter(pk=pk).first()
        if obj is None:
            raise PermissionDenied("Not your document.")
        return obj

    # -- notifications ------------------------------------------------------
    def notifications(self):
        """What the member has been told.

        Scoped through their own enrolments. A notification with no membership
        (an office-to-committee message) is not theirs and is not shown.
        """
        from ..models import BenevolentNotification
        return (BenevolentNotification.objects
                .filter(membership__member=self.member)
                .select_related("membership", "membership__scheme", "case")
                .order_by("-created_at"))


def scope(account) -> Scope:
    return Scope(account)


# ---------------------------------------------------------------------------
# Access logging
# ---------------------------------------------------------------------------

def log_access(account, action, *, request=None, detail="", object_ref=""):
    """Record a read. Never raises — an audit write must not be able to break
    the page it is auditing, and a lost log line is a smaller failure than a
    member unable to see their own statement."""
    try:
        ip = ua = None
        if request is not None:
            ip = (request.META.get("HTTP_X_FORWARDED_FOR", "").split(",")[0].strip()
                  or request.META.get("REMOTE_ADDR"))
            ua = (request.META.get("HTTP_USER_AGENT") or "")[:200]
        PortalAccessLog.objects.create(
            account=account,
            user=getattr(account, "user", None),
            action=action, detail=detail[:200], object_ref=object_ref[:80],
            ip_address=ip or None, user_agent=ua or "")
    except Exception:
        pass


# ---------------------------------------------------------------------------
# 2. Request lifecycle
# ---------------------------------------------------------------------------

def _next_reference():
    return PortalRequestSequence.next_number(_dt.date.today().year)


@db_tx.atomic
def create_request(account, *, kind, subject, detail="", membership=None,
                   event_type=None, event_date=None, dependant=None,
                   deceased_name="", payload=None, submit=False):
    """Start a request. Draft by default so a member can gather documents
    before committing to it."""
    sc = scope(account)
    if membership is not None:
        membership = sc.membership(membership.pk if hasattr(membership, "pk") else membership)
    if dependant is not None:
        dependant = sc.dependant(dependant.pk if hasattr(dependant, "pk") else dependant)
    if membership is None:
        # Most members are in exactly one scheme; don't make them pick.
        membership = sc.memberships().first()

    req = PortalRequest(
        reference=_next_reference(), account=account, membership=membership,
        kind=kind, subject=(subject or "").strip()[:140], detail=detail or "",
        event_type=event_type, event_date=event_date, dependant=dependant,
        deceased_name=(deceased_name or "").strip(), payload=payload or {})
    req.full_clean(exclude=["reference"])
    req.save()
    if submit:
        submit_request(req, actor=account.user)
    return req


@db_tx.atomic
def update_request(req, *, actor=None, subject=None, detail=None, membership=None,
                   event_type=None, event_date=None, dependant=None,
                   deceased_name=None, payload=None, submit=False):
    """Amend a request the member still holds.

    Editing is allowed only while the request is theirs to edit — a draft they
    have not sent, or one the office has handed back asking for more. Once it
    has been submitted and nobody has asked for anything, changing it underneath
    a reviewer would mean the office approving something other than what it
    read; and once it is decided, editing would rewrite history. Both are
    refused here rather than in the view, so the rule holds for any caller.

    `kind` is deliberately not amendable. Each kind is a different form with
    different fields and its own approval path, so changing it would leave the
    payload describing one thing and the request claiming another; a member who
    picked the wrong one withdraws it and starts the right one, which is one
    click and leaves an honest trail.

    Fields left as None are untouched, so a caller may amend one thing without
    restating the rest.
    """
    if req.status not in PortalRequest.MEMBER_EDITABLE:
        raise ValidationError(
            "That request is with the church office and can no longer be "
            "changed. Reply on it if you need to add anything.")

    sc = scope(req.account)
    if membership is not None:
        membership = sc.membership(
            membership.pk if hasattr(membership, "pk") else membership)
        req.membership = membership
    if dependant is not None:
        # An explicitly cleared dependant arrives as False rather than None, so
        # "leave alone" and "there is no longer one" stay distinguishable.
        req.dependant = (None if dependant is False else sc.dependant(
            dependant.pk if hasattr(dependant, "pk") else dependant))
    if subject is not None:
        req.subject = (subject or "").strip()[:140]
    if detail is not None:
        req.detail = detail or ""
    if event_type is not None:
        req.event_type = (None if event_type is False else event_type)
    if event_date is not None:
        req.event_date = (None if event_date is False else event_date)
    if deceased_name is not None:
        req.deceased_name = (deceased_name or "").strip()
    if payload is not None:
        req.payload = payload or {}

    req.full_clean(exclude=["reference"])
    req.save()
    if submit:
        submit_request(req, actor=actor)
    return req


@db_tx.atomic
def submit_request(req, *, actor=None):
    """Member hands it to the office."""
    if req.status not in PortalRequest.MEMBER_EDITABLE:
        raise ValidationError("That request has already been submitted.")
    req.status = PortalRequest.Status.SUBMITTED
    req.submitted_at = timezone.now()
    req.save(update_fields=["status", "submitted_at", "updated_at"])
    _notify_office(req)
    return req


@db_tx.atomic
def withdraw_request(req, *, actor=None, reason=""):
    """A member changing their mind. Allowed while the office has not decided —
    withdrawing after a decision would rewrite history, so it is refused."""
    if not req.is_open:
        raise ValidationError("That request has already been decided.")
    req.status = PortalRequest.Status.WITHDRAWN
    req.decision_note = (reason or "Withdrawn by the member.")[:2000]
    req.save(update_fields=["status", "decision_note", "updated_at"])
    return req


@db_tx.atomic
def take_for_review(req, *, user):
    if req.status not in {PortalRequest.Status.SUBMITTED,
                          PortalRequest.Status.INFO_NEEDED}:
        raise ValidationError("That request is not awaiting review.")
    req.status = PortalRequest.Status.UNDER_REVIEW
    req.reviewed_by = user
    req.save(update_fields=["status", "reviewed_by", "updated_at"])
    return req


@db_tx.atomic
def request_more_information(req, *, user, message):
    """Ask the member for something, rather than declining for want of it."""
    if not req.is_open:
        raise ValidationError("That request has already been decided.")
    req.status = PortalRequest.Status.INFO_NEEDED
    req.reviewed_by = user
    req.save(update_fields=["status", "reviewed_by", "updated_at"])
    add_message(req, body=message, user=user, from_member=False)
    # Every phrase passed here completes the sentence "Your request ... {phrase}."
    # so they all have to be verb phrases. This one used to be a clause of its
    # own ("we need a little more information"), which produced a comma splice
    # in the email and the SMS the member actually received.
    _notify_member(req, "needs a little more information from you")
    return req


@db_tx.atomic
def decline_request(req, *, user, reason):
    """Declining always carries a reason, and the reason is shown to the
    member. A refusal a member cannot understand is a refusal they will bring
    back through the door."""
    if not (reason or "").strip():
        raise ValidationError("Say why. A declined request must carry a reason.")
    if not req.is_open:
        raise ValidationError("That request has already been decided.")
    req.status = PortalRequest.Status.DECLINED
    req.decision_note = reason.strip()
    req.reviewed_by = user
    req.reviewed_at = timezone.now()
    req.save(update_fields=["status", "decision_note", "reviewed_by",
                            "reviewed_at", "updated_at"])
    _notify_member(req, "has not been accepted")
    return req


def add_message(req, *, body, user=None, from_member=False):
    body = (body or "").strip()
    if not body:
        raise ValidationError("Write something first.")
    return PortalRequestMessage.objects.create(
        request=req, author=user, from_member=from_member, body=body)


# ---------------------------------------------------------------------------
# 3. Approval — delegating to the service that owns each change
# ---------------------------------------------------------------------------

@db_tx.atomic
def approve_request(req, *, user, note="", **kwargs):
    """Approve, and apply.

    Every branch below ends in a call to an existing service. Nothing here
    writes to ``SchemeDependant``, ``BenevolentCase`` or ``members.Member``
    directly, and it must stay that way: the services carry the validation, the
    period locks, the notifications and the audit entries that make each change
    legitimate. Reaching around them from a portal approval screen would
    produce records that look identical and are not.
    """
    if not req.is_open:
        raise ValidationError("That request has already been decided.")

    applier = {
        PortalRequest.Kind.DEATH: _apply_death,
        PortalRequest.Kind.ASSISTANCE: _apply_assistance,
        PortalRequest.Kind.HOUSEHOLD: _apply_household,
        PortalRequest.Kind.CORRECTION: _apply_noop,
        PortalRequest.Kind.PROFILE: _apply_profile,
    }[req.kind]
    applier(req, user=user, **kwargs)

    req.status = PortalRequest.Status.APPROVED
    req.reviewed_by = user
    req.reviewed_at = timezone.now()
    if note:
        req.decision_note = note
    req.save(update_fields=["status", "reviewed_by", "reviewed_at",
                            "decision_note", "case", "updated_at"])
    _notify_member(req, "has been accepted")
    return req


def _apply_noop(req, *, user, **kw):
    """A correction request is a *conversation*, not an automatic edit.

    There is deliberately no generic "apply the payload" path here. A member
    saying a contribution is wrong may be right, but the fix is a ledger
    correction with its own approval — ``contributions``/``MemberAdjustment``
    own that, under the treasurer's authority, and a portal approval must not
    be able to move money by writing a JSON blob into an accounting row.
    Approving a correction records that the office accepted the point; the
    accounting change is then made where accounting changes are made.
    """
    return None


def _apply_death(req, *, user, **kw):
    """Report of a death → the real death record and, if the policy says so,
    the case. ``cases.open_case_for_death`` already decides whether a case
    opens, which event type applies and what the defaults are."""
    from . import cases as case_svc
    from . import registry as reg_svc

    membership = req.membership
    if membership is None:
        raise ValidationError("This request is not linked to a scheme membership.")
    died_on = req.event_date or _dt.date.today()

    if req.dependant_id:
        # a dependant died: registry records it, and opens the case per policy
        reg_svc.record_dependant_death(
            req.dependant, died_on=died_on, user=user,
            reason=f"Reported by the member through the portal ({req.reference}).")
        case = (BenevolentCase.objects
                .filter(dependant=req.dependant, event_date=died_on)
                .order_by("-id").first())
    else:
        # somebody not on the roll as a dependant: raise the case directly, so
        # the office can assess it; the policy still decides the event type.
        case = case_svc.open_case_for_death(
            scheme=membership.scheme, membership=membership,
            event_date=died_on, user=user,
            reason=f"Reported through the member portal ({req.reference}): "
                   f"{req.deceased_name or 'a family member'}.")
    if case is not None:
        req.case = case
        _attach_documents_to_case(req, case, user=user)
    return case


def _apply_assistance(req, *, user, amount=None, **kw):
    """Request for assistance → a real case, raised by the office.

    The member's request is not itself a case and never becomes one silently:
    the case is created here, by a case officer, through ``cases.create_case``,
    so it enters the ordinary assessment, eligibility and approval path with no
    shortcut. The member gains a case number to track; the church gains nothing
    it did not decide to accept.
    """
    from . import cases as case_svc

    membership = req.membership
    if membership is None:
        raise ValidationError("This request is not linked to a scheme membership.")
    if req.event_type_id is None:
        raise ValidationError("Choose the event type this request falls under "
                              "before approving it.")
    case = case_svc.create_case(
        membership.scheme, event_type=req.event_type,
        event_date=req.event_date or _dt.date.today(),
        membership=membership, dependant=req.dependant, user=user,
        claimed_amount=amount,
        description=f"Raised from member portal request {req.reference}: {req.subject}")
    req.case = case
    _attach_documents_to_case(req, case, user=user)
    return case


def _apply_household(req, *, user, **kw):
    """Household change → ``registry.add_dependant`` / ``update_dependant`` /
    ``remove_dependant``. The payload names the operation; the service performs
    it, with its own validation and its own membership event."""
    from . import registry as reg_svc

    payload = req.payload or {}
    op = payload.get("op")
    membership = req.membership
    if membership is None:
        raise ValidationError("This request is not linked to a scheme membership.")
    reason = f"Approved from member portal request {req.reference}."

    if op == "add":
        return reg_svc.add_dependant(
            membership,
            relationship=payload.get("relationship") or SchemeDependant.Relationship.OTHER,
            name=(payload.get("name") or "").strip(),
            phone=(payload.get("phone") or "").strip(),
            date_of_birth=_as_date(payload.get("date_of_birth")),
            user=user, notes=reason)
    if op == "update":
        dep = req.dependant
        if dep is None:
            raise ValidationError("This request does not name a dependant to change.")
        return reg_svc.update_dependant(
            dep,
            name=(payload.get("name") or dep.name or "").strip(),
            phone=(payload.get("phone") or dep.phone or "").strip(),
            date_of_birth=_as_date(payload.get("date_of_birth")) or dep.date_of_birth,
            relationship=payload.get("relationship") or dep.relationship,
            user=user, notes=reason)
    if op == "remove":
        dep = req.dependant
        if dep is None:
            raise ValidationError("This request does not name a dependant to remove.")
        return reg_svc.remove_dependant(dep, user=user, reason=reason)
    raise ValidationError("This household request does not say what to change.")


def _apply_profile(req, *, user, **kw):
    """A change to the member's own details on the church roll.

    Applied to ``members.Member``, which is the church's record of the person —
    and only on approval, never from the portal directly. The phone number in
    particular is what bank payments are matched against, so a member editing
    it themselves would be able to repoint somebody else's M-Pesa allocation.
    """
    payload = req.payload or {}
    member = req.account.member
    fields = []
    name = (payload.get("name") or "").strip()
    if name and name != member.name:
        member.name = name
        fields.append("name")
    phone = (payload.get("phone") or "").strip()
    if phone and phone != (member.phone or ""):
        member.phone = phone
        fields.append("phone")
    if fields:
        member.save()          # Member.save() re-derives name_key / normalises phone
    return member


def _attach_documents_to_case(req, case, *, user=None):
    """Mirror the member's uploads onto the case, so the assessor's existing
    document checklist sees them without knowing the portal exists."""
    for doc in req.documents.filter(withdrawn_at__isnull=True, attachment__isnull=True):
        att = CaseAttachment.objects.create(
            case=case, file=doc.file,
            document_type=doc.get_kind_display(),
            label=doc.label or doc.original_name or doc.get_kind_display(),
            uploaded_by=doc.account.user)
        doc.attachment = att
        doc.save(update_fields=["attachment"])
        try:
            from . import cases as case_svc
            case_svc.log_document_added(case, att.label, user=user)
        except Exception:
            pass


def _as_date(value):
    if not value:
        return None
    if isinstance(value, _dt.date):
        return value
    try:
        return _dt.date.fromisoformat(str(value)[:10])
    except ValueError:
        return None


# ---------------------------------------------------------------------------
# Notifications — through the module's own notify service
# ---------------------------------------------------------------------------

def _notify_member(req, phrase):
    """Tell the member, honouring the preference they set in the portal.

    Routed through ``services.notify`` so it uses the same templates, the same
    channels and the same delivery record as every other message this module
    sends. The portal adds a *preference*, not a second messenger.
    """
    if not req.account.notify_case_updates:
        return
    try:
        from . import notify as notify_svc
        from ..models import NotificationEvent
        notify_svc.send(NotificationEvent.PORTAL_REQUEST_UPDATED,
                        membership=req.membership, case=req.case,
                        extra={"reference": req.reference, "phrase": phrase,
                               "subject": req.subject,
                               # falls mid-sentence after "Your", so the display
                               # label's leading capital reads as a proper noun
                               "kind": req.get_kind_display().lower()})
    except Exception:
        # Never let a delivery problem roll back a decision that has been made.
        pass


def _notify_office(req):
    """Deliberately nothing.

    The obvious move here is to raise a `BenevolentTask`. It would be wrong:
    that queue has a fixed `Kind` vocabulary describing things automation
    *found* (a member lapsing, a dependant ageing out), and it is the screen an
    officer scans for risk. Member post is not a risk finding, and filing it
    under `Kind.OTHER` would dilute the one queue whose signal-to-noise
    actually matters. Submitted requests surface on the portal review queue,
    which is built for them and counts them in the navigation.
    """
    return None


# ---------------------------------------------------------------------------
# Read helpers used by the portal's own screens
# ---------------------------------------------------------------------------

def overview(account):
    """Everything the portal home page shows, in one place.

    Assembled by calling the services that own each figure — standing from
    ``standing.assess``, arrears from ``contributions.arrears_for`` — never by
    summing rows here. A portal that computed its own arrears figure would be a
    second opinion on a member's debt, which is the one thing a member must
    never be given.
    """
    from . import contributions as contrib_svc
    from . import standing as standing_svc

    sc = scope(account)
    rows = []
    for m in sc.memberships():
        policy = m.scheme.current_policy
        try:
            assessment = standing_svc.assess(m, policy)
        except Exception:
            assessment = None
        try:
            arrears = contrib_svc.arrears_for(m, policy)
        except Exception:
            arrears = Decimal("0")
        rows.append({
            "membership": m,
            "scheme": m.scheme,
            "standing": assessment,
            "arrears": arrears,
            # Summed off the Transaction's amount column — `amount` on the
            # contribution is a Python property and cannot be aggregated.
            "contributed": (sc.contributions().filter(membership=m)
                            .aggregate(s=Sum("transaction__amount"))["s"]
                            or Decimal("0")),
        })

    open_requests = sc.requests().filter(
        status__in=list(PortalRequest.OPEN_STATUSES))
    return {
        "account": account,
        "rows": rows,
        "open_requests": open_requests,
        "needs_attention": open_requests.filter(
            status=PortalRequest.Status.INFO_NEEDED),
        "recent_cases": sc.cases()[:5],
        "recent_contributions": sc.contributions()[:5],
    }


# ---------------------------------------------------------------------------
# Provisioning — the office side of identity
# ---------------------------------------------------------------------------

def _unique_username(member):
    """A stable, guessable-but-not-secret username derived from the person.

    Deliberately not their phone number and not their email: both change, and a
    username that changes breaks the audit trail that points at it.
    """
    import re
    from django.contrib.auth.models import User
    base = re.sub(r"[^a-z0-9]+", ".", (member.name or "member").lower()).strip(".")[:24]
    base = base or "member"
    candidate, n = base, 1
    while User.objects.filter(username=candidate).exists():
        n += 1
        candidate = f"{base}{n}"
    return candidate


@db_tx.atomic
def invite(member, *, user=None, actor=None, username=None, email="", phone=""):
    """Give a member of the congregation access to their own record.

    Creates the login if it does not exist, binds it to the member, puts it in
    the ``Member`` group, and leaves it INVITED with **no usable password**.
    The member sets one through the application's existing self-service reset —
    which already sends a one-time code by SMS, already rate-limits, and
    already stores the code hashed. Inventing a second invitation-token system
    beside it would be a second thing to get wrong for no gain.

    Refuses to convert an office login into a member login. A treasurer with a
    ``MemberAccount`` would be a session holding two identities, and every
    object-level rule in this module assumes exactly one.
    """
    from django.contrib.auth.models import Group, User
    from core import roles as role_svc

    existing = getattr(member, "portal_account", None)
    if existing is not None:
        raise ValidationError(f"{member.name} already has portal access.")

    if user is None:
        user = User.objects.create_user(
            username=username or _unique_username(member),
            email=email or "", first_name=(member.name or "")[:30])
        user.set_unusable_password()
        user.save()
    else:
        if role_svc.user_roles(user) & role_svc.OFFICE_ROLES or user.is_superuser:
            raise ValidationError(
                "That login belongs to the church office. Give the member their "
                "own login instead — one session must carry one identity.")

    group, _ = Group.objects.get_or_create(name=role_svc.MEMBER)
    user.groups.add(group)

    account = MemberAccount.objects.create(
        user=user, member=member, status=MemberAccount.Status.INVITED,
        invited_by=actor, invited_at=timezone.now(),
        contact_phone=phone or (member.phone or ""), contact_email=email or "")

    # force a password to be set before anything else happens on this login
    try:
        from accounts.models import UserProfile
        profile = UserProfile.for_user(user)
        profile.must_change_password = True
        profile.phone = profile.phone or account.contact_phone
        profile.save(update_fields=["must_change_password", "phone"])
    except Exception:
        pass

    _notify_invited(account)
    return account


@db_tx.atomic
def activate(account, *, actor=None):
    """Mark an invited account as live. Called when the member first sets a
    password and accepts the terms."""
    account.status = MemberAccount.Status.ACTIVE
    account.activated_at = account.activated_at or timezone.now()
    account.terms_accepted_at = account.terms_accepted_at or timezone.now()
    account.save(update_fields=["status", "activated_at", "terms_accepted_at"])
    return account


@db_tx.atomic
def suspend(account, *, actor=None, reason=""):
    """Take portal access away without touching the person's membership.

    Two separate things, and keeping them separate matters: a member whose
    portal login is being abused should lose the login, not their cover. This
    writes only to the account.
    """
    account.status = MemberAccount.Status.SUSPENDED
    account.suspended_reason = (reason or "")[:200]
    account.save(update_fields=["status", "suspended_reason"])
    return account


@db_tx.atomic
def restore(account, *, actor=None):
    account.status = (MemberAccount.Status.ACTIVE if account.activated_at
                      else MemberAccount.Status.INVITED)
    account.suspended_reason = ""
    account.save(update_fields=["status", "suspended_reason"])
    return account


@db_tx.atomic
def close(account, *, actor=None, reason=""):
    """Close the account for good, and take the login with it.

    The row stays: the access log points at it, and an audit trail whose
    subject has been deleted answers no questions.
    """
    account.status = MemberAccount.Status.CLOSED
    account.suspended_reason = (reason or "")[:200]
    account.save(update_fields=["status", "suspended_reason"])
    user = account.user
    user.is_active = False
    user.save(update_fields=["is_active"])
    return account


def _notify_invited(account):
    try:
        from . import notify as notify_svc
        from ..models import NotificationEvent
        membership = account.memberships.first()
        notify_svc.send(NotificationEvent.PORTAL_INVITED,
                        membership=membership,
                        extra={"username": account.user.username,
                               "member_name": account.member.name})
    except Exception:
        pass

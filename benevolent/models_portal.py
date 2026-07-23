"""The Member Self-Service Portal — identity, requests, documents, access log.

Why these four models and nothing more
--------------------------------------
The portal is a *surface*, not a second system. Every figure it shows and every
decision it produces already has an owner elsewhere in this module:

  * what a member has paid      -> ``services.contributions``
  * where a member stands       -> ``services.standing.assess``
  * whether a claim would pay   -> ``services.eligibility.evaluate``
  * enrolling, household, death -> ``services.registry``
  * raising and running a case  -> ``services.cases``
  * telling anybody anything    -> ``services.notify``

So the portal adds no accounting, no eligibility logic and no workflow of its
own. What it genuinely needs, and what does not exist yet, is exactly four
things:

``MemberAccount``
    The missing link. Nothing in this application joins an ``auth.User`` to a
    ``members.Member``, because until now every login belonged to the church
    office. A self-service portal is meaningless without that binding: it is
    what turns "this request is authenticated" into "this request may see
    *these* rows and no others". Object-level permission in this module is
    derived from this one row and nowhere else.

``PortalRequest``
    A member asking for something that changes the record. Deliberately NOT the
    thing itself. A member cannot create a ``BenevolentCase`` (a case is a
    liability), cannot edit a ``SchemeDependant`` (that is cover), and cannot
    correct a ``BenevolentContribution`` (that is money already in the ledger).
    They submit a request; a scheme officer reviews it; and on approval the
    change is applied *by calling the existing service* — never by writing to
    the target model from here.

    This mirrors ``BenevolentApplication`` (models_public), which for the same
    reason is a separate model from ``SchemeMembership`` rather than a public
    write path into it. One reviewed request model covers assistance, death
    reports, household changes and corrections because the *shape* is identical
    in all four cases — submitted, reviewed, applied through a service — and
    four near-identical models would be four places to fix the same bug.

``PortalDocument``
    A file a member uploaded. Held separately from ``CaseAttachment`` until it
    is attached to something, because a member uploading a burial permit
    usually does so *before* a case exists. Once a case is raised, the document
    is attached through the ordinary attachment path and the two stay linked.

``PortalAccessLog``
    ``simple_history`` records writes; ``CaseEvent`` and ``MembershipEvent``
    record decisions. Nothing records *reads*, and for a portal the reads are
    the sensitive part: a portal bug that leaks another family's statement is
    invisible to every audit trail this module currently has. This log records
    who viewed or downloaded what, so that question can be answered.
"""
import datetime as _dt

from django.core.exceptions import ValidationError
from django.db import models
from django.utils import timezone

from simple_history.models import HistoricalRecords


def portal_document_path(instance, filename):
    return f"benevolent/portal/{instance.account_id or 'unassigned'}/{filename}"


# ---------------------------------------------------------------------------
# Identity
# ---------------------------------------------------------------------------

class MemberAccount(models.Model):
    """A login that belongs to a member of the congregation, not to the office.

    One person, one account: ``OneToOne`` on both sides. A household does not
    share a login — a shared login would make every audit entry unattributable
    and is exactly the control weakness a portal is supposed to remove. A
    spouse who needs their own access gets their own member record and their
    own account; the household is expressed by the scheme membership, not by
    sharing a password.

    The account is the ONLY authority for what the portal may show. Views never
    filter by ``request.user`` directly; they call
    ``services.portal.scope`` with this account, which is the single place the
    object-level rule lives.
    """

    class Status(models.TextChoices):
        INVITED = "INVITED", "Invited — not yet activated"
        ACTIVE = "ACTIVE", "Active"
        SUSPENDED = "SUSPENDED", "Suspended"
        CLOSED = "CLOSED", "Closed"

    class Channel(models.TextChoices):
        SMS = "SMS", "Text message"
        EMAIL = "EMAIL", "Email"
        NONE = "NONE", "In the portal only"

    user = models.OneToOneField("auth.User", on_delete=models.CASCADE,
                                related_name="member_account")
    member = models.OneToOneField("members.Member", on_delete=models.PROTECT,
                                  related_name="portal_account")

    status = models.CharField(max_length=10, choices=Status.choices,
                              default=Status.INVITED, db_index=True)

    # --- invitation / activation -------------------------------------------
    invited_by = models.ForeignKey("auth.User", null=True, blank=True,
                                   on_delete=models.SET_NULL, related_name="+")
    invited_at = models.DateTimeField(null=True, blank=True)
    activated_at = models.DateTimeField(null=True, blank=True)
    last_seen_at = models.DateTimeField(null=True, blank=True)

    suspended_reason = models.CharField(max_length=200, blank=True, default="")

    # --- what the member may be told, and where ----------------------------
    # Contact details recorded HERE are the member's own statement of where to
    # reach them. They deliberately do not overwrite members.Member.phone,
    # which is the church's record and is used to match bank payments — a
    # member changing their portal contact number must not silently repoint
    # payment matching. A change to the roll goes through a PortalRequest.
    contact_phone = models.CharField(max_length=32, blank=True, default="")
    contact_email = models.EmailField(blank=True, default="")
    preferred_channel = models.CharField(max_length=6, choices=Channel.choices,
                                         default=Channel.SMS)
    notify_case_updates = models.BooleanField(
        default=True, help_text="Progress on my assistance requests and cases.")
    notify_contributions = models.BooleanField(
        default=True, help_text="When a contribution of mine is recorded.")
    notify_dues_reminders = models.BooleanField(
        default=True, help_text="Reminders when a contribution falls due.")
    notify_announcements = models.BooleanField(
        default=True, help_text="General scheme announcements.")

    terms_accepted_at = models.DateTimeField(null=True, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)
    history = HistoricalRecords()

    class Meta:
        ordering = ["member__name"]
        indexes = [models.Index(fields=["status"])]
        verbose_name = "member portal account"

    def __str__(self):
        return f"{self.member.name} ({self.get_status_display()})"

    # -- state ---------------------------------------------------------------
    @property
    def is_usable(self):
        """True only for an account that may actually sign in and use the
        portal. Checked on every request by ``PortalAccessMixin`` — an account
        suspended mid-session loses access on its next click, not at its next
        login, mirroring ``AccountLockMiddleware``."""
        return self.status == self.Status.ACTIVE and self.user.is_active

    @property
    def memberships(self):
        """Every scheme enrolment belonging to this person. A member may be in
        more than one scheme; the portal shows them all."""
        from .models import SchemeMembership
        return (SchemeMembership.objects.filter(member=self.member)
                .select_related("scheme", "member").order_by("scheme__name"))

    def touch(self):
        now = timezone.now()
        # a coarse timestamp: no need to write on every single request
        if not self.last_seen_at or (now - self.last_seen_at).total_seconds() > 300:
            self.last_seen_at = now
            self.save(update_fields=["last_seen_at"])

    def wants(self, kind):
        """Whether this member has asked to be told about `kind`."""
        return bool(getattr(self, f"notify_{kind}", True))


# ---------------------------------------------------------------------------
# Requests
# ---------------------------------------------------------------------------

class PortalRequest(models.Model):
    """Something a member has asked the church to do.

    A request is a *claim*, never a change. It carries the member's own account
    of what they want, and — for the kinds that end in a record change — a
    small JSON payload of proposed values. Approving it calls the existing
    service that owns the change; this model never writes to the target itself.
    That is the whole design: there is exactly one implementation of "add a
    dependant" (``registry.add_dependant``) and the portal is a caller of it,
    not a rival to it.
    """

    class Kind(models.TextChoices):
        ASSISTANCE = "ASSISTANCE", "Request for assistance"
        DEATH = "DEATH", "Report a death"
        HOUSEHOLD = "HOUSEHOLD", "Change household or dependants"
        CORRECTION = "CORRECTION", "Correct a record"
        PROFILE = "PROFILE", "Update my details on the church roll"

    class Status(models.TextChoices):
        DRAFT = "DRAFT", "Draft"
        SUBMITTED = "SUBMITTED", "Submitted — awaiting review"
        INFO_NEEDED = "INFO_NEEDED", "More information needed"
        UNDER_REVIEW = "UNDER_REVIEW", "Under review"
        APPROVED = "APPROVED", "Approved"
        DECLINED = "DECLINED", "Declined"
        WITHDRAWN = "WITHDRAWN", "Withdrawn by member"

    # The statuses a member may still edit or withdraw from. Anything else is
    # in the church's hands and is read-only to them.
    MEMBER_EDITABLE = {Status.DRAFT, Status.INFO_NEEDED}
    OPEN_STATUSES = {Status.DRAFT, Status.SUBMITTED, Status.INFO_NEEDED,
                     Status.UNDER_REVIEW}

    reference = models.CharField(max_length=24, unique=True, db_index=True,
                                 help_text="Human reference the member quotes, e.g. REQ-2026-0031.")
    account = models.ForeignKey(MemberAccount, on_delete=models.PROTECT,
                                related_name="requests")
    membership = models.ForeignKey(
        "SchemeMembership", null=True, blank=True, on_delete=models.SET_NULL,
        related_name="portal_requests",
        help_text="Which enrolment this concerns, where the member is in more than one scheme.")
    kind = models.CharField(max_length=12, choices=Kind.choices, db_index=True)
    status = models.CharField(max_length=12, choices=Status.choices,
                              default=Status.DRAFT, db_index=True)

    subject = models.CharField(max_length=140)
    detail = models.TextField(
        blank=True, help_text="The member's own account, in their own words.")

    # --- kind-specific facts ------------------------------------------------
    # Structured where the workflow needs to read them, free-form where it does
    # not. `payload` holds the proposed values for HOUSEHOLD/PROFILE/CORRECTION
    # requests; it is never applied directly — see services.portal.approve.
    event_type = models.ForeignKey(
        "BenevolentEventType", null=True, blank=True, on_delete=models.SET_NULL,
        related_name="portal_requests",
        help_text="For an assistance request or a death report: what happened.")
    event_date = models.DateField(null=True, blank=True)
    dependant = models.ForeignKey(
        "SchemeDependant", null=True, blank=True, on_delete=models.SET_NULL,
        related_name="portal_requests",
        help_text="For a death report or household change about a dependant.")
    deceased_name = models.CharField(
        max_length=120, blank=True, default="",
        help_text="Where the person who died is not on the roll as a dependant.")
    payload = models.JSONField(
        default=dict, blank=True,
        help_text="Proposed values. Reviewed by a human and applied through the "
                  "owning service — never written to the target model from here.")

    # --- review -------------------------------------------------------------
    submitted_at = models.DateTimeField(null=True, blank=True, db_index=True)
    reviewed_by = models.ForeignKey("auth.User", null=True, blank=True,
                                    on_delete=models.SET_NULL, related_name="+")
    reviewed_at = models.DateTimeField(null=True, blank=True)
    decision_note = models.TextField(
        blank=True, default="",
        help_text="Shown to the member. A declined request always carries a reason.")
    internal_note = models.TextField(
        blank=True, default="", help_text="Office notes. Never shown to the member.")

    # What the request actually became, once approved. The paper trail runs
    # from "a member typed this on their phone" to "and this is the case it
    # opened", the same way an application links to the membership it became.
    case = models.ForeignKey("BenevolentCase", null=True, blank=True,
                             on_delete=models.SET_NULL, related_name="portal_requests")

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    history = HistoricalRecords()

    class Meta:
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["status", "-created_at"]),
            models.Index(fields=["kind", "status"]),
            models.Index(fields=["account", "-created_at"]),
        ]

    def __str__(self):
        return f"{self.reference} — {self.get_kind_display()} [{self.status}]"

    # -- state ---------------------------------------------------------------
    @property
    def is_open(self):
        return self.status in self.OPEN_STATUSES

    @property
    def member_may_edit(self):
        return self.status in self.MEMBER_EDITABLE

    @property
    def awaiting_office(self):
        return self.status in {self.Status.SUBMITTED, self.Status.UNDER_REVIEW}

    def clean(self):
        # A death report must say who died — either a dependant on the roll or
        # a name. Validated here rather than only in the form so the rule holds
        # for the API and any future import path too.
        if self.kind == self.Kind.DEATH and not (self.dependant_id
                                                 or self.deceased_name.strip()):
            raise ValidationError(
                {"deceased_name": "Say who has died — choose a dependant or type a name."})
        if self.kind in {self.Kind.ASSISTANCE, self.Kind.DEATH} and self.event_date:
            if self.event_date > _dt.date.today():
                raise ValidationError({"event_date": "That date is in the future."})


class PortalRequestMessage(models.Model):
    """The conversation on a request.

    A request that can only be approved or declined forces an officer to guess
    what the member meant. This is the "we need the burial permit" / "here it
    is" exchange, kept on the request so it survives the officer who had it.
    """
    request = models.ForeignKey(PortalRequest, on_delete=models.CASCADE,
                                related_name="messages")
    author = models.ForeignKey("auth.User", null=True, blank=True,
                               on_delete=models.SET_NULL, related_name="+")
    from_member = models.BooleanField(
        default=False, help_text="True when the member wrote it, False for the office.")
    body = models.TextField()
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["created_at"]

    def __str__(self):
        who = "member" if self.from_member else "office"
        return f"{self.request.reference} ({who})"


# ---------------------------------------------------------------------------
# Documents
# ---------------------------------------------------------------------------

class PortalDocument(models.Model):
    """A file the member uploaded.

    Kept out of ``CaseAttachment`` deliberately. A member photographs a burial
    permit on the day, which is normally *before* anyone has raised a case;
    forcing the upload to belong to a case would mean either refusing the
    upload or creating an empty case to hold it. So a document belongs to the
    member, optionally to a request, and — once a case exists — is mirrored
    into ``CaseAttachment`` so the assessor's existing checklist screen sees it
    without knowing the portal exists.
    """

    class Kind(models.TextChoices):
        ID = "ID", "Identification"
        BURIAL_PERMIT = "BURIAL_PERMIT", "Burial permit"
        DEATH_CERT = "DEATH_CERT", "Death certificate"
        MEDICAL = "MEDICAL", "Medical report or invoice"
        RECEIPT = "RECEIPT", "Receipt or proof of payment"
        LETTER = "LETTER", "Letter"
        PHOTO = "PHOTO", "Photograph"
        OTHER = "OTHER", "Other"

    account = models.ForeignKey(MemberAccount, on_delete=models.PROTECT,
                                related_name="documents")
    request = models.ForeignKey(PortalRequest, null=True, blank=True,
                                on_delete=models.SET_NULL, related_name="documents")
    kind = models.CharField(max_length=14, choices=Kind.choices, default=Kind.OTHER)
    label = models.CharField(max_length=140, blank=True, default="")
    file = models.FileField(upload_to=portal_document_path)
    original_name = models.CharField(max_length=200, blank=True, default="")
    content_type = models.CharField(max_length=100, blank=True, default="")
    size_bytes = models.PositiveIntegerField(default=0)

    # The attachment this became once a case existed. Set by the service, so a
    # document is never counted twice on the case's document checklist.
    attachment = models.OneToOneField(
        "CaseAttachment", null=True, blank=True, on_delete=models.SET_NULL,
        related_name="portal_document")

    withdrawn_at = models.DateTimeField(
        null=True, blank=True,
        help_text="A member may withdraw a document they uploaded in error. It is "
                  "hidden, never deleted — evidence attached to a decided case is "
                  "part of that decision's record.")
    uploaded_at = models.DateTimeField(auto_now_add=True)
    history = HistoricalRecords()

    class Meta:
        ordering = ["-uploaded_at"]
        indexes = [models.Index(fields=["account", "-uploaded_at"])]

    def __str__(self):
        return self.label or self.original_name or self.file.name

    @property
    def is_live(self):
        return self.withdrawn_at is None

    @property
    def may_withdraw(self):
        """Only while nothing has relied on it. Once it is evidence on a case,
        it stays."""
        if self.withdrawn_at or self.attachment_id:
            return False
        return self.request is None or self.request.member_may_edit


# ---------------------------------------------------------------------------
# Access log
# ---------------------------------------------------------------------------

class PortalAccessLog(models.Model):
    """Who looked at what.

    Deliberately a read log. Writes are already covered three times over
    (``simple_history`` on the models, ``MembershipEvent`` for registry
    decisions, ``CaseEvent`` for case decisions). Reads are covered nowhere,
    and in a self-service portal the read path is where the risk is: the way
    this goes wrong is not a member changing another family's record, it is a
    member *seeing* it. Without this row that failure leaves no trace at all.
    """

    class Action(models.TextChoices):
        SIGN_IN = "SIGN_IN", "Opened the portal"
        VIEW_CONTRIBUTIONS = "VIEW_CONTRIBUTIONS", "Viewed contribution history"
        VIEW_STANDING = "VIEW_STANDING", "Viewed standing and eligibility"
        VIEW_CASE = "VIEW_CASE", "Viewed a case"
        VIEW_HOUSEHOLD = "VIEW_HOUSEHOLD", "Viewed household"
        DOWNLOAD_STATEMENT = "DOWNLOAD_STATEMENT", "Downloaded a statement"
        DOWNLOAD_RECEIPT = "DOWNLOAD_RECEIPT", "Downloaded a receipt"
        DOWNLOAD_DOCUMENT = "DOWNLOAD_DOCUMENT", "Downloaded a document"
        DENIED = "DENIED", "Refused access to something"

    account = models.ForeignKey(MemberAccount, null=True, blank=True,
                                on_delete=models.SET_NULL, related_name="access_log")
    user = models.ForeignKey("auth.User", null=True, blank=True,
                             on_delete=models.SET_NULL, related_name="+")
    action = models.CharField(max_length=20, choices=Action.choices, db_index=True)
    detail = models.CharField(max_length=200, blank=True, default="")
    object_ref = models.CharField(
        max_length=80, blank=True, default="",
        help_text="What was accessed, e.g. 'case:412' — a reference, not a foreign key, "
                  "so the log survives the row it describes.")
    ip_address = models.GenericIPAddressField(null=True, blank=True)
    user_agent = models.CharField(max_length=200, blank=True, default="")
    at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        ordering = ["-at"]
        indexes = [models.Index(fields=["account", "-at"]),
                   models.Index(fields=["action", "-at"])]
        verbose_name = "portal access log entry"
        verbose_name_plural = "portal access log entries"

    def __str__(self):
        return f"{self.get_action_display()} — {self.account_id} @ {self.at:%Y-%m-%d %H:%M}"

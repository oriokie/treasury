"""Phase 7 — configurable notification templates, and a record of every
attempt to actually send one.

Every notification anywhere else in this module, up to this phase, was a
hardcoded Python f-string built inside a service function and sent ONLY to
treasury staff (`services/cases.py::_notify`, reusing `core.services.
notifications.notify`). That is correct and untouched for staff notices — but
it is not what the brief means by "configurable... templates... placeholders":
a treasurer cannot edit a Python string, and nothing was ever sent to the
member or beneficiary the event was actually ABOUT.

This module is the other half: templates a treasurer CAN edit, rendered with
named placeholders, sent to the person the event concerns (a member, or the
committee), through the SAME sending engines the rest of the system already
uses — `core.services.sms.send_sms` (which already logs to `SmsLog`) and
`core.services.email.send_email` — never a parallel channel.
"""
from django.db import models


class NotificationEvent(models.TextChoices):
    """The stable, finite set of things a template can exist for. Matches the
    brief's own list: registrations, renewals, contribution reminders, case
    notifications, committee approvals, benefit payments, membership status
    changes."""
    REGISTRATION_CONFIRMED = "REGISTRATION_CONFIRMED", "Registration confirmed"
    RENEWAL_REMINDER = "RENEWAL_REMINDER", "Renewal due soon"
    RENEWAL_CONFIRMED = "RENEWAL_CONFIRMED", "Renewal received"
    ARREARS_REMINDER = "ARREARS_REMINDER", "Contribution / arrears reminder"
    CASE_RECEIVED = "CASE_RECEIVED", "Case received"
    CASE_DECIDED = "CASE_DECIDED", "Case decided (approved or rejected)"
    PAYOUT_MADE = "PAYOUT_MADE", "Benefit paid"
    MEMBERSHIP_STATUS_CHANGED = "MEMBERSHIP_STATUS_CHANGED", "Membership status changed"
    COMMITTEE_VOTE_NEEDED = "COMMITTEE_VOTE_NEEDED", "Committee decision needed"


class NotificationTemplate(models.Model):
    """One editable message, for one event, on one channel.

    Placeholders use the same `{name}` syntax `core.services.sms` already uses
    for the envelope receipt template — one convention across the system, not
    a second one invented here. Available placeholders differ by event; the
    settings screen lists them next to the field being edited.
    """

    class Channel(models.TextChoices):
        SMS = "SMS", "SMS"
        EMAIL = "EMAIL", "Email"

    event = models.CharField(max_length=32, choices=NotificationEvent.choices, db_index=True)
    channel = models.CharField(max_length=6, choices=Channel.choices)
    subject = models.CharField(
        max_length=120, blank=True,
        help_text="Email only. SMS has no subject line.")
    body = models.TextField()
    active = models.BooleanField(
        default=True,
        help_text="Switched off, this event sends nothing on this channel — the "
                  "member-facing/committee toggle in settings decides whether the "
                  "event fires at all; this decides whether THIS channel does.")
    updated_by = models.ForeignKey("auth.User", null=True, blank=True,
                                   on_delete=models.SET_NULL, related_name="+")
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["event", "channel"],
                                               name="uniq_template_per_event_channel")]
        ordering = ["event", "channel"]

    def __str__(self):
        return f"{self.get_event_display()} ({self.channel})"

    def render(self, context):
        """Fill in the placeholders. Reuses the exact substitution rule
        core.services.sms._format already established — unmatched keys are
        simply left as literal `{text}` rather than raising, so a template
        referencing a placeholder this event does not provide fails
        VISIBLY (an odd-looking message a treasurer will notice and fix) 
        rather than crashing a financial workflow over a wording mistake."""
        from core.services.sms import _format
        subject = _format(self.subject, **context) if self.subject else ""
        body = _format(self.body, **context)
        return subject, body


class BenevolentNotification(models.Model):
    """One attempt to actually deliver a rendered template. The permanent
    record of "who was told what, when, how, and did it work" — what the
    brief calls notification history and delivery tracking.

    Deliberately references `core.models.SmsLog` rather than duplicating its
    status/response fields: SmsLog is already the authoritative delivery
    record for an SMS attempt (Africa's Talking / Advanta's own response is
    in there). This row is the higher-level "why was this sent" context SmsLog
    itself does not carry — which event, which member, which case, rendered
    from which template.
    """

    class Status(models.TextChoices):
        QUEUED = "QUEUED", "Queued"
        SENT = "SENT", "Sent"
        FAILED = "FAILED", "Failed"
        SKIPPED = "SKIPPED", "Skipped (no recipient / disabled)"

    event = models.CharField(max_length=32, choices=NotificationEvent.choices, db_index=True)
    channel = models.CharField(max_length=6, choices=NotificationTemplate.Channel.choices)
    membership = models.ForeignKey("SchemeMembership", null=True, blank=True,
                                   on_delete=models.SET_NULL, related_name="notifications")
    case = models.ForeignKey("BenevolentCase", null=True, blank=True,
                             on_delete=models.SET_NULL, related_name="notifications")
    user = models.ForeignKey("auth.User", null=True, blank=True,
                             on_delete=models.SET_NULL, related_name="+",
                             help_text="Set instead of membership when the recipient is "
                                       "a committee member, not a scheme member.")
    recipient = models.CharField(max_length=120,
                                 help_text="The phone number or email address actually used.")
    subject = models.CharField(max_length=120, blank=True)
    body = models.TextField()
    status = models.CharField(max_length=8, choices=Status.choices,
                              default=Status.QUEUED, db_index=True)
    attempts = models.PositiveSmallIntegerField(default=0)
    last_error = models.TextField(blank=True)
    sms_log = models.ForeignKey("core.SmsLog", null=True, blank=True,
                                on_delete=models.SET_NULL, related_name="+",
                                help_text="The authoritative SMS delivery record, where "
                                          "the channel is SMS.")
    created_at = models.DateTimeField(auto_now_add=True)
    sent_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [models.Index(fields=["status", "-created_at"]),
                   models.Index(fields=["event", "-created_at"])]

    def __str__(self):
        return f"{self.get_event_display()} → {self.recipient} [{self.status}]"

"""Phase 5 — the case's own narrative.

`django-simple-history` already answers "what was this field on 3 March?" for
`BenevolentCase`. It has never answered "what happened on this case, and why?" —
which is exactly what a treasurer re-opening a case six months later, a board
reviewing how a large payment was decided, or a bereaved family asking why their
claim took so long, actually wants. `MembershipEvent` solved this for a
membership in Phase 3; `CaseEvent` is the same idea, scoped to a case.

Every workflow function in `services/cases.py` writes one of these. No view or
service moves a case through its lifecycle without a line here — the same
discipline Phase 3 established for `services/registry.py`.
"""
import datetime as _dt

from django.db import models


class CaseEvent(models.Model):
    """One line in a case's narrative: what happened, when, why, and whether a
    person decided it or a job did."""

    class Kind(models.TextChoices):
        RAISED = "RAISED", "Case raised"
        SUBMITTED = "SUBMITTED", "Submitted for assessment"
        ASSESSED = "ASSESSED", "Assessed"
        COMMITTEE_VOTE = "COMM_VOTE", "Committee decision recorded"
        APPROVED = "APPROVED", "Approved"
        REJECTED = "REJECTED", "Rejected"
        CANCELLED = "CANCELLED", "Cancelled"
        PAYOUT_RAISED = "PAY_RAISED", "Payment voucher raised"
        PAYOUT_PAID = "PAY_PAID", "Payment voucher paid"
        PAYOUT_REVERSED = "PAY_REV", "Payment voucher reversed or rejected"
        CLOSED = "CLOSED", "Closed"
        DOCUMENT_ADDED = "DOC_ADD", "Document attached"
        FUNDING_TARGET = "FUND_TGT", "Funding target set"
        FUNDING_REACHED = "FUND_MET", "Funding target reached"
        BEREAVED_DECISION = "BER_DEC", "Bereaved member's contribution decided"
        EXEMPTION_GRANTED = "EXEMPTED", "Bereavement exemption granted"
        FUNDED_FROM_BALANCE = "FUNDED_BAL", "Funded from the fund balance, not a levy"
        IMPORTED = "IMPORTED", "Imported from historical records"
        NOTE = "NOTE", "Note"

    case = models.ForeignKey("BenevolentCase", on_delete=models.CASCADE,
                             related_name="events")
    kind = models.CharField(max_length=10, choices=Kind.choices, db_index=True)
    on = models.DateField(default=_dt.date.today, db_index=True,
                          help_text="The date the thing happened (not when it was typed in).")
    summary = models.CharField(max_length=255)
    reason = models.TextField(blank=True)
    automated = models.BooleanField(
        default=False, db_index=True,
        help_text="Recorded by a job or a policy-driven rule rather than a person's "
                  "direct action in the moment.")
    actor = models.ForeignKey("auth.User", null=True, blank=True,
                              on_delete=models.SET_NULL, related_name="+")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-on", "-created_at"]
        indexes = [models.Index(fields=["case", "-on"])]

    def __str__(self):
        return f"{self.on} {self.get_kind_display()} — {self.summary}"

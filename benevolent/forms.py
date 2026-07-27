import datetime as dt
from decimal import Decimal

from django import forms

from accounts.forms import StyledFormMixin
from cashbook.models import Expense
from departments.models import Department
from members.models import Member

from .models import (BenevolentCase, BenevolentContribution, BenevolentEventType,
                     BenevolentScheme, CaseAttachment, SchemeBenefitRule,
                     SchemeDependant, SchemeMembership, SchemePolicy)


def _local_funds():
    return (Department.objects.filter(active=True)
            .exclude(fund_type=Department.FundType.TRUST).order_by("name"))


class SchemeForm(StyledFormMixin, forms.ModelForm):
    class Meta:
        model = BenevolentScheme
        fields = ["name", "code", "kind", "fund", "description"]
        widgets = {"description": forms.Textarea(attrs={"rows": 3})}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["fund"].queryset = _local_funds()
        self.fields["fund"].help_text = (
            "The fund that holds this scheme's money. Its balance IS the scheme's "
            "balance — contributions are receipted into it and benefits paid out of it.")
        self._style()


class EventTypeForm(StyledFormMixin, forms.ModelForm):
    required_documents_text = forms.CharField(
        required=False, widget=forms.Textarea(attrs={"rows": 3}),
        label="Named documents required",
        help_text="One per line, e.g. 'Burial permit', 'Death certificate'. A case "
                  "then shows each as its own checklist item. Leave blank to fall "
                  "back to the plain toggle below (any one document, unnamed).")

    class Meta:
        model = BenevolentEventType
        fields = ["name", "code", "description", "covers_dependants",
                  "triggers_on_death",
                  "requires_document", "sort_order", "active"]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["code"].required = False
        self.fields["code"].help_text = "Leave blank to derive it from the name."
        if self.instance.pk and self.instance.required_documents:
            self.fields["required_documents_text"].initial = "\n".join(
                self.instance.required_documents)
        self._style()

    def clean_required_documents_text(self):
        raw = self.cleaned_data.get("required_documents_text") or ""
        return [line.strip() for line in raw.splitlines() if line.strip()]

    def save(self, commit=True):
        obj = super().save(commit=False)
        obj.required_documents = self.cleaned_data.get("required_documents_text") or []
        if commit:
            obj.save()
        return obj


class PolicyForm(StyledFormMixin, forms.ModelForm):
    """Every rule the engine can enforce, on one form. Adding a rule to the
    engine means adding a field here and a check in the eligibility service —
    never a new code path per scheme."""

    # The form is grouped so the 54 rules read as a constitution rather than a
    # wall of fields. Each group is a chapter a church's document actually has.
    GROUPS = [
        ("Membership & eligibility",
         ["membership_required", "waiting_period_days", "min_contributions",
          "min_paid_months", "no_missed_contributions", "missed_contributions_allowed",
          "arrears_treatment", "max_arrears_allowed", "max_arrears_periods",
          "arrears_block", "catch_up_restores_eligibility", "catch_up_requalify_days",
          "grace_period_days"]),
        ("Registration",
         ["registration_required", "registration_approval", "registration_fee",
          "registration_fee_refundable", "require_registration_form",
          "require_id_document", "min_age", "max_age", "exemption_age"]),
        ("Renewals",
         ["renewal_required", "renewal_period", "renewal_fee", "renewal_month",
          "renewal_grace_days", "lapse_on_non_renewal"]),
        ("Contributions & funding",
         ["contribution_mode", "contribution_amount", "contribution_frequency",
          "levy_amount", "max_levies_per_year", "joining_fee", "funding_methods"]),
        ("The benefit",
         ["benefit_mode", "benefit_amount", "benefit_percent", "benefit_cap",
          "benefit_floor", "benefit_rounding"]),
        ("Approval",
         ["approval_mode", "committee_threshold", "committee_quorum",
          "committee_requires_chair", "require_different_approver"]),
        ("The member a case is about",
         ["bereaved_contribution_policy", "bereaved_reduction_percent",
          "bereaved_deduct_own_levy", "bereaved_dues_waiver_months"]),
        ("Inactivity & lapsing",
         ["inactivity_months", "inactivity_missed_cases", "inactivity_missed_cases_window",
          "inactivity_action", "reinstatement_fee", "reinstatement_waiting_days"]),
        ("Household & dependants",
         ["household_mode", "max_dependants", "dependant_age_limit",
          "max_household_size", "spouse_auto_covered"]),
        ("On a member's death",
         ["inheritance_mode", "transfer_membership_on_death",
          "refund_contributions_on_exit", "refund_percent"]),
        ("Claims",
         ["claim_window_days", "max_claims_per_year", "max_benefit_per_year",
          "require_documents", "allow_override", "allow_exemptions",
          "allow_transfers"]),
    ]

    class Meta:
        model = SchemePolicy
        fields = ["effective_from"] + [
            f for _g, fs in [
                ("Membership & eligibility",
                 ["membership_required", "waiting_period_days", "min_contributions",
                  "min_paid_months", "no_missed_contributions", "missed_contributions_allowed",
                  "arrears_treatment", "max_arrears_allowed", "max_arrears_periods",
                  "arrears_block", "catch_up_restores_eligibility", "catch_up_requalify_days",
                  "grace_period_days"]),
                ("Registration",
                 ["registration_required", "registration_approval", "registration_fee",
                  "registration_fee_refundable", "require_registration_form",
                  "require_id_document", "min_age", "max_age", "exemption_age"]),
                ("Renewals",
                 ["renewal_required", "renewal_period", "renewal_fee", "renewal_month",
                  "renewal_grace_days", "lapse_on_non_renewal"]),
                ("Contributions & funding",
                 ["contribution_mode", "contribution_amount", "contribution_frequency",
                  "levy_amount", "max_levies_per_year", "joining_fee"]),
                ("The benefit",
                 ["benefit_mode", "benefit_amount", "benefit_percent", "benefit_cap",
                  "benefit_floor", "benefit_rounding"]),
                ("Approval",
                 ["approval_mode", "committee_threshold", "committee_quorum",
          "committee_requires_chair", "require_different_approver"]),
                ("The member a case is about",
                 ["bereaved_contribution_policy", "bereaved_reduction_percent",
                  "bereaved_deduct_own_levy", "bereaved_dues_waiver_months"]),
                ("Inactivity & lapsing",
                 ["inactivity_months", "inactivity_missed_cases", "inactivity_missed_cases_window",
                  "inactivity_action", "reinstatement_fee", "reinstatement_waiting_days"]),
                ("Household & dependants",
                 ["household_mode", "max_dependants", "dependant_age_limit",
                  "max_household_size", "spouse_auto_covered"]),
                ("On a member's death",
                 ["inheritance_mode", "transfer_membership_on_death",
                  "refund_contributions_on_exit", "refund_percent"]),
                ("Claims",
                 ["claim_window_days", "max_claims_per_year", "max_benefit_per_year",
                  "require_documents", "allow_override", "allow_exemptions",
                  "allow_transfers"]),
            ] for f in fs
        ] + ["notes"]
        widgets = {
            "effective_from": forms.DateInput(attrs={"type": "date"}),
            "notes": forms.Textarea(attrs={"rows": 3}),
        }

    funding_methods = forms.MultipleChoiceField(
        choices=SchemePolicy.FUNDING_METHODS, required=False,
        widget=forms.CheckboxSelectMultiple,
        label="What may fund this scheme",
        help_text="A rule, not a note: it stops a member-funded scheme being quietly "
                  "subsidised out of the church budget without the constitution being "
                  "changed to allow it.")

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        if not self.instance.pk:
            self.fields["effective_from"].initial = dt.date.today()
        if self.instance.pk:
            self.fields["funding_methods"].initial = self.instance.funding_methods or []
        self._style()

    def grouped(self):
        """[(group, [bound fields])] so the template renders a constitution, not a
        wall of 54 inputs.

        Any field on the form but NOT listed in GROUPS is appended to a final
        "Other settings" group rather than silently disappearing. That silent
        disappearance was a real, shipped bug: six genuinely-enforced policy
        rules (arrears_block, grace_period_days, exemption_age,
        max_household_size, allow_exemptions, allow_transfers) were absent
        from GROUPS, so `grouped()` skipped them, so the template never
        rendered them, so no treasurer could ever configure a rule the
        eligibility engine was nonetheless enforcing against them. Exactly the
        same shape as the settings-page bug found in Phase 9.

        A missing field is now visible rather than invisible — which is the
        point: a field in an oddly-named group is a five-second fix a person
        will notice, whereas a field that renders nowhere is invisible until
        someone reads the source.
        """
        out = []
        placed = set()
        for name, keys in self.GROUPS:
            fields = [self[k] for k in keys if k in self.fields]
            placed.update(k for k in keys if k in self.fields)
            if fields:
                out.append((name, fields))
        leftover = [self[k] for k in self.fields if k not in placed
                    and k != "effective_from"]   # rendered separately by the template
        if leftover:
            out.append(("Other settings", leftover))
        return out

    def clean_funding_methods(self):
        return list(self.cleaned_data.get("funding_methods") or [])

    def clean(self):
        cleaned = super().clean()
        mode = cleaned.get("benefit_mode")
        if mode == SchemePolicy.BenefitMode.FIXED and not cleaned.get("benefit_amount"):
            self.add_error("benefit_amount",
                           "A fixed-benefit policy needs a benefit amount.")
        if mode == SchemePolicy.BenefitMode.PERCENTAGE and not cleaned.get("benefit_percent"):
            self.add_error("benefit_percent",
                           "A percentage policy needs a percentage.")
        if mode == SchemePolicy.BenefitMode.DISCRETIONARY and not cleaned.get("benefit_cap"):
            self.add_error("benefit_cap",
                           "A discretionary policy must set a cap — otherwise there is no "
                           "limit on what one approver can authorise.")
        cmode = cleaned.get("contribution_mode")
        periodic = (SchemePolicy.ContributionMode.FIXED_PERIODIC,
                    SchemePolicy.ContributionMode.HYBRID)
        leviable = (SchemePolicy.ContributionMode.PER_CASE_LEVY,
                    SchemePolicy.ContributionMode.HYBRID)
        if cmode in periodic and not cleaned.get("contribution_amount"):
            self.add_error("contribution_amount",
                           "A scheme with periodic dues needs a dues amount.")
        if cmode in leviable and not cleaned.get("levy_amount"):
            self.add_error("levy_amount",
                           "A scheme that levies per case needs a levy amount.")

        # A pooled benefit is what the levy collects; without a levy there is
        # nothing to pool, and the benefit would silently be zero for every case.
        if mode in (SchemePolicy.BenefitMode.POOLED,
                    SchemePolicy.BenefitMode.PER_MEMBER_MULTIPLE) \
                and cmode not in leviable:
            self.add_error(
                "benefit_mode",
                "This benefit is worked out from the per-case levy, so the scheme must "
                "actually levy per case. Choose a per-case or hybrid contribution mode, "
                "or a different benefit calculation.")

        if cleaned.get("arrears_treatment") != SchemePolicy.ArrearsTreatment.IGNORE \
                and cmode not in periodic:
            self.add_error(
                "arrears_treatment",
                "Arrears can only arise where dues fall due periodically. A scheme with "
                "no standing dues has nothing a member can be behind on.")

        # exempting a member from their own levy and deducting it from their
        # benefit are two answers to the same question
        bcp = cleaned.get("bereaved_contribution_policy")
        if bcp == SchemePolicy.BereavedContributionPolicy.EXEMPT \
                and cleaned.get("bereaved_deduct_own_levy"):
            self.add_error(
                "bereaved_deduct_own_levy",
                "An exempt member has nothing to deduct — 'deduct from the benefit' only "
                "makes sense where the bereaved member DOES contribute (fully or at a "
                "reduced amount).")
        if bcp == SchemePolicy.BereavedContributionPolicy.REDUCED \
                and not cleaned.get("bereaved_reduction_percent"):
            self.add_error("bereaved_reduction_percent",
                           "Say what percentage a reduced contribution actually is.")

        if cleaned.get("approval_mode") in (SchemePolicy.ApprovalMode.COMMITTEE,
                                            SchemePolicy.ApprovalMode.TWO_STAGE) \
                and not cleaned.get("committee_quorum"):
            self.add_error("committee_quorum",
                           "A committee needs a quorum, or nothing could ever be approved.")

        if cleaned.get("renewal_required") \
                and cleaned.get("renewal_period") == SchemePolicy.RenewalPeriod.NONE:
            self.add_error("renewal_period",
                           "Say how often the membership renews.")
        return cleaned


class BenefitRuleForm(StyledFormMixin, forms.ModelForm):
    class Meta:
        model = SchemeBenefitRule
        fields = ["event_type", "amount", "percent", "cap",
                  "waiting_period_days", "max_per_year", "active"]

    def __init__(self, *args, scheme=None, policy=None, **kwargs):
        super().__init__(*args, **kwargs)
        sch = scheme or (policy.scheme if policy else None)
        if sch is not None:
            self.fields["event_type"].queryset = sch.event_types.filter(active=True)
        self._style()


# NOTE: MembershipForm was removed here. It was Phase 1's simple enrolment
# form (member + joined_on + notes), superseded by Phase 3's RegistrationForm
# — which does everything it did plus households, dependants, a spouse, date
# of birth, and registering someone who is not on the church roll at all.
# The view that rendered it (MembershipCreateView) now redirects to the full
# registration screen, so nothing reaches this form any more; keeping it
# would have left a second, divergent way to do exactly one job.


class DependantForm(StyledFormMixin, forms.ModelForm):
    class Meta:
        model = SchemeDependant
        fields = ["name", "relationship", "date_of_birth", "registered_on", "notes"]
        widgets = {"date_of_birth": forms.DateInput(attrs={"type": "date"}),
                   "registered_on": forms.DateInput(attrs={"type": "date"})}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["registered_on"].initial = dt.date.today()
        self._style()


class CaseForm(StyledFormMixin, forms.ModelForm):
    """Raising a case, rethought (Round 9, item 1).

    The old form asked for the member first, then the dependant, then the
    relationship — three things the database already holds or can derive:

      * the relationship is stored on the dependant record;
      * the member is derivable from the dependant (a dependant belongs to one
        membership);
      * the claimed amount, under a fixed or scheduled policy, is a
        constitutional figure, not a number to retype.

    Asking a treasurer to re-enter what the system already knows is asking them
    to introduce a discrepancy. So: dependant first (with the member derived and
    shown), relationship derived and read-only where known, and the claimed
    amount pre-filled and locked whenever the policy fixes it.
    """

    funding_target = forms.DecimalField(
        max_digits=12, decimal_places=2, required=False, min_value=Decimal("0.01"),
        help_text="Optional. What this case is aiming to raise or receive — a "
                  "fundraising goal you can set now or later, independent of "
                  "whatever the policy ultimately computes as the benefit.")

    create_and_approve = forms.BooleanField(
        required=False, label="Approve immediately",
        help_text="Submit, assess and approve this case in one step, for the "
                  "assessed amount, instead of leaving it as a draft. Only "
                  "offered where the scheme's policy allows the same person to "
                  "both raise and approve a case — if assessment finds the case "
                  "ineligible, this stops there rather than overriding "
                  "anything; you decide what to do with it from the case page.")

    class Meta:
        model = BenevolentCase
        # Order matters: this is the order the template renders them, and the
        # dependant now leads. `membership` still exists (a case can be for the
        # member's own event, with no dependant) but follows, pre-filled from
        # the dependant when one is chosen.
        fields = ["dependant", "membership", "event_type", "beneficiary_name",
                  "beneficiary_relationship",
                  "event_date", "reported_date", "claimed_amount", "description"]
        widgets = {"event_date": forms.DateInput(attrs={"type": "date"}),
                   "reported_date": forms.DateInput(attrs={"type": "date"}),
                   "description": forms.Textarea(attrs={"rows": 3})}

    def __init__(self, *args, scheme=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.scheme = scheme or (self.instance.scheme if self.instance.pk else None)
        # The form has no scheme field (the view owns it), but the model's clean()
        # checks membership/event_type/dependant all belong to the case's scheme —
        # and that check runs during this form's _post_clean. Without the scheme on
        # the instance it reads as None and every membership looks "different", so
        # set it here before validation ever runs.
        if self.scheme is not None and not self.instance.scheme_id:
            self.instance.scheme = self.scheme
        self.policy = self.scheme.current_policy if self.scheme else None
        # The fast path is only ever offered where the scheme's own policy
        # already permits the same person to raise and approve (see
        # SchemePolicy.require_different_approver) — popped entirely, not just
        # hidden, so a crafted POST cannot set it where the policy forbids it.
        if self.policy is None or self.policy.require_different_approver:
            self.fields.pop("create_and_approve", None)
        if self.scheme is not None:
            self.fields["membership"].queryset = (
                SchemeMembership.objects.filter(
                    scheme=self.scheme, status__in=SchemeMembership.LIVE_STATUSES)
                .select_related("member").order_by("member__name"))
            self.fields["event_type"].queryset = self.scheme.event_types.filter(active=True)
            self.fields["dependant"].queryset = SchemeDependant.objects.filter(
                membership__scheme=self.scheme, active=True).select_related("membership")

        self.fields["membership"].required = False
        self.fields["membership"].help_text = (
            "The enrolled member this case is for. Filled in automatically when you "
            "pick a dependant; set it directly for the member's own event.")
        self.fields["dependant"].required = False
        self.fields["dependant"].label = "Who is the beneficiary?"
        self.fields["dependant"].help_text = (
            "Pick the registered dependant this case is for — the member and "
            "relationship fill in from their record. Leave blank if the case is "
            "for the member themselves.")
        self.fields["beneficiary_relationship"].required = False
        self.fields["event_date"].initial = dt.date.today()
        self.fields["reported_date"].initial = dt.date.today()

        # Lock the claimed amount when the policy fixes the benefit. The initial
        # value is supplied by the view from cases.derive_case_defaults(); here
        # we just make the field reflect that it is not the treasurer's to edit.
        self._claimed_is_fixed = bool(self.initial.get("claimed_is_fixed"))
        if self._claimed_is_fixed:
            self.fields["claimed_amount"].disabled = True
            self.fields["claimed_amount"].help_text = (
                "Fixed by the scheme's policy for this event — shown for confirmation, "
                "not edited here. Change it on the policy if the constitution changed.")
        else:
            self.fields["claimed_amount"].help_text = (
                "The cost incurred / amount requested. Used by percentage and "
                "discretionary policies; ignored by fixed and scheduled ones.")
        self._style()

    def clean(self):
        cleaned = super().clean()
        dependant = cleaned.get("dependant")
        # Derive the member from the dependant — the treasurer never types it.
        if dependant is not None:
            cleaned["membership"] = dependant.membership
            if not cleaned.get("beneficiary_name"):
                cleaned["beneficiary_name"] = dependant.display_name
            if not cleaned.get("beneficiary_relationship"):
                member_name = dependant.membership.member.name
                cleaned["beneficiary_relationship"] = (
                    f"{dependant.get_relationship_display()} to {member_name}")

        # A disabled field submits nothing, so Django drops it from cleaned_data;
        # restore the policy-fixed figure so it is actually saved. Recompute
        # whether it is fixed from the policy + chosen event here (self.initial's
        # claimed_is_fixed is only present on an UNbound form).
        et = cleaned.get("event_type")
        fixed = (self.policy.fixed_benefit_for(et)
                 if (self.policy is not None and et) else None)
        if fixed:
            cleaned["claimed_amount"] = fixed
        return cleaned


class ContributionForm(StyledFormMixin, forms.Form):
    membership = forms.ModelChoiceField(queryset=SchemeMembership.objects.none(),
                                        required=False)
    member = forms.ModelChoiceField(queryset=Member.objects.none(), required=False,
                                    help_text="For a donation from someone not enrolled.")
    case = forms.ModelChoiceField(
        queryset=BenevolentCase.objects.none(), required=False,
        label="Levy for a case",
        help_text="Set this if the money is a LEVY towards a particular case. Money "
                  "attached to a case is a levy by definition — it shows on that case's "
                  "levy roster, and under a pooled policy it is what the benefit is "
                  "actually made of.")
    date = forms.DateField(initial=dt.date.today,
                           widget=forms.DateInput(attrs={"type": "date"}))
    amount = forms.DecimalField(max_digits=12, decimal_places=2,
                                min_value=Decimal("0.01"))
    channel = forms.ChoiceField(choices=[("CASH", "Cash"), ("BANK", "Bank / M-Pesa"),
                                         ("ENVELOPE", "Envelope")], initial="CASH")
    payer_type = forms.ChoiceField(
        choices=BenevolentContribution.PayerType.choices,
        initial=BenevolentContribution.PayerType.SELF, required=False,
        label="Paid by",
        help_text="Who actually paid. Use the others when someone paid on a member's "
                  "behalf (employer, sponsor, third party) or gave anonymously — the "
                  "money still counts, and the member's statement stays truthful about it.")
    payer_name = forms.CharField(
        required=False, max_length=120, label="Payer name",
        help_text="The employer, sponsor or third party who paid. Leave blank for the "
                  "member's own payment, or for a genuinely anonymous gift.")
    period_label = forms.CharField(required=False, max_length=10,
                                   help_text="Dues period, e.g. 2026-07. Left blank, it is "
                                             "derived from the date and the policy.")
    note = forms.CharField(required=False, max_length=200)

    def __init__(self, *args, scheme=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.scheme = scheme
        if scheme is not None:
            self.fields["membership"].queryset = (
                SchemeMembership.objects.filter(
                    scheme=scheme, status__in=SchemeMembership.LIVE_STATUSES)
                .select_related("member").order_by("member__name"))
            # Only cases that are definitively NOT collecting are excluded. A
            # draft or unassessed case is still collecting — a church starts the
            # harambee the moment a death is known, long before the paperwork
            # catches up, and refusing to let that money be attributed would be
            # the system telling a treasurer their own practice is invalid. A
            # closed, rejected or cancelled case, by contrast, is genuinely
            # finished, and offering it would only invite a mis-posting.
            self.fields["case"].queryset = (
                BenevolentCase.objects.filter(scheme=scheme)
                .exclude(status__in=[BenevolentCase.Status.CLOSED,
                                     BenevolentCase.Status.REJECTED,
                                     BenevolentCase.Status.CANCELLED])
                .select_related("membership__member").order_by("-event_date"))
        self.fields["member"].queryset = Member.objects.filter(active=True).order_by("name")
        self._style()

    def clean(self):
        cleaned = super().clean()
        if not cleaned.get("membership") and not cleaned.get("member"):
            raise forms.ValidationError(
                "Say who contributed — either an enrolled membership or a member.")
        # A levy is a member standing with a bereaved family. Somebody who is not
        # enrolled can donate to a case, but they cannot be LEVIED for one, and
        # recording it as if they had been would put a non-member on the levy
        # roster of a scheme they do not belong to.
        if cleaned.get("case") and not cleaned.get("membership"):
            self.add_error(
                "case",
                "A levy is paid by an enrolled member. Choose the membership, or leave "
                "the case blank to record this as an ordinary donation.")
        return cleaned


class ApproveForm(StyledFormMixin, forms.Form):
    amount = forms.DecimalField(max_digits=12, decimal_places=2,
                                min_value=Decimal("0.01"),
                                label="Benefit approved")
    override_reason = forms.CharField(
        required=False, widget=forms.Textarea(attrs={"rows": 3}),
        label="Reason for overriding the policy",
        help_text="Required only when the case does not meet the policy's conditions, or "
                  "the amount exceeds the cap. It is kept on the permanent record.")

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._style()


class RejectForm(StyledFormMixin, forms.Form):
    reason = forms.CharField(widget=forms.Textarea(attrs={"rows": 3}),
                             label="Reason for rejection")

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._style()


class PayoutForm(StyledFormMixin, forms.Form):
    amount = forms.DecimalField(max_digits=12, decimal_places=2, min_value=Decimal("0.01"))
    date = forms.DateField(initial=dt.date.today,
                           widget=forms.DateInput(attrs={"type": "date"}))
    payee_name = forms.CharField(required=False, max_length=120,
                                 help_text="Leave blank to pay the beneficiary.")
    method = forms.ChoiceField(choices=Expense.Method.choices, initial=Expense.Method.CASH)
    voucher_no = forms.CharField(required=False, max_length=30)
    paid_from_petty_cash = forms.BooleanField(required=False, label="Paid from petty cash")
    note = forms.CharField(required=False, max_length=200)

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._style()


class AttachmentForm(StyledFormMixin, forms.ModelForm):
    class Meta:
        model = CaseAttachment
        fields = ["file", "document_type", "label"]

    def __init__(self, *args, case=None, **kwargs):
        super().__init__(*args, **kwargs)
        named = list((case.event_type.required_documents if case and case.event_type_id
                     else []) or [])
        if named:
            missing_now = []
            try:
                from .services.eligibility import missing_required_documents
                missing_now = missing_required_documents(case.event_type, case)
            except Exception:  # noqa: BLE001
                missing_now = named
            choices = [("", "— choose —")] + [(n, n) for n in named] + [("OTHER", "Other")]
            self.fields["document_type"] = forms.ChoiceField(
                choices=choices, required=False, label="Which document",
                help_text=(f"Still needed: {', '.join(missing_now)}." if missing_now
                          else "All named documents are already on file — this adds "
                               "another anyway."))
        self._style()


class FundingTargetForm(StyledFormMixin, forms.Form):
    amount = forms.DecimalField(max_digits=12, decimal_places=2, min_value=Decimal("0.01"),
                                label="Funding target")

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._style()


class BereavedDecisionForm(StyledFormMixin, forms.Form):
    waived = forms.ChoiceField(
        choices=[("1", "Waived — they do not contribute to this case"),
                 ("0", "They contribute, as normal")],
        label="The committee's decision")
    reason = forms.CharField(widget=forms.Textarea(attrs={"rows": 2}),
                             label="Reason / minute reference")

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._style()


# ===========================================================================
# Phase 2 — settings, profiles, committee, registration
# ===========================================================================

from .models import BenevolentSettings, CaseApproval, PolicyProfile, SchemeNominee


class SettingsForm(StyledFormMixin, forms.ModelForm):
    """The benevolent module's own settings.

    Everything on this form is operational: accounting mappings, notification
    preferences, automation. NOTHING here decides whether a claim qualifies or
    what it is worth — that is what a policy is for, and it is why a policy is
    versioned and this is not.
    """

    class Meta:
        model = BenevolentSettings
        exclude = ["automation_last_run", "automation_last_summary"]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        from cashbook.models import Expense, ExpenseCategory
        choices = list(Expense.Category.choices)
        for ec in ExpenseCategory.objects.all():
            if ec.code not in dict(choices):
                choices.append((ec.code, ec.label))
        self.fields["default_benefit_category"] = forms.ChoiceField(
            choices=choices, required=True,
            label="Benefit expense category",
            help_text=self.Meta.model._meta.get_field(
                "default_benefit_category").help_text)
        self.fields["registration_fee_fund"].queryset = _local_funds()
        self.fields["registration_fee_fund"].required = False
        self.fields["default_profile"].required = False
        self._style()


class PolicyProfileForm(StyledFormMixin, forms.ModelForm):
    class Meta:
        model = PolicyProfile
        fields = ["name", "description", "kind"]
        widgets = {"description": forms.Textarea(attrs={"rows": 3})}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["kind"] = forms.ChoiceField(
            choices=BenevolentScheme.Kind.choices, required=False,
            label="Suits which kind of scheme")
        self._style()


class SaveAsProfileForm(StyledFormMixin, forms.Form):
    name = forms.CharField(max_length=80, label="Profile name")
    description = forms.CharField(required=False, widget=forms.Textarea(attrs={"rows": 3}))

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._style()


class ApplyProfileForm(StyledFormMixin, forms.Form):
    profile = forms.ModelChoiceField(queryset=PolicyProfile.objects.all(),
                                     label="Start from")
    effective_from = forms.DateField(widget=forms.DateInput(attrs={"type": "date"}),
                                     initial=dt.date.today,
                                     label="These rules take effect from")

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._style()


class VoteForm(StyledFormMixin, forms.Form):
    decision = forms.ChoiceField(choices=CaseApproval.Decision.choices,
                                 label="Your decision")
    amount = forms.DecimalField(max_digits=12, decimal_places=2, required=False,
                                label="Amount you would approve",
                                help_text="Leave blank to agree with the assessed "
                                          "entitlement.")
    note = forms.CharField(required=False, widget=forms.Textarea(attrs={"rows": 2}))

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._style()


class MembershipEditForm(StyledFormMixin, forms.ModelForm):
    """Correct a membership's own basic details — a typo in the household
    name, a date of birth added later, a note. Deliberately excludes
    `member` (change WHO this is via Transfer, a deliberate act with its
    own trail), `joined_on`/`registered_on` (accounting-significant dates),
    and `status` (that is what the lifecycle actions are for)."""

    class Meta:
        model = SchemeMembership
        fields = ["household_name", "date_of_birth", "notes"]
        widgets = {"date_of_birth": forms.DateInput(attrs={"type": "date"}),
                  "notes": forms.Textarea(attrs={"rows": 2})}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._style()


class NomineeForm(StyledFormMixin, forms.ModelForm):
    class Meta:
        model = SchemeNominee
        fields = ["name", "relationship", "phone", "national_id", "share_percent",
                  "is_successor"]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._style()


class FeeForm(StyledFormMixin, forms.Form):
    kind = forms.ChoiceField(choices=[("REGISTRATION", "Registration fee"),
                                      ("RENEWAL", "Renewal fee")])
    amount = forms.DecimalField(max_digits=12, decimal_places=2, required=False,
                                help_text="Leave blank to charge what the policy says.")
    date = forms.DateField(initial=dt.date.today,
                           widget=forms.DateInput(attrs={"type": "date"}))
    channel = forms.ChoiceField(choices=[("CASH", "Cash"), ("BANK", "Bank / M-Pesa")],
                                initial="CASH")

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._style()


# ===========================================================================
# Phase 3 — the member registry
# ===========================================================================

from .models import (MembershipEvent, MembershipExemption, RegistrationType,
                     SchemeDependant, Standing)


class RegistrationForm(StyledFormMixin, forms.Form):
    """Register a member — individually, or as a household.

    The household fields are on the SAME form, not a separate one, because a
    household registration is the same membership with more people attached, not a
    different kind of thing. A second form would invite a second code path.
    """
    member = forms.ModelChoiceField(
        queryset=Member.objects.none(), required=False, label="Principal member",
        help_text="If they are on the church roll, link them here — one person, one "
                  "record, everywhere in the system.")
    member_name = forms.CharField(
        required=False, max_length=120, label="…or the person's name",
        help_text="A benevolent scheme is its own thing — someone can be registered "
                  "into one without first needing to exist anywhere else in the "
                  "system. Only if they are not on the church roll.")
    member_phone = forms.CharField(
        required=False, max_length=20, label="Phone (for the new record above)")
    registration_type = forms.ChoiceField(
        choices=RegistrationType.choices, initial=RegistrationType.INDIVIDUAL,
        label="Register as")
    household_name = forms.CharField(
        required=False, max_length=120,
        help_text="Only for a household registration. Left blank, it is derived from "
                  "the principal member's surname.")
    joined_on = forms.DateField(
        initial=dt.date.today, widget=forms.DateInput(attrs={"type": "date"}),
        help_text="Cover — and any waiting period — counts from here.")
    date_of_birth = forms.DateField(
        required=False, widget=forms.DateInput(attrs={"type": "date"}),
        help_text="Only needed where the policy sets a joining-age limit or an age "
                  "exemption.")
    spouse = forms.ModelChoiceField(
        queryset=Member.objects.none(), required=False,
        help_text="If the spouse is on the church roll, link them here rather than "
                  "typing their name — one person, one record.")
    spouse_name = forms.CharField(
        required=False, max_length=120, label="…or the spouse's name",
        help_text="Only if they are not on the church roll.")
    notes = forms.CharField(required=False, max_length=200)

    def __init__(self, *args, scheme=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.scheme = scheme
        live = Member.objects.filter(active=True).order_by("name")
        self.fields["member"].queryset = live
        self.fields["spouse"].queryset = live
        self._style()

    def clean(self):
        cleaned = super().clean()
        if cleaned.get("member") and cleaned.get("member_name"):
            self.add_error("member_name",
                           "Link the person to their member record, or type a name — "
                           "not both.")
        if not cleaned.get("member") and not cleaned.get("member_name"):
            self.add_error("member",
                           "Pick someone from the church roll, or type a name below.")
        if cleaned.get("member_phone") and not cleaned.get("member_name"):
            self.add_error("member_name",
                           "A phone was given but no name to go with it.")
        if cleaned.get("spouse") and cleaned.get("spouse_name"):
            self.add_error("spouse_name",
                           "Link the spouse to their member record, or type a name — "
                           "not both.")
        if cleaned.get("registration_type") == RegistrationType.INDIVIDUAL \
                and (cleaned.get("spouse") or cleaned.get("spouse_name")):
            self.add_error("registration_type",
                           "A spouse is being registered, so this is a household "
                           "registration.")

        # A person already covered as somebody else's spouse or dependant is
        # not a new principal member — they are the same household, reached
        # from the other side. Registering them again would give one person
        # two memberships in one scheme: counted twice on the roll, levied
        # twice, and able to claim twice. `register()` already refuses a
        # duplicate PRINCIPAL membership; this closes the household side of
        # the same hole, and says which household, so the registrar can go and
        # look rather than being told "no" with nowhere to go.
        member = cleaned.get("member")
        if member is not None and self.scheme is not None:
            dep = (SchemeDependant.objects
                   .filter(member=member, active=True,
                           membership__scheme=self.scheme)
                   .select_related("membership__member").first())
            if dep is not None:
                self.add_error(
                    "member",
                    f"{member.name} is already covered under this scheme as "
                    f"{dep.get_relationship_display().lower()} of "
                    f"{dep.membership.member.name} ({dep.membership.number}). "
                    f"Registering them again would give one person two "
                    f"memberships. Open that household instead, or remove them "
                    f"from it first if they are genuinely leaving it.")
        return cleaned

    def resolve_member(self):
        """The Member this registration is actually for — the one picked
        from the roll, or one matched/created on the fly from the free-text
        name. Call only after is_valid() — mirrors match_or_create_member's
        own (member, how) return shape so a caller can tell the two paths
        apart if it wants to."""
        if self.cleaned_data.get("member"):
            return self.cleaned_data["member"], "existing"
        from members.services.matching import match_or_create_member
        member, how = match_or_create_member(
            self.cleaned_data["member_name"], self.cleaned_data.get("member_phone"))
        return member, how


class HouseholdMemberForm(StyledFormMixin, forms.Form):
    """Add a person to a household registration."""
    member = forms.ModelChoiceField(
        queryset=Member.objects.none(), required=False, label="Church member",
        help_text="Link them where the church knows them — then their details live in "
                  "one place, not two.")
    name = forms.CharField(
        required=False, max_length=120, label="…or a name",
        help_text="For someone not on the church roll (a young child, a parent in the "
                  "village).")
    relationship = forms.ChoiceField(choices=SchemeDependant.Relationship.choices)
    phone = forms.CharField(
        required=False, max_length=20,
        help_text="Optional. A spouse or grown child very often pays dues from their "
                  "OWN phone — recording it here lets that payment be matched "
                  "automatically instead of landing in the unmatched queue.")
    date_of_birth = forms.DateField(required=False,
                                    widget=forms.DateInput(attrs={"type": "date"}))
    registered_on = forms.DateField(initial=dt.date.today,
                                    widget=forms.DateInput(attrs={"type": "date"}))

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["member"].queryset = Member.objects.filter(
            active=True).order_by("name")
        self._style()

    def clean(self):
        cleaned = super().clean()
        if not cleaned.get("member") and not (cleaned.get("name") or "").strip():
            raise forms.ValidationError(
                "Link them to their member record, or give a name.")
        return cleaned


class DependantEditForm(HouseholdMemberForm):
    """Editing an existing dependant, as distinct from adding one.

    `registered_on` is deliberately NOT part of this form — editing a
    dependant must never touch their coverage date (a dependant is covered
    from the day they were registered, never retrospectively; see
    add_dependant's docstring). The parent form makes registered_on required,
    which meant the inline edit form — which correctly omits that field — could
    never validate and silently did nothing. Dropping the requirement here is
    what makes editing a dependant actually save.
    """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields.pop("registered_on", None)


class ExemptionForm(StyledFormMixin, forms.ModelForm):
    class Meta:
        model = MembershipExemption
        fields = ["kind", "from_date", "to_date", "reason", "comments",
                  "exempt_dues", "exempt_levies"]
        widgets = {"from_date": forms.DateInput(attrs={"type": "date"}),
                   "to_date": forms.DateInput(attrs={"type": "date"}),
                   "reason": forms.Textarea(attrs={"rows": 3}),
                   "comments": forms.Textarea(attrs={"rows": 2})}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["from_date"].initial = dt.date.today()
        self._style()


class TransferForm(StyledFormMixin, forms.Form):
    to_member = forms.ModelChoiceField(
        queryset=Member.objects.none(), label="Transfer the membership to",
        help_text="Usually the surviving spouse.")
    on = forms.DateField(initial=dt.date.today,
                         widget=forms.DateInput(attrs={"type": "date"}))
    reason = forms.CharField(widget=forms.Textarea(attrs={"rows": 2}))

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["to_member"].queryset = Member.objects.filter(
            active=True).order_by("name")
        self._style()


class LifecycleForm(StyledFormMixin, forms.Form):
    """Suspend / withdraw / close / record a death. Every one needs a reason."""
    on = forms.DateField(initial=dt.date.today,
                         widget=forms.DateInput(attrs={"type": "date"}))
    reason = forms.CharField(widget=forms.Textarea(attrs={"rows": 2}))

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._style()


# ===========================================================================
# Phase 4 — the contribution engine
# ===========================================================================

from .models import (BenevolentCase, BenevolentContribution, ContributionIntake,
                     ContributionRule, MemberAdjustment)


class IntakeResolveForm(StyledFormMixin, forms.Form):
    """A treasurer says whose the money is."""
    membership = forms.ModelChoiceField(queryset=SchemeMembership.objects.none(),
                                        required=False)
    case = forms.ModelChoiceField(queryset=BenevolentCase.objects.none(),
                                  required=False,
                                  help_text="For a per-case levy.")
    kind = forms.ChoiceField(choices=BenevolentContribution.Kind.choices)
    note = forms.CharField(required=False, max_length=200)

    def __init__(self, *args, item=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.item = item
        if item is not None and item.scheme_id:
            self.fields["membership"].queryset = (
                SchemeMembership.objects.filter(scheme=item.scheme)
                .select_related("member").order_by("member__name"))
            # Cases a levy can still be collected against — which includes
            # paid and closed ones, because that is when members pay.
            cases = (BenevolentCase.objects.filter(
                        scheme=item.scheme,
                        status__in=BenevolentCase.LEVIABLE_STATUSES)
                     .order_by("-event_date"))
            self.fields["case"].queryset = cases
            if item.suggested_membership_id:
                self.fields["membership"].initial = item.suggested_membership_id
            if item.suggested_case_id:
                self.fields["case"].initial = item.suggested_case_id
            elif cases.count() == 1:
                # With one case running there is nothing to choose between, and
                # making the treasurer pick it every time is how levy money ends
                # up unattributed. Still a normal field — it can be changed or
                # cleared before saving.
                self.fields["case"].initial = cases.first().pk
            if item.suggested_kind:
                self.fields["kind"].initial = item.suggested_kind
        self._style()


class ContributionRuleForm(StyledFormMixin, forms.ModelForm):
    class Meta:
        model = ContributionRule
        fields = ["pattern", "match_type", "scheme", "kind", "priority", "active"]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["kind"] = forms.ChoiceField(
            choices=[("", "Let the allocator work it out")]
                    + list(BenevolentContribution.Kind.choices),
            required=False, label="Kind of money")
        self.fields["scheme"].queryset = BenevolentScheme.objects.exclude(
            status=BenevolentScheme.Status.CLOSED)
        self._style()


class AdjustmentForm(StyledFormMixin, forms.Form):
    """A penalty, a waiver, a write-off. No money moves."""
    kind = forms.ChoiceField(choices=MemberAdjustment.Kind.choices)
    amount = forms.DecimalField(max_digits=12, decimal_places=2,
                                min_value=Decimal("0.01"),
                                help_text="Always positive. Whether it adds to or "
                                          "reduces what the member owes follows from "
                                          "the kind.")
    on = forms.DateField(initial=dt.date.today,
                         widget=forms.DateInput(attrs={"type": "date"}))
    period_label = forms.CharField(required=False, max_length=10,
                                   help_text="The dues period this applies to, if any.")
    reason = forms.CharField(widget=forms.Textarea(attrs={"rows": 2}))
    comments = forms.CharField(required=False, widget=forms.Textarea(attrs={"rows": 2}),
                               help_text="Optional. Supplementary context — not a "
                                         "substitute for the reason above.")

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._style()


class RefundForm(StyledFormMixin, forms.Form):
    amount = forms.DecimalField(max_digits=12, decimal_places=2,
                                min_value=Decimal("0.01"))
    date = forms.DateField(initial=dt.date.today,
                           widget=forms.DateInput(attrs={"type": "date"}))
    method = forms.ChoiceField(choices=Expense.Method.choices,
                               initial=Expense.Method.CASH)
    voucher_no = forms.CharField(required=False, max_length=30)
    reason = forms.CharField(widget=forms.Textarea(attrs={"rows": 2}))

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._style()


# ===========================================================================
# Phase 6 — committee membership
# ===========================================================================

from django.contrib.auth.models import User

from .models import CommitteeMember


class CommitteeMemberForm(StyledFormMixin, forms.Form):
    user = forms.ModelChoiceField(
        queryset=User.objects.filter(is_active=True).order_by("first_name", "username"),
        label="Person",
        help_text="Only lists active user accounts — they still need the general "
                  "benevolent-committee right to actually vote once seated here.")
    role = forms.ChoiceField(choices=CommitteeMember.Role.choices,
                             initial=CommitteeMember.Role.MEMBER)

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._style()


class CommitteeRoleForm(StyledFormMixin, forms.Form):
    role = forms.ChoiceField(choices=CommitteeMember.Role.choices)

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._style()


class RemoveSeatForm(StyledFormMixin, forms.Form):
    reason = forms.CharField(required=False, widget=forms.Textarea(attrs={"rows": 2}))

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._style()


# ===========================================================================
# Phase 7 — notification templates
# ===========================================================================

from .models import NotificationTemplate


class NotificationTemplateForm(StyledFormMixin, forms.ModelForm):
    class Meta:
        model = NotificationTemplate
        fields = ["subject", "body", "active"]
        widgets = {"body": forms.Textarea(attrs={"rows": 4})}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        if self.instance.channel != NotificationTemplate.Channel.EMAIL:
            del self.fields["subject"]
        self._style()

import datetime as dt
from django import forms

from accounts.forms import StyledFormMixin
from departments.models import Department
from members.models import Member
from .models import Transaction, AllocationRule


class CashEntryForm(StyledFormMixin, forms.ModelForm):
    fund = forms.CharField(required=False, widget=forms.HiddenInput())

    class Meta:
        model = Transaction
        fields = ["date", "channel", "department", "dev_group", "member", "amount",
                  "reference", "payer_name"]
        widgets = {"date": forms.DateInput(attrs={"type": "date"})}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        from departments.models import DevelopmentGroup
        self.fields["channel"].choices = [
            (Transaction.Channel.CASH, "Cash"),
            (Transaction.Channel.ENVELOPE, "Envelope"),
        ]
        self.fields["department"].queryset = Department.objects.filter(active=True)
        self.fields["department"].required = False
        self.fields["department"].widget = forms.HiddenInput()
        self.fields["dev_group"].required = False
        self.fields["dev_group"].queryset = DevelopmentGroup.objects.filter(
            active=True).order_by("number")
        self.fields["member"].required = False
        self.fields["member"].queryset = Member.objects.filter(active=True)
        self.fields["member"].widget = forms.HiddenInput()
        self.fields["reference"].required = False
        self.fields["payer_name"].required = False
        self.fields["payer_name"].label = "Payer name (free text)"
        self.fields["payer_name"].help_text = (
            "Use only if the giver isn't a saved member — e.g. a visitor or loose offering.")
        if not self.instance.pk and not self.initial.get("date"):
            self.initial["date"] = dt.date.today()
        self._style()
        self.split_fund = None

    def clean(self):
        cleaned = super().clean()
        key = (cleaned.get("fund") or "").strip()
        from giving.models import SplitFund
        if key.startswith("s:"):
            self.split_fund = SplitFund.objects.filter(pk=key[2:]).first()
            if not self.split_fund:
                raise forms.ValidationError("Choose a valid fund.")
        elif key.startswith("d:"):
            cleaned["department"] = Department.objects.filter(pk=key[2:]).first()
        if not cleaned.get("department") and not self.split_fund:
            raise forms.ValidationError("Choose a fund to record this against.")
        dept = cleaned.get("department")
        if dept and dept.category == Department.Category.DEVELOPMENT and not cleaned.get("dev_group"):
            raise forms.ValidationError(
                "This is a development fund — please choose the development group.")
        return cleaned


class QueueResolveForm(StyledFormMixin, forms.Form):
    department = forms.ModelChoiceField(
        queryset=Department.objects.filter(active=True, selectable=True), label="Allocate to fund")
    remember_rule = forms.BooleanField(
        required=False, label="Remember this reference for future imports")

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._style()


class RuleForm(StyledFormMixin, forms.ModelForm):
    class Meta:
        model = AllocationRule
        fields = ["reference", "match_type", "department", "split_fund", "source",
                  "valid_from", "valid_to"]
        widgets = {"valid_from": forms.DateInput(attrs={"type": "date"}),
                   "valid_to": forms.DateInput(attrs={"type": "date"})}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # Only funds a gift can be allocated to directly. Excludes the internal
        # halves of a split offering (selectable=False) — a rule must target the
        # split fund itself (via the split-fund field), never one component half,
        # or split giving lands entirely in the wrong fund.
        self.fields["department"].queryset = Department.objects.filter(
            active=True, selectable=True)
        self.fields["department"].required = False
        self.fields["split_fund"].required = False
        self.fields["valid_from"].required = False
        self.fields["valid_to"].required = False
        self.fields["valid_from"].label = "Valid from (optional)"
        self.fields["valid_to"].label = "Valid to (optional)"
        self.fields["valid_from"].help_text = ("Leave both dates blank for a permanent "
            "rule. A dated rule applies only within its period and overrides a "
            "permanent rule for the same reference.")
        self._style()

    def clean(self):
        cleaned = super().clean()
        if not cleaned.get("department") and not cleaned.get("split_fund"):
            raise forms.ValidationError("Choose a fund or a split fund to allocate to.")
        # a regex rule must be a valid pattern, or it can never match
        if cleaned.get("match_type") == AllocationRule.MatchType.REGEX:
            import re as _re
            ref = (cleaned.get("reference") or "").strip().lower()
            if len(ref) > 60:
                self.add_error("reference", "Keep the pattern under 60 characters.")
            try:
                _re.compile(ref)
            except _re.error as e:
                self.add_error("reference", f"That isn't a valid pattern: {e}")
        if cleaned.get("department") and cleaned.get("split_fund"):
            raise forms.ValidationError("Pick either a fund or a split fund, not both.")
        vf, vt = cleaned.get("valid_from"), cleaned.get("valid_to")
        if vf and vt and vf > vt:
            raise forms.ValidationError("'Valid from' must be on or before 'Valid to'.")
        return cleaned


class TransactionEditForm(StyledFormMixin, forms.ModelForm):
    """Edit/alter a banking (or any) entry."""
    class Meta:
        model = Transaction
        fields = ["date", "channel", "direction", "department", "dev_group",
                  "member", "amount", "reference", "payer_name", "payer_phone",
                  "mpesa_ref", "allocation_status", "manual_receipt"]
        widgets = {"date": forms.DateInput(attrs={"type": "date"})}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["department"].queryset = Department.objects.filter(active=True)
        self.fields["department"].required = False
        self.fields["member"].queryset = Member.objects.filter(active=True)
        for f in ("member", "dev_group", "reference", "payer_name",
                  "payer_phone", "mpesa_ref"):
            self.fields[f].required = False
        self._style()

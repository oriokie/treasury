from django import forms
from accounts.forms import StyledFormMixin
from .models import Department, DevelopmentGroup, FundLeader


class DepartmentForm(StyledFormMixin, forms.ModelForm):
    class Meta:
        model = Department
        fields = ["name", "parent", "fund_type", "category", "opening_balance",
                  "children_in_expenses", "show_in_expenses", "collection_only",
                  "income_account"]
        # 'active' is derived from lifecycle status on save; use the close/reopen
        # workflow (not this form) to change whether the account is open.

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # only top-level funds may be parents (one level of nesting), never self
        qs = Department.objects.filter(parent__isnull=True)
        if self.instance and self.instance.pk:
            qs = qs.exclude(pk=self.instance.pk)
        self.fields["parent"].queryset = qs
        self.fields["parent"].required = False
        self.fields["parent"].empty_label = "— none (top-level fund) —"

        # `opening_balance` is the FOUNDING figure — what this fund held on the
        # day the church started using this system. It is NOT a yearly opening:
        # every later year's opening is DERIVED from it (founding + all movement
        # before that year), and year-end close never writes it.
        #
        # So editing it does not adjust "the opening" — it silently rewrites the
        # balance of this fund in EVERY year the church has ever recorded,
        # backwards. The budget page was locked against this; this form was the
        # other way in, and had the same hole.
        #
        # Once a year has been formally closed, the history it underpins is final
        # and the field is read-only. Before that, during first-time setup, it is
        # editable — but says plainly what it is.
        from core.models import YearEndClose
        locked = YearEndClose.objects.exists()
        f = self.fields["opening_balance"]
        f.label = "Founding balance (brought forward at first use)"
        if locked:
            last = YearEndClose.objects.order_by("-year").first()
            f.disabled = True
            f.help_text = (
                f"Locked: {last.year} has been closed, so the history this figure "
                f"underpins is final. Each year's opening balance is calculated "
                f"from it — nothing is carried forward by hand.")
        else:
            f.help_text = (
                "What this fund held on the day the church started using this "
                "system — a ONE-TIME figure, not a yearly one. Every later year's "
                "opening is calculated from it, so changing it shifts this fund's "
                "balance in every period. Set it during first-time setup and then "
                "leave it alone; it locks automatically the first time a year is "
                "closed.")
        self._style()


class DevelopmentGroupForm(StyledFormMixin, forms.ModelForm):
    class Meta:
        model = DevelopmentGroup
        fields = ["number", "name", "target", "leader_name", "leader_email", "active"]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # Surface the auto-assigned DEV… bank code so treasurers can share it;
        # it is not editable here (regeneration would break printed cards).
        if self.instance and self.instance.pk and self.instance.match_code:
            self.fields["match_code_display"] = forms.CharField(
                label="Bank reference code",
                required=False,
                initial=self.instance.match_code,
                disabled=True,
                help_text="Put this code in a bank / M-Pesa reference to "
                          "auto-allocate to this development group.")
        self._style()


class FundLeaderForm(StyledFormMixin, forms.ModelForm):
    class Meta:
        model = FundLeader
        fields = ["name", "phone", "email", "active", "notes",
                  "departments", "groups"]
        widgets = {
            "departments": forms.CheckboxSelectMultiple,
            "groups": forms.CheckboxSelectMultiple,
            "notes": forms.TextInput,
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["departments"].queryset = (
            Department.objects.filter(active=True).order_by("name"))
        self.fields["departments"].required = False
        self.fields["departments"].help_text = (
            "Funds this leader receives statements for. Tick a parent fund to "
            "include its sub-accounts as separate emails.")
        self.fields["groups"].queryset = (
            DevelopmentGroup.objects.filter(active=True).order_by("number"))
        self.fields["groups"].required = False
        self.fields["groups"].label = "Development groups"
        self.fields["phone"].widget.attrs["placeholder"] = "07XXXXXXXX"
        self.fields["email"].widget.attrs["placeholder"] = "optional@example.com"
        self._style()

    def clean_phone(self):
        from members.models import normalize_phone
        raw = (self.cleaned_data.get("phone") or "").strip()
        if not raw:
            return ""
        norm = normalize_phone(raw)
        if not norm:
            raise forms.ValidationError(
                "Enter a valid Kenyan mobile number (e.g. 0712 345 678).")
        return norm

    def clean(self):
        cleaned = super().clean()
        if not cleaned.get("phone") and not cleaned.get("email"):
            raise forms.ValidationError(
                "Add at least a phone number or an email so statements can be sent.")
        return cleaned


class FundLeaderSendForm(StyledFormMixin, forms.Form):
    start = forms.DateField(widget=forms.DateInput(attrs={"type": "date"}))
    end = forms.DateField(widget=forms.DateInput(attrs={"type": "date"}))
    leader_ids = forms.MultipleChoiceField(
        required=False, widget=forms.CheckboxSelectMultiple)

    def __init__(self, *args, leaders=None, **kwargs):
        super().__init__(*args, **kwargs)
        leaders = leaders or FundLeader.objects.none()
        self.fields["leader_ids"].choices = [
            (str(l.pk), l.name) for l in leaders]
        self._style()

    def clean(self):
        cleaned = super().clean()
        start, end = cleaned.get("start"), cleaned.get("end")
        if start and end and end < start:
            raise forms.ValidationError("End date must be on or after the start.")
        return cleaned

    def selected_leaders(self):
        ids = [int(x) for x in self.cleaned_data.get("leader_ids") or []
               if str(x).isdigit()]
        return FundLeader.objects.filter(pk__in=ids, active=True)

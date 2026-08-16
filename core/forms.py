from django import forms
from accounts.forms import StyledFormMixin
from .models import SiteConfig


class SiteConfigForm(StyledFormMixin, forms.ModelForm):
    class Meta:
        model = SiteConfig
        # DENYLIST, not allowlist (recommendation #74a). A ModelForm bound with
        # `fields = [...]` silently omits any model field not named in the list,
        # so adding a setting to SiteConfig used to require also remembering to
        # add it here — and forgetting left the setting unreachable with no error.
        # `exclude` inverts that: every editable field binds automatically, and
        # only the deliberately-excluded few are named. `board_config` is edited
        # through its own board-configuration screen, not this general form;
        # id/updated_at are not user-editable. A test
        # (test_siteconfig_form_binds_every_field) asserts this stays true.
        exclude = ["id", "updated_at", "board_config"]
        widgets = {"sms_receipt_template": forms.Textarea(attrs={"rows": 3}),
                   "receipt_message": forms.Textarea(attrs={"rows": 3}),
                   "pledge_thanks_template": forms.Textarea(attrs={"rows": 2}),
                   "pledge_reminder_template": forms.Textarea(attrs={"rows": 2}),
                   "pledge_fulfilled_template": forms.Textarea(attrs={"rows": 2}),
                   "telegram_envelope_funds": forms.CheckboxSelectMultiple(),
                   "lcb_departments": forms.CheckboxSelectMultiple()}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._style()
        self.fields["sms_api_key"].widget = forms.PasswordInput(
            render_value=True, attrs={"class": "field"})
        self.fields["llm_api_key"].widget = forms.PasswordInput(
            render_value=True, attrs={"class": "field"})
        self.fields["telegram_bot_token"].widget = forms.PasswordInput(
            render_value=True, attrs={"class": "field"})
        from departments.models import Department
        self.fields["telegram_envelope_funds"].queryset = (
            Department.objects.filter(active=True).select_related("parent").order_by("name"))
        self.fields["telegram_envelope_funds"].required = False
        # LCB department picker — local funds only
        self.fields["lcb_departments"].queryset = (
            Department.objects.filter(fund_type=Department.FundType.LOCAL)
            .order_by("name"))
        self.fields["lcb_departments"].required = False
        self.fields["asset_depr_method"] = forms.ChoiceField(
            label="Default depreciation method",
            choices=[("STRAIGHT", "Straight-line"), ("REDUCING", "Reducing balance"),
                     ("NONE", "Not depreciated")],
            initial=self.instance.asset_depr_method or "STRAIGHT", required=False)
        self.fields["asset_depr_method"].widget.attrs.update({"class": "field--select"})


class UserPreferenceForm(StyledFormMixin, forms.ModelForm):
    """Per-user appearance & workspace preferences.

    Uses ``exclude`` rather than a ``fields`` allowlist (the frozen-allowlist
    trap, rec #114): a preference field added to the model later appears here
    automatically instead of silently vanishing from the page. Excluded are
    only the non-form fields (relations, JSON blobs managed by their own UIs,
    timestamps)."""
    class Meta:
        from core.models import UserPreference
        model = UserPreference
        exclude = ["user", "dashboard_widgets", "table_state", "updated_at"]

    def __init__(self, *args, **kwargs):
        from core.models import UserPreference
        super().__init__(*args, **kwargs)
        self._style()
        self.fields["landing_page"] = forms.ChoiceField(
            choices=UserPreference.LANDING_CHOICES, required=False,
            initial=self.instance.landing_page or "dashboard")
        self.fields["landing_page"].widget.attrs.update({"class": "field field--select"})
        self.fields["accent_custom"].widget = forms.TextInput(
            attrs={"type": "color", "class": "field field--color"})
        self.fields["rows_per_page"].widget.attrs.update(
            {"min": 10, "max": 200, "step": 5})
        self.fields["toast_duration"].widget.attrs.update(
            {"min": 2, "max": 30, "step": 1})

    def clean_rows_per_page(self):
        n = self.cleaned_data.get("rows_per_page") or 25
        return max(5, min(200, n))

    def clean_toast_duration(self):
        n = self.cleaned_data.get("toast_duration") or 6
        return max(2, min(30, n))

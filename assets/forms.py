from django import forms
from accounts.forms import StyledFormMixin
from departments.models import Department
from .models import FixedAsset, DepreciationRule, Acquisition


class FixedAssetForm(StyledFormMixin, forms.ModelForm):
    """The register entry plus how the asset was acquired.

    Every asset records its provenance (EAM §9.3), so the acquisition is never
    guessed later: a purchase is paid through an expense, a donation is
    recognised at fair value, and an opening-balance asset predates the system.
    """
    acq_source = forms.ChoiceField(
        label="How acquired", choices=Acquisition.Source.choices,
        initial=Acquisition.Source.PURCHASE, required=False)
    donor_name = forms.CharField(
        label="Donor", max_length=120, required=False,
        help_text="For a donated asset: who gave it.")

    class Meta:
        model = FixedAsset
        fields = ["name", "asset_class", "category", "status", "tag", "serial_no",
                  "acquired_on", "in_service_on", "cost", "salvage_value",
                  "method", "rate", "department", "location_fk", "location",
                  "custodian", "reference", "notes"]
        widgets = {"acquired_on": forms.DateInput(attrs={"type": "date"}),
                   "in_service_on": forms.DateInput(attrs={"type": "date"}),
                   "notes": forms.Textarea(attrs={"rows": 2})}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["department"].queryset = Department.objects.filter(active=True).select_related("parent")
        self.fields["department"].required = False
        self.fields["method"].required = False
        self.fields["rate"].required = False
        self.fields["method"].help_text = "Leave blank to use the category rule / default."
        for f in ("asset_class", "status", "tag", "serial_no", "in_service_on",
                  "location_fk", "custodian"):
            self.fields[f].required = False
        existing = getattr(self.instance, "pk", None)
        if existing:
            # provenance is recorded once, on the way in
            for f in ("acq_source", "donor_name"):
                self.fields[f].widget = forms.HiddenInput()
        self._style()

    def clean_cost(self):
        """Keep small purchases off the register when a threshold is set."""
        from core.models import SiteConfig
        cost = self.cleaned_data.get("cost")
        floor = SiteConfig.get().capitalisation_threshold or 0
        if cost is not None and floor and cost < floor and not getattr(self.instance, "pk", None):
            raise forms.ValidationError(
                f"Below the capitalisation threshold of {floor:,.2f} — record this as "
                f"a running cost (an expense) rather than an asset.")
        return cost


class DepreciationRuleForm(StyledFormMixin, forms.ModelForm):
    class Meta:
        model = DepreciationRule
        fields = ["category", "method", "rate"]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["category"] = forms.ChoiceField(choices=FixedAsset.Category.choices)
        self._style()

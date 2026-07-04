from django import forms
from accounts.forms import StyledFormMixin
from departments.models import Department
from .models import FixedAsset, DepreciationRule


class FixedAssetForm(StyledFormMixin, forms.ModelForm):
    class Meta:
        model = FixedAsset
        fields = ["name", "category", "acquired_on", "cost", "salvage_value",
                  "method", "rate", "department", "location", "reference", "notes"]
        widgets = {"acquired_on": forms.DateInput(attrs={"type": "date"}),
                   "notes": forms.Textarea(attrs={"rows": 2})}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["department"].queryset = Department.objects.filter(active=True).select_related("parent")
        self.fields["department"].required = False
        self.fields["method"].required = False
        self.fields["rate"].required = False
        self.fields["method"].help_text = "Leave blank to use the category rule / default."
        self._style()


class DepreciationRuleForm(StyledFormMixin, forms.ModelForm):
    class Meta:
        model = DepreciationRule
        fields = ["category", "method", "rate"]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["category"] = forms.ChoiceField(choices=FixedAsset.Category.choices)
        self._style()

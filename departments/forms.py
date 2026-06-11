from django import forms
from accounts.forms import StyledFormMixin
from .models import Department, DevelopmentGroup


class DepartmentForm(StyledFormMixin, forms.ModelForm):
    class Meta:
        model = Department
        fields = ["name", "parent", "fund_type", "category", "opening_balance",
                  "children_in_expenses", "show_in_expenses", "active"]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # only top-level funds may be parents (one level of nesting), never self
        qs = Department.objects.filter(parent__isnull=True)
        if self.instance and self.instance.pk:
            qs = qs.exclude(pk=self.instance.pk)
        self.fields["parent"].queryset = qs
        self.fields["parent"].required = False
        self.fields["parent"].empty_label = "— none (top-level fund) —"
        self._style()


class DevelopmentGroupForm(StyledFormMixin, forms.ModelForm):
    class Meta:
        model = DevelopmentGroup
        fields = ["number", "name", "target", "leader_name", "leader_email", "active"]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._style()

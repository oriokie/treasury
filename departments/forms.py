from django import forms
from accounts.forms import StyledFormMixin
from .models import Department, DevelopmentGroup


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
        self._style()

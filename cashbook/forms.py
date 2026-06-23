from decimal import Decimal
import datetime as dt
from django import forms
from accounts.forms import StyledFormMixin
from departments.models import Department
from .models import Expense


class ExpenseForm(StyledFormMixin, forms.ModelForm):
    charge = forms.DecimalField(
        required=False, min_value=0, label="Transaction charge (M-Pesa / bank)",
        help_text="Charge incurred sending this payment. Recorded as a separate "
                  "bank-charge expense on the same fund.")

    class Meta:
        model = Expense
        fields = ["date", "department", "description", "amount", "category",
                  "expenditure_type", "capitalized_asset",
                  "claimant", "method", "voucher_no", "paid_from_petty_cash",
                  "budget_line"]
        widgets = {"date": forms.DateInput(attrs={"type": "date"})}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        from departments.models import expense_departments
        from cashbook.models import category_choices
        self.fields["category"].choices = category_choices()
        ids = [d.id for d in expense_departments()]
        self.fields["department"].queryset = Department.objects.filter(pk__in=ids)
        self.fields["department"].widget = forms.HiddenInput()
        if not self.instance.pk and not self.initial.get("date"):
            self.initial["date"] = dt.date.today()
        self.fields["claimant"].widget.attrs.update({
            "autocomplete": "off", "class": "field claimant-ac",
            "placeholder": "Start typing a member's name…"})
        self.fields["method"].widget.attrs.update({"class": "field--select method-select"})
        if "budget_line" in self.fields:
            from cashbook.models import BudgetLine
            self.fields["budget_line"].required = False
            self.fields["budget_line"].label = "Budget item"
            self.fields["budget_line"].queryset = BudgetLine.objects.all()
            self.fields["budget_line"].widget.attrs.update(
                {"class": "field--select", "id": "id_budget_line"})
        self.fields["expenditure_type"].widget.attrs.update({"class": "field--select"})
        self.fields["expenditure_type"].required = False
        self.fields["expenditure_type"].initial = Expense.ExpenditureType.RECURRENT
        if "capitalized_asset" in self.fields:
            self.fields["capitalized_asset"].required = False
            from assets.models import FixedAsset
            self.fields["capitalized_asset"].queryset = FixedAsset.objects.filter(disposed=False)
            self.fields["capitalized_asset"].widget.attrs.update({"class": "field--select"})
        if "paid_from_petty_cash" in self.fields:
            self.fields["paid_from_petty_cash"].required = False
            self.fields["paid_from_petty_cash"].label = "Paid from petty cash float"
            self.fields["paid_from_petty_cash"].help_text = (
                "Tick if this was paid out of the petty cash float — it reduces the float.")
        self._style()

    def clean_expenditure_type(self):
        return self.cleaned_data.get("expenditure_type") or Expense.ExpenditureType.RECURRENT


class FundTransferForm(StyledFormMixin, forms.ModelForm):
    class Meta:
        from .models import FundTransfer
        model = FundTransfer
        fields = ["date", "source", "destination", "amount", "reason", "reference"]
        widgets = {"date": forms.DateInput(attrs={"type": "date"})}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # only local (non-trust) active funds may be transferred between
        local = Department.objects.filter(active=True, is_trust=False).order_by("name")
        self.fields["source"].queryset = local
        self.fields["destination"].queryset = local
        self.fields["reason"].required = False
        self.fields["reference"].required = False
        self._style()

    def clean(self):
        cleaned = super().clean()
        from django.core.exceptions import ValidationError
        src, dst = cleaned.get("source"), cleaned.get("destination")
        if src and dst and src == dst:
            raise ValidationError("Source and destination funds must be different.")
        for fund, lbl in ((src, "source"), (dst, "destination")):
            if fund and fund.is_trust:
                raise ValidationError(f"Trust funds cannot be the {lbl} of a transfer.")
        return cleaned


class RecurringExpenseForm(StyledFormMixin, forms.ModelForm):
    class Meta:
        from .models import RecurringExpense
        model = RecurringExpense
        fields = ["description", "department", "category", "amount", "frequency",
                  "day_of_month", "claimant", "method", "start_date", "end_date", "active"]
        widgets = {"start_date": forms.DateInput(attrs={"type": "date"}),
                   "end_date": forms.DateInput(attrs={"type": "date"})}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        from departments.models import Department
        self.fields["department"].queryset = Department.objects.filter(
            active=True, is_trust=False).order_by("name")
        self.fields["end_date"].required = False
        self.fields["claimant"].required = False
        for f in ("category", "frequency", "method", "department"):
            self.fields[f].widget.attrs.update({"class": "field--select"})
        self._style()


class PettyCashTopUpForm(StyledFormMixin, forms.Form):
    """Add cash to the petty cash float."""
    date = forms.DateField(widget=forms.DateInput(attrs={"type": "date"}))
    amount = forms.DecimalField(max_digits=12, decimal_places=2, min_value=Decimal("0.01"))
    note = forms.CharField(max_length=200, required=False)

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        import datetime as _dt
        self.fields["date"].initial = _dt.date.today()
        self._style()


class PettyCashDisbursementForm(StyledFormMixin, forms.Form):
    """Record a small payment made out of petty cash."""
    date = forms.DateField(widget=forms.DateInput(attrs={"type": "date"}))
    description = forms.CharField(max_length=200)
    amount = forms.DecimalField(max_digits=12, decimal_places=2, min_value=Decimal("0.01"))
    department = forms.ModelChoiceField(queryset=None, label="Charge to fund / ministry")
    category = forms.ChoiceField(choices=[])
    method = forms.ChoiceField(choices=[], required=False,
                               label="Paid by", initial="CASH")
    claimant = forms.CharField(max_length=120, required=False)
    voucher_no = forms.CharField(max_length=30, required=False, label="Voucher no")
    charge = forms.DecimalField(
        required=False, min_value=0, label="Transaction charge (M-Pesa / bank)",
        help_text="If the float is held on M-Pesa/bank: any withdrawal/transfer charge. "
                  "It's recorded as a linked bank-charge disbursement and also reduces the float.")

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        from cashbook.models import Expense
        import datetime as _dt
        self.fields["category"].choices = [
            c for c in Expense.Category.choices if c[0] != Expense.Category.REMITTANCE]
        self.fields["category"].initial = Expense.Category.OTHER
        self.fields["method"].choices = Expense.Method.choices
        from departments.models import Department
        self.fields["department"].queryset = Department.objects.filter(
            active=True, is_trust=False).order_by("name")
        for f in ("category", "department", "method"):
            self.fields[f].widget.attrs.update({"class": "field--select"})
        self.fields["date"].initial = _dt.date.today()
        self._style()


class PayableForm(StyledFormMixin, forms.Form):
    """Record a credit purchase — goods/services received now, paid later."""
    date = forms.DateField(widget=forms.DateInput(attrs={"type": "date"}), label="Date incurred")
    vendor = forms.CharField(max_length=120)
    description = forms.CharField(max_length=200)
    amount = forms.DecimalField(max_digits=12, decimal_places=2, min_value=Decimal("0.01"))
    department = forms.ModelChoiceField(queryset=None, label="Charge to fund / ministry")
    category = forms.ChoiceField(choices=[])
    due_date = forms.DateField(required=False, widget=forms.DateInput(attrs={"type": "date"}))

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        import datetime as _dt
        from cashbook.models import Expense
        from departments.models import Department
        self.fields["category"].choices = [c for c in Expense.Category.choices
                                            if c[0] != Expense.Category.REMITTANCE]
        self.fields["department"].queryset = Department.objects.filter(
            active=True, is_trust=False).order_by("name")
        self.fields["date"].initial = _dt.date.today()
        for f in ("category", "department"):
            self.fields[f].widget.attrs.update({"class": "field--select"})
        self._style()


class AccrualForm(StyledFormMixin, forms.Form):
    """Record an expense incurred but not yet invoiced/paid."""
    date = forms.DateField(widget=forms.DateInput(attrs={"type": "date"}), label="Period end")
    description = forms.CharField(max_length=200)
    amount = forms.DecimalField(max_digits=12, decimal_places=2, min_value=Decimal("0.01"))
    department = forms.ModelChoiceField(queryset=None, label="Fund / ministry")
    category = forms.ChoiceField(choices=[])

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        import datetime as _dt
        from cashbook.models import Expense
        from departments.models import Department
        self.fields["category"].choices = [c for c in Expense.Category.choices
                                            if c[0] != Expense.Category.REMITTANCE]
        self.fields["department"].queryset = Department.objects.filter(
            active=True, is_trust=False).order_by("name")
        self.fields["date"].initial = _dt.date.today()
        for f in ("category", "department"):
            self.fields[f].widget.attrs.update({"class": "field--select"})
        self._style()


class PrepaymentForm(StyledFormMixin, forms.Form):
    """Record cash paid in advance spanning future periods."""
    date = forms.DateField(widget=forms.DateInput(attrs={"type": "date"}), label="Date paid")
    description = forms.CharField(max_length=200)
    amount = forms.DecimalField(max_digits=12, decimal_places=2, min_value=Decimal("0.01"))
    department = forms.ModelChoiceField(queryset=None, label="Paid from fund / ministry")
    category = forms.ChoiceField(choices=[])
    months = forms.IntegerField(min_value=1, max_value=120, initial=12,
                                label="Spread over (months)")
    start_date = forms.DateField(widget=forms.DateInput(attrs={"type": "date"}),
                                 label="First month covered")

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        import datetime as _dt
        from cashbook.models import Expense
        from departments.models import Department
        self.fields["category"].choices = [c for c in Expense.Category.choices
                                            if c[0] != Expense.Category.REMITTANCE]
        self.fields["department"].queryset = Department.objects.filter(
            active=True, is_trust=False).order_by("name")
        self.fields["date"].initial = _dt.date.today()
        self.fields["start_date"].initial = _dt.date.today()
        for f in ("category", "department"):
            self.fields[f].widget.attrs.update({"class": "field--select"})
        self._style()

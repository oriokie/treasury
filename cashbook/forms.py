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
                  # `vendor` groups this payment under a supplier's account;
                  # `payee` stays as what the voucher said. Both, not either —
                  # see vendors.models for why the free text is kept.
                  "vendor", "claimant", "payee", "method", "voucher_no",
                  "paid_from_petty_cash", "budget_line"]
        widgets = {"date": forms.DateInput(attrs={"type": "date"})}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        from departments.models import expense_departments
        from cashbook.models import category_choices
        self.fields["category"].choices = category_choices()
        # Suppliers, archived ones excluded — an archived supplier should not
        # appear in a picker, though the records that already name one still
        # resolve.
        from vendors.models import Vendor
        self.fields["vendor"].queryset = (
            Vendor.objects.exclude(status=Vendor.Status.ARCHIVED).order_by("name"))
        self.fields["vendor"].required = False
        self.fields["vendor"].label = "Supplier"
        self.fields["vendor"].help_text = (
            "Optional. Choosing one puts this payment on that supplier's account.")
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

    def clean_date(self):
        from core.utils import reject_far_future_date
        d = self.cleaned_data["date"]
        reject_far_future_date(d, field_label="expense date")
        return d

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
        local = Department.objects.filter(active=True, is_trust=False).select_related("parent").order_by("name")
        self.fields["source"].queryset = local
        self.fields["destination"].queryset = local
        self.fields["reason"].required = False
        self.fields["reference"].required = False
        self._style()

    def clean(self):
        cleaned = super().clean()
        from django.core.exceptions import ValidationError
        from core.utils import reject_far_future_date
        if cleaned.get("date"):
            reject_far_future_date(cleaned["date"], field_label="transfer date")
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
        # Everything an Expense carries, plus the two things that make it a
        # schedule: frequency and end date. Ordered as the expense form is, so
        # the two screens read the same way.
        fields = ["description", "department", "category", "expenditure_type",
                  "amount", "vendor", "claimant", "payee", "method",
                  "voucher_no", "paid_from_petty_cash", "budget_line",
                  "frequency", "day_of_month", "start_date", "end_date", "active"]
        widgets = {"start_date": forms.DateInput(attrs={"type": "date"}),
                   "end_date": forms.DateInput(attrs={"type": "date"})}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        from departments.models import Department
        self.fields["department"].queryset = Department.objects.filter(
            active=True, is_trust=False).select_related("parent").order_by("name")
        from vendors.models import Vendor
        self.fields["end_date"].required = False
        self.fields["claimant"].required = False
        for name in ("vendor", "payee", "voucher_no", "budget_line",
                     "expenditure_type"):
            self.fields[name].required = False
        self.fields["vendor"].queryset = (
            Vendor.objects.exclude(status=Vendor.Status.ARCHIVED).order_by("name"))
        self.fields["vendor"].label = "Supplier"
        for f in ("category", "frequency", "method", "department", "vendor",
                  "expenditure_type", "budget_line"):
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


# NOTE: PettyCashDisbursementForm was removed here. It wrote an ordinary
# Expense with paid_from_petty_cash=True — exactly what ExpenseForm writes
# when that box is ticked — but could not attach a receipt, set an
# expenditure type or a budget line, and had its own approval shortcut. Two
# forms for one row, and the lesser one at that. The petty cash page now
# links to the expense form with ?petty=1.



class PayableForm(StyledFormMixin, forms.Form):
    """Record a credit purchase — goods/services received now, paid later."""
    date = forms.DateField(widget=forms.DateInput(attrs={"type": "date"}), label="Date incurred")
    supplier = forms.ModelChoiceField(
        queryset=None, required=False, label="Supplier",
        help_text="Choose from the register to group this bill with the "
                  "supplier's other invoices. Leave blank for a one-off.")
    vendor = forms.CharField(
        max_length=120, required=False, label="Name on the invoice",
        help_text="Filled from the supplier if you leave it blank. Change it "
                  "only if the invoice reads differently — this records what "
                  "the document said.")
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
        from vendors.models import Vendor
        self.fields["supplier"].queryset = (
            Vendor.objects.exclude(status=Vendor.Status.ARCHIVED).order_by("name"))
        self.fields["category"].choices = [c for c in Expense.Category.choices
                                            if c[0] != Expense.Category.REMITTANCE]
        self.fields["department"].queryset = Department.objects.filter(
            active=True, is_trust=False).select_related("parent").order_by("name")
        self.fields["date"].initial = _dt.date.today()
        for f in ("category", "department", "supplier"):
            self.fields[f].widget.attrs.update({"class": "field--select"})
        self._style()

    def clean(self):
        """One of the two names must be present, and the supplier fills the gap.

        `vendor` is what the invoice said and stays the record of that; but
        making a treasurer type it again when they have just picked the supplier
        from a list is the kind of friction that stops the register being used
        at all. So a blank `vendor` is filled from the chosen supplier, and only
        a bill with neither is refused.
        """
        cleaned = super().clean()
        supplier = cleaned.get("supplier")
        vendor = (cleaned.get("vendor") or "").strip()
        if not vendor and supplier is not None:
            cleaned["vendor"] = supplier.name[:120]
        elif not vendor:
            self.add_error(
                "vendor",
                "Say who this is owed to — choose a supplier or type a name.")

        # Terms on the supplier imply the due date, so the treasurer does not
        # work it out. Only when they have not set one themselves.
        if supplier is not None and not cleaned.get("due_date") and cleaned.get("date"):
            cleaned["due_date"] = supplier.due_date_for(cleaned["date"])
        return cleaned


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
            active=True, is_trust=False).select_related("parent").order_by("name")
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
            active=True, is_trust=False).select_related("parent").order_by("name")
        self.fields["date"].initial = _dt.date.today()
        self.fields["start_date"].initial = _dt.date.today()
        for f in ("category", "department"):
            self.fields[f].widget.attrs.update({"class": "field--select"})
        self._style()

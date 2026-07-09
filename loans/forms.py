import datetime as dt
from decimal import Decimal

from django import forms

from accounts.forms import StyledFormMixin
from cashbook.models import Expense
from departments.models import Department

from .models import Lender, Loan, LoanAttachment, LoanNarrationPattern


def _local_funds():
    return (Department.objects.filter(active=True)
            .exclude(fund_type=Department.FundType.TRUST).order_by("name"))


class LenderForm(StyledFormMixin, forms.ModelForm):
    class Meta:
        model = Lender
        fields = ["name", "phone", "email", "national_id", "address",
                  "member", "status", "notes"]
        widgets = {"notes": forms.Textarea(attrs={"rows": 3})}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["member"].required = False
        self.fields["member"].help_text = (
            "Only if this lender is a church member — never linked automatically.")
        self._style()

    def clean(self):
        """Hard-block exact phone / national-ID duplicates (merge instead);
        same-name-only lookalikes are allowed but surfaced by the view as a
        warning with a merge link."""
        cleaned = super().clean()
        from members.models import normalize_phone
        nid = (cleaned.get("national_id") or "").strip()
        ph = normalize_phone(cleaned.get("phone"))
        live = Lender.objects.filter(merged_into__isnull=True)
        if self.instance.pk:
            live = live.exclude(pk=self.instance.pk)
        if nid:
            dup = live.filter(national_id=nid).first()
            if dup:
                raise forms.ValidationError(
                    f"A lender with that national ID already exists: {dup.name}. "
                    f"Use merge instead of creating a duplicate.")
        if ph:
            dup = live.filter(phone=ph).first()
            if dup:
                raise forms.ValidationError(
                    f"A lender with that phone number already exists: {dup.name}. "
                    f"Use merge instead of creating a duplicate.")
        return cleaned


class LoanForm(StyledFormMixin, forms.ModelForm):
    class Meta:
        model = Loan
        fields = ["lender", "loan_type", "fund", "project", "purpose",
                  "principal_amount", "interest_rate", "interest_method",
                  "loan_date", "maturity_date", "status", "notes"]
        widgets = {"loan_date": forms.DateInput(attrs={"type": "date"}),
                   "maturity_date": forms.DateInput(attrs={"type": "date"}),
                   "notes": forms.Textarea(attrs={"rows": 3})}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["fund"].queryset = _local_funds()
        self.fields["fund"].help_text = (
            "Loans finance local funds only — lending to a trust (conference) "
            "fund is not supported.")
        self.fields["lender"].queryset = Lender.objects.filter(
            merged_into__isnull=True).order_by("name")
        # status on the form only offers the two hand-set states; the retired
        # states are derived from transactions, never picked by hand
        self.fields["status"].choices = [
            (Loan.Status.DRAFT, "Draft"), (Loan.Status.ACTIVE, "Active")]
        if not self.instance.pk and not self.initial.get("loan_date"):
            self.initial["loan_date"] = dt.date.today()
        self._style()

    def clean_status(self):
        val = self.cleaned_data["status"]
        if self.instance.pk and self.instance.status not in (
                Loan.Status.DRAFT, Loan.Status.ACTIVE):
            return self.instance.status     # retired states are never editable
        return val

    def clean(self):
        cleaned = super().clean()
        if self.instance.pk and not self.instance.is_editable:
            raise forms.ValidationError(
                "This loan is completed / converted / written off and can no "
                "longer be edited.")
        ld, md = cleaned.get("loan_date"), cleaned.get("maturity_date")
        if ld and md and md < ld:
            raise forms.ValidationError("Maturity date cannot be before the loan date.")
        return cleaned


class MoneyForm(StyledFormMixin, forms.Form):
    """Shared shape for receipt / repayment / interest / retire forms."""
    date = forms.DateField(widget=forms.DateInput(attrs={"type": "date"}),
                           initial=dt.date.today)
    amount = forms.DecimalField(min_value=Decimal("0.01"), decimal_places=2,
                                max_digits=12)
    note = forms.CharField(required=False, max_length=200)

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._style()


class ReceiptForm(MoneyForm):
    destination = forms.ChoiceField(
        choices=[("BANK", "Bank / M-Pesa account"), ("PETTY", "Petty cash float")],
        initial="BANK", required=False, label="Received into",
        help_text="Where the loan money actually landed. Petty cash raises the "
                  "petty float; either way the fund's available cash rises.")
    reference = forms.CharField(required=False, max_length=40,
        label="Bank reference (optional)",
        help_text="Core reference / receipt no. if this arrived by bank and was "
                  "not already imported from a statement.")


class RepaymentForm(MoneyForm):
    paid_from = forms.ChoiceField(
        choices=[("BANK", "Bank / M-Pesa account"), ("PETTY", "Petty cash float")],
        initial="BANK", required=False, label="Paid from",
        help_text="Petty cash reduces the petty float; either way Loans payable "
                  "and the fund's cash both fall.")
    method = forms.ChoiceField(choices=Expense.Method.choices,
                               initial=Expense.Method.BANK)
    voucher_no = forms.CharField(required=False, max_length=30)
    bank_transaction_id = forms.IntegerField(
        required=False, widget=forms.HiddenInput,
        help_text="Link the bank DEBIT row this repayment appears as, if imported.")


class InterestForm(RepaymentForm):
    pass


class RetireForm(MoneyForm):
    """Convert to donation / write off."""


class PatternForm(StyledFormMixin, forms.ModelForm):
    class Meta:
        model = LoanNarrationPattern
        fields = ["pattern", "match_type", "kind", "fund", "active"]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["fund"].required = False
        self.fields["fund"].queryset = _local_funds()
        self._style()


class AttachmentForm(StyledFormMixin, forms.ModelForm):
    class Meta:
        model = LoanAttachment
        fields = ["file", "label"]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._style()


class LinkMemberForm(StyledFormMixin, forms.Form):
    member_id = forms.IntegerField(widget=forms.HiddenInput)

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._style()

from django import forms
from accounts.forms import StyledFormMixin


class UploadForm(StyledFormMixin, forms.Form):
    file = forms.FileField(
        label="Statement file",
        help_text="Bank/M-Pesa statement as .csv, .xls or .xlsx.",
    )
    bank_account = forms.ModelChoiceField(
        queryset=None, required=False, label="Bank account",
        help_text="Which account this statement is for. Defaults to the main account.")

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        from statements.models import BankAccount
        qs = BankAccount.objects.filter(active=True)
        self.fields["bank_account"].queryset = qs
        self.fields["bank_account"].initial = BankAccount.get_default()
        if not qs.exists():
            # single-account churches: hide the selector entirely (non-disruptive)
            self.fields.pop("bank_account")
        self._style()

    def clean_file(self):
        f = self.cleaned_data["file"]
        if not f.name.lower().endswith((".csv", ".xls", ".xlsx")):
            raise forms.ValidationError("Please upload a .csv, .xls or .xlsx file.")
        if f.size > 20 * 1024 * 1024:
            raise forms.ValidationError("File too large (max 20 MB).")
        return f


from .models import BankReconciliation, ReconciliationItem


class BankReconciliationForm(StyledFormMixin, forms.ModelForm):
    class Meta:
        model = BankReconciliation
        fields = ["statement_date", "bank_balance", "book_balance", "notes"]
        widgets = {"statement_date": forms.DateInput(attrs={"type": "date"}),
                   "notes": forms.Textarea(attrs={"rows": 2})}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["book_balance"].required = False
        self._style()


class ReconciliationItemForm(StyledFormMixin, forms.ModelForm):
    class Meta:
        model = ReconciliationItem
        fields = ["kind", "description", "amount", "effect"]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["description"].required = False
        self._style()

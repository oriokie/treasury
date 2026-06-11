from django import forms
from accounts.forms import StyledFormMixin
from .models import Account


class AccountForm(StyledFormMixin, forms.ModelForm):
    class Meta:
        model = Account
        fields = ["code", "name", "type", "parent", "active"]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["parent"].required = False
        self.fields["parent"].queryset = Account.objects.order_by("code")
        for f in ("type", "parent"):
            self.fields[f].widget.attrs.update({"class": "field--select"})
        # System accounts are wired into the posting engine: keep their code/type
        # stable (name, parent and active may still be edited).
        if self.instance and self.instance.pk and self.instance.system_key:
            self.fields["code"].disabled = True
            self.fields["type"].disabled = True
            self.fields["code"].help_text = "Fixed for built-in accounts."
            self.fields["type"].help_text = "Fixed for built-in accounts."
        self._style()

    def clean_code(self):
        # disabled fields return the initial value; guard against blanks anyway
        return self.cleaned_data.get("code") or self.instance.code

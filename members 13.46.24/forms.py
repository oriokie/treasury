from django import forms
from accounts.forms import StyledFormMixin
from .models import Member


class MemberForm(StyledFormMixin, forms.ModelForm):
    class Meta:
        model = Member
        fields = ["name", "phone", "group", "member_type", "dev_group", "active"]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._style()

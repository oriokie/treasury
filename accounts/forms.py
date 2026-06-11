from django import forms
from django.contrib.auth.forms import UserCreationForm
from django.contrib.auth.models import User, Group

from core.roles import ALL_ROLES


class StyledFormMixin:
    """Apply consistent form-control styling to every widget."""
    def _style(self):
        for field in self.fields.values():
            w = field.widget
            css = w.attrs.get("class", "")
            if isinstance(w, (forms.CheckboxInput,)):
                w.attrs["class"] = (css + " form-check").strip()
            elif isinstance(w, forms.Select):
                w.attrs["class"] = (css + " field field--select").strip()
            else:
                w.attrs["class"] = (css + " field").strip()


class NewUserForm(StyledFormMixin, UserCreationForm):
    first_name = forms.CharField(max_length=150, required=False)
    last_name = forms.CharField(max_length=150, required=False)
    email = forms.EmailField(required=False)
    role = forms.ChoiceField(choices=[(r, r) for r in ALL_ROLES])

    class Meta(UserCreationForm.Meta):
        model = User
        fields = ("username", "first_name", "last_name", "email")

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._style()

    def save(self, commit=True):
        user = super().save(commit=False)
        user.first_name = self.cleaned_data.get("first_name", "")
        user.last_name = self.cleaned_data.get("last_name", "")
        user.email = self.cleaned_data.get("email", "")
        if commit:
            user.save()
            group, _ = Group.objects.get_or_create(name=self.cleaned_data["role"])
            user.groups.set([group])
        return user


class EditRoleForm(StyledFormMixin, forms.Form):
    role = forms.ChoiceField(choices=[(r, r) for r in ALL_ROLES])
    active = forms.BooleanField(required=False, initial=True, label="Account active")

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._style()

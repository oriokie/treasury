from django import forms
from django.contrib.auth.forms import UserCreationForm
from django.contrib.auth.models import User, Group

from core.roles import ALL_ROLES


class StyledFormMixin:
    """Apply consistent form-control styling to every widget."""
    def _style(self):
        for name, field in self.fields.items():
            w = field.widget
            css = w.attrs.get("class", "")
            if isinstance(w, (forms.CheckboxInput,)):
                w.attrs["class"] = (css + " form-check").strip()
            elif isinstance(w, forms.Select):
                w.attrs["class"] = (css + " field field--select").strip()
            elif isinstance(w, (forms.CheckboxSelectMultiple,
                                forms.RadioSelect)):
                pass  # rendered as a list; no single control class
            else:
                w.attrs["class"] = (css + " field").strip()
            # accessibility: expose required state to assistive tech
            if field.required:
                w.attrs.setdefault("aria-required", "true")


class NewUserForm(StyledFormMixin, UserCreationForm):
    first_name = forms.CharField(max_length=150, required=False)
    last_name = forms.CharField(max_length=150, required=False)
    email = forms.EmailField(required=False)
    role = forms.ChoiceField(choices=[(r, r) for r in ALL_ROLES])
    led_departments = forms.ModelMultipleChoiceField(
        queryset=None, required=False,
        widget=forms.SelectMultiple(attrs={"size": 8}),
        help_text="For a Department leader: the department(s) they may view. "
                  "Ignored for other roles.")

    class Meta(UserCreationForm.Meta):
        model = User
        fields = ("username", "first_name", "last_name", "email")

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        from departments.models import Department
        self.fields["led_departments"].queryset = Department.objects.filter(
            active=True).order_by("name")
        self._style()

    def save(self, commit=True, created_by=None):
        user = super().save(commit=False)
        user.first_name = self.cleaned_data.get("first_name", "")
        user.last_name = self.cleaned_data.get("last_name", "")
        user.email = self.cleaned_data.get("email", "")
        if commit:
            user.save()
            group, _ = Group.objects.get_or_create(name=self.cleaned_data["role"])
            user.groups.set([group])
            self._sync_leaderships(user)
            from .models import UserProfile, log_user_admin_action
            profile = UserProfile.for_user(user)
            profile.created_by = created_by
            profile.save(update_fields=["created_by"])
            log_user_admin_action(created_by, user, "CREATED",
                detail=f"Created as {self.cleaned_data['role']}")
        return user

    def _sync_leaderships(self, user):
        """Link the user to the chosen departments only when they are a Leader;
        clear any links otherwise so a role change can't leave stale access."""
        from departments.models import DepartmentLeadership
        from core.roles import LEADER
        DepartmentLeadership.objects.filter(user=user).delete()
        if self.cleaned_data["role"] == LEADER:
            for dept in self.cleaned_data.get("led_departments", []):
                DepartmentLeadership.objects.get_or_create(user=user, department=dept)


class EditRoleForm(StyledFormMixin, forms.Form):
    role = forms.ChoiceField(choices=[(r, r) for r in ALL_ROLES])
    active = forms.BooleanField(required=False, initial=True, label="Account active")
    led_departments = forms.ModelMultipleChoiceField(
        queryset=None, required=False,
        widget=forms.SelectMultiple(attrs={"size": 8}),
        help_text="For a Department leader: the department(s) they may view.")

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        from departments.models import Department
        self.fields["led_departments"].queryset = Department.objects.filter(
            active=True).order_by("name")
        self._style()

    def sync_leaderships(self, user):
        from departments.models import DepartmentLeadership
        from core.roles import LEADER
        DepartmentLeadership.objects.filter(user=user).delete()
        if self.cleaned_data["role"] == LEADER:
            for dept in self.cleaned_data.get("led_departments", []):
                DepartmentLeadership.objects.get_or_create(user=user, department=dept)


class UserProfileDetailsForm(StyledFormMixin, forms.Form):
    """Extended profile fields, shown on the Profile tab of a user's admin
    page. Kept separate from EditRoleForm (Roles & Rights tab) and from
    Django's own User fields (name/email, edited here too for a single-place
    edit experience) so each tab's form only touches what it owns."""
    first_name = forms.CharField(max_length=150, required=False, label="First name")
    last_name = forms.CharField(max_length=150, required=False, label="Last name")
    email = forms.EmailField(required=False)
    phone = forms.CharField(max_length=20, required=False)
    gender = forms.ChoiceField(required=False)
    position = forms.CharField(max_length=80, required=False,
        help_text="e.g. Head Deacon, Elder, Youth Leader")
    department = forms.ModelChoiceField(queryset=None, required=False,
        label="Department / ministry",
        help_text="Informational only — a Leader's actual access is set on the Roles & Rights tab.")
    church_assignment = forms.CharField(max_length=120, required=False,
        help_text="For a multi-church deployment; leave blank otherwise.")
    notes = forms.CharField(widget=forms.Textarea(attrs={"rows": 3}), required=False,
        help_text="Internal admin notes — never shown to the user.")

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        from departments.models import Department
        from .models import UserProfile
        self.fields["gender"].choices = [("", "—")] + list(UserProfile.Gender.choices)
        self.fields["department"].queryset = Department.objects.filter(
            active=True).order_by("name")
        self._style()

    def save(self, user, actor=None, request=None):
        from .models import UserProfile, log_user_admin_action
        changes = []
        for f in ("first_name", "last_name", "email"):
            new_val = self.cleaned_data.get(f, "")
            if getattr(user, f) != new_val:
                changes.append(f)
                setattr(user, f, new_val)
        if changes:
            user.save(update_fields=changes)

        profile = UserProfile.for_user(user)
        profile_changes = []
        for f in ("phone", "gender", "position", "church_assignment", "notes"):
            new_val = self.cleaned_data.get(f, "") or ""
            if getattr(profile, f) != new_val:
                profile_changes.append(f)
                setattr(profile, f, new_val)
        new_dept = self.cleaned_data.get("department")
        if profile.department_id != (new_dept.id if new_dept else None):
            profile_changes.append("department")
            profile.department = new_dept
        if changes or profile_changes:
            profile.save()
            log_user_admin_action(actor, user, "PROFILE_UPDATED",
                detail=f"Updated: {', '.join(changes + profile_changes)}",
                request=request)
        return user


class AdminPasswordResetForm(StyledFormMixin, forms.Form):
    """An administrator sets a new password directly for a user who can't
    reset their own (e.g. no working phone/email on file). Shown once to the
    administrator to communicate securely to the user — the system doesn't
    send it anywhere itself, since no outbound email is configured (see
    docs/recommendations.md)."""
    new_password = forms.CharField(
        widget=forms.PasswordInput, min_length=10,
        help_text="At least 10 characters. Shown once — make a note of it before leaving this page.")
    force_change = forms.BooleanField(
        required=False, initial=True,
        label="Require the user to set their own password on next login")

    def save(self, user, actor=None, request=None):
        from django.contrib.auth.password_validation import validate_password
        from .models import UserProfile, log_user_admin_action
        validate_password(self.cleaned_data["new_password"], user=user)
        user.set_password(self.cleaned_data["new_password"])
        user.save()
        if self.cleaned_data.get("force_change"):
            profile = UserProfile.for_user(user)
            profile.must_change_password = True
            profile.save(update_fields=["must_change_password"])
        log_user_admin_action(actor, user, "PASSWORD_RESET",
            detail="Password reset by administrator" +
                  (" — must change on next login" if self.cleaned_data.get("force_change") else ""),
            request=request)


class AccountLockForm(StyledFormMixin, forms.Form):
    reason = forms.CharField(max_length=200, required=False,
        widget=forms.TextInput(attrs={"placeholder": "Optional — shown to other administrators"}))

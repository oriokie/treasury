from django.contrib import messages
from django.contrib.auth.models import User, Group
from django.shortcuts import redirect, get_object_or_404
from django.urls import reverse_lazy
from django.views.generic import ListView, FormView, View

from core.permissions import TreasurerRequiredMixin
from core.roles import user_roles
from .forms import (NewUserForm, EditRoleForm, UserProfileDetailsForm,
                    AdminPasswordResetForm, AccountLockForm)


from core.utils import PrefPaginationMixin


class UserListView(PrefPaginationMixin, TreasurerRequiredMixin, ListView):
    model = User
    template_name = "accounts/user_list.html"
    context_object_name = "users"
    paginate_by = 50

    def get_queryset(self):
        from django.db.models import Q
        qs = (User.objects.all().select_related("profile")
              .prefetch_related("groups").order_by("username"))
        q = (self.request.GET.get("q") or "").strip()
        if q:
            qs = qs.filter(Q(username__icontains=q) | Q(first_name__icontains=q)
                          | Q(last_name__icontains=q) | Q(email__icontains=q)
                          | Q(profile__phone__icontains=q))
        role = self.request.GET.get("role") or ""
        if role:
            qs = qs.filter(groups__name=role)
        status = self.request.GET.get("status") or ""
        if status == "active":
            qs = qs.filter(is_active=True, profile__locked=False)
        elif status == "inactive":
            qs = qs.filter(is_active=False)
        elif status == "locked":
            qs = qs.filter(profile__locked=True)
        sort = self.request.GET.get("sort") or "username"
        allowed_sorts = {"username", "-username", "last_login", "-last_login",
                         "date_joined", "-date_joined"}
        if sort in allowed_sorts:
            qs = qs.order_by(sort)
        return qs.distinct()

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        from core.roles import ALL_ROLES
        from .models import UserAdminLogEntry
        from core.roles import TREASURER, ASSISTANT, AUDITOR
        users = ctx["users"]
        superuser_roles = ", ".join(sorted([TREASURER, ASSISTANT, AUDITOR]))
        ctx["roles_by_user"] = {
            u.id: (superuser_roles if u.is_superuser
                  else ", ".join(sorted(g.name for g in u.groups.all())) or "—")
            for u in users}
        ctx["locked_ids"] = {u.id for u in users
                             if getattr(u, "profile", None) and u.profile.locked}
        ctx["two_fa_ids"] = set(User.objects.filter(
            pk__in=[u.id for u in users], two_factor__confirmed=True)
            .values_list("id", flat=True))
        ctx["all_roles"] = ALL_ROLES
        ctx["q"] = self.request.GET.get("q", "")
        ctx["role_filter"] = self.request.GET.get("role", "")
        ctx["status_filter"] = self.request.GET.get("status", "")
        ctx["sort"] = self.request.GET.get("sort", "username")
        ctx["total_count"] = User.objects.count()
        return ctx


class UserCreateView(TreasurerRequiredMixin, FormView):
    template_name = "accounts/user_form.html"
    form_class = NewUserForm
    success_url = reverse_lazy("user_list")

    def form_valid(self, form):
        user = form.save(created_by=self.request.user)
        messages.success(self.request,
                         f"User '{user.username}' created as {form.cleaned_data['role']}.")
        return super().form_valid(form)


# ---------------------------------------------------------- admin actions ---
from .models import log_user_admin_action, UserProfile


class UserAccountActionView(TreasurerRequiredMixin, View):
    """One-click administrative actions on a user account: lock/unlock,
    disable 2FA, clear a failed-login lockout, terminate active sessions.
    Consolidated into one view (dispatched by the `action` URL segment)
    rather than one class per action, since each handler is a few lines and
    they share the same permission/self-edit/audit-logging shape.

    Every action here is blocked when the target is the acting administrator
    themselves — each has a self-service equivalent elsewhere (2FA settings,
    the account menu), and letting an admin lock/unlock or strip their own
    2FA from this panel is exactly the kind of self-permission-modification
    path a security review would flag."""
    SELF_BLOCKED = {"lock", "unlock", "disable_2fa", "clear_lockout", "terminate_sessions"}

    def post(self, request, pk, action):
        user = get_object_or_404(User, pk=pk)
        if user == request.user and action in self.SELF_BLOCKED:
            messages.error(request, "You can't perform that action on your own "
                "account — ask another administrator.")
            return redirect("user_edit", pk=user.pk)
        handler = getattr(self, f"_do_{action}", None)
        if handler is None:
            messages.error(request, "Unknown action.")
            return redirect("user_edit", pk=user.pk)
        return handler(request, user)

    def _do_lock(self, request, user):
        reason = (request.POST.get("reason") or "").strip()[:200]
        profile = UserProfile.for_user(user)
        if profile.locked:
            messages.info(request, f"{user.username} is already locked.")
            return redirect("user_edit", pk=user.pk)
        from django.utils import timezone
        profile.locked = True
        profile.locked_reason = reason
        profile.locked_at = timezone.now()
        profile.locked_by = request.user
        profile.save(update_fields=["locked", "locked_reason", "locked_at", "locked_by"])
        log_user_admin_action(request.user, user, "LOCKED",
            detail=reason or "No reason given", request=request)
        messages.success(request, f"{user.username}'s account has been locked "
                                  f"(suspended). They can't sign in until reinstated.")
        return redirect("user_edit", pk=user.pk)

    def _do_unlock(self, request, user):
        profile = UserProfile.for_user(user)
        if not profile.locked:
            messages.info(request, f"{user.username} isn't locked.")
            return redirect("user_edit", pk=user.pk)
        profile.locked = False
        profile.locked_reason = ""
        profile.locked_at = None
        profile.locked_by = None
        profile.save(update_fields=["locked", "locked_reason", "locked_at", "locked_by"])
        log_user_admin_action(request.user, user, "UNLOCKED", request=request)
        messages.success(request, f"{user.username}'s account has been reinstated.")
        return redirect("user_edit", pk=user.pk)

    def _do_disable_2fa(self, request, user):
        from .models import TwoFactor
        tf = TwoFactor.objects.filter(user=user).first()
        if not tf:
            messages.info(request, f"{user.username} doesn't have two-factor set up.")
            return redirect("user_edit", pk=user.pk)
        tf.delete()
        log_user_admin_action(request.user, user, "TWO_FA_DISABLED",
            detail="Cleared by administrator — user must re-enrol", request=request)
        messages.success(request, f"Two-factor authentication has been turned off "
                                  f"for {user.username}. They'll be asked to set it "
                                  f"up again if it's required for their role.")
        return redirect("user_edit", pk=user.pk)

    def _do_clear_lockout(self, request, user):
        from axes.utils import reset
        n = reset(username=user.username)
        log_user_admin_action(request.user, user, "LOGIN_LOCKOUT_CLEARED",
            detail=f"Cleared {n or 0} failed-attempt record(s)", request=request)
        messages.success(request, f"Cleared any failed-login lockout for {user.username}.")
        return redirect("user_edit", pk=user.pk)

    def _do_terminate_sessions(self, request, user):
        from django.contrib.sessions.models import Session
        killed = 0
        for s in Session.objects.all():
            try:
                if str(s.get_decoded().get("_auth_user_id")) == str(user.pk):
                    s.delete()
                    killed += 1
            except Exception:
                continue
        log_user_admin_action(request.user, user, "SESSIONS_TERMINATED",
            detail=f"{killed} session(s) ended", request=request)
        messages.success(request, f"Ended {killed} active session(s) for "
                                  f"{user.username}. They'll need to sign in again.")
        return redirect("user_edit", pk=user.pk)


class UserPasswordResetView(TreasurerRequiredMixin, View):
    """An administrator sets a new password for a user directly (e.g. no
    working phone/email to send a self-service reset link — see
    docs/recommendations.md for why this app doesn't send reset emails).
    Shown once, on this page, for the administrator to note down and pass to
    the user through a secure channel of their choosing."""
    template_name = "accounts/user_password_reset.html"

    def get(self, request, pk):
        from django.shortcuts import render
        user = get_object_or_404(User, pk=pk)
        if user == request.user:
            messages.error(request, "Use your own account menu to change your "
                "password, not this admin tool.")
            return redirect("user_edit", pk=user.pk)
        return render(request, self.template_name,
                     {"object": user, "form": AdminPasswordResetForm()})

    def post(self, request, pk):
        from django.shortcuts import render
        user = get_object_or_404(User, pk=pk)
        if user == request.user:
            return redirect("user_edit", pk=user.pk)
        form = AdminPasswordResetForm(request.POST)
        if form.is_valid():
            form.save(user, actor=request.user, request=request)
            return render(request, self.template_name, {
                "object": user, "form": AdminPasswordResetForm(),
                "new_password": form.cleaned_data["new_password"], "done": True})
        return render(request, self.template_name, {"object": user, "form": form})


class UserCloneView(TreasurerRequiredMixin, View):
    """Create a new account with the same role, led departments, and rights
    profiles as an existing one — never credentials (a fresh password must
    always be set explicitly). Useful when onboarding someone into a role
    that already exists (e.g. \"another Assistant like Jane\")."""
    template_name = "accounts/user_clone.html"

    def get(self, request, pk):
        from django.shortcuts import render
        source = get_object_or_404(User, pk=pk)
        return render(request, self.template_name, {"source": source})

    def post(self, request, pk):
        source = get_object_or_404(User, pk=pk)
        username = (request.POST.get("username") or "").strip()
        if not username:
            messages.error(request, "Give the new account a username.")
            return redirect("user_clone", pk=source.pk)
        if User.objects.filter(username__iexact=username).exists():
            messages.error(request, f"'{username}' is already taken.")
            return redirect("user_clone", pk=source.pk)
        import secrets
        temp_password = secrets.token_urlsafe(12)
        new_user = User.objects.create_user(
            username=username,
            first_name=request.POST.get("first_name", "").strip(),
            last_name=request.POST.get("last_name", "").strip(),
            email=request.POST.get("email", "").strip(),
            password=temp_password)
        new_user.groups.set(source.groups.all())
        for dept_id in source.led_departments.values_list("department_id", flat=True):
            from departments.models import DepartmentLeadership
            DepartmentLeadership.objects.get_or_create(user=new_user, department_id=dept_id)
        for profile in source.profiles.all():
            profile.users.add(new_user)
        up = UserProfile.for_user(new_user)
        up.must_change_password = True
        up.created_by = request.user
        up.save(update_fields=["must_change_password", "created_by"])
        log_user_admin_action(request.user, new_user, "CLONED",
            detail=f"Cloned from {source.username}", request=request)
        log_user_admin_action(request.user, new_user, "CREATED", request=request)
        messages.success(request, f"Created '{username}' with the same role and "
            f"rights as {source.username}. Temporary password: {temp_password} "
            f"— they must change it on first login.")
        return redirect("user_edit", pk=new_user.pk)


class UserEditRoleView(TreasurerRequiredMixin, View):
    """The user administration page: Profile / Security / Roles & Rights /
    Activity / Audit Log tabs. Handles three separate forms (profile details,
    role & leadership, and — implicitly — the various one-click admin
    actions, which post to UserAccountActionView instead of here) on one
    page, dispatched by a hidden `form_name` field so each tab's Save button
    only touches what it owns."""
    template_name = "accounts/user_edit.html"

    def get(self, request, pk):
        user = get_object_or_404(User, pk=pk)
        current = next(iter(user_roles(user)), "Assistant")
        led = list(user.led_departments.values_list("department_id", flat=True))
        role_form = EditRoleForm(initial={"role": current, "active": user.is_active,
                                          "led_departments": led})
        profile = self._profile(user)
        profile_form = UserProfileDetailsForm(initial={
            "first_name": user.first_name, "last_name": user.last_name,
            "email": user.email, "phone": profile.phone, "gender": profile.gender,
            "position": profile.position, "department": profile.department_id,
            "church_assignment": profile.church_assignment, "notes": profile.notes})
        return self._render(request, user, role_form=role_form, profile_form=profile_form)

    def post(self, request, pk):
        user = get_object_or_404(User, pk=pk)
        form_name = request.POST.get("form_name")
        if form_name == "profile_form":
            return self._post_profile(request, user)
        return self._post_role(request, user)

    def _post_profile(self, request, user):
        form = UserProfileDetailsForm(request.POST)
        if form.is_valid():
            form.save(user, actor=request.user, request=request)
            messages.success(request, f"Updated {user.username}'s profile.")
            return redirect("user_edit", pk=user.pk)
        return self._render(request, user, profile_form=form,
                           role_form=self._default_role_form(user))

    def _post_role(self, request, user):
        form = EditRoleForm(request.POST)
        if form.is_valid():
            from core.roles import TREASURER
            if user == request.user:
                messages.error(request, "You can't change your own role or account "
                    "status here — ask another administrator to make the change.")
                return self._render(request, user, role_form=self._default_role_form(user),
                                   profile_form=self._default_profile_form(user))
            new_role = form.cleaned_data["role"]
            new_active = form.cleaned_data["active"]
            was_treasurer = user.groups.filter(name=TREASURER).exists()
            losing_treasurer = was_treasurer and (new_role != TREASURER or not new_active)
            if losing_treasurer:
                other_active_treasurers = (User.objects.filter(
                        groups__name=TREASURER, is_active=True)
                    .exclude(pk=user.pk).exists())
                if not other_active_treasurers:
                    messages.error(request,
                        f"{user.username} is the only active Treasurer. Make "
                        "someone else a Treasurer first, so the church always "
                        "has at least one — otherwise no one could manage users, "
                        "approve expenses, or unlock periods.")
                    return self._render(request, user, role_form=form,
                                       profile_form=self._default_profile_form(user))
            old_role = next(iter(user_roles(user)), "—")
            old_active = user.is_active
            group, _ = Group.objects.get_or_create(name=new_role)
            user.groups.set([group])
            user.is_active = new_active
            user.save()
            form.sync_leaderships(user)
            from .models import log_user_admin_action
            if old_role != new_role:
                log_user_admin_action(request.user, user, "ROLE_CHANGED",
                    detail=f"Role changed", before=old_role, after=new_role,
                    request=request)
            if old_active != new_active:
                log_user_admin_action(request.user, user,
                    "ACTIVATED" if new_active else "DEACTIVATED",
                    detail="Account " + ("activated" if new_active else "deactivated"),
                    request=request)
            messages.success(request, f"Updated {user.username}.")
            return redirect("user_edit", pk=user.pk)
        return self._render(request, user, role_form=form,
                           profile_form=self._default_profile_form(user))

    def _profile(self, user):
        from .models import UserProfile
        return UserProfile.for_user(user)

    def _default_role_form(self, user):
        current = next(iter(user_roles(user)), "Assistant")
        led = list(user.led_departments.values_list("department_id", flat=True))
        return EditRoleForm(initial={"role": current, "active": user.is_active,
                                     "led_departments": led})

    def _default_profile_form(self, user):
        profile = self._profile(user)
        return UserProfileDetailsForm(initial={
            "first_name": user.first_name, "last_name": user.last_name,
            "email": user.email, "phone": profile.phone, "gender": profile.gender,
            "position": profile.position, "department": profile.department_id,
            "church_assignment": profile.church_assignment, "notes": profile.notes})

    def _render(self, request, user, role_form, profile_form):
        from django.shortcuts import render
        from django.contrib.auth.models import Group as _Group
        from core import roles as _roles
        from core.rights import user_rights, RIGHT_LABELS, grouped_rights
        from .models import UserProfile, UserAdminLogEntry, Profile
        from axes.models import AccessAttempt, AccessLog

        profile = UserProfile.for_user(user)
        tf = getattr(user, "two_factor", None)

        # -- security dashboard --
        attempts = AccessAttempt.objects.filter(username__iexact=user.username)
        last_attempt = attempts.order_by("-attempt_time").first()
        failure_count = sum(a.failures_since_start for a in attempts)
        is_axes_locked = any(
            a.failures_since_start >= self._axes_limit() for a in attempts)
        last_login_log = (AccessLog.objects.filter(username__iexact=user.username)
                          .order_by("-attempt_time").first())
        active_sessions = self._session_count(user)

        # -- roles & rights --
        assigned_profiles = list(user.profiles.all())
        effective_rights = sorted(user_rights(user))
        effective_labels = [RIGHT_LABELS.get(k, k) for k in effective_rights]

        # -- audit log + activity --
        audit_entries = user.admin_log_entries.select_related("actor")[:100]

        ctx = {
            "object": user, "profile": profile, "role_form": role_form,
            "profile_form": profile_form, "two_factor": tf,
            "is_self": user == request.user,
            "active_tab": request.GET.get("tab", "profile"),
            # security dashboard
            "failed_login_count": failure_count,
            "last_failed_login": last_attempt.attempt_time if last_attempt else None,
            "last_failed_ip": last_attempt.ip_address if last_attempt else None,
            "is_axes_locked": is_axes_locked,
            "last_successful_login": user.last_login,
            "last_login_ip": last_login_log.ip_address if last_login_log else None,
            "active_session_count": active_sessions,
            # roles & rights
            "assigned_profiles": assigned_profiles,
            "all_profiles": Profile.objects.all(),
            "effective_rights": effective_labels,
            "current_role": next(iter(_roles.user_roles(user)), None),
            # audit
            "audit_entries": audit_entries,
        }
        return render(request, self.template_name, ctx)

    def _axes_limit(self):
        from django.conf import settings
        return getattr(settings, "AXES_FAILURE_LIMIT", 5)

    def _session_count(self, user):
        from django.contrib.sessions.models import Session
        from django.utils import timezone
        n = 0
        for s in Session.objects.filter(expire_date__gte=timezone.now()):
            try:
                if str(s.get_decoded().get("_auth_user_id")) == str(user.pk):
                    n += 1
            except Exception:
                continue
        return n


# ---------------------------------------------------------------- profiles ---
from django.shortcuts import render
from core.permissions import RightRequiredMixin
from core import rights as R
from .models import Profile


class ProfileListView(RightRequiredMixin, View):
    required_right = "manage_profiles"
    permission_message = "Managing profiles requires the 'Manage profiles & users' right."

    def get(self, request):
        profiles = Profile.objects.prefetch_related("users").all()
        return render(request, "accounts/profile_list.html", {
            "profiles": profiles,
            "n_rights": len(R.RIGHT_KEYS),
        })


class ProfileEditView(RightRequiredMixin, View):
    required_right = "manage_profiles"

    def _get(self, pk):
        return get_object_or_404(Profile, pk=pk) if pk else None

    def get(self, request, pk=None):
        profile = self._get(pk)
        return render(request, "accounts/profile_form.html", {
            "profile": profile,
            "grouped_rights": R.grouped_rights(),
            "granted": set(profile.rights) if profile else set(),
            "all_users": User.objects.filter(is_active=True).order_by("username"),
            "assigned": set(profile.users.values_list("id", flat=True)) if profile else set(),
        })

    def post(self, request, pk=None):
        profile = self._get(pk)
        name = (request.POST.get("name") or "").strip()
        if not name:
            messages.error(request, "Give the profile a name.")
            return redirect(request.path)
        chosen = [k for k in R.RIGHT_KEYS if request.POST.get(f"right_{k}")]
        if profile is None:
            profile = Profile(name=name)
        elif profile.is_system and profile.name != name:
            # allow editing a system profile's rights but keep its name stable-ish
            pass
        profile.name = name
        profile.description = (request.POST.get("description") or "").strip()[:200]
        profile.rights = chosen
        try:
            profile.save()
        except Exception:
            messages.error(request, "A profile with that name already exists.")
            return redirect(request.path)
        user_ids = request.POST.getlist("users")
        profile.users.set(User.objects.filter(id__in=user_ids))
        messages.success(request, f"Saved profile “{profile.name}” with {len(chosen)} right(s) "
                                  f"and {len(user_ids)} user(s).")
        return redirect("profile_list")


class ProfileDeleteView(RightRequiredMixin, View):
    required_right = "manage_profiles"

    def post(self, request, pk):
        profile = get_object_or_404(Profile, pk=pk)
        if profile.is_system:
            messages.error(request, "Default profiles can't be deleted — edit or clone them instead.")
            return redirect("profile_list")
        name = profile.name
        profile.delete()
        messages.success(request, f"Deleted profile “{name}”. Affected users fall back to their role.")
        return redirect("profile_list")



# --- Sign-out page with a rotating, public-domain (KJV) verse -----------------
from django.contrib.auth import logout as _django_logout
from django.views.generic import TemplateView as _TemplateView

SIGNOUT_VERSES = [
    ("Whatsoever ye do, do it heartily, as to the Lord, and not unto men.", "Colossians 3:23"),
    ("Every man according as he purposeth in his heart, so let him give; "
     "for God loveth a cheerful giver.", "2 Corinthians 9:7"),
    ("Moreover it is required in stewards, that a man be found faithful.", "1 Corinthians 4:2"),
    ("She hath done what she could.", "Mark 14:8"),
    ("Well done, thou good and faithful servant.", "Matthew 25:21"),
    ("Bring ye all the tithes into the storehouse, that there may be meat in mine house.", "Malachi 3:10"),
    ("The Lord bless thee, and keep thee.", "Numbers 6:24"),
    ("Commit thy works unto the Lord, and thy thoughts shall be established.", "Proverbs 16:3"),
    ("Let all things be done decently and in order.", "1 Corinthians 14:40"),
    ("Honour the Lord with thy substance, and with the firstfruits of all thine increase.", "Proverbs 3:9"),
]


class SignOutView(_TemplateView):
    template_name = "registration/logged_out.html"

    def post(self, request, *args, **kwargs):
        return self._out(request)

    def get(self, request, *args, **kwargs):
        return self._out(request)

    def _out(self, request):
        import datetime as _dt
        idx = _dt.date.today().toordinal() % len(SIGNOUT_VERSES)
        verse, ref = SIGNOUT_VERSES[idx]
        if request.user.is_authenticated:
            _django_logout(request)
        return render(request, self.template_name, {"verse": verse, "verse_ref": ref})

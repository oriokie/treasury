from django.contrib import messages
from django.contrib.auth.models import User, Group
from django.shortcuts import redirect, get_object_or_404
from django.urls import reverse_lazy
from django.views.generic import ListView, FormView, View

from core.permissions import TreasurerRequiredMixin
from core.roles import user_roles
from .forms import NewUserForm, EditRoleForm


class UserListView(TreasurerRequiredMixin, ListView):
    model = User
    template_name = "accounts/user_list.html"
    context_object_name = "users"

    def get_queryset(self):
        return User.objects.all().prefetch_related("groups").order_by("username")

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx["roles_by_user"] = {u.id: ", ".join(sorted(user_roles(u))) or "—"
                                for u in ctx["users"]}
        return ctx


class UserCreateView(TreasurerRequiredMixin, FormView):
    template_name = "accounts/user_form.html"
    form_class = NewUserForm
    success_url = reverse_lazy("user_list")

    def form_valid(self, form):
        user = form.save()
        messages.success(self.request,
                         f"User '{user.username}' created as {form.cleaned_data['role']}.")
        return super().form_valid(form)


class UserEditRoleView(TreasurerRequiredMixin, View):
    template_name = "accounts/user_edit.html"

    def get(self, request, pk):
        user = get_object_or_404(User, pk=pk)
        current = next(iter(user_roles(user)), "Assistant")
        led = list(user.led_departments.values_list("department_id", flat=True))
        form = EditRoleForm(initial={"role": current, "active": user.is_active,
                                     "led_departments": led})
        return self._render(request, user, form)

    def post(self, request, pk):
        user = get_object_or_404(User, pk=pk)
        form = EditRoleForm(request.POST)
        if form.is_valid():
            group, _ = Group.objects.get_or_create(name=form.cleaned_data["role"])
            user.groups.set([group])
            user.is_active = form.cleaned_data["active"]
            user.save()
            form.sync_leaderships(user)
            messages.success(request, f"Updated {user.username}.")
            return redirect("user_list")
        return self._render(request, user, form)

    def _render(self, request, user, form):
        from django.shortcuts import render
        return render(request, self.template_name, {"object": user, "form": form})


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

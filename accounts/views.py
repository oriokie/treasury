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
        form = EditRoleForm(initial={"role": current, "active": user.is_active})
        return self._render(request, user, form)

    def post(self, request, pk):
        user = get_object_or_404(User, pk=pk)
        form = EditRoleForm(request.POST)
        if form.is_valid():
            group, _ = Group.objects.get_or_create(name=form.cleaned_data["role"])
            user.groups.set([group])
            user.is_active = form.cleaned_data["active"]
            user.save()
            messages.success(request, f"Updated {user.username}.")
            return redirect("user_list")
        return self._render(request, user, form)

    def _render(self, request, user, form):
        from django.shortcuts import render
        return render(request, self.template_name, {"object": user, "form": form})

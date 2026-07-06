from django.contrib import admin
from django.urls import path, include
from django.conf import settings
from django.conf.urls.static import static
from django.contrib.auth import views as auth_views
from accounts import views as accounts_views
from accounts import password_reset as self_reset_views

from core.views import DashboardView
from accounts.auth import TwoFactorLoginView

urlpatterns = [
    path("admin/", admin.site.urls),
    path("accounts/login/", TwoFactorLoginView.as_view(), name="login"),
    path("accounts/logout/", accounts_views.SignOutView.as_view(), name="logout"),
    path("accounts/password_change/", auth_views.PasswordChangeView.as_view(
        template_name="registration/password_change.html",
        success_url="/accounts/login/"), name="password_change"),

    # Self-service password reset — SMS one-time-code channel (custom)
    path("accounts/forgot-password/", self_reset_views.SelfPasswordResetRequestView.as_view(),
         name="self_reset_request"),
    path("accounts/forgot-password/verify/", self_reset_views.SelfPasswordResetVerifyView.as_view(),
         name="self_reset_verify"),

    # Second half of the email channel: the link Django's PasswordResetForm
    # (called internally by SelfPasswordResetRequestView) emails out. Django's
    # own well-tested token mechanism; degrades to a no-op console backend if
    # SMTP isn't configured, same as the rest of this app's outbound email.
    path("accounts/forgot-password/email/confirm/<uidb64>/<token>/",
         auth_views.PasswordResetConfirmView.as_view(
             template_name="registration/self_reset_confirm.html",
             success_url="/accounts/login/"), name="password_reset_confirm"),

    path("", DashboardView.as_view(), name="dashboard"),
    path("", include("core.urls")),
    path("", include("accounts.urls")),
    path("", include("departments.urls")),
    path("", include("members.urls")),
    path("", include("giving.urls")),
    path("", include("statements.urls")),
    path("", include("cashbook.urls")),
    path("", include("envelopes.urls")),
    path("reports/", include("reports.urls")),
    path("", include("assets.urls")),
    path("", include("ledger.urls")),
    path("", include("pledges.urls")),
    path("", include("leaders.urls")),
]

if settings.DEBUG:
    urlpatterns += static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)


handler500 = "core.views.error_500"
handler404 = "core.views.error_404"
handler403 = "core.views.error_403"

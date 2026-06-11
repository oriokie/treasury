from django.contrib import admin
from django.urls import path, include
from django.conf import settings
from django.conf.urls.static import static
from django.contrib.auth import views as auth_views

from core.views import DashboardView

urlpatterns = [
    path("admin/", admin.site.urls),
    path("accounts/login/", auth_views.LoginView.as_view(
        template_name="registration/login.html"), name="login"),
    path("accounts/logout/", auth_views.LogoutView.as_view(), name="logout"),
    path("accounts/password_change/", auth_views.PasswordChangeView.as_view(
        template_name="registration/password_change.html",
        success_url="/accounts/login/"), name="password_change"),
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
]

if settings.DEBUG:
    urlpatterns += static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)

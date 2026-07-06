from django.urls import path
from . import views
from . import twofactor as tf

urlpatterns = [
    path("users/", views.UserListView.as_view(), name="user_list"),
    path("profiles/", views.ProfileListView.as_view(), name="profile_list"),
    path("profiles/new/", views.ProfileEditView.as_view(), name="profile_create"),
    path("profiles/<int:pk>/edit/", views.ProfileEditView.as_view(), name="profile_edit"),
    path("profiles/<int:pk>/delete/", views.ProfileDeleteView.as_view(), name="profile_delete"),

    path("users/new/", views.UserCreateView.as_view(), name="user_create"),
    path("users/<int:pk>/edit/", views.UserEditRoleView.as_view(), name="user_edit"),
    path("users/<int:pk>/action/<str:action>/", views.UserAccountActionView.as_view(),
         name="user_account_action"),
    path("users/<int:pk>/reset-password/", views.UserPasswordResetView.as_view(),
         name="user_password_reset"),
    path("users/<int:pk>/clone/", views.UserCloneView.as_view(), name="user_clone"),
    # two-factor authentication
    path("2fa/setup/", tf.TwoFactorSetupView.as_view(), name="twofactor_setup"),
    path("2fa/verify/", tf.TwoFactorVerifyView.as_view(), name="twofactor_verify"),
    path("2fa/disable/", tf.TwoFactorDisableView.as_view(), name="twofactor_disable"),
    path("2fa/recovery/regenerate/", tf.TwoFactorRecoveryRegenView.as_view(),
         name="twofactor_recovery_regen"),
]

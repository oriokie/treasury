from django.urls import path
from . import views

urlpatterns = [
    path("users/", views.UserListView.as_view(), name="user_list"),
    path("users/new/", views.UserCreateView.as_view(), name="user_create"),
    path("users/<int:pk>/edit/", views.UserEditRoleView.as_view(), name="user_edit"),
]

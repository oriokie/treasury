from django.urls import path
from . import views

urlpatterns = [
    path("departments/", views.DepartmentListView.as_view(), name="department_list"),
    path("departments/new/", views.DepartmentCreateView.as_view(), name="department_create"),
    path("departments/<int:pk>/edit/", views.DepartmentUpdateView.as_view(), name="department_edit"),
    path("dev-groups/new/", views.DevGroupCreateView.as_view(), name="dev_group_create"),
    path("dev-groups/<int:pk>/edit/", views.DevGroupUpdateView.as_view(), name="dev_group_edit"),
    path("dev-groups/<int:pk>/delete/", views.DevGroupDeleteView.as_view(), name="dev_group_delete"),
    path("budget/", views.BudgetView.as_view(), name="budget"),
    path("budget/<int:pk>/lines/", views.BudgetLinesView.as_view(), name="budget_lines"),
]

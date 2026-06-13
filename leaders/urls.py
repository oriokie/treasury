from django.urls import path
from . import views

urlpatterns = [
    path("leader/", views.LeaderDashboardView.as_view(), name="leader_dashboard"),
    path("leader/department/<int:pk>/", views.LeaderDepartmentDetailView.as_view(),
         name="leader_department_detail"),
]

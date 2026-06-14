from django.urls import path
from . import views

urlpatterns = [
    path("leader/", views.LeaderDashboardView.as_view(), name="leader_dashboard"),
    path("leader/department/<int:pk>/", views.LeaderDepartmentDetailView.as_view(),
         name="leader_department_detail"),
    path("leader/department/<int:pk>/collections/", views.LeaderCollectionsView.as_view(),
         name="leader_collections"),
    path("leader/department/<int:pk>/expenses/", views.LeaderExpensesView.as_view(),
         name="leader_expenses"),
    path("leader/department/<int:pk>/pledges/", views.LeaderPledgesView.as_view(),
         name="leader_pledges"),
    path("leader/group/<int:pk>/", views.LeaderGroupDetailView.as_view(),
         name="leader_group_detail"),
]

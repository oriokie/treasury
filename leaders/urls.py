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
    path("leader/department/<int:pk>/pledges/import/",
         views.LeaderPledgeImportView.as_view(), name="leader_pledge_import"),
    path("leader/members/search/", views.LeaderMemberSearchView.as_view(),
         name="leader_member_search"),
    path("leader/advances/", views.LeaderAdvancesView.as_view(), name="leader_advances"),
    path("leader/advances/<int:pk>/", views.LeaderAdvanceDetailView.as_view(), name="leader_advance_detail"),
    path("leader/loans/", views.LeaderLoansView.as_view(), name="leader_loans"),
    path("leader/loans/<int:pk>/", views.LeaderLoanDetailView.as_view(), name="leader_loan_detail"),
    path("leader/group/<int:pk>/", views.LeaderGroupDetailView.as_view(),
         name="leader_group_detail"),
]

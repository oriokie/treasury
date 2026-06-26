from django.urls import path
from . import views

urlpatterns = [
    path("departments/", views.DepartmentListView.as_view(), name="department_list"),
    path("departments/new/", views.DepartmentCreateView.as_view(), name="department_create"),
    path("departments/<int:pk>/edit/", views.DepartmentUpdateView.as_view(), name="department_edit"),
    path("departments/<int:pk>/consolidate/", views.ConsolidateView.as_view(), name="department_consolidate"),
    path("departments/<int:pk>/close/", views.CloseAccountView.as_view(), name="department_close"),
    path("departments/<int:pk>/archive/", views.ArchiveAccountView.as_view(), name="department_archive"),
    path("departments/<int:pk>/reopen/", views.ReopenAccountView.as_view(), name="department_reopen"),
    path("departments/historical/", views.HistoricalAccountsView.as_view(), name="historical_accounts"),
    path("dev-groups/new/", views.DevGroupCreateView.as_view(), name="dev_group_create"),
    path("dev-groups/<int:pk>/edit/", views.DevGroupUpdateView.as_view(), name="dev_group_edit"),
    path("dev-groups/<int:pk>/delete/", views.DevGroupDeleteView.as_view(), name="dev_group_delete"),
    path("dev-groups/sms/", views.DevGroupSmsView.as_view(), name="dev_group_sms"),
    path("dev-groups/<int:pk>/sms/", views.DevGroupSmsView.as_view(), name="dev_group_sms_one"),
    path("budget/", views.BudgetView.as_view(), name="budget"),
    path("funds/bulk-import/", views.BulkFundImportView.as_view(), name="bulk_fund_import"),
    path("funds/structure-import/", views.FundStructureImportView.as_view(), name="fund_structure_import"),
    path("budget/template/", views.BudgetTemplateDownloadView.as_view(), name="budget_template_download"),
    path("budget/<int:pk>/lines/", views.BudgetLinesView.as_view(), name="budget_lines"),
]

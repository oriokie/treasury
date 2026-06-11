from django.urls import path
from . import views
from .webhook import CbsEventWebhookView

urlpatterns = [
    path("statements/<int:pk>/purge/", views.StatementPurgeView.as_view(), name="statement_purge"),
    path("api/bank/cbs-events/", CbsEventWebhookView.as_view(), name="cbs_webhook"),
    path("statements/", views.StatementListView.as_view(), name="statement_list"),
    path("statements/upload/", views.StatementUploadView.as_view(), name="statement_upload"),
    path("statements/accounts/", views.BankAccountListView.as_view(), name="bank_accounts"),
    path("statements/feed-log/", views.BankFeedLogView.as_view(), name="bank_feed_log"),
    path("statements/<int:pk>/", views.ImportStatusView.as_view(), name="statement_detail"),
    path("statements/<int:pk>/review/", views.AutoAllocationReviewView.as_view(), name="statement_auto_review"),
    path("statements/<int:pk>/review/excel/", views.AutoAllocationExcelView.as_view(), name="statement_auto_excel"),
    path("reconciliations/", views.ReconciliationListView.as_view(), name="reconciliation_list"),
    path("reconciliations/new/", views.ReconciliationCreateView.as_view(), name="reconciliation_new"),
    path("reconciliations/<int:pk>/", views.ReconciliationDetailView.as_view(), name="reconciliation_detail"),
    path("reconciliations/auto/", views.AutoReconcileView.as_view(), name="auto_reconcile"),
    path("reconciliations/auto/run/", views.AutoReconcileRunView.as_view(), name="auto_reconcile_run"),
    path("reconciliations/auto/<int:pk>/confirm/", views.AutoReconcileConfirmView.as_view(), name="auto_reconcile_confirm"),
    path("reconciliations/auto/<int:pk>/reject/", views.AutoReconcileRejectView.as_view(), name="auto_reconcile_reject"),
]

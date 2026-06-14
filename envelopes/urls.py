from django.urls import path
from . import views

urlpatterns = [
    path("envelopes/", views.EnvelopeListView.as_view(), name="envelope_list"),
    path("envelopes/ledger/", views.EnvelopeLedgerCreate.as_view(), name="envelope_ledger"),
    path("envelopes/template/", views.EnvelopeTemplateView.as_view(), name="envelope_template"),
    path("envelopes/import/", views.EnvelopeImportView.as_view(), name="envelope_import"),
    path("envelopes/<int:pk>/reassign/", views.EnvelopeReassignView.as_view(), name="envelope_reassign"),
    path("envelopes/sabbath.xlsx", views.EnvelopeSabbathExcelView.as_view(), name="envelope_sabbath_excel"),
    path("envelopes/pull-bank/", views.EnvelopePullBankView.as_view(), name="envelope_pull_bank"),
    path("envelopes/reconcile/", views.SabbathReconciliationView.as_view(), name="sabbath_reconcile"),
    path("envelopes/reconcile/apply/", views.ReconcileApplyView.as_view(), name="reconcile_apply"),
    path("transactions/<int:pk>/receipt-envelope/", views.EnvelopeReceiptOneBankView.as_view(), name="receipt_one_bank"),
    path("envelopes/<int:pk>/", views.EnvelopeDetailView.as_view(), name="envelope_detail"),
    path("envelopes/<int:pk>/edit/", views.EnvelopeUpdateView.as_view(), name="envelope_edit"),
    path("envelopes/<int:pk>/receipt/", views.EnvelopeReceiptView.as_view(), name="envelope_receipt"),
    path("envelopes/<int:pk>/delete/", views.EnvelopeDeleteView.as_view(), name="envelope_delete"),
    path("envelopes/<int:pk>/send/", views.EnvelopeSendReceiptView.as_view(), name="envelope_send_receipt"),
    path("envelopes/send-bulk/", views.EnvelopeBulkSendView.as_view(), name="envelope_send_bulk"),
    path("envelopes/receipts/bulk/", views.EnvelopeBulkReceiptsView.as_view(), name="envelope_receipts_bulk"),
    path("envelopes/counts/", views.CountSessionListView.as_view(), name="count_list"),
    path("envelopes/counts/new/", views.CountSessionCreate.as_view(), name="count_new"),
    path("envelopes/sabbath/close/", views.SabbathCloseView.as_view(), name="sabbath_close"),
    path("envelopes/counts/<int:pk>/", views.CountSessionDetail.as_view(), name="count_detail"),
]

from django.urls import path
from . import views

urlpatterns = [
    path("queue/sabbath/", views.SabbathConfirmQueueView.as_view(), name="sabbath_queue"),
    path("transactions/", views.TransactionListView.as_view(), name="transaction_list"),
    path("transactions/<int:pk>/reverse/", views.TransactionReverseView.as_view(), name="transaction_reverse"),
    path("queue/", views.ReviewQueueView.as_view(), name="queue"),
    path("queue/export/", views.QueueExportView.as_view(), name="queue_export"),
    path("queue/import/", views.QueueImportView.as_view(), name="queue_import"),

    path("queue/<int:pk>/claim/", views.ClaimResolveView.as_view(), name="queue_claim"),
    path("cash/new/", views.CashEntryCreate.as_view(), name="cash_new"),
    path("cash/", views.CashEntryListView.as_view(), name="cash_list"),
    path("transactions/<int:pk>/edit/", views.TransactionUpdateView.as_view(), name="transaction_edit"),
    path("transactions/<int:pk>/split/", views.TransactionSplitView.as_view(), name="transaction_split"),
    path("transactions/<int:pk>/shift-sabbath/", views.TransactionShiftSabbathView.as_view(), name="transaction_shift_sabbath"),
    path("rules/", views.RuleListView.as_view(), name="rule_list"),
    path("rules/new/", views.RuleCreateView.as_view(), name="rule_create"),
    path("rules/<int:pk>/delete/", views.RuleDeleteView.as_view(), name="rule_delete"),
    path("debits/", views.DebitQueueView.as_view(), name="debit_queue"),
    path("debits/<int:pk>/resolve/", views.DebitResolveView.as_view(), name="debit_resolve"),
]

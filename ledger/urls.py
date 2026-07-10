from django.urls import path
from . import views

urlpatterns = [
    path("ledger/accounts/", views.ChartOfAccountsView.as_view(), name="chart_of_accounts"),
    path("ledger/trial-balance/", views.TrialBalanceView.as_view(), name="trial_balance"),
    path("ledger/general-ledger/", views.GeneralLedgerView.as_view(), name="general_ledger"),
    path("ledger/journal/", views.JournalView.as_view(), name="journal"),
    path("ledger/journal/archive/", views.JournalArchiveView.as_view(), name="journal_archive"),
    path("ledger/journal/new/", views.ManualJournalCreate.as_view(), name="manual_journal"),
    path("ledger/reconciliation/", views.ReconciliationReportView.as_view(), name="ledger_reconciliation"),
    path("ledger/health/", views.LedgerHealthView.as_view(), name="ledger_health"),
    path("ledger/reconciliation/fund/<int:pk>/", views.FundVarianceView.as_view(), name="ledger_fund_variance"),
    path("ledger/rebuild/", views.RebuildLedgerView.as_view(), name="ledger_rebuild"),
    path("ledger/accounts/new/", views.AccountCreate.as_view(), name="account_create"),
    path("ledger/accounts/<int:pk>/edit/", views.AccountUpdate.as_view(), name="account_edit"),
    path("ledger/accounts/<int:pk>/delete/", views.AccountDelete.as_view(), name="account_delete"),
]

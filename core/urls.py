from django.urls import path
from . import views
from .telegram_webhook import TelegramWebhookView

urlpatterns = [
    path("healthz/", views.HealthCheckView.as_view(), name="healthz"),
    path("update/", views.UpdateRunView.as_view(), name="update_run"),
    path("update/status/", views.UpdateStatusView.as_view(), name="update_status"),
    path("backup/database/", views.BackupView.as_view(), name="backup_database"),
    path("backup/offsite-now/", views.OffsiteBackupNowView.as_view(), name="backup_offsite_now"),
    path("backup/restore/", views.RestoreView.as_view(), name="backup_restore"),
    path("backup/data-export/", views.DataExportView.as_view(), name="data_export"),
    path("api/telegram/webhook/<str:token>/", TelegramWebhookView.as_view(), name="telegram_webhook"),
    path("settings/", views.SettingsView.as_view(), name="settings"),
    path("settings/telegram-pin/", views.TelegramSetPinView.as_view(), name="telegram_set_pin"),
    path("notifications/", views.NotificationListView.as_view(), name="notifications"),
    path("members/search/", views.MemberSearchView.as_view(), name="member_search"),
    path("envelopes/next-receipt/", views.NextReceiptView.as_view(), name="next_receipt"),
    path("departments/balance/", views.DepartmentBalanceView.as_view(), name="department_balance"),
    path("funds/search/", views.FundSearchView.as_view(), name="fund_search"),
    path("assistant/", views.AssistantView.as_view(), name="assistant"),
    path("assistant/ask/", views.AssistantAskView.as_view(), name="assistant_ask"),
    path("controls/", views.ControlsView.as_view(), name="controls"),
    path("controls/check/<str:kind>/", views.ControlsDuplicatesView.as_view(), name="controls_duplicates"),
    path("executive/", views.ExecutiveDashboardView.as_view(), name="executive"),
    path("executive/insights/", views.ExecutiveInsightsView.as_view(), name="executive_insights"),
]

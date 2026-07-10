from django.urls import path
from . import views

urlpatterns = [
    path("pledges/", views.PledgeDashboardView.as_view(), name="pledge_dashboard"),
    path("pledges/list/", views.PledgeListView.as_view(), name="pledge_list"),
    path("pledges/import/", views.PledgeImportView.as_view(), name="pledge_import"),
    path("pledges/new/", views.PledgeCreateView.as_view(), name="pledge_create"),
    path("pledges/<int:pk>/", views.PledgeDetailView.as_view(), name="pledge_detail"),
    path("pledges/<int:pk>/edit/", views.PledgeCreateView.as_view(), name="pledge_edit"),
    path("pledges/<int:pk>/delete/", views.PledgeDeleteView.as_view(), name="pledge_delete"),
    path("pledges/<int:pk>/approve/", views.PledgeApproveView.as_view(), name="pledge_approve"),
    path("pledges/<int:pk>/match/", views.PledgeMatchView.as_view(), name="pledge_match"),
    path("pledges/<int:pk>/remind/", views.PledgeReminderView.as_view(), name="pledge_remind"),
    path("pledges/payment/<int:pk>/delete/", views.PledgePaymentDeleteView.as_view(), name="pledge_payment_delete"),
    path("pledges/auto-match/", views.PledgeAutoMatchAllView.as_view(), name="pledge_auto_match_all"),
    path("pledges/reminders/", views.PledgeReminderBatchView.as_view(), name="pledge_reminder_batch"),
    # campaigns
    path("pledges/campaigns/", views.CampaignListView.as_view(), name="pledge_campaign_list"),
    path("pledges/campaigns/new/", views.CampaignCreateView.as_view(), name="pledge_campaign_create"),
    path("pledges/campaigns/<int:pk>/", views.CampaignDetailView.as_view(), name="pledge_campaign_detail"),
    path("pledges/campaigns/<int:pk>/import/", views.PledgeImportView.as_view(), name="pledge_campaign_import"),
    path("pledges/campaigns/<int:pk>/edit/", views.CampaignCreateView.as_view(), name="pledge_campaign_edit"),
    path("pledges/campaigns/<int:pk>/delete/", views.CampaignDeleteView.as_view(), name="pledge_campaign_delete"),
    # reports
    path("pledges/report/", views.PledgeReportView.as_view(), name="pledge_report"),
    path("pledges/member/<int:pk>/statement/", views.MemberPledgeStatementView.as_view(), name="pledge_member_statement"),
    # match suggestions (SUGGEST mode review)
    path("pledges/suggestions/", views.PledgeSuggestionListView.as_view(), name="pledge_suggestions"),
    path("pledges/suggestions/<int:pk>/", views.PledgeSuggestionActionView.as_view(), name="pledge_suggestion_action"),
    # public member pledge form (no login; off by default; writes unverified drafts)
    path("pledge/", views.PublicPledgeView.as_view(), name="public_pledge"),
    path("pledge/thanks/", views.PublicPledgeThanksView.as_view(), name="public_pledge_thanks"),
]

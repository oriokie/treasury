from django.urls import path

from . import api, views, views_config

urlpatterns = [
    path("", views.BenevolentDashboardView.as_view(), name="benevolent_dashboard"),

    # Schemes & policies (setup)
    path("schemes/", views.SchemeListView.as_view(), name="benevolent_scheme_list"),
    path("schemes/new/", views.SchemeFormView.as_view(), name="benevolent_scheme_new"),
    path("schemes/<int:pk>/", views.SchemeDetailView.as_view(),
         name="benevolent_scheme_detail"),
    path("schemes/<int:pk>/edit/", views.SchemeFormView.as_view(),
         name="benevolent_scheme_edit"),
    # NOTE the explicit "status/" prefix. An unprefixed <str:action> here would
    # greedily swallow every other single-segment route under schemes/<pk>/ —
    # events/, enrol/ and contribute/ all matched it and returned 405 (the
    # action view is POST-only). Namespacing the verb removes the whole class of
    # collision rather than relying on declaration order.
    path("schemes/<int:pk>/status/<str:action>/", views.SchemeActionView.as_view(),
         name="benevolent_scheme_action"),
    path("schemes/<int:pk>/events/", views.EventTypeView.as_view(),
         name="benevolent_event_types"),
    path("schemes/<int:pk>/policy/new/", views.PolicyFormView.as_view(),
         name="benevolent_policy_new"),
    path("schemes/<int:pk>/policy/<int:policy_id>/", views.PolicyFormView.as_view(),
         name="benevolent_policy_edit"),
    path("schemes/<int:pk>/policy/<int:policy_id>/rule/", views.PolicyRuleView.as_view(),
         name="benevolent_policy_rule"),
    path("schemes/<int:pk>/policy/<int:policy_id>/do/<str:action>/",
         views.PolicyActionView.as_view(), name="benevolent_policy_action"),

    # Membership
    path("members/", views.MembershipListView.as_view(), name="benevolent_membership_list"),
    path("schemes/<int:pk>/enrol/", views.MembershipCreateView.as_view(),
         name="benevolent_enrol"),
    path("members/<int:pk>/", views.MembershipDetailView.as_view(),
         name="benevolent_membership_detail"),

    # Contributions
    path("contributions/", views.ContributionListView.as_view(),
         name="benevolent_contribution_list"),
    path("schemes/<int:pk>/contribute/", views.ContributionCreateView.as_view(),
         name="benevolent_contribute"),

    # Cases
    path("cases/", views.CaseListView.as_view(), name="benevolent_case_list"),
    path("schemes/<int:pk>/cases/new/", views.CaseCreateView.as_view(),
         name="benevolent_case_new"),
    path("cases/<int:pk>/", views.CaseDetailView.as_view(), name="benevolent_case_detail"),
    path("cases/<int:pk>/payout/", views.CasePayoutView.as_view(),
         name="benevolent_case_payout"),
    path("cases/<int:pk>/decide/<str:action>/", views.CaseDecisionView.as_view(),
         name="benevolent_case_decide"),
    # same reasoning as the scheme action above: keep the verb namespaced so it
    # can never shadow a sibling route added later
    path("cases/<int:pk>/action/<str:action>/", views.CaseActionView.as_view(),
         name="benevolent_case_action"),

    # ---- Phase 2: settings, profiles, wizard, committee ----
    path("settings/", views_config.BenevolentSettingsView.as_view(),
         name="benevolent_settings"),
    path("profiles/", views_config.ProfileListView.as_view(),
         name="benevolent_profile_list"),
    path("profiles/<int:pk>/", views_config.ProfileDetailView.as_view(),
         name="benevolent_profile_detail"),
    path("schemes/<int:pk>/policy/<int:policy_id>/save-as-profile/",
         views_config.ProfileSaveAsView.as_view(), name="benevolent_profile_save_as"),
    path("wizard/", views_config.WizardView.as_view(), {"step": 0},
         name="benevolent_wizard_start"),
    path("wizard/<int:step>/", views_config.WizardView.as_view(),
         name="benevolent_wizard"),
    path("cases/<int:pk>/vote/", views_config.CaseVoteView.as_view(),
         name="benevolent_case_vote"),
    path("cases/<int:pk>/levy/", views_config.CaseLevyView.as_view(),
         name="benevolent_case_levy"),
    path("members/<int:pk>/admin/<str:action>/",
         views_config.MembershipAdminView.as_view(), name="benevolent_membership_admin"),

    # JSON API (read-only integration surface)
    path("api/schemes/", api.SchemeListAPI.as_view(), name="benevolent_api_schemes"),
    path("api/schemes/<int:pk>/", api.SchemeSummaryAPI.as_view(),
         name="benevolent_api_scheme"),
    path("api/eligibility/", api.EligibilityAPI.as_view(), name="benevolent_api_eligibility"),
    path("api/cases/<int:pk>/", api.CaseAPI.as_view(), name="benevolent_api_case"),
]

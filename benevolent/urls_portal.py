"""Routes for the member self-service portal.

Mounted at ``/portal/`` rather than under ``/benevolent/``, on purpose. The
confinement middleware keeps a member login inside one path prefix, and that
prefix has to be the portal's own: mounting the portal under the office
module's URL space would mean either confining members to ``/benevolent/`` —
which is most of the scheme's *administration* — or writing a confinement rule
that enumerates individual views, which is precisely the per-view auditing the
middleware exists to avoid. One prefix, one rule.
"""
from django.urls import path

from . import views_portal as v
from . import views_portal_admin as admin_v

urlpatterns = [
    path("", v.PortalHomeView.as_view(), name="portal_home"),
    path("unavailable/", v.PortalUnavailableView.as_view(), name="portal_unavailable"),

    # money
    path("contributions/", v.PortalContributionsView.as_view(),
         name="portal_contributions"),
    path("contributions/<int:pk>/receipt/", v.PortalReceiptView.as_view(),
         name="portal_receipt"),
    path("statement/", v.PortalStatementView.as_view(), name="portal_statement"),

    # standing, household, cases
    path("standing/", v.PortalStandingView.as_view(), name="portal_standing"),
    path("household/", v.PortalHouseholdView.as_view(), name="portal_household"),
    path("cases/", v.PortalCaseListView.as_view(), name="portal_cases"),
    path("cases/<int:pk>/", v.PortalCaseDetailView.as_view(), name="portal_case_detail"),

    # requests
    path("requests/", v.PortalRequestListView.as_view(), name="portal_requests"),
    path("requests/new/<str:kind>/", v.PortalRequestCreateView.as_view(),
         name="portal_request_new"),
    path("requests/<int:pk>/", v.PortalRequestDetailView.as_view(),
         name="portal_request_detail"),
    path("requests/<int:pk>/edit/", v.PortalRequestEditView.as_view(),
         name="portal_request_edit"),

    # documents
    path("documents/", v.PortalDocumentListView.as_view(), name="portal_documents"),
    path("documents/<int:pk>/<str:action>/", v.PortalDocumentActionView.as_view(),
         name="portal_document_action"),

    # notifications & profile
    path("notifications/", v.PortalNotificationsView.as_view(),
         name="portal_notifications"),
    path("profile/", v.PortalProfileView.as_view(), name="portal_profile"),
]

# The office side. Deliberately in this module beside the member views — the
# two halves of one feature, reviewed together — but mounted under
# /benevolent/portal/ so it stays inside the office URL space and out of reach
# of the confinement middleware.
admin_urlpatterns = [
    path("portal/accounts/", admin_v.PortalAccountListView.as_view(),
         name="portal_admin_accounts"),
    path("portal/requests/", admin_v.PortalRequestQueueView.as_view(),
         name="portal_admin_queue"),
    path("portal/requests/<int:pk>/", admin_v.PortalRequestReviewView.as_view(),
         name="portal_admin_review"),
]

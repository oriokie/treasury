from django.urls import path
from . import views

urlpatterns = [
    path("members/duplicates/merge-all/", views.BulkMergeView.as_view(), name="member_bulk_merge"),
    path("members/", views.MemberListView.as_view(), name="member_list"),
    path("members/new/", views.MemberCreateView.as_view(), name="member_create"),
    path("members/duplicates/", views.DuplicateReviewView.as_view(), name="member_duplicates"),
    path("members/bulk/", views.MemberBulkView.as_view(), name="member_bulk"),
    path("members/sms/", views.MemberSmsView.as_view(), name="member_sms"),
    path("members/export/", views.MemberExportView.as_view(), name="member_export"),
    path("members/import/", views.MemberImportView.as_view(), name="member_import"),

    path("members/merge/", views.MergeMembersView.as_view(), name="member_merge"),
    path("members/<int:pk>/", views.MemberDetailView.as_view(), name="member_detail"),
    path("members/<int:pk>/edit/", views.MemberUpdateView.as_view(), name="member_edit"),
    path("members/<int:pk>/phones/add/", views.MemberPhoneAddView.as_view(),
         name="member_phone_add"),
    path("members/<int:pk>/phones/<int:phone_id>/remove/",
         views.MemberPhoneRemoveView.as_view(), name="member_phone_remove"),
]

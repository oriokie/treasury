from django.urls import path
from . import views

urlpatterns = [
    path("assets/", views.AssetListView.as_view(), name="asset_list"),
    path("assets/new/", views.AssetCreate.as_view(), name="asset_create"),
    path("assets/<int:pk>/edit/", views.AssetUpdate.as_view(), name="asset_edit"),
    path("assets/<int:pk>/dispose/", views.AssetDisposeView.as_view(), name="asset_dispose"),
    path("assets/<int:pk>/accumulate/", views.AssetAccumulateView.as_view(), name="asset_accumulate"),
    path("assets/capitalise/<int:pk>/", views.ExpenseCapitaliseView.as_view(), name="expense_capitalise"),
    path("assets/board/", views.AssetBoardView.as_view(), name="asset_board"),
    path("assets/preflight/", views.AssetPreflightView.as_view(), name="asset_preflight"),
    path("assets/import/", views.AssetImportView.as_view(), name="asset_import"),
    path("assets/<int:pk>/status/", views.AssetTransitionView.as_view(), name="asset_transition"),
    path("assets/<int:pk>/assign/", views.AssetAssignView.as_view(), name="asset_assign"),
    path("assets/<int:pk>/checkin/", views.AssetCheckInView.as_view(), name="asset_checkin"),
    path("assets/<int:pk>/transfer/", views.AssetTransferCreateView.as_view(), name="asset_transfer"),
    path("assets/transfers/<int:pk>/decide/", views.AssetTransferDecideView.as_view(), name="asset_transfer_decide"),
    path("assets/depreciation/", views.DepreciationRulesView.as_view(), name="depreciation_rules"),
    path("assets/depreciation/runs/", views.DepreciationRunsView.as_view(), name="depreciation_runs"),
    path("<int:pk>/", views.AssetDetailView.as_view(), name="asset_detail"),
    path("<int:pk>/attach/", views.AssetAttachmentUpload.as_view(), name="asset_attachment_upload"),
    path("<int:pk>/attach/<int:att>/delete/", views.AssetAttachmentDelete.as_view(), name="asset_attachment_delete"),
]

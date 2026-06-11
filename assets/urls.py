from django.urls import path
from . import views

urlpatterns = [
    path("assets/", views.AssetListView.as_view(), name="asset_list"),
    path("assets/new/", views.AssetCreate.as_view(), name="asset_create"),
    path("assets/<int:pk>/edit/", views.AssetUpdate.as_view(), name="asset_edit"),
    path("assets/<int:pk>/dispose/", views.AssetDisposeView.as_view(), name="asset_dispose"),
    path("assets/depreciation/", views.DepreciationRulesView.as_view(), name="depreciation_rules"),
    path("<int:pk>/", views.AssetDetailView.as_view(), name="asset_detail"),
    path("<int:pk>/attach/", views.AssetAttachmentUpload.as_view(), name="asset_attachment_upload"),
    path("<int:pk>/attach/<int:att>/delete/", views.AssetAttachmentDelete.as_view(), name="asset_attachment_delete"),
]

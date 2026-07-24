from django.urls import path

from . import views

urlpatterns = [
    path("", views.VendorListView.as_view(), name="vendor_list"),
    path("new/", views.VendorSaveView.as_view(), name="vendor_create"),
    path("lookup/", views.VendorLookupView.as_view(), name="vendor_lookup"),
    path("<int:pk>/", views.VendorDetailView.as_view(), name="vendor_detail"),
    path("<int:pk>/save/", views.VendorSaveView.as_view(), name="vendor_save"),
    path("<int:pk>/archive/", views.VendorArchiveView.as_view(), name="vendor_archive"),
    path("<int:pk>/merge/", views.VendorMergeView.as_view(), name="vendor_merge"),
]

from django.urls import path

from . import views

urlpatterns = [
    path("", views.LoanDashboardView.as_view(), name="loan_dashboard"),
    path("register/", views.LoanRegisterView.as_view(), name="loan_register"),
    path("new/", views.LoanCreateView.as_view(), name="loan_new"),
    path("<int:pk>/", views.LoanDetailView.as_view(), name="loan_detail"),
    path("<int:pk>/edit/", views.LoanEditView.as_view(), name="loan_edit"),
    path("<int:pk>/delete/", views.LoanDeleteView.as_view(), name="loan_delete"),
    path("<int:pk>/receipt/", views.LoanReceiptView.as_view(), name="loan_receipt"),
    path("<int:pk>/repay/", views.LoanRepaymentView.as_view(), name="loan_repay"),
    path("<int:pk>/interest/", views.LoanInterestView.as_view(), name="loan_interest"),
    path("<int:pk>/convert/", views.LoanConvertView.as_view(), name="loan_convert"),
    path("<int:pk>/write-off/", views.LoanWriteOffView.as_view(), name="loan_write_off"),
    path("<int:pk>/attach/", views.LoanAttachmentView.as_view(), name="loan_attach"),
    path("from-transaction/<int:txn_id>/",
         views.LoanReceiptFromTransactionView.as_view(), name="loan_from_transaction"),
    path("lenders/", views.LenderListView.as_view(), name="lender_list"),
    path("lenders/new/", views.LenderFormView.as_view(), name="lender_new"),
    path("lenders/<int:pk>/edit/", views.LenderFormView.as_view(), name="lender_edit"),
    path("lenders/matching/", views.LenderMatchingView.as_view(), name="lender_matching"),
    path("patterns/", views.PatternListView.as_view(), name="loan_patterns"),
]

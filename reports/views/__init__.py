"""reports.views — split into feature modules (P1-2, v2.95).
This __init__ reproduces the original single-module namespace exactly,
so urls.py and every `from reports.views import X` keep working."""
from decimal import Decimal
from django.contrib import messages
from django.db.models import Sum, Count, Q
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.views import View
from django.views.generic import TemplateView
from core.permissions import (ReportAccessMixin, TreasurerRequiredMixin,
                              RightRequiredMixin, ReportAccessMixin)
from core.utils import parse_period, safe_json
from cashbook.models import Expense
from departments.models import Department
from giving.models import Transaction
from members.models import Member
from ..services import balances
from ..exports import csv_response
from ..services.devgroups import balanced_partition as _balanced_partition  # noqa: E402
import datetime as dt
from ..services import envelope_reports
from core.utils import last_saturday as _last_saturday
from ..services import monthly
import datetime as _dt
from django.utils import timezone as _tz
from cashbook.models import RemittanceBatch
from core.models import SiteConfig
from core.utils import sabbath_week_of
from ..services.remittance import (                          # noqa: E402
    days_outstanding as _days_outstanding,
    repost_to_ledger as _repost_to_ledger,
    remittance_dashboard_rows)
from ..services import budget as budget_svc
from ..exports import xlsx_response
from core.utils import sabbath_of
from core.models import SiteConfig
from ..services.goals import (sentence_fund_name as _sfund,      # noqa: E402
                             camp_goal_records as _camp_goal_records)
from ._shared import PeriodMixin
from .overview import ReportIndexView, MonthlyReportView, OfferingSummaryView, TitheReportView, GroupGivingView, ExpenseReportView, IncomeExpenditureView, CashBookView, ReconciliationView, AnnualSummaryView, HistoricalYearManageView, AuditLogView, _audit_trace, _qs_without, MemberStatementView
from .funds import FundLedgerView, FundMembersView, FundThankSmsView, BankPositionView
from .dev_groups import DevGroupUnassignedView, DevGroupProgressView, DevGroupBuilderView, DevGroupMembersView, DevGroupAllExcelView, DevGroupEmailAllView
from .envelopes import EnvelopeSabbathView, EnvelopeSummaryView
from .monthly_accounts import _year_from, MonthlyAccountsView, TrustMonthlyView, CollectionsSummaryView, CollectionsDetailView
from .remittance import TrustFundView, RemittanceView, RemitTrustView, _remit_period, RemittanceDashboardView, RemittanceBatchCreateView, RemittanceBatchDetailView, RemittanceBatchApproveView, RemittanceBatchRemitView, RemittanceBatchIssuePaymentView, RemittanceBatchListView, RemittanceCalendarView, RemittanceCalendarGenerateView, RemittanceDeadlineUpdateView
from .summaries import BudgetVsActualView, _export, _day_income_expense, DailySummaryView, WeeklySummaryView, CashFlowView, CashFlowForecastView
from .board import BoardReportSettingsView, BoardReportView, PastorReportView, ConferenceSubmissionView, BudgetBoardReportView
from .financial_statements import IncomeStatementView, FinancialPositionView, ChangesInNetAssetsView, StatementOfCashFlowsView
from .treasurer_report import MonthlyTreasurerReportView, _monthly_report_context, MonthlyReportExcelView, MonthlyReportWordView
from .engine import MetricsCatalogueView, EngineReportView, ComponentCatalogueView
from .narrative import _date, NarrativeSaveView, NarrativeAiView

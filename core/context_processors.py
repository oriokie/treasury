from django.conf import settings
from . import roles
from .models import SiteConfig


def site_context(request):
    user = getattr(request, "user", None)
    cfg = SiteConfig.get()
    ctx = {
        "SITE_NAME": settings.SITE_NAME,
        "APP_VERSION": __import__("core.version", fromlist=["version_string"]).version_string(),
        "APP_WHATS_NEW": __import__("core.version", fromlist=["whats_new"]).whats_new(),
        "CHURCH_NAME": cfg.church_name or settings.CHURCH_NAME,
        "FIELD_NAME": cfg.field_name,
        "CHURCH_ADDRESS": cfg.church_address,
        "CHURCH_CONTACT": cfg.church_contact,
        "CURRENCY": cfg.currency_symbol or "KSh",
        "REPORT_FOOTER_NOTE": cfg.report_footer_note,
        "cfg": cfg,
        "show_mpesa_ref": cfg.show_mpesa_ref,
        "sms_enabled": cfg.sms_enabled,
        "enable_dev_groups": cfg.enable_dev_groups,
        "is_treasurer": roles.is_treasurer(user) if user else False,
        "can_enter_data": roles.can_enter_data(user) if user else False,
        "is_auditor": roles.is_auditor(user) if user else False,
        "is_leader": roles.is_leader(user) if user else False,
        "is_elder": roles.is_elder(user) if user else False,
        "is_staff_role": roles.is_staff_role(user) if user else False,
    }
    from . import rights as _rights
    _granted = _rights.user_rights(user) if user else set()
    ctx["rights"] = _granted
    ctx["can"] = {k: (k in _granted) for k in _rights.RIGHT_KEYS}
    # per-user workspace preferences (appearance, a11y, tables, notifications)
    try:
        from .models import UserPreference
        ctx["prefs"] = UserPreference.get_for(user)
    except Exception:  # noqa: BLE001 — table may not exist yet (migrations)
        ctx["prefs"] = None
    # for a leader who leads exactly one department, expose it so the nav can
    # label and link straight to that department
    ctx["leader_single_dept"] = None
    ctx["leader_primary_dept"] = None
    try:
        from core import roles as _roles
        if user and getattr(user, "is_authenticated", False) and _roles.is_leader(user):
            from departments.models import departments_led_by
            led = list(departments_led_by(user))
            if len(led) == 1:
                ctx["leader_single_dept"] = led[0]
            # primary = a root of the led set (parent not also led), else first
            if led:
                led_ids = {d.id for d in led}
                roots = [d for d in led if d.parent_id not in led_ids]
                ctx["leader_primary_dept"] = roots[0] if roots else led[0]
            # conditional Loans menu: only when the leader actually has a loan
            # on one of their funds (never show an empty loans page)
            try:
                from loans.services.loans import user_has_accessible_loans
                ctx["leader_has_loans"] = user_has_accessible_loans(user)
            except Exception:  # noqa: BLE001
                ctx["leader_has_loans"] = False
    except Exception:  # noqa: BLE001
        pass
    ctx["phone_full"] = ("view_member_phone_full" in _granted) or bool(getattr(user, "is_superuser", False))
    if user and user.is_authenticated:
        if user.is_superuser or user.groups.filter(name="Treasurer").exists():
            try:
                from core.services.updates import update_available
                avail, tag, cur = update_available()
                if avail:
                    ctx["update_available"] = tag
            except Exception:
                pass
        from django.db.models import Count, Q
        from giving.models import Transaction
        from cashbook.models import Expense
        # Consolidated badge counts. These used to be four separate COUNT queries
        # against Transaction and two against Expense — six fixed queries on every
        # page. They are now two grouped aggregates (one per model), which both
        # cuts the per-page query count AND leaves room for the benevolent task
        # badge below to be added without growing the total. This is the
        # consolidation docs/recommendations.md flagged as the right time to add a
        # benevolent badge.
        txn_badges = Transaction.objects.aggregate(
            queue=Count("pk", filter=Q(
                allocation_status=Transaction.Status.REVIEW,
                direction=Transaction.Direction.CREDIT)),
            sabbath=Count("pk", filter=Q(sabbath_confirm_pending=True)),
            debit=Count("pk", filter=Q(
                allocation_status=Transaction.Status.REVIEW,
                direction=Transaction.Direction.DEBIT,
                channel=Transaction.Channel.BANK)))
        ctx["queue_badge"] = txn_badges["queue"]
        ctx["sabbath_badge"] = txn_badges["sabbath"]
        ctx["debit_badge"] = txn_badges["debit"]
        exp_badges = Expense.objects.aggregate(
            expense=Count("pk", filter=Q(
                status=Expense.Status.PENDING,
                doc_class=Expense.DocClass.EXPENSE)),
            liability=Count("pk", filter=Q(
                status=Expense.Status.PENDING,
                doc_class=Expense.DocClass.LIABILITY)))
        ctx["expense_badge"] = exp_badges["expense"]
        # pending liability documents surface on the Liability Register
        ctx["liability_badge"] = exp_badges["liability"]
        from core.services.notifications import unread_count
        ctx["notif_badge"] = unread_count(user)
        # Review-tasks badge: only queried for users who can actually see the
        # benevolent module, so it adds no cost to any other page render. With
        # the consolidation above this is well within the per-page query budget.
        if "view_benevolent" in _granted:
            from benevolent.models import BenevolentTask
            ctx["benevolent_task_badge"] = BenevolentTask.objects.filter(
                status=BenevolentTask.Status.OPEN).count()
    return ctx


# url_name -> (section label, page label) for a consistent breadcrumb trail.
_BREADCRUMBS = {
    # Giving
    "transaction_list": ("Giving", "Transactions"),
    "envelope_list": ("Giving", "Envelopes"),
    "envelope_detail": ("Giving", "Envelopes"),
    "count_list": ("Giving", "Cash counts"),
    "queue": ("Giving", "Review queue"),
    "sabbath_queue": ("Giving", "Sabbath confirmations"),
    # Banking
    "statement_list": ("Banking", "Statement imports"),
    "debit_queue": ("Banking", "Bank debits"),
    "reconciliation_list": ("Banking", "Bank reconciliation"),
    "reconciliation_detail": ("Banking", "Bank reconciliation"),
    "payment_register": ("Banking", "Payment register"),
    "payment_outstanding": ("Banking", "Outstanding payments"),
    # Expenses
    "expense_list": ("Expenses", "Expenses"),
    "expense_detail": ("Expenses", "Expenses"),
    "recurring_list": ("Expenses", "Recurring"),
    "petty_cash": ("Expenses", "Petty cash"),
    "accruals": ("Expenses", "Payables & accruals"),
    "advance_list": ("Expenses", "Staff advances"),
    # People
    "member_list": ("People", "Members"),
    "member_detail": ("People", "Members"),
    "pledge_dashboard": ("People", "Pledges"),
    "campaign_list": ("People", "Campaigns"),
    # Funds & setup
    "department_list": ("Funds & setup", "Funds & departments"),
    "transfer_list": ("Funds & setup", "Fund transfers"),
    "budget": ("Funds & setup", "Budgeting"),
    "fund_budget": ("Funds & setup", "Fund budget"),
    "asset_list": ("Funds & setup", "Fixed assets"),
    "rule_list": ("Funds & setup", "Allocation rules"),
    "dev_patterns": ("Funds & setup", "Development-group patterns"),
    # Accounting
    "chart_of_accounts": ("Accounting", "Chart of accounts"),
    "general_ledger": ("Accounting", "General ledger"),
    "trial_balance": ("Accounting", "Trial balance"),
    "journal": ("Accounting", "Journal"),
    "ledger_reconciliation": ("Accounting", "Ledger integrity"),
    # Reports
    "report_index": ("Reports", "All reports"),
    "report_board": ("Reports", "Monthly Treasurer's Report"),
    "report_monthly": ("Reports", "Fund movement summary"),
    "remittance_dashboard": ("Banking", "Trust remittance"),
    "remittance_calendar": ("Reports", "Remittance calendar"),
    "report_remittance": ("Reports", "Conference remittance"),
    "report_envelope_sabbath": ("Reports", "Sabbath statement"),
    "report_collections_summary": ("Reports", "Collections summary"),
    "report_dev_groups": ("Reports", "Development groups"),
    "report_audit": ("Reports", "Audit log"),
    "report_reconciliation": ("Reports", "Reconciliation summary"),
    "report_pastor": ("Reports", "Pastor's report"),
    "report_conference": ("Reports", "Conference submission"),
    # Administration
    "user_list": ("Administration", "Users & roles"),
    "profile_list": ("Administration", "Profiles & rights"),
    "controls": ("Administration", "Period locks & controls"),
    "settings": ("Administration", "Settings"),
    "board_settings": ("Reports", "Board report settings"),
    "preferences": ("Account", "Preferences"),
}


def breadcrumb(request):
    """Provide (section, page) breadcrumb labels for the current view."""
    m = getattr(request, "resolver_match", None)
    if not m:
        return {}
    section, page = _BREADCRUMBS.get(m.url_name, (None, None))
    return {"crumb_section": section, "crumb_page": page}

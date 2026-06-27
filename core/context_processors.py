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
        "cfg": cfg,
        "show_mpesa_ref": cfg.show_mpesa_ref,
        "sms_enabled": cfg.sms_enabled,
        "enable_dev_groups": cfg.enable_dev_groups,
        "is_treasurer": roles.is_treasurer(user) if user else False,
        "can_enter_data": roles.can_enter_data(user) if user else False,
        "is_auditor": roles.is_auditor(user) if user else False,
        "is_leader": roles.is_leader(user) if user else False,
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
        from giving.models import Transaction
        from cashbook.models import Expense
        ctx["queue_badge"] = Transaction.objects.filter(
            allocation_status=Transaction.Status.REVIEW,
            direction=Transaction.Direction.CREDIT).count()
        ctx["sabbath_badge"] = Transaction.objects.filter(
            sabbath_confirm_pending=True).count()
        ctx["debit_badge"] = Transaction.objects.filter(
            allocation_status=Transaction.Status.REVIEW,
            direction=Transaction.Direction.DEBIT,
            channel=Transaction.Channel.BANK).count()
        ctx["expense_badge"] = Expense.objects.filter(
            status=Expense.Status.PENDING).count()
        from core.services.notifications import unread_count
        ctx["notif_badge"] = unread_count(user)
    return ctx

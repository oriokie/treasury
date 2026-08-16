"""
Pledge reminders — reuses the existing SMS / WhatsApp services.

Sends a gentle reminder to a member about an outstanding pledge and logs it.
Never raises; respects the member's opt-out and the global channel switches.

When a member has several pledges, the batch send aggregates them into one
message so nobody is texted twice for the same appeal cycle.
"""
from decimal import Decimal

from core.models import SiteConfig
from core.services.sms import send_sms
from core.services.whatsapp import send_whatsapp
from pledges.models import PledgeReminderLog


#: What a pledge message may say about itself. Kept in one place so the
#: settings help, the preview and the renderer cannot drift apart.
PLACEHOLDERS = ("name", "amount", "campaign", "church", "paid", "outstanding",
                "due")

DEFAULTS = {
    "REMINDER": ("Dear {name}, thank you for your pledge of KES {amount} to "
                 "{campaign}. So far KES {paid} received; KES {outstanding} "
                 "outstanding. May God bless your faithfulness. - {church}"),
    "THANKS": ("Dear {name}, thank you for pledging KES {amount} to "
               "{campaign}. Your promise is recorded. God bless you. "
               "- {church}"),
    "FULFILLED": ("Dear {name}, thank you — your pledge of KES {amount} to "
                  "{campaign} is fully paid. May God bless your faithfulness. "
                  "- {church}"),
}


def message_context(pledge, cfg=None):
    """The values a pledge message may draw on."""
    cfg = cfg or SiteConfig.get()
    return {
        "name": (pledge.member.name or "").title(),
        "amount": f"{pledge.amount:,.0f}",
        "campaign": pledge.campaign.name,
        "church": cfg.church_name or "our church",
        "paid": f"{pledge.paid:,.0f}",
        "outstanding": f"{pledge.outstanding:,.0f}",
        "due": pledge.end_date.strftime("%d %b %Y") if pledge.end_date else "",
    }


def message_context_for_pledges(pledges, cfg=None):
    """Values for one SMS that covers every pledge in ``pledges``.

    Amounts are totals. ``campaign`` lists each appeal with its outstanding
    (or pledged amount for THANKS), so one text can still name every promise.
    """
    cfg = cfg or SiteConfig.get()
    pledges = list(pledges)
    if not pledges:
        return {}
    if len(pledges) == 1:
        return message_context(pledges[0], cfg)
    total_amount = sum((p.amount for p in pledges), Decimal("0"))
    total_paid = sum((p.paid for p in pledges), Decimal("0"))
    total_out = sum((p.outstanding for p in pledges), Decimal("0"))
    dues = [p.end_date for p in pledges if p.end_date]
    parts = []
    for p in pledges:
        bal = p.outstanding if p.outstanding > 0 else p.amount
        parts.append(f"{p.campaign.name} (KES {bal:,.0f})")
    return {
        "name": (pledges[0].member.name or "").title(),
        "amount": f"{total_amount:,.0f}",
        "campaign": "; ".join(parts),
        "church": cfg.church_name or "our church",
        "paid": f"{total_paid:,.0f}",
        "outstanding": f"{total_out:,.0f}",
        "due": min(dues).strftime("%d %b %Y") if dues else "",
    }


def build_pledge_text(pledge=None, kind="REMINDER", cfg=None, template=None,
                      pledges=None):
    """Render a pledge message from the church's own wording.

    Pass ``pledge`` for a single promise, or ``pledges`` for an aggregated
    member message. An unknown placeholder is left as it was typed rather than
    raising — a treasurer editing this text is not writing code.
    """
    cfg = cfg or SiteConfig.get()
    if template is None:
        if kind == "THANKS":
            field = "pledge_thanks_template"
        elif kind == "FULFILLED":
            field = "pledge_fulfilled_template"
        else:
            field = "pledge_reminder_template"
        template = (getattr(cfg, field, "") or "").strip() or DEFAULTS[kind]
    if pledges is not None:
        values = message_context_for_pledges(pledges, cfg)
    else:
        values = message_context(pledge, cfg)
    try:
        return template.format_map(_Lenient(values))
    except Exception:  # noqa: BLE001 — never block a send on wording
        return DEFAULTS[kind].format_map(_Lenient(values))


class _Lenient(dict):
    def __missing__(self, key):
        return "{" + key + "}"


def build_reminder_text(pledge, cfg=None):
    """Backwards-compatible name — the reminder is one kind of pledge message."""
    return build_pledge_text(pledge, kind="REMINDER", cfg=cfg)


def send_pledge_reminder(pledge=None, channel="SMS", user=None, cfg=None,
                         kind="REMINDER", pledges=None):
    """Send one pledge message. Returns the PledgeReminderLog row. Never raises.

    ``kind`` selects the wording — REMINDER, THANKS (for pledging), or
    FULFILLED (paid in full). Pass ``pledges`` to cover several of one member's
    promises in a single text; the log is attached to the first pledge.
    """
    cfg = cfg or SiteConfig.get()
    group = list(pledges) if pledges is not None else ([pledge] if pledge else [])
    group = [p for p in group if p is not None]
    if not group:
        return None
    primary = group[0]
    phone = primary.member.receipt_phone
    log = PledgeReminderLog(pledge=primary, channel=channel, to=phone or "",
                            sent_by=user)
    if any(p.reminders_opt_out for p in group):
        log.message = "(skipped — member opted out)"
        log.ok = False
        log.save()
        return log
    if not phone:
        log.message = "(skipped — no phone on file)"
        log.ok = False
        log.save()
        return log
    msg = build_pledge_text(kind=kind, cfg=cfg, pledges=group)
    log.message = msg
    try:
        if channel == "WHATSAPP":
            res = send_whatsapp(phone, msg, cfg)
        else:
            res = send_sms(phone, msg, cfg)
        # both return a log-like object with a status; treat SENT as ok
        status = getattr(res, "status", "")
        log.ok = str(status).upper() in ("SENT", "OK", "QUEUED", "SUCCESS")
    except Exception as exc:  # never fatal
        log.ok = False
        log.message = f"{msg}\n[error: {type(exc).__name__}: {exc}]"
    log.save()
    return log


def reminder_targets(campaign=None, due_within_days=None, tag=None,
                     kind="REMINDER"):
    """Who a bulk pledge message would go to — one entry per pledge.

    Prefer ``reminder_batches`` for sending: it groups by member so a person
    with several pledges gets one SMS, not several.
    """
    import datetime as dt
    from pledges.models import Pledge
    qs = Pledge.objects.filter(status=Pledge.Status.ACTIVE,
                               reminders_opt_out=False).select_related(
        "member", "campaign").prefetch_related("member__tags")
    if campaign:
        qs = qs.filter(campaign=campaign)
    if tag:
        qs = qs.filter(member__tags__name=tag).distinct()
    rows = list(qs) if kind == "THANKS" else [p for p in qs if p.outstanding > 0]
    if due_within_days is not None:
        cutoff = dt.date.today() + dt.timedelta(days=due_within_days)
        rows = [p for p in rows if p.end_date and p.end_date <= cutoff]
    return rows


def reminder_batches(campaign=None, due_within_days=None, tag=None,
                     kind="REMINDER"):
    """Group reminder targets by member — one SMS per person.

    Returns a list of dicts: ``{member, pledges, outstanding, amount, paid,
    campaigns}``. Order follows the first pledge of each member.
    """
    rows = reminder_targets(campaign=campaign, due_within_days=due_within_days,
                            tag=tag, kind=kind)
    by_member = {}
    order = []
    for p in rows:
        mid = p.member_id
        if mid not in by_member:
            by_member[mid] = {"member": p.member, "pledges": []}
            order.append(mid)
        by_member[mid]["pledges"].append(p)
    batches = []
    for mid in order:
        b = by_member[mid]
        pledges = b["pledges"]
        batches.append({
            "member": b["member"],
            "pledges": pledges,
            "amount": sum((p.amount for p in pledges), Decimal("0")),
            "paid": sum((p.paid for p in pledges), Decimal("0")),
            "outstanding": sum((p.outstanding for p in pledges), Decimal("0")),
            "campaigns": ", ".join(p.campaign.name for p in pledges),
            "pledge_count": len(pledges),
        })
    return batches


def maybe_send_submit_thanks(pledge_id):
    """Thank-you SMS after a public (or other) pledge is recorded. Never raises."""
    try:
        from pledges.models import Pledge
        cfg = SiteConfig.get()
        if not getattr(cfg, "pledge_send_submit_thanks", True):
            return None
        if not cfg.sms_enabled:
            return None
        pledge = (Pledge.objects.select_related("member", "campaign")
                  .filter(pk=pledge_id).first())
        if not pledge:
            return None
        if pledge.reminders_opt_out:
            return None
        return send_pledge_reminder(pledge, channel="SMS", kind="THANKS",
                                    cfg=cfg)
    except Exception:
        from core.utils import log_exception as _lx
        _lx("pledges/services/reminders.py")
        return None


def maybe_send_fulfilled_thanks(pledge_id):
    """Send the paid-in-full thank-you if settings allow. Safe to call after
    commit; never raises into the payment path."""
    try:
        from pledges.models import Pledge
        cfg = SiteConfig.get()
        if not getattr(cfg, "pledge_send_fulfilled_thanks", True):
            return None
        if not cfg.sms_enabled:
            return None
        pledge = (Pledge.objects.select_related("member", "campaign")
                  .filter(pk=pledge_id).first())
        if not pledge or pledge.status != Pledge.Status.FULFILLED:
            return None
        if pledge.reminders_opt_out:
            return None
        return send_pledge_reminder(pledge, channel="SMS", kind="FULFILLED",
                                    cfg=cfg)
    except Exception:
        from core.utils import log_exception as _lx
        _lx("pledges/services/reminders.py")
        return None

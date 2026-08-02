"""
Pledge reminders — reuses the existing SMS / WhatsApp services.

Sends a gentle reminder to a member about an outstanding pledge and logs it.
Never raises; respects the member's opt-out and the global channel switches.
"""
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


def build_pledge_text(pledge, kind="REMINDER", cfg=None, template=None):
    """Render a pledge message from the church's own wording.

    An unknown placeholder is left as it was typed rather than raising. A
    treasurer editing this text is not writing code, and a message that fails
    to send because of a stray brace is worse than one that reads slightly
    oddly — the preview is there to catch the latter.
    """
    cfg = cfg or SiteConfig.get()
    if template is None:
        field = ("pledge_thanks_template" if kind == "THANKS"
                 else "pledge_reminder_template")
        template = (getattr(cfg, field, "") or "").strip() or DEFAULTS[kind]
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


def send_pledge_reminder(pledge, channel="SMS", user=None, cfg=None,
                         kind="REMINDER"):
    """Send one pledge message. Returns the PledgeReminderLog row. Never raises.

    ``kind`` selects the wording — REMINDER for an outstanding balance, THANKS
    for acknowledging a pledge just recorded. Both go through the same opt-out,
    the same phone check and the same log, because a member who has asked not
    to be messaged has asked about all of it.
    """
    cfg = cfg or SiteConfig.get()
    phone = pledge.member.receipt_phone
    log = PledgeReminderLog(pledge=pledge, channel=channel, to=phone or "",
                            sent_by=user)
    if pledge.reminders_opt_out:
        log.message = "(skipped — member opted out)"
        log.ok = False
        log.save()
        return log
    if not phone:
        log.message = "(skipped — no phone on file)"
        log.ok = False
        log.save()
        return log
    msg = build_pledge_text(pledge, kind=kind, cfg=cfg)
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
    """Who a bulk pledge message would go to.

    A REMINDER goes to pledges with something still outstanding — there is
    nothing to remind a member of once they have paid. A THANKS goes to every
    active pledge, paid or not, because thanking somebody for a promise they
    have already kept is the whole point.

    ``tag`` narrows to members holding a role — the board, the committee — so a
    treasurer can address the group they mean rather than the whole roll.
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

"""
Pledge reminders — reuses the existing SMS / WhatsApp services.

Sends a gentle reminder to a member about an outstanding pledge and logs it.
Never raises; respects the member's opt-out and the global channel switches.
"""
from core.models import SiteConfig
from core.services.sms import send_sms
from core.services.whatsapp import send_whatsapp
from pledges.models import PledgeReminderLog


def build_reminder_text(pledge, cfg=None):
    cfg = cfg or SiteConfig.get()
    church = cfg.church_name or "our church"
    name = (pledge.member.name or "").title()
    return (f"Dear {name}, thank you for your pledge of KES {pledge.amount:,.0f} "
            f"to {pledge.campaign.name}. So far KES {pledge.paid:,.0f} received; "
            f"KES {pledge.outstanding:,.0f} outstanding. May God bless your "
            f"faithfulness. — {church}")


def send_pledge_reminder(pledge, channel="SMS", user=None, cfg=None):
    """Send one reminder. Returns the PledgeReminderLog row. Never raises."""
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
    msg = build_reminder_text(pledge, cfg)
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


def reminder_targets(campaign=None, due_within_days=None):
    """Active pledges with an outstanding balance that haven't opted out — the
    list a treasurer would remind. Optionally limited to a campaign or to pledges
    whose end date is within N days."""
    import datetime as dt
    from pledges.models import Pledge
    qs = Pledge.objects.filter(status=Pledge.Status.ACTIVE,
                               reminders_opt_out=False).select_related(
        "member", "campaign")
    if campaign:
        qs = qs.filter(campaign=campaign)
    rows = [p for p in qs if p.outstanding > 0]
    if due_within_days is not None:
        cutoff = dt.date.today() + dt.timedelta(days=due_within_days)
        rows = [p for p in rows if p.end_date and p.end_date <= cutoff]
    return rows

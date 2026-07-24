"""Sending a message to one group of a campaign's members.

Bulk SMS is the one action in this application that cannot be undone and that
costs money per press. A mistake here is not a wrong number in a report — it is
four hundred people receiving the wrong thing, and no way to recall it.

So the design is deliberately cautious in three ways:

* **Preview before send.** `preview()` resolves exactly who would be written to,
  with the message each of them would get, and it is the same code path `send()`
  uses. The count on the confirmation screen is therefore the real count, not an
  estimate of it.
* **No silent skipping.** Members without a usable phone number are returned
  explicitly rather than quietly dropped, because "sent to 38 of 52" is
  information the sender needs before they decide the job is done.
* **Every message is logged.** `send_sms` writes an `SmsLog` row whether it
  succeeds, fails, or finds SMS switched off — so what went out is answerable
  afterwards.

Nothing here formats money or touches a fund; a campaign message is
communication, not accounting.
"""
from members.models import normalize_phone


#: Placeholders a sender may use in the message body. Kept short and obvious —
#: a treasurer writing this on a phone should not need a reference card.
PLACEHOLDERS = {
    "{name}": "the member's name as it appears on the sheet",
    "{group}": "their group number or name",
    "{campaign}": "the campaign name",
}


def groups_for(campaign):
    """Every group on the campaign's uploaded sheet, with its members.

    Ordered so numeric groups sort as numbers — "Group 2" before "Group 10",
    which is what a person expects and what a plain text sort gets wrong.
    Members with no group are collected under a single unnamed heading rather
    than dropped, because a member the sheet forgot to group is exactly the one
    somebody needs to find.
    """
    buckets = {}
    for member in campaign.members.all().order_by("name"):
        key = (member.group or "").strip()
        buckets.setdefault(key, []).append(member)

    def sort_key(name):
        digits = "".join(ch for ch in name if ch.isdigit())
        return (0 if digits else 1, int(digits) if digits else 0, name.lower())

    rows = []
    for name in sorted(buckets, key=sort_key):
        members = buckets[name]
        reachable = [m for m in members if normalize_phone(m.phone)]
        rows.append({
            "name": name,
            "label": name or "No group recorded",
            "members": members,
            "count": len(members),
            "reachable": len(reachable),
            "unreachable": len(members) - len(reachable),
        })
    return rows


def render_message(template, *, member, campaign):
    """Fill the placeholders for one member."""
    text = template or ""
    for token, value in (("{name}", member.name),
                         ("{group}", member.group or ""),
                         ("{campaign}", campaign.name)):
        text = text.replace(token, str(value))
    return text


def preview(campaign, group, template):
    """Who would be written to, and what each would receive.

    The same resolution `send` performs, so the confirmation screen cannot
    disagree with what actually happens.
    """
    wanted = (group or "").strip()
    recipients, skipped = [], []
    for member in campaign.members.filter(group=wanted).order_by("name"):
        phone = normalize_phone(member.phone)
        row = {"member": member, "phone": phone,
               "message": render_message(template, member=member,
                                         campaign=campaign)}
        (recipients if phone else skipped).append(row)
    return {"recipients": recipients, "skipped": skipped,
            "count": len(recipients), "skipped_count": len(skipped)}


def recent_sends(campaign, group=None, limit=10):
    """What has already gone out, so a treasurer can see before sending again."""
    from ..models import CampaignMessage
    qs = CampaignMessage.objects.filter(campaign=campaign).select_related("sent_by")
    if group is not None:
        qs = qs.filter(group=(group or "").strip())
    return list(qs[:limit])


def already_sent(campaign, group, template, *, within_hours=48):
    """The same message, to the same group, recently.

    Compared on the composed template rather than the rendered messages, since
    those differ per member by design. A duplicate is not blocked — a church may
    legitimately repeat a reminder — but it is put in front of the person about
    to press send, which is the part that was missing.
    """
    import datetime as _dt

    from django.utils import timezone

    from ..models import CampaignMessage
    cutoff = timezone.now() - _dt.timedelta(hours=within_hours)
    return (CampaignMessage.objects
            .filter(campaign=campaign, group=(group or "").strip(),
                    body=(template or "").strip(), sent_at__gte=cutoff)
            .first())


def send(campaign, group, template, *, user=None):
    """Actually send. Returns what happened, per member.

    One `SmsLog` row per message, written by `send_sms` itself, plus one
    `CampaignMessage` recording the batch as a whole — the log answers "what did
    this number receive", the campaign message answers "have we told this group
    yet", and neither answers the other.
    """
    from core.models import SiteConfig
    from core.services.sms import send_sms

    from ..models import CampaignMessage

    plan = preview(campaign, group, template)
    cfg = SiteConfig.get()

    # The record is opened BEFORE the first message and updated as the send
    # proceeds. A large group sent inside a web request can hit the server's
    # timeout part-way through; written only at the end, the messages that had
    # already gone would leave no trace, and the treasurer would be left
    # guessing whether to send again.
    record = CampaignMessage.objects.create(
        campaign=campaign, group=(group or "").strip(),
        body=(template or "").strip(), sent_by=user,
        skipped_count=plan["skipped_count"],
        intended_count=plan["count"],
        state=CampaignMessage.State.RUNNING)

    sent = failed = 0
    try:
        for i, row in enumerate(plan["recipients"], start=1):
            log = send_sms(row["phone"], row["message"], cfg=cfg)
            if getattr(log, "status", "") == "SENT":
                sent += 1
            else:
                failed += 1
            # Checkpoint often enough that an interrupted send is accurate to
            # within a handful of messages, rarely enough not to make a write
            # per text message.
            if i % 25 == 0:
                CampaignMessage.objects.filter(pk=record.pk).update(
                    sent_count=sent, failed_count=failed)
    finally:
        CampaignMessage.objects.filter(pk=record.pk).update(
            sent_count=sent, failed_count=failed,
            state=(CampaignMessage.State.DONE
                   if sent + failed == plan["count"]
                   else CampaignMessage.State.INTERRUPTED))

    return {"sent": sent, "failed": failed,
            "skipped": plan["skipped_count"],
            "total": plan["count"] + plan["skipped_count"],
            "record": record}

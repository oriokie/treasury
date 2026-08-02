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
from django.db.models import Q

from members.models import normalize_phone


#: The `group` argument that means "everyone on the sheet, whatever their
#: group". It cannot be "" — that already means something else and specific:
#: the members the sheet left ungrouped. A separate sentinel keeps the two
#: apart everywhere they are stored, counted and shown in history.
ALL_GROUPS = "*"

#: "Only the groups still short of the target set on the fund's budget page."
#: A second sentinel rather than a flag on the send: the audience it produces
#: depends on money collected TODAY, so what it means changes between one press
#: and the next, and the history has to record which it was.
BEHIND_TARGET = "<behind>"

#: "Only the groups that have reached their target." The other half of the same
#: question, and the half worth asking: a church that only ever writes to the
#: people who are behind is a church whose members hear from the treasurer only
#: when they have fallen short. A group that finished should be told so.
#:
#: Same reasoning as BEHIND_TARGET for being a sentinel — who it means depends
#: on the money at the moment it is pressed, so the history must record which
#: send it was rather than a list of names that was true once.
TARGET_MET = "<met>"

#: Placeholders a sender may use in the message body. Kept short and obvious —
#: a treasurer writing this on a phone should not need a reference card.
PLACEHOLDERS = {
    "{name}": "the member's name as it appears on the sheet",
    "{group}": "their group as written on the sheet, e.g. CAMP_1",
    "{group_no}": "just the number in it, e.g. 1",
    "{goal}": "their group's target from the fund's budget page",
    "{collected}": "what their group has raised so far this year",
    "{short}": "how much their group still needs",
    "{campaign}": "the campaign name",
}


def group_progress(campaign, year=None):
    """Each group's fund, its target, and how far short it is.

    The target is the `contribution_goal` on the group's own sub-account —
    the same figure the fund's budget page shows and the same one it edits.
    Read, never written, and never created: `Campaign.subgroup_department`
    makes a fund on demand, which is right when money is arriving and quite
    wrong when a treasurer is only asking who is behind.

    A group whose fund does not exist yet, or which has no target set, is
    reported with `has_target` False rather than as "behind" — nobody is behind
    a target nobody set, and chasing them for it would be the church's error
    showing up as the member's.
    """
    import datetime as _dt
    from decimal import Decimal

    from django.db.models import Sum

    from departments.models import Department
    from giving.models import Transaction

    year = year or _dt.date.today().year
    parent = campaign.department
    subs = {d.name.strip().lower(): d
            for d in Department.objects.filter(parent=parent, active=True)}

    def _fund_ids(d):
        ids = [d.id]
        for sub in d.subgroups.all():
            ids.extend(_fund_ids(sub))
        return ids

    rows = []
    for g in groups_for(campaign):
        fund = subs.get((g["name"] or "").strip().lower())
        goal = (fund.contribution_goal or Decimal(0)) if fund else Decimal(0)
        collected = Decimal(0)
        if fund is not None:
            collected = (Transaction.objects.filter(
                department_id__in=_fund_ids(fund),
                direction=Transaction.Direction.CREDIT, confirmed=True,
                is_reversal=False, is_reversed=False,
                excluded_from_income=False, date__year=year)
                .aggregate(t=Sum("amount"))["t"] or Decimal(0))
        short = max(goal - collected, Decimal(0))
        rows.append({
            **g,
            "fund": fund,
            "goal": goal,
            "collected": collected,
            "short": short,
            "has_target": bool(fund is not None and goal > 0),
            "behind": bool(fund is not None and goal > 0 and short > 0),
            "pct": int(min(collected / goal * 100, 100)) if goal else 0,
        })
    return rows


def behind_target_groups(campaign, year=None):
    """Just the group names still short of their target."""
    return [r["name"] for r in group_progress(campaign, year) if r["behind"]]


def target_met_groups(campaign, year=None):
    """Just the group names that have reached their target.

    A group with no target set is not "met" any more than it was "behind" —
    both readings require a target to measure against, and inventing one would
    thank a group for clearing a bar nobody put up.
    """
    return [r["name"] for r in group_progress(campaign, year)
            if r["has_target"] and not r["behind"]]


def group_number(group_name):
    """The number inside a group's name — "1" from "CAMP_1", "10" from
    "Group 10".

    A sheet's groups are named for filing, not for reading aloud, so a message
    built from `{group}` says "your group CAMP_1 meets at 9" when what the
    member should read is "your group 1 meets at 9".

    The FIRST run of digits, not every digit in the name: "CAMP_1_B" is group
    1, and joining all the digits in "CAMP_1_2" into "12" would invent a group
    that does not exist. Digits are returned as written, so a sheet that
    deliberately numbers its groups 01..30 keeps its own convention.

    A group with no digits in it has no number, and the caller decides what to
    do about that.
    """
    import re
    m = re.search(r"\d+", group_name or "")
    return m.group(0) if m else ""


def _group_sort_key(name):
    """Numeric groups sort as numbers — "Group 2" before "Group 10", which is
    what a person expects and what a plain text sort gets wrong. Groups without
    a number sort after, alphabetically."""
    digits = group_number(name)
    return (0 if digits else 1, int(digits) if digits else 0, name.lower())


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

    rows = []
    for name in sorted(buckets, key=_group_sort_key):
        members = buckets[name]
        reachable = [m for m in members if normalize_phone(m.phone)]
        rows.append({
            "name": name,
            "label": group_label(name),
            "number": group_number(name),
            "members": members,
            "count": len(members),
            "reachable": len(reachable),
            "unreachable": len(members) - len(reachable),
        })
    return rows


def _money(value):
    """A figure as it should read in a text message: no decimals, thousands
    separated. "5,000" rather than "5000.00" — this is a sentence, not a ledger."""
    from decimal import Decimal
    try:
        return f"{Decimal(value):,.0f}"
    except Exception:      # noqa: BLE001
        return "0"


def render_message(template, *, member, campaign, progress=None):
    """Fill the placeholders for one member.

    `{group_no}` falls back to the group's full name when there is no number in
    it. A church that names a group "Youth" should get "your group Youth", not
    the hole in the sentence that an empty string would leave.

    `progress` maps group name -> the row from `group_progress`, which is what
    lets one message tell each group its OWN shortfall. Absent, the money
    placeholders resolve to 0 rather than being left as raw braces — a member
    should never receive a text with "{short}" in it.

    Longest token first, so a placeholder that starts with another one cannot
    be half-substituted.
    """
    group = member.group or ""
    row = (progress or {}).get(group) or {}
    text = template or ""
    for token, value in (("{group_no}", group_number(group) or group),
                         ("{collected}", _money(row.get("collected", 0))),
                         ("{campaign}", campaign.name),
                         ("{short}", _money(row.get("short", 0))),
                         ("{group}", group),
                         ("{goal}", _money(row.get("goal", 0))),
                         ("{name}", member.name)):
        text = text.replace(token, str(value))
    return text


def audience(campaign, group):
    """The members one send is addressed to, in the order they will be written.

    `ALL_GROUPS` means everyone on the sheet. Ordered by group before name so
    that a whole-campaign send reads as a sequence of groups on the
    confirmation screen rather than one undifferentiated list of four hundred
    names — the sender is checking group coverage, not spelling.
    """
    qs = campaign.members.all()
    if group == BEHIND_TARGET:
        return qs.filter(group__in=behind_target_groups(campaign)).order_by("group", "name")
    if group == TARGET_MET:
        return qs.filter(group__in=target_met_groups(campaign)).order_by("group", "name")
    if group != ALL_GROUPS:
        qs = qs.filter(group=(group or "").strip())
        return qs.order_by("name")
    return qs.order_by("group", "name")


def preview(campaign, group, template):
    """Who would be written to, and what each would receive.

    The same resolution `send` performs, so the confirmation screen cannot
    disagree with what actually happens.
    """
    # Resolved once for the whole send rather than per member: a message naming
    # each group's shortfall would otherwise re-total that group's fund for
    # every member of it.
    progress = {r["name"]: r for r in group_progress(campaign)} \
        if any(t in (template or "") for t in ("{short}", "{goal}", "{collected}")) \
        else {}

    recipients, skipped = [], []
    for member in audience(campaign, group):
        phone = normalize_phone(member.phone)
        row = {"member": member, "phone": phone,
               "group": member.group or "",
               "message": render_message(template, member=member,
                                         campaign=campaign, progress=progress)}
        (recipients if phone else skipped).append(row)
    return {"recipients": recipients, "skipped": skipped,
            "count": len(recipients), "skipped_count": len(skipped),
            "groups": sorted({r["group"] for r in recipients})}


def gap_warning(plan, template):
    """Recipients for whom a group placeholder resolves to nothing.

    `{group_no}` falls back to the group's name, which covers a group called
    "Youth" — but a member the sheet never grouped has neither, and the message
    goes out reading "Your group is . Please arrive by 4pm." That is not worth
    blocking a send over; it IS worth the sender seeing before they press,
    which is what a whole-campaign send makes likely for the first time (a
    per-group send to the ungrouped is at least obviously that).
    """
    if not any(tok in (template or "") for tok in ("{group}", "{group_no}")):
        return []
    return [r["member"] for r in plan["recipients"] if not (r["group"] or "").strip()]


def breakdown(plan):
    """Per-group counts for a plan, in the same order the send will run.

    A whole-campaign confirmation shows the first eight recipients, and on a
    real sheet those are all from the first group — so the sample cannot answer
    the question the sender has, which is whether every group is covered.
    """
    rows = {}
    for row in plan["recipients"]:
        rows.setdefault(row["group"], {"count": 0, "skipped": 0})["count"] += 1
    for row in plan["skipped"]:
        rows.setdefault(row["group"], {"count": 0, "skipped": 0})["skipped"] += 1
    out = []
    for name in sorted(rows, key=_group_sort_key):
        out.append({"name": name, "label": group_label(name),
                    "number": group_number(name), **rows[name]})
    return out


def group_label(group):
    """How a group is named on screen. One place, so the confirmation screen,
    the history line and the flash message cannot describe the same send
    differently."""
    if group == ALL_GROUPS:
        return "every group"
    if group == BEHIND_TARGET:
        return "the groups behind target"
    if group == TARGET_MET:
        return "the groups that reached target"
    return group or "No group recorded"


def recent_sends(campaign, group=None, limit=10):
    """What has already gone out, so a treasurer can see before sending again.

    Asking about one group also returns the whole-campaign sends, because those
    reached this group too. Leaving them out would show Group 2 as never
    written to on the day everybody was written to — which is precisely the
    moment somebody sends the message a second time.
    """
    from ..models import CampaignMessage
    qs = CampaignMessage.objects.filter(campaign=campaign).select_related("sent_by")
    if group is not None:
        wanted = (group or "").strip()
        if wanted != ALL_GROUPS:
            qs = qs.filter(Q(group=wanted) | Q(group=ALL_GROUPS))
        else:
            qs = qs.filter(group=ALL_GROUPS)
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
    wanted = (group or "").strip()
    qs = CampaignMessage.objects.filter(
        campaign=campaign, body=(template or "").strip(), sent_at__gte=cutoff)
    # A whole-campaign send already reached this group, so the same words going
    # out to one group afterwards is the same duplicate — and the one most
    # likely to happen, since the two are composed on different screens.
    if wanted != ALL_GROUPS:
        qs = qs.filter(Q(group=wanted) | Q(group=ALL_GROUPS))
    else:
        qs = qs.filter(group=ALL_GROUPS)
    return qs.first()


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

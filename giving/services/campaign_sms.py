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
    "{code}": "their rallying code — supporters put it in the bank reference",
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
    from departments.models import subtree_ids
    subs = {d.name.strip().lower(): d
            for d in Department.objects.filter(parent=parent, active=True)}

    groups = groups_for(campaign)
    root_ids = []
    for g in groups:
        fund = subs.get((g["name"] or "").strip().lower())
        g["_fund"] = fund
        if fund is not None:
            root_ids.append(fund.id)

    collected_by_fund = {}
    children = {}
    if root_ids:
        tree_ids = subtree_ids(root_ids)
        for fid, parent_id in Department.objects.filter(
                id__in=tree_ids).values_list("id", "parent_id"):
            if parent_id:
                children.setdefault(parent_id, []).append(fid)
        for row in (Transaction.objects.filter(
                department_id__in=tree_ids,
                direction=Transaction.Direction.CREDIT, confirmed=True,
                is_reversal=False, is_reversed=False,
                excluded_from_income=False, date__year=year)
                .values("department_id").annotate(t=Sum("amount"))):
            collected_by_fund[row["department_id"]] = row["t"] or Decimal(0)

    def _tree_ids(root):
        ids = [root]
        for child in children.get(root, []):
            ids.extend(_tree_ids(child))
        return ids


    rows = []
    for g in groups:
        fund = g.pop("_fund")
        ids = _tree_ids(fund.id) if fund is not None else []
        goal = (fund.contribution_goal or Decimal(0)) if fund else Decimal(0)
        collected = sum((collected_by_fund.get(i, Decimal(0)) for i in ids),
                        Decimal(0))
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
                         ("{code}", member.match_code or ""),
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


def _campaign_fund_ids(campaign):
    """The campaign's fund and every nested sub-account — same tree the
    group-progress and "not contributed to campaign" SMS criterion use."""
    from departments.models import subtree_ids
    if campaign.department_id is None:
        return []
    return list(subtree_ids([campaign.department_id]))


def _campaign_gift_qs(campaign, start=None, end=None, extra_q=None):
    """Confirmed credits on the campaign's fund tree, or already stamped
    ``campaign=…``. Shared by sheet-member giving and the ungrouped export so
    the two lists cannot disagree about which gifts are in scope."""
    from django.db.models import Q

    from giving.models import Transaction

    fund_ids = _campaign_fund_ids(campaign)
    scoped = Q(campaign=campaign)
    if fund_ids:
        scoped |= Q(department_id__in=fund_ids)
    q = scoped if extra_q is None else (scoped & extra_q)
    qs = (Transaction.objects.filter(
            q,
            direction=Transaction.Direction.CREDIT, confirmed=True,
            is_reversal=False, is_reversed=False, excluded_from_income=False)
          .select_related("member"))
    if start:
        qs = qs.filter(date__gte=start)
    if end:
        qs = qs.filter(date__lte=end)
    return qs


def _sheet_matcher(members):
    """Attribute a gift to a campaign sheet row, in memory.

    Same order as the campaign page: rallying code, then phone, then unique
    name. Built once for a sheet rather than hitting the campaign-code table
    per gift — that is what timed the page out.

    Returns ``(by_phone, ranked_codes, resolve)``. ``resolve(txn)`` is a sheet
    row or ``None``.
    """
    from members.models import name_key, normalize_phone
    from pledges.services.codes import _norm_code, codes_in_reference

    by_phone = {}
    by_name = {}
    ranked_codes = []
    for m in members:
        ph = normalize_phone(m.phone)
        if ph:
            by_phone.setdefault(ph, []).append(m)
        if m.name_key:
            by_name.setdefault(m.name_key, []).append(m)
        code = _norm_code(m.match_code)
        if len(code) >= 4:
            ranked_codes.append((code, m.match_code, m))
    ranked_codes.sort(key=lambda row: len(row[0]), reverse=True)

    def _by_phone(raw):
        ph = normalize_phone(raw or "")
        if ph and len(by_phone.get(ph, [])) == 1:
            return by_phone[ph][0]
        return None

    def _by_name(raw):
        key = name_key(raw or "")
        if key and len(by_name.get(key, [])) == 1:
            return by_name[key][0]
        return None

    def _by_code(reference):
        # This campaign's codes only — never reload every active campaign's
        # sheet (find_campaign_member_by_code) once per gift.
        if not reference or not ranked_codes:
            return None
        s = codes_in_reference(reference)
        if not s:
            return None
        for code, _raw, m in ranked_codes:
            if code in s:
                return m
        return None

    def resolve(txn):
        cm = _by_code(txn.reference)
        if cm is not None:
            return cm
        cm = _by_phone(txn.payer_phone)
        if cm is not None:
            return cm
        if txn.member_id:
            cm = _by_phone(txn.member.phone)
            if cm is not None:
                return cm
            cm = _by_name(getattr(txn.member, "name_key", None)
                          or txn.member.name)
            if cm is not None:
                return cm
        return _by_name(txn.payer_name)

    return by_phone, ranked_codes, resolve


def member_contributions(campaign, start=None, end=None):
    """How much each sheet member has contributed in a period, and how often.

    Matching is phone-first against the sheet (``CampaignMember.phone``),
    because that is how the list is identified in the real world — a gift
    from 2547… belongs to the sheet row with that mobile, whether or not the
    transaction was tagged with the campaign FK at import time.

    Scope is the campaign's fund tree (parent + sub-accounts) *or* rows
    already stamped ``campaign=…``. Restricting to the FK alone left everyone
    dormant when gifts landed on the fund via other allocation paths
    (development-group tokens, manual allocate, etc.).

    Attribution order:
      1. Campaign match code in the reference (rallying for someone else)
      2. Mobile number — ``payer_phone``, then the credited register member's
         phone — must identify exactly one sheet row
      3. Unique name on the sheet

    Returns ``{campaign_member_id: {"amount": Decimal, "count": int}}``.
    """
    from decimal import Decimal

    from django.db.models import Q

    members = list(campaign.members.all())
    if not members:
        return {}

    by_phone, ranked_codes, resolve = _sheet_matcher(members)
    # Only pull gifts that can possibly match a sheet row. Scanning every
    # credit on a parent fund (e.g. Development, year-to-date) and then
    # hitting the campaign-code table once per row is what timed the page out.
    match_q = Q(campaign=campaign)
    if by_phone:
        match_q |= Q(payer_phone__in=list(by_phone.keys())) | Q(
            member__phone__in=list(by_phone.keys()))
    if ranked_codes:
        code_q = Q()
        for _norm, raw, _m in ranked_codes:
            if raw:
                code_q |= Q(reference__icontains=raw)
        match_q |= code_q

    totals = {m.id: {"amount": Decimal(0), "count": 0} for m in members}
    for txn in _campaign_gift_qs(campaign, start, end, extra_q=match_q).iterator():
        cm = resolve(txn)
        if cm is None:
            continue
        totals[cm.id]["amount"] += txn.amount
        totals[cm.id]["count"] += 1
    return totals


def ungrouped_contributors(campaign, start=None, end=None):
    """People who gave to the campaign in the period but are not on the
    uploaded group sheet.

    The member-giving Excel is the sheet. This is everyone else whose money
    still landed on the campaign's fund tree (or is stamped ``campaign=…``) —
    visitors, members the sheet missed, gifts that could not be matched to a
    row. A treasurer asking "who contributed but is not in any group in the
    list" is asking this, and it is the gap the group totals cannot explain.

    Matching is the same as ``member_contributions``, so a gift cannot appear
    in both lists. Computed on demand (the Excel download) rather than on
    every page load — scanning the whole fund tree is what timed the campaign
    page out before.

    Returns a list of dicts ``{name, phone, amount, count}``, sorted by name.
    """
    from decimal import Decimal

    from members.models import name_key, normalize_phone

    members = list(campaign.members.all())
    resolve = _sheet_matcher(members)[2] if members else (lambda _txn: None)

    def identity(txn):
        ph = normalize_phone(txn.payer_phone or "")
        if not ph and txn.member_id:
            ph = normalize_phone(txn.member.phone or "")
        if ph:
            return ("phone", ph)
        if txn.member_id:
            return ("member", txn.member_id)
        key = name_key(txn.payer_name or "")
        if not key and txn.member_id:
            key = txn.member.name_key or name_key(txn.member.name)
        if key:
            return ("name", key)
        return ("txn", txn.id)

    buckets = {}
    for txn in _campaign_gift_qs(campaign, start, end).iterator():
        if resolve(txn) is not None:
            continue
        row = buckets.setdefault(identity(txn), {
            "name": "", "phone": "", "amount": Decimal(0), "count": 0,
        })
        row["amount"] += txn.amount
        row["count"] += 1
        name = (txn.member.name if txn.member_id else "") or (txn.payer_name or "")
        phone = ""
        if txn.member_id:
            phone = (normalize_phone(txn.member.phone or "")
                     or txn.member.phone or "")
        if not phone:
            phone = (normalize_phone(txn.payer_phone or "")
                     or txn.payer_phone or "")
        if name and (not row["name"] or txn.member_id):
            row["name"] = name
        if phone and not row["phone"]:
            row["phone"] = phone

    out = sorted(buckets.values(), key=lambda r: (r["name"] or "").lower())
    for row in out:
        if not row["name"]:
            row["name"] = row["phone"] or "Unknown"
    return out

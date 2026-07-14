"""The case statement — what a treasurer posts to WhatsApp after a case.

Modelled directly on the document a real church already produces by hand
(CASE_68.docx): a short summary table, then three lists of names — who newly
registered, who contributed, and who did not.

That last list is the point of the whole exercise. A benevolent scheme runs on
the plain fact that everybody can see who stood with the bereaved family and who
did not. The treasurer was building it by hand, from a spreadsheet, for every
case. The system already holds every fact in it.
"""
import datetime as _dt
from decimal import Decimal

from django.db.models import Sum


def case_statement(case, as_of=None):
    """Everything the WhatsApp update needs, computed once.

    Membership is taken AS THE CASE SEES IT — everyone who was on the roll when
    the event happened. A member who joined afterwards was not asked to
    contribute to this case and must not appear on its defaulters list; a member
    who has since left was, and must.
    """
    from benevolent.models import (BenevolentContribution, BenevolentPayout,
                                   SchemeMembership)
    from benevolent.services import contributions as contrib_svc

    as_of = as_of or _dt.date.today()
    scheme = case.scheme
    event_date = case.event_date

    # --- who was a member when this happened -------------------------------
    roll = (SchemeMembership.objects
            .filter(scheme=scheme, joined_on__lte=event_date)
            .exclude(status=SchemeMembership.Status.PENDING)
            .exclude(left_on__isnull=False, left_on__lt=event_date)
            .select_related("member")
            .order_by("member__name"))

    # the bereaved member is not a defaulter on their own case. They are the
    # reason for it.
    bereaved_id = case.membership_id

    # --- who paid ----------------------------------------------------------
    paid_rows = (BenevolentContribution.objects
                 .filter(case=case)
                 .filter(contrib_svc._effective_q())
                 .values("membership_id")
                 .annotate(total=Sum("transaction__amount")))
    paid_by = {r["membership_id"]: (r["total"] or Decimal(0)) for r in paid_rows}

    contributed, defaulted = [], []
    for m in roll:
        if m.pk in paid_by and paid_by[m.pk] > 0:
            contributed.append(m)
        elif m.pk != bereaved_id:
            defaulted.append(m)

    # --- who joined since the last case ------------------------------------
    # "New registrations" on a real case statement means those who came in since
    # the previous case — that is what the church means by it, and it is what the
    # registration fees on this statement actually relate to.
    prev = (scheme.cases.filter(event_date__lt=event_date)
            .order_by("-event_date").first())
    since = prev.event_date if prev else None
    new_regs = (SchemeMembership.objects
                .filter(scheme=scheme, joined_on__lte=event_date)
                .exclude(status=SchemeMembership.Status.PENDING)
                .select_related("member").order_by("member__name"))
    new_regs = ([m for m in new_regs if since is None or m.joined_on > since]
                if since is not None else list(new_regs))

    # --- the money ---------------------------------------------------------
    member_contribs = sum(paid_by.values(), Decimal(0))

    reg_fees = (BenevolentContribution.objects
                .filter(scheme=scheme,
                        kind=BenevolentContribution.Kind.REGISTRATION,
                        membership__in=[m.pk for m in new_regs])
                .filter(contrib_svc._effective_q())
                .aggregate(t=Sum("transaction__amount"))["t"] or Decimal(0))

    expenses = (BenevolentPayout.objects
                .filter(case=case, expense__status__in=["APPROVED", "PAID"])
                .aggregate(t=Sum("expense__amount"))["t"] or Decimal(0))

    total_in = member_contribs + reg_fees
    surplus = total_in - expenses

    return {
        "case": case,
        "scheme": scheme,
        "as_of": as_of,
        "registered": len(roll),
        "contributed": contributed,
        "defaulted": defaulted,
        "new_regs": new_regs,
        "n_contributed": len(contributed),
        "n_defaulted": len(defaulted),
        "n_new_regs": len(new_regs),
        "member_contributions": member_contribs,
        "registration_fees": reg_fees,
        "total_contribution": total_in,
        "expenses": expenses,
        "surplus": surplus,
        "paid_by": paid_by,
    }


def as_text(data, currency="KES"):
    """The statement as plain text, ready to paste into WhatsApp.

    Deliberately plain: no markdown, no emoji, no box-drawing. WhatsApp mangles
    most of it, and a treasurer pasting a broken table into a congregation group
    at 10pm is not a problem worth creating.
    """
    c = data["case"]
    beneficiary = c.beneficiary_name or (
        c.dependant.display_name if c.dependant_id else "") or (
        c.membership.member.name if c.membership_id else "")

    # "Mzee Harun Kanyi — Father to Grace Nyaboke". The relationship line is what
    # the congregation actually reads: it tells them WHOSE loss this is, which is
    # the whole reason anybody is being asked to contribute. A church's own
    # records have always carried it.
    rel = c.beneficiary_relationship
    if not rel and c.dependant_id and c.membership_id:
        rel = (f"{c.dependant.get_relationship_display()} to "
               f"{c.membership.member.name.title()}")
    title = f"{beneficiary}".strip()
    if rel:
        title = f"{title} — {rel}"
    title = f"{title}\n{c.number}" if title else c.number

    def money(v):
        return f"{v:,.0f}"

    lines = [title, ""]
    lines.append(f"No. of registered members: {data['registered']}")
    lines.append(f"No. of contributing members: {data['n_contributed']}")
    lines.append(f"No. of defaulting members: {data['n_defaulted']}")
    lines.append(f"No. of new registrations: {data['n_new_regs']}")
    lines.append("")
    lines.append(f"Contribution from members: {currency} {money(data['member_contributions'])}")
    lines.append(f"Fees for new registrations: {currency} {money(data['registration_fees'])}")
    lines.append(f"Total contribution: {currency} {money(data['total_contribution'])}")
    lines.append(f"Disbursement and expenses: ({currency} {money(data['expenses'])})")
    lines.append(f"Surplus: {currency} {money(data['surplus'])}")

    if data["new_regs"]:
        lines += ["", "NEW REGISTRATIONS"]
        lines += [f"{i}. {m.member.name.title()}"
                  for i, m in enumerate(data["new_regs"], 1)]

    lines += ["", f"MEMBERS WHO CONTRIBUTED TO {c.number}"]
    if data["contributed"]:
        lines += [f"{i}. {m.member.name.title()}"
                  for i, m in enumerate(data["contributed"], 1)]
    else:
        lines.append("(none yet)")

    lines += ["", f"MEMBERS WHO DID NOT CONTRIBUTE TO {c.number}"]
    if data["defaulted"]:
        lines += [f"{i}. {m.member.name.title()}"
                  for i, m in enumerate(data["defaulted"], 1)]
    else:
        lines.append("(none — everybody contributed)")

    return "\n".join(lines)

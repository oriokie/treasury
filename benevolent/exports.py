"""Excel / CSV exports for the benevolent module's tables (Round 9, item 5).

Every list a treasurer looks at — the register, the memberships, contributions,
cases — can now be downloaded as a spreadsheet. Nothing here recomputes a
financial figure: amounts come straight off the same rows the page shows (a
contribution's amount is its transaction's amount, a case's figures are the
case's own), so an export can never disagree with the screen it came from.

Each builder returns (header, rows) for the SAME queryset the view already
filtered, so the download respects whatever scheme / status / period / search
the user has applied. The view hands that off to reports.exports.xlsx_response
or csv_response — the one styled-workbook helper the rest of the app uses — so
there is a single export format across the whole system.
"""
from __future__ import annotations

from core.rights import display_phone
from reports.exports import csv_response, xlsx_response


def _fmt_date(d):
    return d.strftime("%Y-%m-%d") if d else ""


# ---------------------------------------------------------------------------
# Row builders — (header, rows) for a queryset the view has already filtered
# ---------------------------------------------------------------------------

def membership_rows(qs, *, user):
    header = ["Number", "Scheme", "Member", "Phone", "Status", "Standing",
              "Registration type", "Joined", "Renewed until", "Household"]
    rows = []
    for m in qs.select_related("scheme", "member"):
        rows.append([
            m.number, m.scheme.name if m.scheme_id else "",
            m.member.name if m.member_id else "",
            display_phone(user, (m.member.phone if m.member_id else "") or ""),
            m.get_status_display(), m.get_standing_display(),
            m.get_registration_type_display() if m.registration_type else "",
            _fmt_date(m.joined_on), _fmt_date(m.renewed_until),
            m.household_name or "",
        ])
    return header, rows


def registry_rows(rows_in, *, user):
    """The registry service returns membership rows in register order; export
    the same columns the register table shows. Arrears is deliberately not
    included: the register itself reads a cached standing rather than
    recomputing arrears per row, and computing it here would be an N+1 across
    the whole roll — the standing column already tells a treasurer who is
    behind, and the arrears reports give the figures."""
    header = ["Number", "Scheme", "Member", "Phone", "Status", "Standing",
              "Registration type", "Joined"]
    rows = []
    for m in rows_in:
        rows.append([
            m.number, m.scheme.name if m.scheme_id else "",
            m.member.name if m.member_id else "",
            display_phone(user, (m.member.phone if m.member_id else "") or ""),
            m.get_status_display(), m.get_standing_display(),
            m.get_registration_type_display() if m.registration_type else "",
            _fmt_date(m.joined_on),
        ])
    return header, rows


def contribution_rows(qs, *, user):
    header = ["Date", "Scheme", "Member", "Kind", "Amount", "Period",
              "Case", "Channel", "Auto?", "Note"]
    rows = []
    for c in qs.select_related("scheme", "membership__member", "transaction",
                               "case"):
        tx = c.transaction
        member = (c.membership.member.name
                  if c.membership_id and c.membership.member_id else "")
        rows.append([
            _fmt_date(tx.date if tx else None),
            c.scheme.name if c.scheme_id else "",
            member, c.get_kind_display(),
            float(c.amount) if c.amount is not None else "",
            c.period_label or "",
            c.case.number if c.case_id else "",
            (tx.get_channel_display() if tx and hasattr(tx, "get_channel_display")
             else ""),
            "yes" if c.allocated_automatically else "",
            c.note or "",
        ])
    return header, rows


def case_rows(qs, *, user):
    header = ["Number", "Old reference", "Scheme", "Member", "Beneficiary", "Relationship",
              "Event", "Event date", "Reported", "Status",
              "Claimed", "Approved", "Paid", "Funding target"]
    rows = []
    for c in qs.select_related("scheme", "event_type", "membership__member"):
        member = (c.membership.member.name
                  if c.membership_id and c.membership.member_id else "")
        rows.append([
            c.number, c.external_reference or "",
            c.scheme.name if c.scheme_id else "", member,
            c.beneficiary_display, c.beneficiary_relationship or "",
            c.event_type.name if c.event_type_id else "",
            _fmt_date(c.event_date), _fmt_date(c.reported_date),
            c.get_status_display(),
            float(c.claimed_amount) if c.claimed_amount is not None else "",
            float(c.approved_amount) if c.approved_amount is not None else "",
            float(c.paid_total) if c.paid_total else "",
            float(c.funding_target) if c.funding_target is not None else "",
        ])
    return header, rows


# ---------------------------------------------------------------------------
# Dispatch — one entry point a view calls when ?export= is present
# ---------------------------------------------------------------------------

def export_response(fmt, *, filename, title, header, rows, church=None):
    """Return an xlsx or csv HttpResponse. `fmt` is 'xlsx' or 'csv'."""
    if fmt == "csv":
        return csv_response(f"{filename}.csv", header, rows)
    return xlsx_response(f"{filename}.xlsx", header, rows,
                         title=title, church=church)

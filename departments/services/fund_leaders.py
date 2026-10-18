"""Build and send fund-leader period statements by SMS and email.

Figures come from ``reports.services.balances.department_summary`` — the same
opening / receipts / expenses / closing the fund reports use. Recipients
without a valid phone (SMS) or email (email) are skipped, never failed loudly.
"""
from __future__ import annotations

import datetime as dt
from decimal import Decimal

from django.core.exceptions import ValidationError
from django.core.validators import validate_email

from members.models import normalize_phone
from reports.services import balances


def _money(amount):
    return f"{(amount or Decimal(0)):,.0f}"


def _period_label(start, end):
    if start.year == end.year and start.month == end.month:
        return f"{start:%d}–{end:%d %b %Y}"
    return f"{start:%d %b %Y} – {end:%d %b %Y}"


def statement_rows_by_id(start, end):
    """``{dept_id: row}`` for every fund that has something to report."""
    return {
        r["department"].id: r
        for r in balances.department_summary(start, end, consolidated=False)
    }


def figures_for(dept, rows_by_id):
    """Opening / receipts / expenses / closing for one fund, or zeros."""
    r = rows_by_id.get(dept.id)
    if not r:
        return {
            "opening": Decimal(0), "receipts": Decimal(0),
            "expenses": Decimal(0), "transfers_in": Decimal(0),
            "transfers_out": Decimal(0), "closing": Decimal(0),
            "department": dept,
        }
    return r


def sms_body(leader, dept, figures, start, end, church=""):
    from core.services.sms import sms_first_name
    who = sms_first_name(leader.name) or leader.name
    lines = [
        f"{who}, {dept.name} ({_period_label(start, end)})",
        f"Opening: {_money(figures['opening'])}",
        f"Receipts: {_money(figures['receipts'])}",
        f"Expenses: {_money(figures['expenses'])}",
        f"Closing: {_money(figures['closing'])}",
    ]
    if church:
        lines.append(f"- {church}")
    return "\n".join(lines)


def email_body(leader, dept, figures, start, end, church=""):
    net_t = (figures.get("transfers_in") or Decimal(0)) - (
        figures.get("transfers_out") or Decimal(0))
    lines = [
        f"Dear {leader.name},",
        "",
        f"Fund statement — {dept.name}",
        f"Period: {_period_label(start, end)}",
        "",
        f"Opening balance:  {_money(figures['opening'])}",
        f"Receipts:         {_money(figures['receipts'])}",
        f"Expenses:         {_money(figures['expenses'])}",
    ]
    if net_t:
        lines.append(f"Net transfers:    {_money(net_t)}")
    lines += [
        f"Closing balance:  {_money(figures['closing'])}",
        "",
    ]
    if church:
        lines.append(f"{church}")
        lines.append("Church treasury")
    return "\n".join(lines)


def email_subject(dept, start, end, church=""):
    prefix = f"{church} — " if church else ""
    return f"{prefix}{dept.name} statement ({_period_label(start, end)})"


def subaccounts_to_email(dept, rows_by_id):
    """Direct child funds with any movement or balance in the period."""
    kids = list(dept.subgroups.filter(active=True).order_by("name"))
    out = []
    for sub in kids:
        fig = figures_for(sub, rows_by_id)
        if any((fig["opening"], fig["receipts"], fig["expenses"], fig["closing"],
                fig.get("transfers_in"), fig.get("transfers_out"))):
            out.append((sub, fig))
    return out


def send_sms_to_leader(leader, start, end, *, cfg=None, rows_by_id=None):
    """SMS one O/R/E/C message per linked fund. Returns (sent, skipped, failed)."""
    from core.models import SiteConfig
    from core.services.sms import send_sms

    cfg = cfg or SiteConfig.get()
    phone = normalize_phone(leader.phone)
    if not phone:
        return 0, 1, 0
    if not cfg.sms_enabled:
        return 0, 0, 1

    rows_by_id = rows_by_id or statement_rows_by_id(start, end)
    church = cfg.church_name or ""
    funds = list(leader.departments.filter(active=True).order_by("name"))
    if not funds:
        return 0, 1, 0

    sent = failed = 0
    for dept in funds:
        fig = figures_for(dept, rows_by_id)
        msg = sms_body(leader, dept, fig, start, end, church)
        log = send_sms(phone, msg, cfg)
        if getattr(log, "status", "") == "SENT":
            sent += 1
        else:
            failed += 1
    return sent, 0, failed


def send_email_to_leader(leader, start, end, *, cfg=None, rows_by_id=None):
    """Email the parent fund statement, then each active sub-account with
    activity as its own message. Returns (sent, skipped, failed)."""
    from core.models import SiteConfig
    from core.services.email import send_email, is_configured

    cfg = cfg or SiteConfig.get()
    email = (leader.email or "").strip()
    try:
        validate_email(email)
    except ValidationError:
        return 0, 1, 0
    if not is_configured(cfg):
        return 0, 0, 1

    rows_by_id = rows_by_id or statement_rows_by_id(start, end)
    church = cfg.church_name or ""
    funds = list(leader.departments.filter(active=True)
                 .prefetch_related("subgroups").order_by("name"))
    if not funds:
        return 0, 1, 0

    sent = failed = 0
    for dept in funds:
        fig = figures_for(dept, rows_by_id)
        ok, _ = send_email(
            email_subject(dept, start, end, church),
            email_body(leader, dept, fig, start, end, church),
            email, cfg=cfg)
        if ok:
            sent += 1
        else:
            failed += 1
        for sub, sfig in subaccounts_to_email(dept, rows_by_id):
            ok, _ = send_email(
                email_subject(sub, start, end, church),
                email_body(leader, sub, sfig, start, end, church),
                email, cfg=cfg)
            if ok:
                sent += 1
            else:
                failed += 1
    return sent, 0, failed


def default_period(today=None):
    """Month-to-date ending today — the usual treasurer period pick."""
    today = today or dt.date.today()
    return today.replace(day=1), today

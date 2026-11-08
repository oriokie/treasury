"""Advanta bulk-SMS integration.

Implements the common Advanta / QuickSMS v3 request shape:
    POST {base}/api/services/sendsms/
    {"apikey", "partnerID", "message", "shortcode", "mobile"}

The base URL, API key, partner ID and shortcode are all editable on the
Settings page, so they can be adjusted to match Advanta's current bulksms-api
documentation (https://www.advantasms.com/bulksms-api) without code changes.

Every attempt is recorded in SmsLog. Sending is a no-op (logged as DISABLED)
unless SMS is switched on and credentials are present.
"""
import json
import urllib.request

from members.models import normalize_phone
from core.models import SiteConfig, SmsLog


def sms_first_name(full):
    """The first word of a name, for a text message.

    People are stored as 'EDWIN ORIOKI'. A text should say Edwin, not every
    name on the record. A one-word name is used as it is, title-cased.
    """
    parts = (full or "").split()
    if not parts:
        return ""
    return parts[0].title()


def _format(template, **ctx):
    out = template or ""
    for k, v in ctx.items():
        out = out.replace("{" + k + "}", str(v))
    return out


def send_sms(to, message, cfg=None):
    """Send one SMS. Returns the SmsLog row. Never raises."""
    cfg = cfg or SiteConfig.get()
    phone = normalize_phone(to) or (to or "")
    log = SmsLog(to=phone, message=message)

    if not cfg.sms_enabled:
        log.status = SmsLog.Status.DISABLED
        log.response = "SMS is disabled in settings."
        log.save()
        return log
    if not (cfg.sms_api_key and cfg.sms_partner_id and cfg.sms_shortcode and phone):
        log.status = SmsLog.Status.FAILED
        log.response = "Missing SMS credentials or recipient."
        log.save()
        return log

    payload = {
        "apikey": cfg.sms_api_key,
        "partnerID": cfg.sms_partner_id,
        "message": message,
        "shortcode": cfg.sms_shortcode,
        "mobile": phone,
    }
    url = cfg.sms_api_url.rstrip("/") + "/api/services/sendsms/"
    try:
        from core.services.net import post_json
        _status, body = post_json(url, payload, timeout=15)
        log.status = SmsLog.Status.SENT
        log.response = body[:2000]
    except Exception as exc:  # network/credential errors surfaced, never fatal
        log.status = SmsLog.Status.FAILED
        log.response = f"{type(exc).__name__}: {exc}"[:2000]
    log.save()
    return log


# Compact labels keep SMS under one segment when possible.
_SMS_SHORT_LABELS = {
    "Combined Offering": "Combined",
    "Local Church Budget": "LCB",
}


def _sms_fund_label(name):
    """Short, member-facing fund name for a text message."""
    raw = (name or "").strip()
    if not raw:
        return "Gift"
    if raw in _SMS_SHORT_LABELS:
        return _SMS_SHORT_LABELS[raw]
    # Collapse "Combined Offering (Trust 50%)" → Combined when not already
    # rolled up via SplitFund (defensive for older rows).
    for full, short in _SMS_SHORT_LABELS.items():
        if raw.startswith(full):
            return short
    return raw


def format_gift_breakdown(envelope, *, include_total=False):
    """Minimal gift lines for SMS: ``Tithe - 200, Combined - 100``.

    Split-fund halves (e.g. Combined Offering Trust/Local) roll up to the
    parent name — same idea as the Sabbath Excel entries sheet — so the
    member sees what they gave, not the internal remittance split.
    Numbered small-group sub-accounts roll up to their parent fund.
    """
    from decimal import Decimal
    from departments.models import numbered_subgroup_parent_map
    from giving.models import SplitFund

    split_parent = {}
    for sf in SplitFund.objects.filter(active=True).prefetch_related(
            "components"):
        for comp in sf.components.all():
            split_parent[comp.department_id] = sf.name
    subgroup_parent = numbered_subgroup_parent_map()

    totals = {}  # label -> Decimal
    lines = list(envelope.lines.select_related("department").all())
    for line in lines:
        amt = line.amount or Decimal(0)
        if not amt:
            continue
        if line.department_id in split_parent:
            label = split_parent[line.department_id]
        else:
            dept = subgroup_parent.get(line.department_id, line.department)
            label = dept.name if dept is not None else "Gift"
        label = _sms_fund_label(label)
        totals[label] = totals.get(label, Decimal(0)) + amt

    if not totals:
        return ""

    # Trust-ish first is nice but we lack is_trust after rollup; keep
    # insertion order from preferred church funds then alpha.
    preferred = ["Tithe", "Combined", "LCB", "Camp Meeting", "Development"]
    order = {n: i for i, n in enumerate(preferred)}
    items = sorted(totals.items(), key=lambda kv: (order.get(kv[0], 99), kv[0]))
    body = ", ".join(f"{name} - {amt:,.0f}" for name, amt in items)
    if include_total:
        total = envelope.total
        if total is None:
            total = sum(totals.values(), Decimal(0))
        body = f"{body} · total {total:,.0f}"
    return body


def _inject_breakdown(text, breakdown, church_name=""):
    """Insert gift lines into a template that has no {breakdown} placeholder.

    Prefer ``: Tithe - 200, Combined - 100`` just before the blessing / church
    sign-off so the existing greeting and total stay intact.
    """
    if not breakdown:
        return text
    snippet = f": {breakdown}"
    # Common closing: "God bless you. - Church"
    for marker in (" God bless", " May God", " Blessings"):
        idx = text.find(marker)
        if idx > 0:
            return text[:idx].rstrip(" .") + snippet + "." + text[idx:]
    church = (church_name or "").strip()
    if church and text.rstrip().endswith(church):
        stem = text.rstrip()[: -len(church)].rstrip(" -–—")
        return f"{stem.rstrip(' .')}{snippet}. - {church}"
    return text.rstrip() + snippet + "."


def build_receipt_text(envelope, cfg=None, include_breakdown=None):
    """The receipt message body for an envelope (shared by SMS and WhatsApp).

    ``include_breakdown`` overrides Settings → SMS → include gift breakdown.
    When on, fills ``{breakdown}`` or injects compact gift lines into the
    message (``Tithe - 200, Combined - 100``).
    """
    cfg = cfg or SiteConfig.get()
    if include_breakdown is None:
        include_breakdown = bool(getattr(cfg, "sms_include_breakdown", False))
    template = cfg.sms_receipt_template or ""
    # Skip repeating the total when the template already shows {amount}.
    include_total = "{amount}" not in template
    breakdown = (
        format_gift_breakdown(envelope, include_total=include_total)
        if include_breakdown else ""
    )
    text = _format(template,
                   name=sms_first_name(envelope.contributor_name) or "Friend",
                   amount=f"{envelope.total:,.0f}",
                   receipt=envelope.receipt_no,
                   date=envelope.date.strftime("%d %b %Y"),
                   church=cfg.church_name,
                   breakdown=breakdown)
    if include_breakdown and breakdown and "{breakdown}" not in template:
        text = _inject_breakdown(text, breakdown, cfg.church_name)
    return text


def send_receipt_sms(envelope, cfg=None):
    """SMS a giving receipt for an Envelope, honouring the configured scope:
    off, all envelope entries, or bank receipts only — and only if the member
    has a phone number."""
    cfg = cfg or SiteConfig.get()
    if not cfg.sms_enabled:
        return None
    scope = cfg.sms_receipt_scope
    if scope == SiteConfig.SmsReceiptScope.OFF:
        return None
    if scope == SiteConfig.SmsReceiptScope.BANK and envelope.channel != "BANK":
        return None
    phone = envelope.member.receipt_phone if envelope.member else None
    if not phone:
        return None
    msg = build_receipt_text(envelope, cfg)
    log = send_sms(phone, msg, cfg)
    if log and log.status == SmsLog.Status.SENT:
        envelope.sms_sent = True
        envelope.save(update_fields=["sms_sent"])
    return log

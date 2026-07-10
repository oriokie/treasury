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


def build_receipt_text(envelope, cfg=None):
    """The receipt message body for an envelope (shared by SMS and WhatsApp)."""
    cfg = cfg or SiteConfig.get()
    return _format(cfg.sms_receipt_template,
                   name=envelope.contributor_name,
                   amount=f"{envelope.total:,.0f}",
                   receipt=envelope.receipt_no,
                   date=envelope.date.strftime("%d %b %Y"),
                   church=cfg.church_name)


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

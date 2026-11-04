"""Review-queue inquiry SMS — ask givers the purpose of selected gifts."""
from core.models import SiteConfig
from core.services.sms import _format, send_sms, sms_first_name


def inquiry_recipient_phone(txn):
    """Prefer the statement payer phone; else the linked member's receipt phone."""
    phone = (txn.payer_phone or "").strip()
    if phone:
        return phone
    member = getattr(txn, "member", None)
    if member is None:
        return ""
    return (member.receipt_phone or member.phone or "").strip()


def inquiry_display_name(txn):
    member = getattr(txn, "member", None)
    if member is not None and (member.name or "").strip():
        return sms_first_name(member.name) or "Friend"
    return sms_first_name(txn.payer_name) or "Friend"


def build_inquiry_text(txn, cfg=None):
    cfg = cfg or SiteConfig.get()
    template = (cfg.queue_inquiry_sms_template or "").strip()
    if not template:
        template = (
            "Dear {name}, {church} received KES {amount} on {date} "
            "(ref {reference}). Please call or WhatsApp {treasurer_phone} "
            "to confirm the purpose of this gift so we can allocate it "
            "correctly. Thank you."
        )
    return _format(
        template,
        name=inquiry_display_name(txn),
        amount=f"{txn.amount:,.0f}",
        date=txn.date.strftime("%d %b %Y") if txn.date else "",
        reference=(txn.reference or "").strip() or "—",
        church=cfg.church_name or "Church",
        treasurer_phone=(cfg.queue_inquiry_phone or "").strip() or "(office number)",
    )


def send_inquiry_sms(txn, cfg=None):
    """Send one inquiry SMS. Returns (SmsLog|None, skip_reason)."""
    cfg = cfg or SiteConfig.get()
    phone = inquiry_recipient_phone(txn)
    if not phone:
        return None, "no phone"
    if not (cfg.queue_inquiry_phone or "").strip():
        return None, "no treasurer phone"
    log = send_sms(phone, build_inquiry_text(txn, cfg), cfg)
    return log, None

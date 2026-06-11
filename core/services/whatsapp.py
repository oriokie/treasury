"""Optional WhatsApp delivery for receipts/reminders (Twilio or Africa's Talking).

Disabled unless ``whatsapp_enabled`` is set in Settings → Channels. Uses the same
TLS-verified HTTP helper as the rest of the app. Kept deliberately small — a
working scaffold that builds the right request per provider.
"""
from core.models import SiteConfig
from core.services.net import post_json


def send_whatsapp(to, message, cfg=None):
    """Send a WhatsApp message. Returns (ok, detail). Never raises."""
    cfg = cfg or SiteConfig.get()
    if not cfg.whatsapp_enabled:
        return False, "WhatsApp delivery is disabled in settings."
    if not (cfg.whatsapp_api_key and cfg.whatsapp_api_url and to):
        return False, "WhatsApp is not fully configured."
    provider = (cfg.whatsapp_provider or "TWILIO").upper()
    try:
        if provider == "TWILIO":
            # Twilio Content/Messages API (account-specific URL configured by the user)
            payload = {"From": f"whatsapp:{cfg.whatsapp_sender}",
                       "To": f"whatsapp:{to}", "Body": message}
            status, body = post_json(cfg.whatsapp_api_url, payload,
                                     headers={"authorization": f"Bearer {cfg.whatsapp_api_key}"})
        else:  # AFRICASTALKING
            payload = {"username": cfg.whatsapp_sender, "to": to, "message": message}
            status, body = post_json(cfg.whatsapp_api_url, payload,
                                     headers={"apiKey": cfg.whatsapp_api_key})
        if status >= 400:
            return False, f"HTTP {status}: {body[:160]}"
        return True, body[:160]
    except Exception as exc:  # noqa: BLE001
        return False, f"{type(exc).__name__}: {exc}"

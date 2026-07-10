"""Optional Safaricom Daraja (M-Pesa) integration scaffold for pulling Paybill
transactions in real time, replacing the weekly CSV upload.

Disabled unless ``daraja_enabled`` is set in Settings → Channels. This module
provides OAuth token retrieval and a placeholder for C2B/transaction pulls; the
webhook/confirmation endpoints are added when the church's Daraja app is live.
"""
import base64

from core.models import SiteConfig
from core.services.net import post_json

_BASES = {"SANDBOX": "https://sandbox.safaricom.co.ke",
          "PRODUCTION": "https://api.safaricom.co.ke"}


def _base(cfg):
    return _BASES.get((cfg.daraja_env or "SANDBOX").upper(), _BASES["SANDBOX"])


def get_access_token(cfg=None):
    """Fetch an OAuth access token. Returns (token, error)."""
    cfg = cfg or SiteConfig.get()
    if not cfg.daraja_enabled:
        return None, "Daraja is disabled in settings."
    if not (cfg.daraja_consumer_key and cfg.daraja_consumer_secret):
        return None, "Daraja consumer key/secret are not set."
    import urllib.request
    cred = base64.b64encode(
        f"{cfg.daraja_consumer_key}:{cfg.daraja_consumer_secret}".encode()).decode()
    url = _base(cfg) + "/oauth/v1/generate?grant_type=client_credentials"
    try:
        from core.services.net import _contexts
        req = urllib.request.Request(url, headers={"Authorization": f"Basic {cred}",
            "User-Agent": "Mozilla/5.0 (compatible; ChurchTreasury/1.0)"})
        for ctx in _contexts():
            try:
                with urllib.request.urlopen(req, timeout=20, context=ctx) as resp:
                    import json
                    data = json.loads(resp.read().decode())
                    return data.get("access_token"), None
            except Exception:  # noqa: BLE001
                continue
        return None, "Could not reach Daraja."
    except Exception as exc:  # noqa: BLE001
        return None, f"{type(exc).__name__}: {exc}"


def test_connection(cfg=None):
    token, err = get_access_token(cfg)
    return (True, "Token received.") if token else (False, err or "No token.")

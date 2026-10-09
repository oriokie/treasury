"""Safaricom Daraja: OAuth, and the Lipa na M-Pesa (STK) prompt used by a
member's personal contribution link.

Disabled unless ``daraja_enabled`` is set in Settings. The consumer key and
secret obtain a token; the passkey signs the STK password.
"""
import base64
import json
from datetime import datetime, timedelta, timezone

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


def _eat_timestamp():
    """Safaricom expects the password timestamp in East Africa Time."""
    eat = timezone(timedelta(hours=3))
    return datetime.now(eat).strftime("%Y%m%d%H%M%S")


def stk_password(cfg, timestamp):
    raw = f"{cfg.daraja_shortcode}{cfg.daraja_passkey}{timestamp}"
    return base64.b64encode(raw.encode()).decode()


def stk_ready(cfg=None):
    """(True, '') when a prompt can be sent, else (False, reason)."""
    cfg = cfg or SiteConfig.get()
    if not cfg.daraja_enabled:
        return False, "M-Pesa prompts are switched off in settings."
    missing = []
    if not cfg.daraja_consumer_key or not cfg.daraja_consumer_secret:
        missing.append("consumer key and secret")
    if not cfg.daraja_shortcode:
        missing.append("shortcode")
    if not cfg.daraja_passkey:
        missing.append("passkey")
    if missing:
        return False, "Daraja is missing " + " and ".join(missing) + "."
    return True, ""


def _stk_post(path, payload, cfg):
    token, err = get_access_token(cfg)
    if not token:
        return None, err or "Could not reach Daraja."
    url = _base(cfg) + path
    try:
        _status, body = post_json(
            url, payload, headers={"Authorization": f"Bearer {token}"}, timeout=30)
    except Exception as exc:  # noqa: BLE001
        return None, f"{type(exc).__name__}: {exc}"
    try:
        data = json.loads(body)
    except json.JSONDecodeError:
        return None, (body or "Daraja returned nothing.")[:300]
    if not isinstance(data, dict):
        return None, "Daraja returned an unexpected response."
    return data, None


def stk_push(phone, amount, account_ref, callback_url, cfg=None):
    """Ask Safaricom to prompt ``phone`` for ``amount`` shillings.

    Returns (response_dict, error). A response with ResponseCode 0 means the
    prompt was accepted, not that the payer has entered a PIN yet.
    """
    cfg = cfg or SiteConfig.get()
    ok, reason = stk_ready(cfg)
    if not ok:
        return None, reason
    timestamp = _eat_timestamp()
    txn_type = "CustomerBuyGoodsOnline" if cfg.daraja_txn_type == "TILL" \
        else "CustomerPayBillOnline"
    ref = (account_ref or "GIFT")[:12]
    payload = {
        "BusinessShortCode": cfg.daraja_shortcode,
        "Password": stk_password(cfg, timestamp),
        "Timestamp": timestamp,
        "TransactionType": txn_type,
        "Amount": int(amount),
        "PartyA": phone,
        "PartyB": cfg.daraja_shortcode,
        "PhoneNumber": phone,
        "CallBackURL": callback_url,
        "AccountReference": ref,
        "TransactionDesc": "Contribution",
    }
    data, err = _stk_post("/mpesa/stkpush/v1/processrequest", payload, cfg)
    if err:
        return None, err
    if str(data.get("ResponseCode", "")) != "0":
        return None, (data.get("errorMessage")
                      or data.get("ResponseDescription")
                      or "Safaricom did not accept the prompt.")[:255]
    return data, None


def stk_query(checkout_request_id, cfg=None):
    """Ask Daraja how a prompt ended. Returns (response_dict, error)."""
    cfg = cfg or SiteConfig.get()
    ok, reason = stk_ready(cfg)
    if not ok:
        return None, reason
    timestamp = _eat_timestamp()
    payload = {
        "BusinessShortCode": cfg.daraja_shortcode,
        "Password": stk_password(cfg, timestamp),
        "Timestamp": timestamp,
        "CheckoutRequestID": checkout_request_id,
    }
    return _stk_post("/mpesa/stkpushquery/v1/query", payload, cfg)

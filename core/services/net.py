"""Small HTTP helper for the few outbound calls the app makes (SMS, assistant LLM).

TLS verification is the usual source of trouble:

* Many Windows boxes and minimal Linux installs have no usable CA bundle, giving
  ``CERTIFICATE_VERIFY_FAILED: unable to get local issuer certificate``. The
  bundled ``certifi`` roots fix that.
* Some networks sit behind a TLS-inspecting proxy whose private CA is only in the
  *system* trust store (not in certifi).

So we try, in order: an explicit ``TREASURY_CA_BUNDLE`` (or ``SSL_CERT_FILE``),
then the system trust store, then certifi — using the first that establishes a
connection. An HTTP error status (e.g. 401) means TLS already succeeded, so it is
returned rather than retried. Verification is never disabled.
"""
import json as _json
import os
import ssl
import urllib.error
import urllib.request


def _contexts():
    ctxs = []
    bundle = os.environ.get("TREASURY_CA_BUNDLE") or os.environ.get("SSL_CERT_FILE")
    if bundle and os.path.exists(bundle):
        try:
            ctxs.append(ssl.create_default_context(cafile=bundle))
        except Exception:
            pass
    ctxs.append(ssl.create_default_context())          # system trust store
    try:
        import certifi
        ctxs.append(ssl.create_default_context(cafile=certifi.where()))
    except Exception:
        pass
    return ctxs


def _is_ssl_error(exc):
    return isinstance(exc, ssl.SSLError) or (
        isinstance(exc, urllib.error.URLError) and isinstance(exc.reason, ssl.SSLError))


def post_json(url, payload, headers=None, timeout=20):
    """POST a JSON body. Returns (status_code, decoded_text).

    A non-2xx status is returned (not raised) so callers can read the provider's
    error message. Only genuine network/TLS failures raise.
    """
    data = _json.dumps(payload).encode("utf-8")
    hdrs = {"Content-Type": "application/json",
            # A default "Python-urllib/x" User-Agent is blocked by some
            # Cloudflare-fronted APIs (HTTP 403, "error code: 1010"). Use a
            # normal one so providers like Groq accept the request.
            "User-Agent": "Mozilla/5.0 (compatible; ChurchTreasury/1.0)",
            "Accept": "application/json"}
    hdrs.update(headers or {})
    last_err = None
    for ctx in _contexts():
        req = urllib.request.Request(url, data=data, headers=hdrs)
        try:
            with urllib.request.urlopen(req, timeout=timeout, context=ctx) as resp:
                return resp.status, resp.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as exc:        # TLS fine, server said no
            return exc.code, exc.read().decode("utf-8", "replace")
        except Exception as exc:                     # noqa: BLE001
            last_err = exc
            if _is_ssl_error(exc):
                continue                             # try the next CA source
            raise
    raise last_err or RuntimeError("Could not establish a secure connection.")

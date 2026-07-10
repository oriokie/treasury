"""Outgoing email via the SMTP settings stored in SiteConfig (Settings → Email).

Builds a runtime connection from the DB config rather than static Django settings,
so the treasurer can configure email from the UI. All sends are best-effort.
"""
from core.models import SiteConfig


def _connection(cfg):
    from django.core.mail import get_connection
    port = cfg.email_port or 587
    # Port 465 is implicit SSL (SMTPS); 587 is STARTTLS. They are mutually
    # exclusive in Django's SMTP backend, and using STARTTLS against a 465 port
    # makes the socket hang until it times out — the cause of the disconnect.
    use_ssl = bool(getattr(cfg, "email_use_ssl", False)) or port == 465
    use_tls = bool(cfg.email_use_tls) and not use_ssl
    return get_connection(
        backend="django.core.mail.backends.smtp.EmailBackend",
        host=cfg.email_host, port=port,
        username=cfg.email_host_user or None,
        password=cfg.email_host_password or None,
        use_tls=use_tls, use_ssl=use_ssl,
        timeout=30)


def is_configured(cfg=None):
    cfg = cfg or SiteConfig.get()
    return bool(cfg.email_enabled and cfg.email_host and cfg.email_from)


def send_email(subject, body, to, cfg=None, html=None, attachments=None):
    """Send an email. Returns (ok, detail). Never raises into the caller.
    `attachments` is a list of (filename, content_bytes, mimetype)."""
    cfg = cfg or SiteConfig.get()
    if not is_configured(cfg):
        return False, "Email is not enabled/configured in Settings → Email."
    recipients = [to] if isinstance(to, str) else list(to)
    recipients = [r for r in recipients if r]
    if not recipients:
        return False, "No recipient email address."
    try:
        from django.core.mail import EmailMultiAlternatives
        msg = EmailMultiAlternatives(subject, body, cfg.email_from, recipients,
                                     connection=_connection(cfg))
        if html:
            msg.attach_alternative(html, "text/html")
        for (fname, content, mimetype) in (attachments or []):
            msg.attach(fname, content, mimetype)
        msg.send(fail_silently=False)
        return True, f"Sent to {', '.join(recipients)}."
    except Exception as exc:  # noqa: BLE001
        return False, f"{type(exc).__name__}: {exc}"


def test_email(to, cfg=None):
    return send_email("Church treasury — test email",
                      "This is a test email from your church treasury system. "
                      "If you received it, email is configured correctly.", to, cfg)

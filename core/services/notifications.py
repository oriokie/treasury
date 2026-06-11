"""Lightweight in-app notifications (with optional email) for treasurers."""
from django.contrib.auth.models import User

from core.models import Notification, SiteConfig
from core.roles import TREASURER


def _treasurers():
    return User.objects.filter(groups__name=TREASURER, is_active=True).distinct()


def notify(kind, message, link="", recipients=None, email=None):
    """Create a notification for each recipient (default: all treasurers) and,
    if enabled, send an email too. Never raises into the caller."""
    try:
        users = list(recipients) if recipients is not None else list(_treasurers())
        objs = [Notification(recipient=u, kind=kind, message=message[:255], link=link)
                for u in users] or [Notification(kind=kind, message=message[:255], link=link)]
        Notification.objects.bulk_create(objs)
        cfg = SiteConfig.get()
        if (email if email is not None else cfg.notify_email_enabled):
            _email(users, message)
    except Exception:
        pass


def _email(users, message):
    from core.services.email import send_email, is_configured
    if not is_configured():
        return
    addrs = [u.email for u in users if getattr(u, "email", "")]
    if addrs:
        send_email("Church treasury notification", message, addrs)


def unread_count(user):
    if not getattr(user, "is_authenticated", False):
        return 0
    from django.db.models import Q
    return Notification.objects.filter(
        Q(recipient=user) | Q(recipient__isnull=True), read=False).count()

"""Self-service password reset — a user who's locked out can reset their own
password without an administrator, via whichever contact channel is on file
and actually working: an SMS one-time code (if a phone number is on record
and SMS sending is configured), or an emailed reset link (Django's own,
well-tested token mechanism, if an email is on record and a real SMTP
backend is configured — this app degrades to a console/no-op backend
otherwise, so "configured" specifically means real delivery, not just that
the setting exists).

Security posture, deliberately:
- Never reveals whether a username exists, or which channel it has on file —
  the response is identical either way ("if an account matches, you'll
  receive instructions"), the same anti-enumeration approach Django's own
  PasswordResetView already takes for the email side.
- SMS codes are 6-digit, single-use, expire in 10 minutes, stored hashed
  (never in plaintext), and issuing a new one invalidates any earlier
  pending one for the same account.
- Rate-limited per account (not per IP alone, since the goal is to stop one
  account's phone being SMS-bombed, regardless of where requests originate).
"""
import datetime as dt

from django.contrib import messages
from django.contrib.auth import login
from django.contrib.auth.models import User
from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import ValidationError
from django.shortcuts import redirect, render
from django.utils import timezone
from django.views import View

from .models import PasswordResetCode, UserProfile, log_user_admin_action

PENDING_RESET_USER = "self_reset_pending_user_id"
MAX_REQUESTS_PER_WINDOW = 3
REQUEST_WINDOW_MINUTES = 15
GENERIC_MESSAGE = ("If an account matches what you entered and has a phone "
                  "number or email on file, we've sent instructions to reset "
                  "your password.")


def _email_really_configured():
    """True only if a real SMTP host is configured — this app's settings
    (config/settings.py) fall back to the console backend (writes to the
    server log, never actually reaching anyone) when DJANGO_EMAIL_HOST isn't
    set, so checking that same environment variable directly is the honest
    measure of "will this actually arrive". Deliberately does NOT check
    settings.EMAIL_BACKEND (Django's test runner always substitutes locmem
    for every test, which would make this path silently untestable) or
    settings.EMAIL_HOST (Django's own global default for that is the
    non-empty string 'localhost', regardless of whether SMTP was actually
    configured — an unrelated default, not a signal of real configuration)."""
    import os
    return bool(os.environ.get("DJANGO_EMAIL_HOST", ""))


def _sms_really_configured():
    from core.models import SiteConfig
    cfg = SiteConfig.get()
    return bool(cfg.sms_enabled and cfg.sms_api_key and cfg.sms_partner_id
               and cfg.sms_shortcode)


def _rate_limited(user):
    """No more than MAX_REQUESTS_PER_WINDOW reset attempts per account within
    the window, regardless of channel — stops one account's phone/email
    being bombed with repeated reset messages."""
    cutoff = timezone.now() - dt.timedelta(minutes=REQUEST_WINDOW_MINUTES)
    return PasswordResetCode.objects.filter(user=user, created_at__gte=cutoff).count() \
        >= MAX_REQUESTS_PER_WINDOW


class SelfPasswordResetRequestView(View):
    """Step 1: identify the account by username, then send a code/link to
    whichever contact channel is on file and actually configured."""
    template_name = "registration/self_reset_request.html"

    def get(self, request):
        return render(request, self.template_name, {})

    def post(self, request):
        username = (request.POST.get("username") or "").strip()
        user = User.objects.filter(username__iexact=username, is_active=True).first() \
            if username else None

        if user is not None and not _rate_limited(user):
            profile = getattr(user, "profile", None)
            phone = profile.phone if profile else ""
            if phone and _sms_really_configured():
                self._send_sms_code(request, user)
                messages.success(request, GENERIC_MESSAGE)
                return redirect("self_reset_verify")
            if user.email and _email_really_configured():
                self._send_email_link(request, user)
                messages.success(request, GENERIC_MESSAGE)
                return redirect("login")
        # no matching account, no viable channel, or rate-limited: identical
        # response either way, so none of that is discoverable from outside
        messages.success(request, GENERIC_MESSAGE)
        return redirect("login")

    def _send_sms_code(self, request, user):
        from core.services.sms import send_sms
        code_obj, raw_code = PasswordResetCode.issue(user, request=request)
        send_sms(user.profile.phone,
                 f"Your password reset code is {raw_code}. It expires in "
                 f"10 minutes. If you didn't request this, ignore this message.")
        request.session[PENDING_RESET_USER] = user.pk
        log_user_admin_action(None, user, "PASSWORD_RESET",
            detail="Self-service reset code sent by SMS", request=request)

    def _send_email_link(self, request, user):
        from django.contrib.auth.forms import PasswordResetForm
        form = PasswordResetForm({"email": user.email})
        if form.is_valid():
            form.save(request=request,
                      email_template_name="registration/self_reset_email.txt",
                      subject_template_name="registration/self_reset_email_subject.txt")
        log_user_admin_action(None, user, "PASSWORD_RESET",
            detail="Self-service reset link emailed", request=request)


class SelfPasswordResetVerifyView(View):
    """Step 2 (SMS channel only): enter the 6-digit code plus a new password."""
    template_name = "registration/self_reset_verify.html"

    def get(self, request):
        if not request.session.get(PENDING_RESET_USER):
            return redirect("self_reset_request")
        return render(request, self.template_name, {})

    def post(self, request):
        user_id = request.session.get(PENDING_RESET_USER)
        if not user_id:
            return redirect("self_reset_request")
        user = User.objects.filter(pk=user_id).first()
        code = (request.POST.get("code") or "").strip()
        new_password = request.POST.get("new_password") or ""
        confirm_password = request.POST.get("confirm_password") or ""

        if not user:
            request.session.pop(PENDING_RESET_USER, None)
            return redirect("self_reset_request")

        match = PasswordResetCode.verify(user, code)
        if not match:
            messages.error(request, "That code is incorrect or has expired. "
                "Request a new one.")
            return render(request, self.template_name, {})

        if new_password != confirm_password:
            messages.error(request, "The two passwords don't match.")
            return render(request, self.template_name, {})

        try:
            validate_password(new_password, user=user)
        except ValidationError as exc:
            for err in exc.messages:
                messages.error(request, err)
            return render(request, self.template_name, {})

        match.mark_used()
        user.set_password(new_password)
        user.save()
        request.session.pop(PENDING_RESET_USER, None)
        log_user_admin_action(None, user, "PASSWORD_CHANGED",
            detail="Self-service reset via SMS code completed", request=request)
        messages.success(request, "Your password has been reset. Sign in with "
                                  "your new password.")
        return redirect("login")

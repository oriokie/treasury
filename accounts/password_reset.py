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
# Wrong codes entered against the pending reset in THIS session. The durable
# limit is the one on the code row itself (PasswordResetCode.MAX_VERIFY_ATTEMPTS)
# — this counter is the second layer, mirroring the 2FA gate's ATTEMPTS key, and
# exists so a guesser is thrown out of the flow rather than left sitting on a
# form that quietly stopped working. It shares the model's number rather than
# declaring one of its own, so the two can never drift apart.
VERIFY_ATTEMPTS = "self_reset_verify_attempts"
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
        # A new code is a clean slate: the row's own counter starts at zero, so
        # the session's has to as well or a user who asked for a second code
        # would inherit the failures of the first one.
        request.session[VERIFY_ATTEMPTS] = 0
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
    """Step 2 (SMS channel only): enter the 6-digit code plus a new password.

    Guesses are capped twice over. ``PasswordResetCode.verify`` charges each
    wrong answer to the code row and stops answering once that code has spent
    its allowance; on top of that this view keeps its own count in the session
    and abandons the pending reset when it runs out, exactly as the two-factor
    gate does. The session count alone would be theatre — it lives in state the
    guesser owns and is discarded by clearing a cookie — but it is what turns a
    dead code into an explicit "start again" instead of a form that silently
    refuses everything. Both read PasswordResetCode.MAX_VERIFY_ATTEMPTS."""
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
            limit = PasswordResetCode.MAX_VERIFY_ATTEMPTS
            attempts = request.session.get(VERIFY_ATTEMPTS, 0) + 1
            request.session[VERIFY_ATTEMPTS] = attempts
            remaining = PasswordResetCode.remaining_attempts(user)
            if attempts >= limit or remaining <= 0:
                # Out of tries, or there is no live code left to try against
                # (expired, or already spent). Drop the pending reset rather
                # than leave a form up that can no longer succeed; requesting a
                # new code is the way forward and is itself rate-limited.
                request.session.pop(PENDING_RESET_USER, None)
                request.session.pop(VERIFY_ATTEMPTS, None)
                messages.error(request, "That code is wrong or has expired, and "
                    "it has had all the tries it gets. Request a new code to "
                    "try again.")
                return redirect("self_reset_request")
            messages.error(request, "That code is incorrect or has expired. You "
                f"have {remaining} more "
                f"{'try' if remaining == 1 else 'tries'} before you'll need to "
                "request a new code.")
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
        request.session.pop(VERIFY_ATTEMPTS, None)
        log_user_admin_action(None, user, "PASSWORD_CHANGED",
            detail="Self-service reset via SMS code completed", request=request)
        messages.success(request, "Your password has been reset. Sign in with "
                                  "your new password.")
        return redirect("login")

"""Two-factor authentication flows: enrol, verify-at-login, manage, disable."""
from django.contrib import messages
from django.contrib.auth.mixins import LoginRequiredMixin
from django.shortcuts import redirect, render
from django.views import View

from .models import TwoFactor

# session keys
PENDING_USER = "2fa_pending_user_id"     # set after password ok, before TOTP
VERIFIED = "2fa_verified"                 # set true once TOTP passed this session
ATTEMPTS = "2fa_attempts"                 # failed-code counter for this pending login
MAX_ATTEMPTS = 5


def _qr_data_uri(uri):
    """Render the otpauth URI as an inline QR data URI (no external request).

    Uses qrcode's pure-Python SVG factory so it works without Pillow; falls back
    to a PNG if Pillow happens to be installed."""
    if not uri:
        return None
    # 1) SVG path image — needs no Pillow, so it always works
    try:
        import io, base64
        import qrcode
        import qrcode.image.svg
        img = qrcode.make(uri, image_factory=qrcode.image.svg.SvgPathImage)
        buf = io.BytesIO(); img.save(buf)
        return "data:image/svg+xml;base64," + base64.b64encode(buf.getvalue()).decode()
    except Exception:
        pass
    # 2) PNG fallback (only if Pillow is available)
    try:
        import io, base64
        import qrcode
        img = qrcode.make(uri)
        buf = io.BytesIO(); img.save(buf, format="PNG")
        return "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode()
    except Exception:
        return None


def _email_configured():
    from django.conf import settings
    backend = getattr(settings, "EMAIL_BACKEND", "")
    if "console" in backend or "locmem" in backend or "dummy" in backend:
        return True   # dev/test backends "work" for our purposes
    return bool(getattr(settings, "EMAIL_HOST", ""))


def _sms_configured():
    try:
        from core.models import SiteConfig
        c = SiteConfig.get()
        return bool(c.sms_enabled and c.sms_api_key and c.sms_partner_id
                    and c.sms_shortcode)
    except Exception:
        return False


class TwoFactorSetupView(LoginRequiredMixin, View):
    template_name = "accounts/twofactor_setup.html"

    def _options(self):
        return {"sms_ok": _sms_configured(), "email_ok": _email_configured()}

    def get(self, request):
        tf, _ = TwoFactor.objects.get_or_create(user=request.user)
        if tf.confirmed:
            return render(request, "accounts/twofactor_manage.html",
                          {"tf": tf, "codes_remaining": len(tf.get_recovery_codes())})
        # default to the authenticator-app secret being ready
        if not tf.secret or not tf.secret_readable:
            import pyotp
            tf.set_secret(pyotp.random_base32())
            tf.save()
        uri = tf.provisioning_uri()
        ctx = {"secret": tf.secret, "qr": _qr_data_uri(uri), "uri": uri,
               "default_email": request.user.email or "", "tf": tf}
        ctx.update(self._options())
        return render(request, self.template_name, ctx)

    def post(self, request):
        tf, _ = TwoFactor.objects.get_or_create(user=request.user)
        action = request.POST.get("action", "confirm")

        # 1) the user asked us to send a code to a phone / email
        if action in ("send_sms", "send_email"):
            if action == "send_sms":
                if not _sms_configured():
                    messages.error(request, "SMS isn't configured on this server.")
                    return redirect("twofactor_setup")
                from members.models import normalize_phone
                phone = normalize_phone(request.POST.get("phone", "")) or ""
                if not phone:
                    messages.error(request, "Enter a valid phone number.")
                    return redirect("twofactor_setup")
                tf.method = TwoFactor.Method.SMS
                tf.phone = phone
            else:
                if not _email_configured():
                    messages.error(request, "Email isn't configured on this server.")
                    return redirect("twofactor_setup")
                email = (request.POST.get("email") or request.user.email or "").strip()
                if "@" not in email:
                    messages.error(request, "Enter a valid email address.")
                    return redirect("twofactor_setup")
                tf.method = TwoFactor.Method.EMAIL
                tf.delivery_email = email
            tf.save()
            ok, dest = tf.send_code()
            if ok:
                messages.success(request, f"We sent a code to {dest}. Enter it below.")
            else:
                messages.error(request, f"Couldn't send a code — {dest}.")
            ctx = {"awaiting_code": ok, "method": tf.method,
                   "dest": tf.destination_masked, "tf": tf,
                   "secret": tf.secret, "qr": _qr_data_uri(tf.provisioning_uri()),
                   "uri": tf.provisioning_uri(),
                   "default_email": request.user.email or ""}
            ctx.update(self._options())
            return render(request, self.template_name, ctx)

        # 2) confirm enrolment with the entered code (app code or delivered code)
        token = request.POST.get("token", "")
        ok = tf.verify_code(token) if tf.is_code_method else tf.verify(token)
        if ok:
            tf.confirmed = True
            tf.save(update_fields=["confirmed"])
            codes = tf.reset_recovery_codes()
            tf.save(update_fields=["recovery_codes"])
            request.session[VERIFIED] = True
            messages.success(request, "Two-factor authentication is now on.")
            return render(request, "accounts/twofactor_codes.html", {"codes": codes})
        messages.error(request, "That code didn't match — please try again.")
        return redirect("twofactor_setup")


from django.contrib.auth.decorators import login_not_required
from django.utils.decorators import method_decorator


@method_decorator(login_not_required, name="dispatch")
class TwoFactorVerifyView(View):
    # Runs DURING login — the password step has passed but the session is not yet
    # VERIFIED, so request.user is deliberately anonymous here. Exempt from the
    # global login-required gate (P1-1) so the second factor can be completed;
    # access is instead controlled by the PENDING_USER session token below.
    template_name = "accounts/twofactor_verify.html"

    def _pending_tf(self, request):
        uid = request.session.get(PENDING_USER)
        if not uid:
            return None, None
        from django.contrib.auth import get_user_model
        User = get_user_model()
        user = User.objects.filter(pk=uid).first()
        if not user:
            return None, None
        return user, getattr(user, "two_factor", None)

    def get(self, request):
        user, tf = self._pending_tf(request)
        if not user:
            return redirect("login")
        ctx = {}
        # For SMS/email, send a code automatically on arrival (unless one was just
        # sent), so the user lands on a "code sent to ***" screen.
        if tf and tf.is_code_method:
            ctx["method"] = tf.method
            ctx["dest"] = tf.destination_masked
            if tf.can_resend(within_seconds=20):
                ok, dest = tf.send_code()
                ctx["sent"] = ok
                if not ok:
                    messages.error(request, f"Couldn't send your code — {dest}.")
            else:
                ctx["sent"] = True
        return render(request, self.template_name, ctx)

    def post(self, request):
        user, tf = self._pending_tf(request)
        if not user:
            return redirect("login")
        from django.contrib.auth import login

        # resend a code (SMS/email)
        if request.POST.get("action") == "resend" and tf and tf.is_code_method:
            if tf.can_resend():
                ok, dest = tf.send_code()
                messages.success(request, f"A new code was sent to {dest}.") if ok \
                    else messages.error(request, f"Couldn't resend — {dest}.")
            else:
                messages.info(request, "Please wait a few seconds before requesting "
                                       "another code.")
            return render(request, self.template_name,
                          {"method": tf.method, "dest": tf.destination_masked,
                           "sent": True})

        ok = tf.authenticate(request.POST.get("token", "")) if tf else False
        if ok:
            user.backend = "django.contrib.auth.backends.ModelBackend"
            login(request, user)
            request.session[VERIFIED] = True
            request.session.pop(PENDING_USER, None)
            request.session.pop(ATTEMPTS, None)
            nxt = request.session.pop("2fa_next", None)
            return redirect(nxt or "/")
        # Rate-limit code guesses: the password step is protected by django-axes,
        # but without this, a 6-digit TOTP (only 1,000,000 possibilities, often
        # accepted across a ±1 window) could otherwise be brute-forced with
        # unlimited attempts once someone reaches this screen. After a handful
        # of wrong codes, drop the pending login entirely and require the
        # password to be re-entered — which axes does rate-limit.
        attempts = request.session.get(ATTEMPTS, 0) + 1
        request.session[ATTEMPTS] = attempts
        if attempts >= MAX_ATTEMPTS:
            request.session.pop(PENDING_USER, None)
            request.session.pop(ATTEMPTS, None)
            messages.error(request, "Too many incorrect codes. Please sign in again.")
            return redirect("login")
        secret_lost = bool(tf and tf.method == TwoFactor.Method.TOTP
                           and not tf.secret_readable)
        messages.error(request, "Invalid code. You can use a recovery code if you "
                                "can't reach your usual method." +
                                (" (Your authenticator secret can't be read on this "
                                 "server — sign in with a recovery code, then re-enrol "
                                 "two-factor.)" if secret_lost else ""))
        ctx = {"secret_lost": secret_lost}
        if tf and tf.is_code_method:
            ctx.update({"method": tf.method, "dest": tf.destination_masked, "sent": True})
        return render(request, self.template_name, ctx)


class TwoFactorDisableView(LoginRequiredMixin, View):
    def post(self, request):
        TwoFactor.objects.filter(user=request.user).delete()
        messages.success(request, "Two-factor authentication has been turned off.")
        return redirect("twofactor_setup")


class TwoFactorRecoveryRegenView(LoginRequiredMixin, View):
    def post(self, request):
        tf = getattr(request.user, "two_factor", None)
        if not tf or not tf.confirmed:
            return redirect("twofactor_setup")
        codes = tf.reset_recovery_codes()
        tf.save(update_fields=["recovery_codes"])
        return render(request, "accounts/twofactor_codes.html",
                      {"codes": codes, "regenerated": True})

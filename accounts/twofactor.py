"""Two-factor authentication flows: enrol, verify-at-login, manage, disable."""
from django.contrib import messages
from django.contrib.auth.mixins import LoginRequiredMixin
from django.shortcuts import redirect, render
from django.views import View

from .models import TwoFactor

# session keys
PENDING_USER = "2fa_pending_user_id"     # set after password ok, before TOTP
VERIFIED = "2fa_verified"                 # set true once TOTP passed this session


def _qr_data_uri(uri):
    """Render the otpauth URI as an inline PNG data URI (no external request)."""
    try:
        import qrcode
        import io
        import base64
        img = qrcode.make(uri)
        buf = io.BytesIO(); img.save(buf, format="PNG")
        return "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode()
    except Exception:
        return None


class TwoFactorSetupView(LoginRequiredMixin, View):
    template_name = "accounts/twofactor_setup.html"

    def get(self, request):
        tf, _ = TwoFactor.objects.get_or_create(user=request.user)
        if tf.confirmed:
            return render(request, "accounts/twofactor_manage.html",
                          {"tf": tf, "codes_remaining": len(tf.get_recovery_codes())})
        if not tf.secret or not tf.secret_readable:
            import pyotp
            tf.set_secret(pyotp.random_base32())
            tf.save()
        uri = tf.provisioning_uri()
        return render(request, self.template_name, {
            "secret": tf.secret, "qr": _qr_data_uri(uri), "uri": uri})

    def post(self, request):
        tf, _ = TwoFactor.objects.get_or_create(user=request.user)
        if tf.verify(request.POST.get("token", "")):
            tf.confirmed = True
            tf.save(update_fields=["confirmed"])
            codes = tf.reset_recovery_codes()
            tf.save(update_fields=["recovery_codes"])
            request.session[VERIFIED] = True
            messages.success(request, "Two-factor authentication is now on.")
            return render(request, "accounts/twofactor_codes.html", {"codes": codes})
        messages.error(request, "That code didn't match — check the time on your "
                                "phone and try again.")
        return redirect("twofactor_setup")


class TwoFactorVerifyView(View):
    template_name = "accounts/twofactor_verify.html"

    def get(self, request):
        if not request.session.get(PENDING_USER):
            return redirect("login")
        return render(request, self.template_name, {})

    def post(self, request):
        uid = request.session.get(PENDING_USER)
        if not uid:
            return redirect("login")
        from django.contrib.auth import get_user_model, login
        User = get_user_model()
        try:
            user = User.objects.get(pk=uid)
        except User.DoesNotExist:
            request.session.pop(PENDING_USER, None)
            return redirect("login")
        tf = getattr(user, "two_factor", None)
        ok = False
        if tf:
            token = request.POST.get("token", "")
            ok = tf.verify(token)
            if not ok:
                # accept a recovery code in the same box (no toggle needed) — this
                # gives a second, always-available form of authentication
                ok = tf.consume_recovery_code(token)
        if ok:
            user.backend = "django.contrib.auth.backends.ModelBackend"
            login(request, user)
            request.session[VERIFIED] = True
            request.session.pop(PENDING_USER, None)
            nxt = request.session.pop("2fa_next", None)
            return redirect(nxt or "/")
        # If the secret can't be read at all (e.g. the encryption key changed
        # after enrolment), TOTP can never succeed — let a recovery code through,
        # and otherwise tell the user plainly rather than failing forever.
        secret_lost = bool(tf and not tf.secret_readable)
        messages.error(request, "Invalid code. You can use a recovery code if you "
                                "can't reach your authenticator." +
                                (" (Your authenticator secret can't be read on this "
                                 "server — please sign in with a recovery code, then "
                                 "re-enrol two-factor.)" if secret_lost else ""))
        return render(request, self.template_name, {"secret_lost": secret_lost})


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

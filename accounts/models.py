"""Two-factor authentication (TOTP) for treasury logins.

A per-user TOTP secret (encrypted at rest with the app's Fernet key) plus a set
of one-time recovery codes. Verification is woven into login via a session gate
and middleware. Designed to be required for treasurers (who can move money) and
optional for others, per SiteConfig.
"""
import datetime as dt
import json
import secrets

from django.conf import settings
from django.db import models

from core.fields import encrypt, decrypt


def _gen_recovery_codes(n=10):
    # human-friendly codes, e.g. 'a1b2-c3d4'
    out = []
    for _ in range(n):
        raw = secrets.token_hex(4)
        out.append(f"{raw[:4]}-{raw[4:]}")
    return out


class TwoFactor(models.Model):
    user = models.OneToOneField(settings.AUTH_USER_MODEL, on_delete=models.CASCADE,
                                related_name="two_factor")
    # the TOTP secret, stored encrypted (enc1:… ciphertext)
    secret_enc = models.CharField(max_length=255, blank=True, default="")
    confirmed = models.BooleanField(default=False)
    # JSON list of unused recovery codes (each stored as plain string; they are
    # single-use and the table itself is protected, mirroring app conventions)
    recovery_codes = models.TextField(blank=True, default="[]")
    created_at = models.DateTimeField(auto_now_add=True)
    last_used_at = models.DateTimeField(null=True, blank=True)

    def __str__(self):
        return f"2FA for {self.user}"

    # --- secret handling ---
    def set_secret(self, raw_secret):
        self.secret_enc = encrypt(raw_secret)

    @property
    def secret(self):
        return decrypt(self.secret_enc) if self.secret_enc else ""

    # --- recovery codes ---
    def reset_recovery_codes(self):
        codes = _gen_recovery_codes()
        self.recovery_codes = json.dumps(codes)
        return codes

    def get_recovery_codes(self):
        try:
            return json.loads(self.recovery_codes or "[]")
        except ValueError:
            return []

    def consume_recovery_code(self, code):
        code = (code or "").strip().lower().replace(" ", "")
        codes = self.get_recovery_codes()
        if code in codes:
            codes.remove(code)
            self.recovery_codes = json.dumps(codes)
            self.save(update_fields=["recovery_codes"])
            return True
        return False

    @property
    def secret_readable(self):
        """True only if the stored secret decrypts to a usable base32 secret.
        A failed decryption (e.g. the encryption key changed after enrolment)
        leaves the ciphertext in place; we must not feed that to pyotp."""
        s = self.secret
        if not s:
            return False
        try:
            import base64
            base64.b32decode(s, casefold=True)
            return True
        except Exception:
            return False

    # --- verification ---
    def verify(self, token):
        """True if the 6-digit token is currently valid (±1 step tolerance).
        Never raises — an unreadable secret simply fails to verify."""
        import pyotp
        from django.utils import timezone
        if not self.secret_readable:
            return False
        token = (token or "").strip().replace(" ", "")
        if not token:
            return False
        try:
            ok = pyotp.TOTP(self.secret).verify(token, valid_window=1)
        except Exception:
            return False
        if ok:
            self.last_used_at = timezone.now()
            self.save(update_fields=["last_used_at"])
        return ok

    def provisioning_uri(self):
        import pyotp
        from core.models import SiteConfig
        issuer = SiteConfig.get().church_name or "Church Treasury"
        label = self.user.get_username()
        try:
            return pyotp.TOTP(self.secret).provisioning_uri(name=label,
                                                            issuer_name=issuer)
        except Exception:
            return ""

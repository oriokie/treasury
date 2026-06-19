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
    class Method(models.TextChoices):
        TOTP = "TOTP", "Authenticator app"
        SMS = "SMS", "Text message (SMS)"
        EMAIL = "EMAIL", "Email"

    user = models.OneToOneField(settings.AUTH_USER_MODEL, on_delete=models.CASCADE,
                                related_name="two_factor")
    method = models.CharField(max_length=6, choices=Method.choices, default=Method.TOTP)
    # the TOTP secret, stored encrypted (enc1:… ciphertext)
    secret_enc = models.CharField(max_length=255, blank=True, default="")
    # where one-time codes are delivered for the SMS / EMAIL methods
    phone = models.CharField(max_length=20, blank=True, default="")
    delivery_email = models.EmailField(blank=True, default="")
    # the in-flight one-time code for SMS / EMAIL (stored only as a hash)
    otp_hash = models.CharField(max_length=64, blank=True, default="")
    otp_expires_at = models.DateTimeField(null=True, blank=True)
    otp_sent_at = models.DateTimeField(null=True, blank=True)
    otp_attempts = models.PositiveSmallIntegerField(default=0)
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

    # --- one-time codes for SMS / EMAIL methods ---
    @property
    def is_code_method(self):
        return self.method in (self.Method.SMS, self.Method.EMAIL)

    @staticmethod
    def _hash_code(code):
        import hashlib
        return hashlib.sha256((code or "").strip().encode()).hexdigest()

    @property
    def destination(self):
        """The (unmasked) place a code is sent for this method."""
        if self.method == self.Method.SMS:
            return self.phone
        if self.method == self.Method.EMAIL:
            return self.delivery_email or (self.user.email or "")
        return ""

    @property
    def destination_masked(self):
        from members.models import mask_phone
        if self.method == self.Method.SMS:
            return mask_phone(self.phone) or "your phone"
        if self.method == self.Method.EMAIL:
            d = self.destination
            if "@" in d:
                name, _, dom = d.partition("@")
                show = name[:2] if len(name) > 2 else name[:1]
                return f"{show}{'*' * max(len(name) - len(show), 1)}@{dom}"
            return "your email"
        return ""

    def can_resend(self, within_seconds=30):
        """Rate-limit resends so a button mash can't fan out SMS/email."""
        from django.utils import timezone
        if not self.otp_sent_at:
            return True
        return (timezone.now() - self.otp_sent_at).total_seconds() >= within_seconds

    def send_code(self):
        """Generate, store (hashed) and deliver a 6-digit code. Returns
        (ok, masked_destination_or_error). Never raises."""
        import secrets as _secrets
        import datetime as _dt
        from django.utils import timezone
        if not self.destination:
            return False, "no destination on file"
        code = f"{_secrets.randbelow(900000) + 100000}"
        self.otp_hash = self._hash_code(code)
        self.otp_expires_at = timezone.now() + _dt.timedelta(minutes=5)
        self.otp_sent_at = timezone.now()
        self.otp_attempts = 0
        self.save(update_fields=["otp_hash", "otp_expires_at", "otp_sent_at",
                                 "otp_attempts"])
        from core.models import SiteConfig
        church = SiteConfig.get().church_name or "Church Treasury"
        body = (f"{church}: your verification code is {code}. "
                f"It expires in 5 minutes. If you didn't request it, ignore this.")
        try:
            if self.method == self.Method.SMS:
                from core.services.sms import send_sms
                log = send_sms(self.phone, body)
                ok = str(getattr(log, "status", "")).upper() in (
                    "SENT", "OK", "QUEUED", "SUCCESS")
                return ok, (self.destination_masked if ok else "couldn't send the SMS")
            else:  # EMAIL
                from django.core.mail import send_mail
                from django.conf import settings as _s
                send_mail(f"{church} verification code", body,
                          getattr(_s, "DEFAULT_FROM_EMAIL", None) or None,
                          [self.destination], fail_silently=False)
                return True, self.destination_masked
        except Exception:
            return False, "couldn't send the code (delivery isn't configured)"

    def verify_code(self, token):
        """True if `token` matches the in-flight code, isn't expired, and is
        within the attempt limit. Consumes the code on success."""
        from django.utils import timezone
        token = (token or "").strip().replace(" ", "")
        if not (token and self.otp_hash and self.otp_expires_at):
            return False
        if timezone.now() > self.otp_expires_at:
            return False
        if self.otp_attempts >= 5:
            return False
        self.otp_attempts += 1
        if self._hash_code(token) == self.otp_hash:
            self.otp_hash = ""
            self.otp_expires_at = None
            self.last_used_at = timezone.now()
            self.save(update_fields=["otp_hash", "otp_expires_at", "otp_attempts",
                                     "last_used_at"])
            return True
        self.save(update_fields=["otp_attempts"])
        return False

    def authenticate(self, token):
        """Verify a login token by whatever method this user uses, falling back
        to a recovery code. The single entry point used by the verify view."""
        if self.is_code_method:
            ok = self.verify_code(token)
        else:
            ok = self.verify(token)
        if not ok:
            ok = self.consume_recovery_code(token)
        return ok


class Profile(models.Model):
    """A named, fully-configurable bundle of rights a treasurer can assign to
    users. Layered on top of the role groups: a user with at least one profile
    is governed by the union of their profiles' rights; a user with none keeps
    their role-group access (see core.rights.user_rights)."""
    name = models.CharField(max_length=60, unique=True)
    description = models.CharField(max_length=200, blank=True)
    rights = models.JSONField(default=list, blank=True,
                              help_text="List of right keys from core.rights.RIGHT_KEYS.")
    users = models.ManyToManyField(settings.AUTH_USER_MODEL, blank=True,
                                   related_name="profiles")
    is_system = models.BooleanField(default=False,
                                    help_text="Seeded default profile mirroring a role group.")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["name"]

    def __str__(self):
        return self.name

    def clean_rights(self):
        from core.rights import RIGHT_KEYS
        valid = set(RIGHT_KEYS)
        self.rights = [r for r in (self.rights or []) if r in valid]

    def save(self, *args, **kwargs):
        self.clean_rights()
        super().save(*args, **kwargs)

    def has(self, key):
        return key in (self.rights or [])

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


class PasswordResetCode(models.Model):
    """A one-time SMS verification code for self-service password reset.
    Kept deliberately separate from Django's own token-based reset (used for
    the email channel, via the standard PasswordResetView/ConfirmView) since
    an SMS OTP has different lifecycle needs: short-lived, numeric (easy to
    type from a text message), and single-use, tracked explicitly rather
    than derived from the password hash the way Django's email token is.

    The code itself is stored hashed (never in plaintext), the same way a
    password would be — a leaked database row must not hand over a working
    reset code."""
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE,
                             related_name="password_reset_codes")
    code_hash = models.CharField(max_length=128)
    created_at = models.DateTimeField(auto_now_add=True)
    expires_at = models.DateTimeField()
    used_at = models.DateTimeField(null=True, blank=True)
    ip_address = models.GenericIPAddressField(null=True, blank=True)
    # Wrong guesses made against THIS code. See MAX_VERIFY_ATTEMPTS below for
    # why a counter on the row (rather than only in the guesser's session) is
    # the part that actually binds.
    attempts = models.PositiveSmallIntegerField(default=0)

    # How many wrong guesses one code will tolerate before it is dead.
    #
    # This was missing entirely, and its absence was the whole ballgame. The
    # reset flow rate-limits how many codes are *issued* (three per account per
    # fifteen minutes, so nobody's phone gets bombed) and that was mistaken for
    # a limit on guessing. Anyone can cause a code to exist — the request form
    # is public and answers identically for every username, by design — and for
    # the ten minutes that code lived, ``verify`` would cheerfully check an
    # unlimited number of POSTed candidates against it. Six digits is 1,000,000
    # possibilities and django-axes does not help, because axes hooks
    # ``authenticate()`` and this flow never calls it. That is account takeover
    # without ever seeing the SMS.
    #
    # ``TwoFactor.verify_code`` above had already reasoned this out for its own
    # 6-digit code and capped attempts on the row; this mirrors it deliberately
    # rather than inventing a second scheme. The number lives here, once, and
    # ``accounts.password_reset`` reads it for the session-level cap it applies
    # on top — two layers, one rule, so they cannot drift apart.
    #
    # Five is the same allowance the two-factor gate gives, and is chosen to be
    # survivable by somebody squinting at digits on a phone screen while being
    # nowhere near enough to make guessing worthwhile.
    MAX_VERIFY_ATTEMPTS = 5

    class Meta:
        indexes = [models.Index(fields=["user", "used_at", "expires_at"])]

    @staticmethod
    def _hash(raw_code):
        from django.contrib.auth.hashers import make_password
        return make_password(raw_code)

    def check_code(self, raw_code):
        from django.contrib.auth.hashers import check_password
        return check_password(raw_code, self.code_hash)

    @classmethod
    def issue(cls, user, request=None, ttl_minutes=10):
        """Create a fresh 6-digit code for `user`, invalidating any earlier
        unused codes for them first (only the most recent code ever works)."""
        import secrets
        from django.utils import timezone
        cls.objects.filter(user=user, used_at__isnull=True).update(
            used_at=timezone.now())   # invalidate any earlier pending codes
        raw_code = f"{secrets.randbelow(900000) + 100000}"
        ip = None
        if request is not None:
            ip = request.META.get("HTTP_X_FORWARDED_FOR", "").split(",")[0].strip() \
                or request.META.get("REMOTE_ADDR")
        obj = cls.objects.create(user=user, code_hash=cls._hash(raw_code),
            expires_at=timezone.now() + timezone.timedelta(minutes=ttl_minutes),
            ip_address=ip)
        return obj, raw_code

    @classmethod
    def _live_codes(cls, user):
        """Every code for `user` that is still unused and unexpired, newest
        first. The one definition of "live" — ``verify`` and
        ``remaining_attempts`` must never disagree about which rows they are
        talking about, or the count shown to the user describes a different
        code from the one being checked."""
        from django.utils import timezone
        return cls.objects.filter(user=user, used_at__isnull=True,
                                  expires_at__gte=timezone.now()
                                  ).order_by("-created_at")

    @classmethod
    def verify(cls, user, raw_code):
        """Return the matching, still-valid code row, or None. Always checks
        every recent candidate (not just the latest) so a code isn't rejected
        just because of ordering, but only ever the most-recently-issued one
        is actually still valid (issue() invalidates earlier ones).

        Every wrong answer is charged to the candidate it was wrong about, and
        a candidate that has spent MAX_VERIFY_ATTEMPTS stops answering at all —
        including to whoever holds the right code, because by then we can no
        longer tell them apart from whoever was guessing. Getting a new code is
        the way back, and that path is separately rate-limited.

        Two deliberate differences from ``TwoFactor.verify_code``, which is
        otherwise the model for this:

        * A *correct* code costs nothing. Its caller checks the code before it
          checks that the two password boxes agree, so charging for correct
          answers would let somebody fumble the confirmation field a handful of
          times and destroy a code they had typed perfectly. ``verify_code``
          can charge for them because it consumes its code on success; this one
          does not consume anything (the caller decides, via ``mark_used``).
        * A blank code costs nothing either — an empty box is a submitted form,
          not a guess."""
        raw_code = (raw_code or "").strip()
        if not raw_code:
            return None
        for candidate in cls._live_codes(user):
            if candidate.attempts >= cls.MAX_VERIFY_ATTEMPTS:
                continue
            if candidate.check_code(raw_code):
                return candidate
            candidate.attempts += 1
            candidate.save(update_fields=["attempts"])
        return None

    @classmethod
    def remaining_attempts(cls, user):
        """How many guesses `user`'s live code still has, so the person typing
        can be told the code is about to die rather than discovering it. Zero
        when there is no live code at all — an expired or spent code and one
        that never existed look the same from here, and should."""
        row = cls._live_codes(user).first()
        if row is None:
            return 0
        return max(cls.MAX_VERIFY_ATTEMPTS - row.attempts, 0)

    def mark_used(self):
        from django.utils import timezone
        self.used_at = timezone.now()
        self.save(update_fields=["used_at"])


class UserProfile(models.Model):
    """Extended profile information for a user account, plus the fields the
    admin user-management module needs for account lifecycle (lock/suspend,
    forced password change) — kept separate from Django's own User model so
    nothing about core authentication changes.

    Created lazily (get_or_create) the first time it's needed, so existing
    users never need a data migration to "catch up"."""
    class Gender(models.TextChoices):
        MALE = "M", "Male"
        FEMALE = "F", "Female"

    user = models.OneToOneField(settings.AUTH_USER_MODEL, on_delete=models.CASCADE,
                                related_name="profile")
    phone = models.CharField(max_length=20, blank=True, default="")
    gender = models.CharField(max_length=1, choices=Gender.choices, blank=True, default="")
    position = models.CharField(max_length=80, blank=True, default="",
        help_text="e.g. Head Deacon, Elder, Youth Leader — free text, for reference only.")
    department = models.ForeignKey("departments.Department", null=True, blank=True,
        on_delete=models.SET_NULL, related_name="assigned_users",
        help_text="The department/ministry this person is primarily associated with — "
                  "informational only; a Leader's actual access is governed by "
                  "DepartmentLeadership, set on the Edit role tab.")
    church_assignment = models.CharField(max_length=120, blank=True, default="",
        help_text="For a multi-church or conference deployment; leave blank for a single church.")
    avatar = models.ImageField(upload_to="avatars/", null=True, blank=True)
    notes = models.TextField(blank=True, default="",
        help_text="Internal admin notes about this account — never shown to the user.")

    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True,
        on_delete=models.SET_NULL, related_name="+",
        help_text="Who created this account. Blank for accounts that predate this field.")
    updated_at = models.DateTimeField(auto_now=True)

    # --- password lifecycle ---
    password_changed_at = models.DateTimeField(null=True, blank=True)
    must_change_password = models.BooleanField(default=False,
        help_text="Force a password change the next time this user logs in.")

    # --- admin-imposed lock (distinct from is_active): a short-term,
    # easily-reversible "suspend", vs. is_active=False which is the
    # longer-term "this person has left" deactivation. Both block login.
    locked = models.BooleanField(default=False)
    locked_reason = models.CharField(max_length=200, blank=True, default="")
    locked_at = models.DateTimeField(null=True, blank=True)
    locked_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True,
        on_delete=models.SET_NULL, related_name="+")

    class Meta:
        verbose_name = "user profile"

    def __str__(self):
        return f"Profile for {self.user}"

    @classmethod
    def for_user(cls, user):
        obj, _ = cls.objects.get_or_create(user=user)
        return obj


class UserAdminLogEntry(models.Model):
    """A dedicated, purpose-built audit trail for administrative actions taken
    on a user account — distinct from django-simple-history (which tracks raw
    field changes on models generically): this reads naturally as a security
    log ("who did what to whose account, and when"), exactly what an auditor
    reviewing user management asks for first.

    PROTECT on target_user: an audit record must never silently disappear
    just because the account it describes is later deleted — consistent with
    this application's general principle that historical/audit records
    outlive the thing they describe. In practice a User is never deleted
    through this application (only deactivated), so this is a backstop, not
    an expected path."""
    class Action(models.TextChoices):
        CREATED = "CREATED", "Account created"
        PROFILE_UPDATED = "PROFILE_UPDATED", "Profile updated"
        ROLE_CHANGED = "ROLE_CHANGED", "Role changed"
        ACTIVATED = "ACTIVATED", "Account activated"
        DEACTIVATED = "DEACTIVATED", "Account deactivated"
        LOCKED = "LOCKED", "Account locked (suspended)"
        UNLOCKED = "UNLOCKED", "Account unlocked (reinstated)"
        PASSWORD_RESET = "PASSWORD_RESET", "Password reset by administrator"
        PASSWORD_CHANGED = "PASSWORD_CHANGED", "Password changed"
        FORCE_PASSWORD_CHANGE_SET = "FORCE_PASSWORD_CHANGE_SET", "Forced password change on next login"
        TWO_FA_DISABLED = "TWO_FA_DISABLED", "Two-factor authentication disabled by administrator"
        LOGIN_LOCKOUT_CLEARED = "LOGIN_LOCKOUT_CLEARED", "Failed-login lockout cleared"
        SESSIONS_TERMINATED = "SESSIONS_TERMINATED", "All sessions terminated"
        PROFILE_ASSIGNED = "PROFILE_ASSIGNED", "Rights profile assigned"
        PROFILE_REMOVED = "PROFILE_REMOVED", "Rights profile removed"
        CLONED = "CLONED", "Account cloned from another user"

    target_user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT,
        related_name="admin_log_entries")
    actor = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True,
        on_delete=models.SET_NULL, related_name="+")
    action = models.CharField(max_length=32, choices=Action.choices)
    detail = models.CharField(max_length=300, blank=True, default="")
    before = models.CharField(max_length=200, blank=True, default="")
    after = models.CharField(max_length=200, blank=True, default="")
    ip_address = models.GenericIPAddressField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        ordering = ["-created_at"]
        verbose_name = "user admin log entry"
        verbose_name_plural = "user admin log entries"

    def __str__(self):
        return f"{self.get_action_display()} — {self.target_user} ({self.created_at:%Y-%m-%d %H:%M})"


def log_user_admin_action(actor, target_user, action, detail="", before="", after="",
                          request=None):
    """Record one entry in the user-management audit trail. Never raises —
    a logging failure must not block the underlying admin action itself."""
    try:
        ip = None
        if request is not None:
            ip = request.META.get("HTTP_X_FORWARDED_FOR", "").split(",")[0].strip() \
                or request.META.get("REMOTE_ADDR")
        UserAdminLogEntry.objects.create(
            target_user=target_user, actor=actor if actor and actor.is_authenticated else None,
            action=action, detail=detail[:300], before=str(before)[:200],
            after=str(after)[:200], ip_address=ip or None)
    except Exception:   # noqa: BLE001 — audit logging must never break the admin action
        from core.utils import log_exception
        log_exception("accounts/models.py:log_user_admin_action")


from django.db.models.signals import pre_save
from django.dispatch import receiver


@receiver(pre_save, sender=settings.AUTH_USER_MODEL)
def _track_password_change(sender, instance, **kwargs):
    """Stamp UserProfile.password_changed_at whenever the password hash
    actually changes — Django's User model has no such field natively, and
    the security dashboard ("password last changed") needs it. Compares
    against the stored value rather than trusting a flag, so it's correct
    regardless of which code path changed the password (self-service change,
    an admin's reset, or the createsuperuser command)."""
    if not instance.pk:
        return   # new user — handled by the profile's own creation, not here
    try:
        old_password = sender.objects.filter(pk=instance.pk).values_list(
            "password", flat=True).first()
    except Exception:
        return
    if old_password is not None and old_password != instance.password:
        from django.utils import timezone
        profile = UserProfile.for_user(instance)
        profile.password_changed_at = timezone.now()
        profile.must_change_password = False
        profile.save(update_fields=["password_changed_at", "must_change_password"])


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

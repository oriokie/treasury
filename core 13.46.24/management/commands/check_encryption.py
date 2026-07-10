"""
Inspect and rotate application-layer encryption.

Status (default):
    python manage.py check_encryption
    — shows whether encryption is enabled, which key source is in use, and
      whether the encrypted SiteConfig fields decrypt cleanly with the current
      key.

Key rotation (after changing TREASURY_ENCRYPTION_KEY):
    python manage.py check_encryption --reencrypt-from OLD_KEY
    — decrypts each encrypted field with OLD_KEY and re-saves it (which encrypts
      with the *current* key). Run this immediately after changing the key, or
      the old values become unreadable.

This only touches the SiteConfig singleton, where all encrypted fields live.
"""
from django.conf import settings
from django.core.management.base import BaseCommand

from core import fields as enc


# the EncryptedCharField attributes on SiteConfig
ENCRYPTED_FIELDS = [
    "sms_api_key", "sms_partner_id", "llm_api_key", "telegram_bot_token",
    "email_host_password", "whatsapp_api_key", "daraja_consumer_key",
    "daraja_consumer_secret", "bank_feed_username", "bank_feed_password",
]


class Command(BaseCommand):
    help = "Show encryption status, or re-encrypt secrets after a key change."

    def add_arguments(self, parser):
        parser.add_argument("--reencrypt-from", metavar="OLD_KEY", default=None,
                            help="Re-encrypt secrets that were encrypted with this "
                                 "previous key, under the current key.")

    def handle(self, *args, **opts):
        from core.models import SiteConfig
        cfg = SiteConfig.get()

        enabled = enc.encryption_enabled()
        key_source = ("TREASURY_ENCRYPTION_KEY" if getattr(settings, "ENCRYPTION_KEY", "")
                      else "SECRET_KEY (fallback)")
        self.stdout.write(f"Encryption enabled : {enabled}")
        self.stdout.write(f"Key source         : {key_source}")

        if opts["reencrypt_from"]:
            return self._reencrypt(cfg, opts["reencrypt_from"])

        # status: read the RAW stored column (bypassing transparent decryption)
        # and check each set secret decrypts cleanly with the current key.
        raw = self._raw_columns(cfg)
        present = readable = ciphertext = 0
        for f in ENCRYPTED_FIELDS:
            stored = raw.get(f) or ""
            if not stored:
                continue
            present += 1
            if str(stored).startswith(enc._PREFIX):
                ciphertext += 1
                val = enc.decrypt(stored)
                if not str(val).startswith(enc._PREFIX):
                    readable += 1
                else:
                    self.stdout.write(self.style.WARNING(
                        f"  ! {f}: does NOT decrypt with the current key"))
            else:
                # stored as plaintext (encryption was off, or legacy value)
                readable += 1
        self.stdout.write(f"Secrets set        : {present} "
                          f"({ciphertext} encrypted, {present - ciphertext} plaintext)")
        self.stdout.write(self.style.SUCCESS(
            f"Readable with current key : {readable}/{present}"))
        if present and readable < present:
            self.stdout.write(self.style.WARNING(
                "Some secrets can't be read with the current key. If you changed "
                "the key, run: manage.py check_encryption --reencrypt-from OLD_KEY"))

    def _raw_columns(self, cfg):
        """Read the encrypted columns straight from the DB, without the field's
        transparent decryption, so we can inspect what's actually stored."""
        from django.db import connection
        cols = ", ".join(ENCRYPTED_FIELDS)
        with connection.cursor() as c:
            c.execute(f"SELECT {cols} FROM core_siteconfig WHERE id = %s", [cfg.id])
            row = c.fetchone()
        return dict(zip(ENCRYPTED_FIELDS, row)) if row else {}

    def _reencrypt(self, cfg, old_key):
        old_fernet = enc._fernet(old_key)
        raw = self._raw_columns(cfg)
        changed = 0
        for f in ENCRYPTED_FIELDS:
            stored = raw.get(f) or ""
            if not stored or not str(stored).startswith(enc._PREFIX):
                continue
            try:
                plain = old_fernet.decrypt(
                    stored[len(enc._PREFIX):].encode("ascii")).decode("utf-8")
            except Exception:
                self.stdout.write(self.style.WARNING(
                    f"  ! {f}: could not decrypt with the supplied old key — skipped"))
                continue
            # assigning the plaintext + saving re-encrypts under the current key
            setattr(cfg, f, plain)
            changed += 1
        if changed:
            cfg.save()
            self.stdout.write(self.style.SUCCESS(
                f"Re-encrypted {changed} secret(s) under the current key."))
        else:
            self.stdout.write("Nothing to re-encrypt.")

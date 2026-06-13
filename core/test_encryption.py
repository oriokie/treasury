"""Tests for configurable application-layer encryption."""
from django.test import TestCase, override_settings

from core import fields as enc


class EncryptionConfigTests(TestCase):
    def test_roundtrip_with_explicit_key(self):
        with override_settings(ENCRYPTION_KEY="alpha-key", ENCRYPTION_ENABLED=True):
            token = enc.encrypt("secret-value")
            self.assertTrue(token.startswith("enc1:"))
            self.assertEqual(enc.decrypt(token), "secret-value")

    def test_disabled_stores_plaintext(self):
        with override_settings(ENCRYPTION_ENABLED=False):
            token = enc.encrypt("secret-value")
            self.assertEqual(token, "secret-value")        # not encrypted
            self.assertFalse(token.startswith("enc1:"))

    def test_disabled_still_decrypts_legacy_ciphertext(self):
        # value encrypted while enabled remains readable after disabling
        with override_settings(ENCRYPTION_KEY="k", ENCRYPTION_ENABLED=True):
            token = enc.encrypt("legacy")
        with override_settings(ENCRYPTION_KEY="k", ENCRYPTION_ENABLED=False):
            self.assertEqual(enc.decrypt(token), "legacy")

    def test_wrong_key_does_not_decrypt(self):
        with override_settings(ENCRYPTION_KEY="key-one", ENCRYPTION_ENABLED=True):
            token = enc.encrypt("hush")
        with override_settings(ENCRYPTION_KEY="key-two", ENCRYPTION_ENABLED=True):
            # tolerant decrypt returns the value unchanged (still prefixed)
            self.assertTrue(enc.decrypt(token).startswith("enc1:"))

    def test_encrypted_field_on_model_roundtrips(self):
        from core.models import SiteConfig
        from django.db import connection
        with override_settings(ENCRYPTION_KEY="model-key", ENCRYPTION_ENABLED=True):
            cfg = SiteConfig.get()
            cfg.sms_api_key = "field-secret"
            cfg.save()
            # raw column is ciphertext
            with connection.cursor() as c:
                c.execute("SELECT sms_api_key FROM core_siteconfig WHERE id=%s",
                          [cfg.id])
                raw = c.fetchone()[0]
            self.assertTrue(raw.startswith("enc1:"))
            # instance transparently decrypts
            self.assertEqual(SiteConfig.objects.get(id=cfg.id).sms_api_key,
                             "field-secret")


class EncryptionRotationCommandTests(TestCase):
    def test_reencrypt_after_key_change(self):
        from io import StringIO
        from django.core.management import call_command
        from core.models import SiteConfig
        with override_settings(ENCRYPTION_KEY="old-key", ENCRYPTION_ENABLED=True):
            cfg = SiteConfig.get(); cfg.sms_api_key = "rotate"; cfg.save()
        with override_settings(ENCRYPTION_KEY="new-key", ENCRYPTION_ENABLED=True):
            out = StringIO()
            call_command("check_encryption", reencrypt_from="old-key", stdout=out)
            self.assertIn("Re-encrypted 1", out.getvalue())
            self.assertEqual(SiteConfig.objects.get(id=cfg.id).sms_api_key, "rotate")

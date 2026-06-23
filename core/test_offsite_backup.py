"""Off-site backup storage upload over HTTPS (#5)."""
from unittest import mock
from django.test import TestCase, Client
from django.contrib.auth.models import User, Group
from core.models import SiteConfig
from core.services.backup import upload_offsite


class _Resp:
    def __init__(self, code): self.code = code
    def getcode(self): return self.code
    def __enter__(self): return self
    def __exit__(self, *a): return False


class OffsiteBackupTests(TestCase):
    def test_disabled_returns_message(self):
        cfg = SiteConfig.get(); cfg.offsite_backup_enabled = False; cfg.save()
        ok, _ = upload_offsite("b.enc", b"x")
        self.assertFalse(ok)

    def test_no_url_returns_message(self):
        cfg = SiteConfig.get(); cfg.offsite_backup_enabled = True
        cfg.offsite_backup_url = ""; cfg.save()
        ok, _ = upload_offsite("b.enc", b"x")
        self.assertFalse(ok)

    def test_uploads_with_put_and_auth(self):
        cfg = SiteConfig.get()
        cfg.offsite_backup_enabled = True
        cfg.offsite_backup_url = "https://cloud.example.com/dav/backups/"
        cfg.offsite_backup_user = "user"; cfg.offsite_backup_password = "pw"; cfg.save()
        cap = {}
        def fake(req, timeout=30):
            cap["url"] = req.full_url; cap["method"] = req.get_method()
            cap["auth"] = req.get_header("Authorization")
            return _Resp(201)
        with mock.patch("urllib.request.urlopen", fake):
            ok, _ = upload_offsite("treasury-backup.enc", b"data", cfg)
        self.assertTrue(ok)
        self.assertTrue(cap["url"].endswith("backups/treasury-backup.enc"))
        self.assertEqual(cap["method"], "PUT")
        self.assertTrue(cap["auth"].startswith("Basic "))

    def test_http_error_reported(self):
        cfg = SiteConfig.get()
        cfg.offsite_backup_enabled = True
        cfg.offsite_backup_url = "https://cloud.example.com/dav/backups/"; cfg.save()
        def fake(req, timeout=30):
            return _Resp(500)
        with mock.patch("urllib.request.urlopen", fake):
            ok, detail = upload_offsite("b.enc", b"x", cfg)
        self.assertFalse(ok)

    def test_settings_page_has_offsite(self):
        u = User.objects.create_user("ob", password="x", is_superuser=True)
        u.groups.add(Group.objects.get_or_create(name="Treasurer")[0])
        c = Client(); c.force_login(u)
        b = c.get("/settings/").content.decode()
        self.assertIn("Off-site backup storage", b)
        self.assertIn("backup/offsite-now", b)

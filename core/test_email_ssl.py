"""Email uses implicit SSL on port 465 and STARTTLS on 587 (#2). Using STARTTLS
against a 465 port was causing the SMTP connection to time out."""
from django.test import TestCase
from core.models import SiteConfig
from core.services.email import _connection


class EmailSSLTests(TestCase):
    def _cfg(self, port, tls=True, ssl=False):
        cfg = SiteConfig.get()
        cfg.email_host = "mail.example.com"; cfg.email_port = port
        cfg.email_use_tls = tls; cfg.email_use_ssl = ssl
        return cfg

    def test_port_465_uses_implicit_ssl(self):
        conn = _connection(self._cfg(465, tls=True, ssl=False))
        self.assertTrue(conn.use_ssl)
        self.assertFalse(conn.use_tls)     # mutually exclusive

    def test_port_587_uses_starttls(self):
        conn = _connection(self._cfg(587, tls=True, ssl=False))
        self.assertTrue(conn.use_tls)
        self.assertFalse(conn.use_ssl)

    def test_explicit_ssl_flag(self):
        conn = _connection(self._cfg(465, tls=False, ssl=True))
        self.assertTrue(conn.use_ssl)
        self.assertFalse(conn.use_tls)

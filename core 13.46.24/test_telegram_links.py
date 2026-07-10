"""Telegram assistant replies render report links as full URLs when a site base
URL is configured, and degrade gracefully without one."""
from django.test import TestCase

from core.models import SiteConfig
from core.services.telegram_bot import _format_assistant


class TelegramLinkTests(TestCase):
    ans = {"text": "Tithe received: KES 100.", "link": "/reports/tithe/",
           "link_label": "Tithe report"}

    def _out(self, base):
        cfg = SiteConfig.get(); cfg.site_base_url = base; cfg.save()
        return _format_assistant(self.ans, SiteConfig.get())

    def test_full_url(self):
        self.assertIn('href="https://kws.oriokie.com/reports/tithe/"',
                      self._out("https://kws.oriokie.com"))

    def test_scheme_added(self):
        self.assertIn('href="https://kws.oriokie.com/reports/tithe/"',
                      self._out("kws.oriokie.com"))

    def test_trailing_slash_trimmed(self):
        self.assertIn('href="https://kws.oriokie.com/reports/tithe/"',
                      self._out("https://kws.oriokie.com/"))

    def test_no_base_no_link_but_text(self):
        out = self._out("")
        self.assertNotIn("href=", out)
        self.assertIn("Tithe received", out)

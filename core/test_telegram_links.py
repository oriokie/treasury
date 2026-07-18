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

    def test_no_base_shows_actionable_hint(self):
        # the report path is still named, and the treasurer is told how to make
        # it a link — rather than the link vanishing without trace
        out = self._out("")
        self.assertIn("/reports/tithe/", out)
        self.assertIn("Settings", out)


class AppLinkHelperTests(TestCase):
    def _cfg(self, base):
        cfg = SiteConfig.get(); cfg.site_base_url = base; cfg.save()
        return SiteConfig.get()

    def test_absolute_link_when_configured(self):
        from core.services.telegram_bot import _app_link
        out = _app_link("/benevolent/cases/7/", "Open this case",
                        cfg=self._cfg("kws.oriokie.com"))
        self.assertIn('href="https://kws.oriokie.com/benevolent/cases/7/"', out)
        self.assertIn("Open this case", out)

    def test_hint_when_not_configured(self):
        from core.services.telegram_bot import _app_link
        out = _app_link("/benevolent/cases/7/", "Open this case", cfg=self._cfg(""))
        self.assertNotIn("href=", out)
        self.assertIn("/benevolent/cases/7/", out)
        self.assertIn("Settings", out)


class HelpTextTests(TestCase):
    def test_balance_help_not_corrupted(self):
        """The /balance help line was once mangled by a bad escape edit
        ('fundlt;fund…'); guard against it regressing."""
        from core.services.telegram_bot import HELP
        self.assertNotIn("fundlt", HELP)
        self.assertNotIn("fundgt", HELP)
        self.assertIn("/balance", HELP)

    def test_help_lists_core_commands(self):
        from core.services.telegram_bot import HELP
        for cmd in ("/summary", "/trust", "/pending", "/member", "/case",
                    "/benevolent", "/arrears", "/balance", "/expense",
                    "/envelope", "/lock"):
            self.assertIn(cmd, HELP)

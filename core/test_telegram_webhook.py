"""The inbound Telegram webhook: reachable by Telegram, and by nobody else.

This endpoint had no tests at all, and that is exactly how it came to be
completely dead. ``TelegramWebhookView`` was written CSRF-exempt (correctly —
Telegram has no CSRF token to give us) but it never carried
``@login_not_required``, and the global default-deny gate
(``LoginRequiredMiddleware``, review P1-1) sits in front of every view. So the
gate 302'd every inbound update to /accounts/login/ before the view's own token
check ever ran: Telegram POSTed, got a redirect to an HTML login page instead of
the ``{"ok": true}`` it waits for, and the bot silently did nothing on every
church that switched it on.

``accounts/test_default_deny.py`` could not have caught this. Its URL walk skips
any pattern containing '<', and this endpoint's whole authentication scheme is a
secret token in the path (``/api/telegram/webhook/<token>/``), so it was invisible
to the one test whose job is noticing exactly this. Hence a dedicated file.

Making the view public is a deliberate, reviewed decision and belongs written
down next to the tests that police it — the same reasoning
``statements/webhook.py``'s ``CbsEventWebhookView`` records for the bank feed:

  This is a machine-to-machine endpoint. Telegram is not, and cannot be, a
  logged-in user, so a session gate can only ever break it. It is NOT
  unauthenticated: the URL path itself carries the bot token, which only
  Telegram and this server know (the standard Telegram practice), and it is
  compared in constant time against the configured token. The endpoint is
  inert unless an administrator has both enabled the bot and stored a token,
  and it reads nothing and writes nothing until that comparison passes.

The tests below pin all three halves of that: the gate must not intercept it,
the token check must actually reject impostors, and a blank/absent configuration
must never be coaxed into accepting anything.
"""
from unittest import mock

from django.test import Client, TestCase
from django.urls import reverse

from core.models import SiteConfig

TOKEN = "123456789:AAHreal-bot-token-from-botfather"


def _enable_bot(token=TOKEN):
    cfg = SiteConfig.get()
    cfg.telegram_enabled = True
    cfg.telegram_bot_token = token
    cfg.save()
    return cfg


def _url(token):
    return reverse("telegram_webhook", args=[token])


class TelegramWebhookReachabilityTests(TestCase):
    """The bug that motivated this file: the default-deny gate ate every update."""

    def setUp(self):
        self.anon = Client()

    def test_anonymous_post_with_the_right_token_is_accepted_not_redirected(self):
        """Telegram is anonymous by definition — it has no session with us. If
        this ever 302s to the login page again the bot is dead in the water,
        because Telegram treats anything that is not a 2xx as a failed delivery
        and keeps retrying the same update."""
        _enable_bot()
        with mock.patch("core.services.telegram_bot.process_and_reply") as handler:
            response = self.anon.post(_url(TOKEN), data="{}",
                                      content_type="application/json")
        self.assertNotEqual(
            response.status_code, 302,
            f"the webhook redirected Telegram to "
            f"{response.headers.get('Location', '')!r} — the login gate is in "
            f"front of it again and the bot cannot work")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"ok": True})
        self.assertTrue(handler.called,
                        "the update never reached the bot handler")

    def test_the_view_is_marked_login_not_required(self):
        """Structural companion to the request-level test above: assert the
        marker itself, so the reason the endpoint is reachable stays visible in
        the source rather than being an accident of middleware ordering."""
        from core.telegram_webhook import TelegramWebhookView
        self.assertTrue(
            getattr(TelegramWebhookView.as_view(), "login_required", True) is False,
            "TelegramWebhookView is missing @login_not_required")


class TelegramWebhookTokenTests(TestCase):
    """The token in the path is the only thing standing between the internet and
    the bot, now that the login gate deliberately does not apply."""

    def setUp(self):
        self.anon = Client()

    def test_a_wrong_token_is_rejected(self):
        _enable_bot()
        with mock.patch("core.services.telegram_bot.process_and_reply") as handler:
            response = self.anon.post(_url("not-the-token"), data="{}",
                                      content_type="application/json")
        self.assertEqual(response.status_code, 403)
        self.assertFalse(handler.called,
                         "an unauthenticated update reached the bot handler")

    def test_a_blank_configured_token_cannot_be_matched_by_anything(self):
        """The dangerous shape of a token check is one that compares "" to ""
        and calls it a pass. A church that enabled the bot but never pasted a
        token must have a closed door, not an open one keyed to the empty
        string."""
        cfg = SiteConfig.get()
        cfg.telegram_enabled = True
        cfg.telegram_bot_token = ""
        cfg.save()
        for attempt in ("x", "None", "0", " "):
            with mock.patch("core.services.telegram_bot.process_and_reply") as handler:
                response = self.anon.post(_url(attempt), data="{}",
                                          content_type="application/json")
            self.assertEqual(response.status_code, 403,
                             f"token {attempt!r} was accepted against a blank "
                             f"configured token")
            self.assertFalse(handler.called)

    def test_the_bot_being_disabled_closes_the_endpoint(self):
        cfg = SiteConfig.get()
        cfg.telegram_enabled = False
        cfg.telegram_bot_token = TOKEN
        cfg.save()
        with mock.patch("core.services.telegram_bot.process_and_reply") as handler:
            response = self.anon.post(_url(TOKEN), data="{}",
                                      content_type="application/json")
        self.assertEqual(response.status_code, 403)
        self.assertFalse(handler.called)

    def test_a_non_ascii_token_is_rejected_rather_than_crashing(self):
        """``hmac.compare_digest`` refuses str arguments that are not pure
        ASCII — it raises TypeError rather than returning False. Any passer-by
        can put a non-ASCII character in the path, so comparing the raw strings
        turned a wrong guess into an unhandled 500. Compare the encoded bytes
        instead: still constant time, and a bad guess is simply a bad guess."""
        _enable_bot()
        response = self.anon.post(_url("tökén-ünicode"), data="{}",
                                  content_type="application/json")
        self.assertEqual(response.status_code, 403)

    def test_malformed_json_from_a_valid_caller_is_a_400(self):
        _enable_bot()
        response = self.anon.post(_url(TOKEN), data="{not json",
                                  content_type="application/json")
        self.assertEqual(response.status_code, 400)

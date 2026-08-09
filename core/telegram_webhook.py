"""Telegram webhook endpoint.

Telegram delivers updates to ``/api/telegram/webhook/<token>/`` where <token>
must equal the configured bot token (this is the standard Telegram practice for
authenticating that the request really came from Telegram). The update is parsed
and handled, and replies are sent back via the Bot API.
"""
import hmac
import json

from django.contrib.auth.decorators import login_not_required
from django.http import JsonResponse
from django.utils.decorators import method_decorator
from django.views import View
from django.views.decorators.csrf import csrf_exempt

from core.models import SiteConfig
from core.services import telegram_bot


@method_decorator(csrf_exempt, name="dispatch")
@method_decorator(login_not_required, name="dispatch")
class TelegramWebhookView(View):
    """POST-only endpoint Telegram calls for every update.

    Machine-to-machine, exactly like ``statements.webhook.CbsEventWebhookView``:
    Telegram is not, and cannot be, a logged-in user, so it is exempt from the
    global login-required gate (P1-1). That exemption was missing until now and
    the bot was therefore completely dead wherever it was switched on — the gate
    302'd every inbound update to /accounts/login/ before this view's own token
    check ever ran, so Telegram received an HTML login page instead of the
    ``{"ok": true}`` it waits for and retried the same update forever.

    It is NOT unauthenticated. The bot token is carried in the URL path (the
    standard Telegram practice: only Telegram and this server know it) and is
    compared below in constant time against the configured token. The endpoint
    stays inert unless an administrator has both enabled the bot AND stored a
    token, so a fresh or half-configured install has a closed door rather than
    one keyed to the empty string.
    """

    @staticmethod
    def _token_ok(supplied, configured):
        """Constant-time comparison of the path token against the configured
        one. Compares the UTF-8 *bytes* rather than the str values on purpose:
        ``hmac.compare_digest`` raises TypeError on a str containing non-ASCII
        characters instead of simply returning False, and the supplied half of
        this comparison is a URL path segment that any passer-by can fill with
        whatever they like — comparing raw str turned a wrong guess into an
        unhandled 500. Bytes have no such restriction and the comparison stays
        constant time."""
        return hmac.compare_digest(str(supplied).encode("utf-8", "surrogatepass"),
                                   str(configured).encode("utf-8", "surrogatepass"))

    def post(self, request, token):
        cfg = SiteConfig.get()
        if not cfg.telegram_enabled or not cfg.telegram_bot_token:
            return JsonResponse({"ok": False, "error": "disabled"}, status=403)
        if not self._token_ok(token, cfg.telegram_bot_token):
            return JsonResponse({"ok": False, "error": "bad token"}, status=403)
        try:
            update = json.loads(request.body.decode() or "{}")
        except (ValueError, UnicodeDecodeError):
            return JsonResponse({"ok": False, "error": "bad json"}, status=400)
        try:
            telegram_bot.process_and_reply(update)
        except Exception:  # never 500 back to Telegram or it retries forever
            pass
        return JsonResponse({"ok": True})

    def get(self, request, token):
        return JsonResponse({"ok": True, "info": "Telegram webhook endpoint."})

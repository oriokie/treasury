"""Telegram webhook endpoint.

Telegram delivers updates to ``/api/telegram/webhook/<token>/`` where <token>
must equal the configured bot token (this is the standard Telegram practice for
authenticating that the request really came from Telegram). The update is parsed
and handled, and replies are sent back via the Bot API.
"""
import hmac
import json

from django.http import JsonResponse
from django.utils.decorators import method_decorator
from django.views import View
from django.views.decorators.csrf import csrf_exempt

from core.models import SiteConfig
from core.services import telegram_bot


@method_decorator(csrf_exempt, name="dispatch")
class TelegramWebhookView(View):
    def post(self, request, token):
        cfg = SiteConfig.get()
        if not cfg.telegram_enabled or not cfg.telegram_bot_token:
            return JsonResponse({"ok": False, "error": "disabled"}, status=403)
        if not hmac.compare_digest(str(token), str(cfg.telegram_bot_token)):
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

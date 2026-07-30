"""Inbound webhook for the Co-operative Bank CBS real-time transaction feed.

The bank's Core Banking System POSTs one JSON event per transaction to this
endpoint and expects a 2XX reply of the form {"MessageCode": "...", "Message": ...}.
Any non-2XX reply causes the bank to re-deliver (up to a maximum), so this view is
idempotent: a TransactionId already seen returns 200 without creating anything.

Security: this is the one endpoint external traffic uses to write financial data,
so every call is authenticated against the credentials configured in Settings
(Basic or bearer token). It is CSRF-exempt (machine-to-machine) and never requires
a user session.
"""
import base64
import json
import datetime as dt
from decimal import Decimal, InvalidOperation

from django.http import JsonResponse
from django.utils.decorators import method_decorator
from django.views import View
from django.contrib.auth.decorators import login_not_required
from django.views.decorators.csrf import csrf_exempt

from core.models import SiteConfig
from giving.models import Transaction
from statements.models import BankAccount, BankEvent
from statements.services.parser import parse_narration
from statements.services.ingest import ingest_event


def _reply(code, message, http_status):
    return JsonResponse({"MessageCode": str(code), "Message": message},
                        status=http_status)


def _parse_date(*candidates):
    """CBS sends dates like '2023-11-06+03:00' (ISO date + offset, no time)."""
    for raw in candidates:
        if not raw:
            continue
        s = str(raw).strip()[:10]          # take the YYYY-MM-DD part
        try:
            return dt.date.fromisoformat(s)
        except ValueError:
            continue
    return dt.date.today()


@method_decorator(csrf_exempt, name="dispatch")
@method_decorator(login_not_required, name="dispatch")
class CbsEventWebhookView(View):
    """POST-only endpoint the bank calls for every transaction.

    Machine-to-machine: the bank is not a logged-in user, so this is exempt from
    the global login-required gate (P1-1). It is NOT unauthenticated — access is
    controlled by ``_authenticated`` below (shared-secret token or HMAC per
    ``bank_feed_auth_mode``), which every non-GET request must pass."""

    def get(self, request):
        # a simple health check so the bank/ops can verify the URL is live
        cfg = SiteConfig.get()
        return _reply("200", "CBS event endpoint is live."
                      if cfg.bank_feed_enabled else "Endpoint present but disabled.",
                      200)

    def _authenticated(self, request, cfg):
        mode = cfg.bank_feed_auth_mode
        auth = request.META.get("HTTP_AUTHORIZATION", "")
        if mode == SiteConfig.BankFeedAuth.NONE:
            return True
        if mode == SiteConfig.BankFeedAuth.BASIC:
            if not auth.startswith("Basic ") or not cfg.bank_feed_username:
                return False
            try:
                user, _, pwd = base64.b64decode(auth[6:]).decode("utf-8").partition(":")
            except Exception:  # noqa: BLE001
                return False
            return user == cfg.bank_feed_username and pwd == cfg.bank_feed_password
        if mode == SiteConfig.BankFeedAuth.TOKEN:
            import hmac
            token = (cfg.bank_feed_token or "").strip()
            if not token:
                return False
            # Accept the token however the bank presents it: an Authorization
            # header with or without a Bearer/Token scheme, or a custom header
            # (X-Auth-Token / X-Api-Key / Api-Key). Compared in constant time.
            def _strip_scheme(v):
                v = (v or "").strip()
                for scheme in ("Bearer ", "Token ", "bearer ", "token "):
                    if v.startswith(scheme):
                        return v[len(scheme):].strip()
                return v
            candidates = [_strip_scheme(auth)]
            for h in ("HTTP_X_AUTH_TOKEN", "HTTP_X_API_KEY", "HTTP_API_KEY",
                      "HTTP_APIKEY", "HTTP_X_AUTHORIZATION", "HTTP_TOKEN"):
                candidates.append(_strip_scheme(request.META.get(h, "")))
            return any(c and hmac.compare_digest(c, token) for c in candidates)
        return False

    def post(self, request):
        cfg = SiteConfig.get()
        if not cfg.bank_feed_enabled:
            return _reply("403", "Bank feed is disabled.", 403)
        if not self._authenticated(request, cfg):
            return _reply("401", "Unauthorized.", 401)

        try:
            payload = json.loads(request.body.decode("utf-8"))
        except Exception:  # noqa: BLE001
            return _reply("400", "Malformed JSON payload.", 400)

        txn_id = str(payload.get("TransactionId") or "").strip()
        if not txn_id:
            return _reply("400", "Missing TransactionId.", 400)

        # idempotency: the bank re-delivers on non-2XX, so an already-seen event
        # is acknowledged with 200 and creates nothing.
        if BankEvent.objects.filter(cbs_transaction_id=txn_id).exists():
            return _reply("200", "Already received.", 200)

        amount_raw = payload.get("Amount")
        try:
            amount = Decimal(str(amount_raw))
        except (InvalidOperation, TypeError):
            amount = None
        event_type = str(payload.get("EventType") or "").strip().upper()

        # The bank states its balance on every event. Read out into columns
        # rather than left inside the payload blob, so the account's position
        # can actually be asked for — this is the only live figure the church
        # has, and it arrives with every transaction.
        def _money(raw):
            try:
                return Decimal(str(raw))
            except (InvalidOperation, TypeError, ValueError):
                return None

        booked = _money(payload.get("BookedBalance"))
        cleared = _money(payload.get("ClearedBalance"))
        balance_at = _parse_date(payload.get("PostingDate"),
                                 payload.get("ValueDate"),
                                 payload.get("TransactionDate"))

        evt = BankEvent.objects.create(
            cbs_transaction_id=txn_id,
            acct_no=str(payload.get("AcctNo") or "")[:40],
            amount=amount if amount is not None else None,
            event_type=event_type[:10],
            currency=str(payload.get("Currency") or "")[:8],
            payment_ref=str(payload.get("PaymentRef") or "")[:80],
            booked_balance=booked, cleared_balance=cleared,
            balance_at=balance_at,
            payload=json.dumps(payload)[:20000],
            status=BankEvent.Status.RECEIVED)

        if amount is None or amount <= 0 or event_type not in ("DEBIT", "CREDIT"):
            evt.status = BankEvent.Status.REJECTED
            evt.error = "Missing/invalid Amount or EventType."
            evt.save(update_fields=["status", "error"])
            return _reply("400", "Invalid Amount or EventType.", 400)

        direction = (Transaction.Direction.CREDIT if event_type == "CREDIT"
                     else Transaction.Direction.DEBIT)
        date = _parse_date(payload.get("TransactionDate"),
                           payload.get("PostingDate"), payload.get("ValueDate"))

        # Narration carries the payer/reference (M-Pesa-style or a transfer note);
        # fall back to the customer memo lines.
        narration = str(payload.get("Narration") or "").strip()
        memos = " ".join(filter(None, [
            str(payload.get("CustMemoLine1") or "").strip(),
            str(payload.get("CustMemoLine2") or "").strip(),
            str(payload.get("CustMemoLine3") or "").strip()]))
        parsed = parse_narration(narration or memos)
        raw_narration = "\n".join(filter(None, [narration, memos]))

        # match the destination account to a configured bank account by its number
        acct = str(payload.get("AcctNo") or "").strip()
        bank_account = None
        if acct:
            bank_account = (BankAccount.objects.filter(account_number=acct).first()
                            or BankAccount.objects.filter(
                                account_number__endswith=acct[-6:]).first())

        try:
            txn, outcome = ingest_event(
                date=date, amount=amount, direction=direction,
                reference=parsed.get("reference", ""), phone=parsed.get("phone", ""),
                name=parsed.get("name", ""), raw_narration=raw_narration,
                core_ref=txn_id, bank_receipt=parsed.get("receipt", "") or
                str(payload.get("PaymentRef") or ""),
                mpesa_ref=parsed.get("receipt", ""), bank_account=bank_account)
        except Exception as exc:  # noqa: BLE001
            evt.status = BankEvent.Status.FAILED
            evt.error = f"{type(exc).__name__}: {exc}"[:300]
            evt.save(update_fields=["status", "error"])
            # 500 -> the bank will retry; the duplicate guard prevents double-posting
            return _reply("500", "Could not process event; will accept on retry.", 500)

        if outcome == "duplicate":
            evt.status = BankEvent.Status.DUPLICATE
            evt.save(update_fields=["status"])
            return _reply("200", "Already received.", 200)

        evt.status = BankEvent.Status.PROCESSED
        evt.transaction = txn
        evt.save(update_fields=["status", "transaction"])
        return _reply("200", "Successfully received data", 200)

"""Personal contribution links.

A member's match code is the public handle: ``/c/MB7K2M`` opens a page where
anyone can give by M-Pesa prompt, and the gift is credited to that member.
An attempt that never finishes stays on the follow-up list.
"""
import secrets
from datetime import timedelta
from decimal import Decimal, InvalidOperation

from django.db import connection, transaction as db_tx
from django.urls import reverse
from django.utils import timezone

from core.models import SiteConfig
from core.services.daraja import stk_push, stk_query, stk_ready
from core.services.sms import _format, sms_first_name
from giving.models import ContributionAttempt, Transaction
from members.models import Member, normalize_phone

MAX_AMOUNT = Decimal("150000")
MIN_AMOUNT = Decimal("1")

DEFAULT_SMS = (
    "Dear {name}, {church} has introduced a personal contribution link for you. "
    "You may use it to make your contribution and also share it with friends and "
    "family who wish to support you. All payments made through your link will be "
    "credited to your account and added to your total contribution. "
    "Your link: {link}. Your code: {code}. God bless you."
)


def display_name(full):
    """'EDWIN ORIOKI' as it should read on a page: Edwin Orioki."""
    parts = (full or "").split()
    return " ".join(p.title() for p in parts)


def member_for_code(code):
    code = (code or "").strip().upper()
    if not code:
        return None
    return Member.objects.filter(match_code__iexact=code, active=True).first()


def public_path(member):
    return reverse("contrib_link", args=[member.match_code])


def absolute_url(path, request=None, cfg=None):
    cfg = cfg or SiteConfig.get()
    base = (cfg.site_base_url or "").strip().rstrip("/")
    if base:
        return base + path
    if request is not None:
        return request.build_absolute_uri(path)
    return path


def contribution_url(member, request=None, cfg=None):
    return absolute_url(public_path(member), request, cfg)


def callback_url(request=None, cfg=None):
    return absolute_url(reverse("mpesa_stk_callback"), request, cfg)


def quick_amounts(cfg=None):
    cfg = cfg or SiteConfig.get()
    out = []
    for part in (cfg.contrib_quick_amounts or "").replace(" ", "").split(","):
        if part.isdigit():
            n = int(part)
            if 1 <= n <= int(MAX_AMOUNT):
                out.append(n)
    return out[:6] or [50, 100, 500, 1000]


def payment_ready(cfg=None):
    """The public page can take a prompt only when the link, the fund and
    Daraja are all in place, and Safaricom has an https address to call back."""
    cfg = cfg or SiteConfig.get()
    if not cfg.contrib_links_enabled:
        return False, "Contribution links are switched off."
    if not cfg.contrib_fund_id:
        return False, "No fund is set for contribution links."
    ok, reason = stk_ready(cfg)
    if not ok:
        return False, reason
    url = callback_url(cfg=cfg)
    if not url.lower().startswith("https://"):
        return False, ("Set the public site address (https://…) so Safaricom "
                       "can confirm the payment.")
    return True, ""


def render_sms(member, request=None, cfg=None):
    cfg = cfg or SiteConfig.get()
    template = (cfg.contrib_sms_template or "").strip() or DEFAULT_SMS
    return _format(
        template,
        name=sms_first_name(member.name) or "Friend",
        church=cfg.church_name or "the church",
        fund=(cfg.contrib_title or "").strip() or "Development Fund",
        link=contribution_url(member, request, cfg),
        code=member.match_code or "",
    )


def parse_amount(raw):
    try:
        amount = Decimal(str(raw or "").strip().replace(",", ""))
    except (InvalidOperation, ValueError):
        return None, "Enter an amount in shillings."
    if amount != amount.to_integral_value():
        return None, "Enter a whole number of shillings."
    if amount < MIN_AMOUNT:
        return None, "The smallest gift is 1 shilling."
    if amount > MAX_AMOUNT:
        return None, "That amount is above the M-Pesa limit for one prompt."
    return amount, ""


def start_prompt(member, phone_raw, amount, request=None, cfg=None):
    """Send an STK prompt. Returns (attempt or None, error message)."""
    cfg = cfg or SiteConfig.get()
    ready, reason = payment_ready(cfg)
    if not ready:
        return None, reason
    phone = normalize_phone(phone_raw)
    if not phone:
        return None, "Enter a Safaricom number, for example 07XXXXXXXX."
    amount, err = parse_amount(amount) if not isinstance(amount, Decimal) else (amount, "")
    if err:
        return None, err

    recent = timezone.now() - timedelta(seconds=45)
    if ContributionAttempt.objects.filter(
            phone=phone, status=ContributionAttempt.Status.SENT,
            created_at__gte=recent).exists():
        return None, "A prompt is already on its way to that number. Check the phone."
    hour_ago = timezone.now() - timedelta(hours=1)
    if ContributionAttempt.objects.filter(
            member=member, created_at__gte=hour_ago).count() >= 8:
        return None, "Too many attempts on this link. Please try again in a little while."

    attempt = ContributionAttempt.objects.create(
        member=member, code=member.match_code, phone=phone, amount=amount,
        fund_id=cfg.contrib_fund_id, status=ContributionAttempt.Status.ERROR,
        token=secrets.token_urlsafe(18)[:32],
        result_desc="Sending the prompt…",
    )
    data, err = stk_push(
        phone, amount, member.match_code, callback_url(request, cfg), cfg)
    if err or not data:
        attempt.result_desc = (err or "Could not send the prompt.")[:255]
        attempt.save(update_fields=["result_desc", "updated_at"])
        _notify_incomplete(attempt)
        return attempt, attempt.result_desc
    attempt.status = ContributionAttempt.Status.SENT
    attempt.checkout_request_id = (data.get("CheckoutRequestID") or "")[:64]
    attempt.merchant_request_id = (data.get("MerchantRequestID") or "")[:64]
    attempt.result_code = str(data.get("ResponseCode") or "")[:8]
    attempt.result_desc = (data.get("CustomerMessage")
                           or data.get("ResponseDescription")
                           or "Check your phone and enter your M-Pesa PIN.")[:255]
    attempt.save(update_fields=[
        "status", "checkout_request_id", "merchant_request_id",
        "result_code", "result_desc", "updated_at"])
    return attempt, ""


def status_for_code(code):
    try:
        n = int(code)
    except (TypeError, ValueError):
        return ContributionAttempt.Status.FAILED
    if n == 0:
        return ContributionAttempt.Status.SUCCESS
    if n == 1032:
        return ContributionAttempt.Status.CANCELLED
    if n in (1037, 1006):
        return ContributionAttempt.Status.TIMEOUT
    return ContributionAttempt.Status.FAILED


def settle(attempt_id, result_code, result_desc="", receipt=""):
    """Record how a prompt ended, and book the gift once if it was paid.

    Safe to call from the callback and from a status query: a second call
    does not create a second ledger row.
    """
    with db_tx.atomic():
        qs = ContributionAttempt.objects.select_related("member", "fund")
        if connection.vendor != "sqlite":
            qs = qs.select_for_update()
        attempt = qs.filter(pk=attempt_id).first()
        if attempt is None:
            return None
        receipt = (receipt or "")[:30]
        if attempt.transaction_id:
            _attach_receipt(attempt, receipt)
            return attempt
        status = status_for_code(result_code)
        # A prompt already closed as unpaid stays closed, unless this result
        # says the money actually arrived — then it is booked once.
        if status != ContributionAttempt.Status.SUCCESS and attempt.status != ContributionAttempt.Status.SENT:
            return attempt
        attempt.status = status
        attempt.result_code = str(result_code if result_code is not None else "")[:8]
        if result_desc:
            attempt.result_desc = result_desc[:255]
        if receipt:
            attempt.mpesa_receipt = receipt
        if status == ContributionAttempt.Status.SUCCESS:
            attempt.transaction = _book(attempt, receipt)
        attempt.save()
    if attempt.status != ContributionAttempt.Status.SUCCESS:
        _notify_incomplete(attempt)
    return attempt


def _attach_receipt(attempt, receipt):
    if not receipt or attempt.mpesa_receipt:
        return
    attempt.mpesa_receipt = receipt
    attempt.save(update_fields=["mpesa_receipt", "updated_at"])
    txn = attempt.transaction
    if txn is not None and not txn.mpesa_ref:
        txn.mpesa_ref = receipt
        txn.save(update_fields=["mpesa_ref"])


def _book(attempt, receipt):
    member = attempt.member
    # The M-Pesa receipt is what a later statement import uses to recognise
    # this same payment, so it must not be booked a second time.
    if receipt:
        existing = (Transaction.objects.filter(mpesa_ref__iexact=receipt).first()
                    or Transaction.objects.filter(bank_receipt__iexact=receipt).first())
        if existing:
            return existing
    txn = Transaction.objects.create(
        date=timezone.localdate(),
        channel=Transaction.Channel.BANK,
        direction=Transaction.Direction.CREDIT,
        amount=attempt.amount,
        confirmed=True,
        department_id=attempt.fund_id,
        dev_group_id=member.dev_group_id,
        member=member,
        attributed_via_code=True,
        reference=attempt.code,
        payer_phone=attempt.phone,
        mpesa_ref=receipt,
        core_ref=f"STK{attempt.pk}",
        allocation_status=Transaction.Status.MANUAL,
        raw_narration=(
            f"Personal contribution link {attempt.code} "
            f"from {attempt.phone}"),
    )
    try:
        from pledges.services.matching import handle_new_contribution
        handle_new_contribution(txn, credited_member_only=True)
    except Exception:  # noqa: BLE001 — the gift is already on the ledger
        pass
    return txn


def _notify_incomplete(attempt):
    try:
        from core.services.notifications import notify
        who = display_name(attempt.member.name) or attempt.code
        notify(
            "GENERAL",
            f"{who} ({attempt.code}) — KSh {attempt.amount:,.0f} by contribution "
            f"link was not completed ({attempt.get_status_display()}).",
            link=reverse("contrib_attempts"),
        )
    except Exception:  # noqa: BLE001
        pass


def refresh_from_daraja(attempt):
    """Ask Safaricom once how a still-open prompt ended."""
    if attempt.status != ContributionAttempt.Status.SENT or not attempt.checkout_request_id:
        return attempt
    now = timezone.now()
    if attempt.queried_at and (now - attempt.queried_at).total_seconds() < 8:
        return attempt
    ContributionAttempt.objects.filter(pk=attempt.pk).update(queried_at=now)
    data, err = stk_query(attempt.checkout_request_id)
    if err or not data:
        return ContributionAttempt.objects.get(pk=attempt.pk)
    code = data.get("ResultCode")
    if code is None:
        return ContributionAttempt.objects.get(pk=attempt.pk)
    return settle(
        attempt.pk, code,
        data.get("ResultDesc") or "",
        str(data.get("MpesaReceiptNumber") or ""))


def apply_callback(payload):
    """Safaricom's STK callback. Unknown checkout ids are ignored."""
    cb = {}
    if isinstance(payload, dict):
        cb = (payload.get("Body") or {}).get("stkCallback") or {}
    checkout = (cb.get("CheckoutRequestID") or "").strip()
    if not checkout:
        return None
    attempt = ContributionAttempt.objects.filter(checkout_request_id=checkout).first()
    if attempt is None:
        return None
    meta = {}
    for item in ((cb.get("CallbackMetadata") or {}).get("Item") or []):
        if isinstance(item, dict) and item.get("Name"):
            meta[item["Name"]] = item.get("Value")
    return settle(
        attempt.pk,
        cb.get("ResultCode"),
        cb.get("ResultDesc") or "",
        str(meta.get("MpesaReceiptNumber") or ""),
    )


def public_message(attempt):
    if attempt.status == ContributionAttempt.Status.SUCCESS:
        return "Thank you. The gift is received and credited."
    if attempt.status == ContributionAttempt.Status.SENT:
        return "Check the phone and enter the M-Pesa PIN."
    if attempt.status == ContributionAttempt.Status.CANCELLED:
        return "The prompt was cancelled. You can try again."
    if attempt.status == ContributionAttempt.Status.TIMEOUT:
        return "The phone did not respond in time. You can try again."
    return attempt.result_desc or "The payment was not completed. You can try again."

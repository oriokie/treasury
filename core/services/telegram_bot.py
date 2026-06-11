"""Telegram bot for remote treasurer access.

Security model
--------------
* Every chat must enter the configured PIN before ANY data is shown or any
  expense is recorded. A correct PIN unlocks the chat for
  ``telegram_session_minutes`` only; after that the PIN is required again.
* Expenses entered remotely are created as PENDING and must be approved inside
  the web app by a treasurer — remote entry never auto-approves money out.
* The bot token is stored encrypted; the webhook is validated against it.

This module is transport-agnostic: ``handle_update(update)`` takes a Telegram
update dict and returns a list of reply dicts ``{"chat_id", "text"}``. It is
driven either by the webhook view or by the ``telegram_bot`` polling command,
and can be unit-tested without a live bot.
"""
import datetime as dt
import json
import urllib.request
import urllib.parse
from decimal import Decimal, InvalidOperation

from django.utils import timezone


# ----------------------------------------------------------------------------- helpers
def _cfg():
    from core.models import SiteConfig
    return SiteConfig.get()


def _session(chat_id):
    from core.models import TelegramSession
    s, _ = TelegramSession.objects.get_or_create(chat_id=str(chat_id))
    return s


def _money(v):
    return f"{float(v or 0):,.2f}"


def send_message(chat_id, text, cfg=None):
    """Send a message via the Telegram Bot API. Returns (ok, error)."""
    cfg = cfg or _cfg()
    token = cfg.telegram_bot_token
    if not token:
        return False, "No bot token configured."
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    data = urllib.parse.urlencode({
        "chat_id": chat_id, "text": text, "parse_mode": "HTML"}).encode()
    try:
        req = urllib.request.Request(url, data=data)
        with urllib.request.urlopen(req, timeout=15) as r:
            return (r.status == 200), None
    except Exception as exc:  # noqa
        return False, str(exc)[:120]


# ----------------------------------------------------------------------------- commands
HELP = (
    "<b>Treasury bot</b>\n"
    "Queries:\n"
    "• /summary — collections, expenses, surplus this month\n"
    "• /trust — trust collected / remitted / outstanding\n"
    "• /balance &lt;fund&gt; — closing balance of a fund\n"
    "• /today — today's collections\n"
    "• Or just type a question, e.g. <i>how much tithe in May?</i>\n\n"
    "Record an expense:\n"
    "• /expense — guided (amount → fund → description)\n"
    "• /cancel — abandon the current entry\n"
    "• /lock — lock the chat now")


def _do_summary():
    from giving.models import Transaction
    from cashbook.models import Expense
    from django.db.models import Sum
    today = dt.date.today()
    s = today.replace(day=1)
    inc = (Transaction.objects.confirmed_credits()
           .filter(excluded_from_income=False, date__gte=s, date__lte=today,
                   department__is_trust=False)
           .aggregate(t=Sum("amount"))["t"] or Decimal(0))
    exp = (Expense.objects.filter(
        status__in=[Expense.Status.APPROVED, Expense.Status.PAID],
        date__gte=s, date__lte=today, department__is_trust=False)
        .aggregate(t=Sum("amount"))["t"] or Decimal(0))
    return (f"<b>{today:%B %Y}</b>\n"
            f"Collections: {_money(inc)}\nExpenditure: {_money(exp)}\n"
            f"Surplus/(deficit): {_money(inc - exp)}")


def _do_trust():
    from reports.services import balances
    rows = balances.trust_summary()
    coll = sum((r["collected"] for r in rows), Decimal(0))
    rem = sum((r["remitted"] for r in rows), Decimal(0))
    out = sum((r["to_remit"] for r in rows), Decimal(0))
    return (f"<b>Trust funds</b>\nCollected: {_money(coll)}\n"
            f"Remitted: {_money(rem)}\nOutstanding: {_money(out)}")


def _do_today():
    from giving.models import Transaction
    from django.db.models import Sum
    today = dt.date.today()
    t = (Transaction.objects.confirmed_credits()
         .filter(excluded_from_income=False, date=today)
         .aggregate(x=Sum("amount"))["x"] or Decimal(0))
    return f"Today's collections ({today:%d %b}): <b>{_money(t)}</b>"


def _do_balance(arg):
    from departments.models import Department
    from reports.services import balances
    if not arg:
        return "Usage: /balance <fund name>"
    dept = (Department.objects.filter(name__icontains=arg).first()
            or Department.objects.filter(slug__icontains=arg).first())
    if not dept:
        return f"No fund matching “{arg}”."
    rows = balances.department_summary()
    for r in rows:
        if r["department"].id == dept.id:
            return (f"<b>{dept.name}</b>\nOpening: {_money(r['opening'])}\n"
                    f"Receipts: {_money(r['receipts'])}\nExpenses: {_money(r['expenses'])}\n"
                    f"Closing: <b>{_money(r['closing'])}</b>")
    return f"{dept.name}: no activity recorded."


def _funds_list():
    from departments.models import Department
    qs = Department.objects.filter(active=True, parent__isnull=True).order_by("name")[:40]
    return list(qs)


# ----------------------------------------------------------------------------- expense flow
def _start_expense(session):
    session.state = "EXP_AMOUNT"
    session.state_data = {}
    session.save()
    return "Recording an expense. What is the <b>amount</b>? (e.g. 1500)"


def _match_member(name):
    """Find the most likely member for a typed claimant name. Returns
    (member_or_None, ambiguous_bool). Uses the order-insensitive name key first,
    then a contains search."""
    from members.models import Member
    from members.services.matching import name_key
    key = name_key(name)
    if key:
        exact = list(Member.objects.filter(name_key=key)[:2])
        if len(exact) == 1:
            return exact[0], False
        if len(exact) > 1:
            return None, True
    parts = [p for p in name.strip().split() if len(p) > 1]
    if parts:
        from django.db.models import Q
        q = Q()
        for p in parts:
            q &= Q(name__icontains=p)
        hits = list(Member.objects.filter(q)[:2])
        if len(hits) == 1:
            return hits[0], False
        if len(hits) > 1:
            return None, True
    return None, False


def _expense_step(session, text):
    st = session.state
    data = session.state_data or {}
    if st == "EXP_AMOUNT":
        try:
            amt = Decimal(text.replace(",", "").strip())
            if amt <= 0:
                raise InvalidOperation
        except (InvalidOperation, ValueError):
            return "Please send a valid amount, e.g. 1500."
        data["amount"] = str(amt)
        funds = _funds_list()
        data["fund_ids"] = [f.id for f in funds]
        session.state = "EXP_FUND"; session.state_data = data; session.save()
        listing = "\n".join(f"{i+1}. {f.name}" for i, f in enumerate(funds))
        return "Which <b>fund</b>? Reply with the number or part of the name:\n" + listing
    if st == "EXP_FUND":
        from departments.models import Department
        dept = None
        ids = data.get("fund_ids") or []
        if text.strip().isdigit() and ids:
            i = int(text.strip()) - 1
            if 0 <= i < len(ids):
                dept = Department.objects.filter(id=ids[i]).first()
        if not dept:
            dept = Department.objects.filter(name__icontains=text.strip()).first()
        if not dept:
            return "Couldn't find that fund — reply with the number or part of the name."
        data["fund_id"] = dept.id; data["fund_name"] = dept.name
        session.state = "EXP_DESC"; session.state_data = data; session.save()
        return f"Fund: <b>{dept.name}</b>. Now a short <b>description</b>?"
    if st == "EXP_DESC":
        data["description"] = text.strip()[:200]
        session.state = "EXP_CLAIMANT"; session.state_data = data; session.save()
        return ("Who is being <b>paid</b> (the claimant)? Type their name, "
                "or send <b>-</b> to skip.")
    if st == "EXP_CLAIMANT":
        raw = text.strip()
        if raw in ("-", "/skip", "skip"):
            data["claimant"] = ""; data["member_id"] = None
        else:
            data["claimant"] = raw[:120]
            member, ambiguous = _match_member(raw)
            if member:
                data["claimant"] = member.name
                data["member_id"] = member.id
            else:
                data["member_id"] = None
        session.state = "EXP_METHOD"; session.state_data = data; session.save()
        note = ""
        if data.get("member_id"):
            note = f"Matched member <b>{data['claimant']}</b>. "
        return (note + "How was it <b>paid</b>?\n"
                "1. Cash\n2. M-Pesa\n3. Bank / cheque")
    if st == "EXP_METHOD":
        from cashbook.models import Expense
        choice = text.strip().lower()
        method_map = {"1": "CASH", "cash": "CASH",
                      "2": "MPESA", "mpesa": "MPESA", "m-pesa": "MPESA",
                      "3": "BANK", "bank": "BANK", "cheque": "CHEQUE"}
        method = method_map.get(choice)
        if not method:
            return "Reply 1 (cash), 2 (M-Pesa) or 3 (bank/cheque)."
        data["method"] = method
        session.state_data = data; session.save()
        # M-Pesa / bank payments often carry a transaction charge — ask for it
        if method in ("MPESA", "BANK", "CHEQUE"):
            session.state = "EXP_CHARGE"; session.save()
            return ("Any <b>transaction charge</b> on this payment? Send the amount "
                    "(e.g. 30), or <b>0</b> / <b>-</b> if none.")
        data["charge"] = "0"
        session.state = "EXP_CONFIRM"; session.state_data = data; session.save()
        return _expense_summary(data)
    if st == "EXP_CHARGE":
        raw = text.strip().replace(",", "")
        if raw in ("-", "0", "none", "no"):
            data["charge"] = "0"
        else:
            try:
                c = Decimal(raw)
                if c < 0:
                    raise InvalidOperation
                data["charge"] = str(c)
            except (InvalidOperation, ValueError):
                return "Send the charge amount (e.g. 30) or 0 if none."
        session.state = "EXP_CONFIRM"; session.state_data = data; session.save()
        return _expense_summary(data)
    if st == "EXP_CONFIRM":
        if text.strip().lower() not in ("yes", "y"):
            session.reset_flow(); session.save()
            return "Cancelled."
        return _create_expense(session, data)
    return None


def _expense_summary(data):
    lines = ["Confirm expense:",
             f"Amount: {_money(data['amount'])}",
             f"Fund: {data['fund_name']}",
             f"Description: {data['description']}"]
    if data.get("claimant"):
        tag = " (member)" if data.get("member_id") else ""
        lines.append(f"Paid to: {data['claimant']}{tag}")
    method_lbl = {"CASH": "Cash", "MPESA": "M-Pesa", "BANK": "Bank", "CHEQUE": "Cheque"}
    lines.append(f"Method: {method_lbl.get(data.get('method'), data.get('method'))}")
    if Decimal(data.get("charge", "0")) > 0:
        lines.append(f"Transaction charge: {_money(data['charge'])} (posted to LCB)")
    lines.append("\nReply <b>yes</b> to record (PENDING approval) or <b>no</b> to cancel.")
    return "\n".join(lines)


def _create_expense(session, data):
    from cashbook.models import Expense
    from departments.models import Department
    from core.models import entry_blocked
    from django.contrib.auth.models import User
    date = dt.date.today()
    blocked = entry_blocked(date)
    if blocked:
        session.reset_flow(); session.save()
        return f"{blocked} — the expense was not recorded."
    dept = Department.objects.filter(id=data["fund_id"]).first()
    # attribute to the Telegram user who raised it (per-user PIN), else fallback
    user = (session.user if session.user_id else None) \
        or User.objects.filter(groups__name="Treasurer").first() \
        or User.objects.filter(is_superuser=True).first()
    method = data.get("method", "CASH")
    exp = Expense.objects.create(
        date=date, department=dept, description=data["description"],
        amount=Decimal(data["amount"]), category=Expense.Category.OTHER,
        method=method, status=Expense.Status.PENDING,
        claimant=data.get("claimant") or "Telegram",
        voucher_no=f"TG-{session.chat_id}-{int(timezone.now().timestamp())}",
        recorded_by=user)
    extra = ""
    # a transaction charge is its own bank-charge expense, posted to LCB
    charge = Decimal(data.get("charge", "0") or "0")
    if charge > 0:
        lcb = (Department.objects.filter(name__icontains="LCB").first()
               or Department.objects.filter(name__icontains="Local Church Budget").first()
               or dept)
        Expense.objects.create(
            date=date, department=lcb, description=f"Bank/M-Pesa charge — {data['description']}",
            amount=charge, category=Expense.Category.BANK_CHARGE, method=method,
            status=Expense.Status.PENDING, claimant=data.get("claimant") or "Telegram",
            voucher_no=f"TG-{session.chat_id}-chg-{int(timezone.now().timestamp())}",
            recorded_by=user)
        extra = f" A charge of {_money(charge)} was posted to {lcb.name}."
    who = f" by {user.get_full_name() or user.username}" if user else ""
    session.reset_flow(); session.save()
    return (f"✅ Recorded expense #{exp.id} ({_money(exp.amount)}, {dept.name}){who} "
            f"as <b>PENDING</b>.{extra} A treasurer must approve it in the web app.")


# ----------------------------------------------------------------------------- dispatch
def handle_update(update):
    """Process one Telegram update dict. Returns a list of {chat_id, text}."""
    msg = update.get("message") or update.get("edited_message") or {}
    chat = msg.get("chat") or {}
    chat_id = chat.get("id")
    text = (msg.get("text") or "").strip()
    if chat_id is None or not text:
        return []
    cfg = _cfg()
    if not cfg.telegram_enabled:
        return [{"chat_id": chat_id, "text": "The treasury bot is currently switched off."}]

    session = _session(chat_id)
    low = text.lower()

    # Always-available controls
    if low in ("/lock", "/logout"):
        session.authenticated_until = None
        session.reset_flow(); session.save()
        return [{"chat_id": chat_id, "text": "🔒 Chat locked. Send your PIN to unlock."}]

    # PIN gate
    if not session.is_authenticated():
        from core.models import TelegramProfile
        # personal PIN first (identifies the user), then the shared fallback PIN
        user = TelegramProfile.user_for_pin(text)
        shared = (cfg.telegram_pin or "1234").strip()
        if user or (text.strip() and text.strip() == shared):
            session.user = user
            session.authenticated_until = timezone.now() + dt.timedelta(
                minutes=cfg.telegram_session_minutes or 30)
            session.reset_flow(); session.save()
            who = f" Signed in as <b>{user.get_full_name() or user.username}</b>." if user else ""
            return [{"chat_id": chat_id, "text": f"🔓 Unlocked.{who}\n\n" + HELP}]
        return [{"chat_id": chat_id,
                 "text": "🔐 Please enter your PIN to continue."}]

    # Authenticated --------------------------------------------------------
    if low in ("/cancel",):
        session.reset_flow(); session.save()
        return [{"chat_id": chat_id, "text": "Cancelled."}]

    # mid-flow (expense entry)
    if session.state.startswith("EXP_"):
        if low == "/cancel":
            session.reset_flow(); session.save()
            return [{"chat_id": chat_id, "text": "Cancelled."}]
        reply = _expense_step(session, text)
        if reply is not None:
            return [{"chat_id": chat_id, "text": reply}]

    if low in ("/start", "/help"):
        return [{"chat_id": chat_id, "text": HELP}]
    if low == "/expense":
        return [{"chat_id": chat_id, "text": _start_expense(session)}]
    if low == "/summary":
        return [{"chat_id": chat_id, "text": _do_summary()}]
    if low == "/trust":
        return [{"chat_id": chat_id, "text": _do_trust()}]
    if low == "/today":
        return [{"chat_id": chat_id, "text": _do_today()}]
    if low.startswith("/balance"):
        return [{"chat_id": chat_id, "text": _do_balance(text[8:].strip())}]

    # free text -> natural-language assistant (read-only). When the LLM is
    # enabled in settings the assistant answers conversationally; otherwise it
    # falls back to the rule-based responder.
    try:
        from core.services.assistant import answer
        ans = answer(text, user=session.user)
        if isinstance(ans, tuple):
            ans = ans[0]
        hint = "" if cfg.llm_enabled else "\n\n<i>Tip: try /summary, /trust, /today or /expense.</i>"
        return [{"chat_id": chat_id, "text": (ans or "I didn't catch that.") + hint}]
    except Exception:
        return [{"chat_id": chat_id, "text": "Try /help for what I can do."}]


def process_and_reply(update):
    """Handle an update and actually send the replies. Returns count sent."""
    cfg = _cfg()
    sent = 0
    for r in handle_update(update):
        ok, _ = send_message(r["chat_id"], r["text"], cfg)
        sent += 1 if ok else 0
    return sent

"""A lightweight, rule-based treasury assistant.

It understands plain-language questions about the data already in the system and
answers them with live database queries — no external service or API key needed,
so it works fully offline. It is read-only: it never changes data, it only
retrieves and explains. Each answer may include a small table and a link to the
matching full screen.

The matching is deliberately simple (keywords + a date-period parser). If nothing
matches, it explains what it can do.
"""
import calendar
import datetime as dt
import re
from decimal import Decimal

from django.db.models import Sum, Count, Q
from django.urls import reverse


# ---------- helpers ----------

def _money(v):
    return f"KES {Decimal(v or 0):,.2f}"


MONTHS = {m.lower(): i for i, m in enumerate(calendar.month_name) if m}
MONTHS.update({m.lower(): i for i, m in enumerate(calendar.month_abbr) if m})


def parse_period(text):
    """Return (start, end, label). Defaults to the current month."""
    t = text.lower()
    today = dt.date.today()

    if "today" in t:
        return today, today, "today"
    if "yesterday" in t:
        y = today - dt.timedelta(days=1)
        return y, y, "yesterday"
    if "this year" in t or "year to date" in t or "ytd" in t:
        return dt.date(today.year, 1, 1), today, f"{today.year}"
    if "last year" in t:
        y = today.year - 1
        return dt.date(y, 1, 1), dt.date(y, 12, 31), f"{y}"
    if "last month" in t:
        first = today.replace(day=1)
        prev_end = first - dt.timedelta(days=1)
        return prev_end.replace(day=1), prev_end, prev_end.strftime("%B %Y")
    if "this month" in t:
        last = calendar.monthrange(today.year, today.month)[1]
        return today.replace(day=1), today.replace(day=last), today.strftime("%B %Y")
    if "all time" in t or "overall" in t or "total ever" in t:
        return None, None, "all time"

    # explicit "<month> [year]"
    m = re.search(r"\b(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\.?\s*(\d{4})?", t)
    if m:
        mon = MONTHS.get(m.group(1)[:3])
        if mon:
            year = int(m.group(2)) if m.group(2) else today.year
            last = calendar.monthrange(year, mon)[1]
            return dt.date(year, mon, 1), dt.date(year, mon, last), dt.date(year, mon, 1).strftime("%B %Y")

    # explicit year only
    m = re.search(r"\b(20\d{2})\b", t)
    if m:
        year = int(m.group(1))
        return dt.date(year, 1, 1), dt.date(year, 12, 31), str(year)

    last = calendar.monthrange(today.year, today.month)[1]
    return today.replace(day=1), today.replace(day=last), today.strftime("%B %Y") + " (this month)"


def _credit_filter(start, end):
    # Consolidated: the "recognised income" credit basis now has a single home
    # in core.metrics.income_credit_filter (confirmed, non-reversed credits,
    # excluding envelope-twin / loan rows via excluded_from_income). This
    # wrapper is kept so existing call sites in this module keep working.
    from core.metrics import income_credit_filter
    return income_credit_filter(start, end)


def _find_fund(text):
    from departments.models import Department
    t = text.lower()
    best = None
    for d in Department.objects.all():
        if d.name.lower() in t and (best is None or len(d.name) > len(best.name)):
            best = d
    return best


# ---------- answers ----------

def _fund_balance(dept):
    """Accurate fund balance via the balance engine (carry-forward + transfers)."""
    from reports.services import balances
    row = next((r for r in balances.department_summary(None, None, consolidated=False)
                if r["department"].id == dept.id), None)
    if not row:
        return {"text": f"{dept.name} has no recorded activity yet.",
                "link": reverse("report_fund", args=[dept.id]), "link_label": "Open fund ledger"}
    table = [("Opening", _money(row["opening"])),
             ("Receipts", _money(row["receipts"])),
             ("Expenses", _money(row["expenses"]))]
    if row.get("transfers_in") or row.get("transfers_out"):
        table += [("Transfers in", _money(row["transfers_in"])),
                  ("Transfers out", _money(row["transfers_out"]))]
    table.append(("Balance", _money(row["closing"])))
    kind = "trust liability outstanding" if dept.is_trust else "available balance"
    article = "an" if kind[0] in "aeiou" else "a"
    return {"text": f"{dept.name} has {article} {kind} of {_money(row['closing'])}.",
            "rows": table,
            "link": reverse("report_fund", args=[dept.id]), "link_label": "Open fund ledger"}


def _data_context():
    """A compact snapshot the LLM can reason over (totals only, no member PII)."""
    import datetime as _dt
    from decimal import Decimal as _D
    from giving.models import Transaction
    from cashbook.models import Expense
    from reports.services import balances
    today = _dt.date.today()
    y0 = _dt.date(today.year, 1, 1)
    lines = [f"Today: {today}. Currency: KES. Financial year: {today.year}."]
    ytd = (Transaction.objects.filter(_credit_filter(y0, today))
           .aggregate(t=Sum("amount"))["t"] or 0)
    lines.append(f"Collections year-to-date: {ytd}.")
    # fund balances (carried-forward, includes transfers)
    rows = balances.department_summary(None, None)
    for r in rows:
        tag = "TRUST/liability" if r["is_trust"] else "local fund"
        lines.append(f"Fund '{r['department'].name}' ({tag}): balance {r['closing']}, "
                     f"receipts {r['receipts']}, expenses {r['expenses']}.")
    local = sum((r["closing"] for r in rows if not r["is_trust"]), _D(0))
    trust = sum((r["closing"] for r in rows if r["is_trust"]), _D(0))
    cash = local + trust
    lines.append(f"Total cash & bank: {cash}. Local fund balances: {local}. "
                 f"Trust funds payable (liability): {trust}.")
    # trust outstanding to remit
    to_remit = sum((r["to_remit"] for r in balances.trust_summary(None, None)), _D(0))
    lines.append(f"Trust funds still to remit: {to_remit}.")
    # expenses awaiting payment
    pend = (Expense.objects.filter(status__in=[Expense.Status.PENDING, Expense.Status.APPROVED])
            .aggregate(t=Sum("amount"))["t"] or 0)
    lines.append(f"Expenses awaiting payment: {pend}.")
    # operating result YTD (income - recurrent)
    inc = sum((r["receipts"] for r in rows if not r["is_trust"]), _D(0))
    rec = (Expense.objects.filter(status__in=[Expense.Status.APPROVED, Expense.Status.PAID],
           expenditure_type=Expense.ExpenditureType.RECURRENT)
           .exclude(doc_class=Expense.DocClass.LIABILITY)
           .aggregate(t=Sum("amount"))["t"] or 0)
    lines.append(f"Local income (all time): {inc}. Recurrent expenditure: {rec}. "
                 f"Operating result: {inc - rec}.")
    # fixed assets net book value
    try:
        from assets.models import FixedAsset
        nbv = sum((a.net_book_value(today) for a in FixedAsset.objects.filter(disposed=False)), _D(0))
        lines.append(f"Fixed assets net book value: {nbv}.")
    except Exception:
        pass
    # staff advances outstanding + petty cash float
    try:
        from cashbook.views import outstanding_advances_total, _petty_balance_asof
        adv_out = outstanding_advances_total(today)
        petty = _petty_balance_asof(today)
        lines.append(f"Staff advances outstanding (receivable, not yet accounted): {adv_out}. "
                     f"Petty cash float on hand: {petty}.")
    except Exception:
        pass
    try:
        from core.version import get_version, WHATS_NEW
        notes = "; ".join(f"v{v}: {n}" for v, n in list(WHATS_NEW.items())[:3])
        lines.append(f"Software version: v{get_version()}. Recent updates — {notes}")
        lines.append("Terminology note: member giving is called 'contributions' in this "
                     "system. All collection/income figures above are recognised income "
                     "(confirmed, no reversed or double-counted envelope-twin rows).")
    except Exception:
        pass
    # --- richer context for more intelligent answers ---------------------
    import calendar as _cal
    m_start = _dt.date(today.year, today.month, 1)
    m_end = _dt.date(today.year, today.month, _cal.monthrange(today.year, today.month)[1])
    pm_y, pm_m = (today.year, today.month - 1) if today.month > 1 else (today.year - 1, 12)
    pm_start = _dt.date(pm_y, pm_m, 1)
    pm_end = _dt.date(pm_y, pm_m, _cal.monthrange(pm_y, pm_m)[1])
    try:
        this_m = (Transaction.objects.filter(_credit_filter(m_start, m_end))
                  .aggregate(t=Sum("amount"))["t"] or 0)
        last_m = (Transaction.objects.filter(_credit_filter(pm_start, pm_end))
                  .aggregate(t=Sum("amount"))["t"] or 0)
        lines.append(f"Collections this month ({today:%B}): {this_m}. "
                     f"Last month ({pm_start:%B}): {last_m}.")
    except Exception:
        pass
    # income by channel (YTD)
    try:
        for c in balances.income_by_channel(y0, today):
            lines.append(f"Channel {c['channel']}: {c['total'] or 0} "
                         f"across {c['count']} receipt(s) YTD.")
    except Exception:
        pass
    # tithe YTD (a key conference figure)
    try:
        from core.metrics import metrics
        tithe = metrics.tithe(y0, today)
        lines.append(f"Tithe received YTD: {tithe}.")
    except Exception:
        pass
    # trust remittance compliance
    try:
        tr_rows = balances.trust_summary(None, None)
        col = sum((r["collected"] for r in tr_rows), _D(0))
        rem = sum((r["remitted"] for r in tr_rows), _D(0))
        unrec = sum((r["unreceipted"] for r in tr_rows), _D(0))
        pct = (rem / col * 100) if col else _D(100)
        lines.append(f"Trust remittance compliance: {rem} of {col} remitted "
                     f"({pct:.0f}%). Unreceipted trust (not yet a firm liability, "
                     f"excluded from 'to remit'): {unrec}.")
    except Exception:
        pass
    # latest bank reconciliation status
    try:
        from statements.models import BankReconciliation
        r = BankReconciliation.objects.order_by("-statement_date").first()
        if r:
            state = "reconciled" if r.is_reconciled else "NOT yet reconciled"
            lines.append(f"Latest bank reconciliation: statement dated "
                         f"{r.statement_date}, {state}"
                         + (f", difference {r.difference}." if r.difference is not None else "."))
    except Exception:
        pass
    # outstanding payments (issued but not cleared)
    try:
        from cashbook.views import unpresented_cheques_total
        unp = unpresented_cheques_total(today)
        if unp:
            lines.append(f"Unpresented cheques/payments (issued, not yet cleared): {unp}.")
    except Exception:
        pass
    # pledges & campaigns
    try:
        from pledges.models import Pledge
        pl = Pledge.objects.aggregate(p=Sum("amount"))["p"] or 0
        if pl:
            lines.append(f"Total pledged: {pl}.")
    except Exception:
        pass
    # membership
    try:
        from members.models import Member
        active = Member.objects.filter(active=True).count()
        lines.append(f"Active members on file: {active} "
                     "(individual names withheld for privacy).")
    except Exception:
        pass
    # top expense categories YTD
    try:
        cats = (Expense.objects.filter(
                    status__in=[Expense.Status.APPROVED, Expense.Status.PAID],
                    date__gte=y0, date__lte=today)
                .exclude(doc_class=Expense.DocClass.LIABILITY)
                .values("category").annotate(t=Sum("amount")).order_by("-t")[:5])
        if cats:
            label = dict(Expense.Category.choices)
            top = "; ".join(f"{label.get(c['category'], c['category'])} {c['t']}"
                            for c in cats)
            lines.append(f"Top expense categories YTD: {top}.")
    except Exception:
        pass
    return "\n".join(lines)


_OPENAI_BASES = {
    "OPENAI": "https://api.openai.com/v1",
    "GROQ": "https://api.groq.com/openai/v1",
    "OPENROUTER": "https://openrouter.ai/api/v1",
    "GEMINI": "https://generativelanguage.googleapis.com/v1beta/openai",
}
# Sensible default model per provider (the OpenAI name is wrong for Groq etc.)
_DEFAULT_MODEL = {
    "ANTHROPIC": "claude-3-5-sonnet-latest",
    "OPENAI": "gpt-4o-mini",
    "GROQ": "llama-3.3-70b-versatile",
    "OPENROUTER": "openai/gpt-4o-mini",
    "GEMINI": "gemini-1.5-flash",
}


def _parse_json(body, status):
    """Parse a provider response body. Returns (data, error). When the body isn't
    JSON (empty, or an HTML error page from a wrong URL), give a clear message
    instead of a raw JSONDecodeError."""
    import json as _json
    body = (body or "").strip()
    if not body:
        return None, f"The provider returned an empty response (HTTP {status}). " \
                     "Check the base URL and that the service is reachable."
    try:
        return _json.loads(body), None
    except ValueError:
        snippet = " ".join(body.split())[:160]
        return None, (f"The provider returned a non-JSON response (HTTP {status}). "
                      f"This usually means the base URL or endpoint is wrong. "
                      f"Response began: {snippet}")


def _normalise_openai_base(base, provider):
    base = (base or "").rstrip("/")
    if not base:
        return base
    # tolerate a pasted full endpoint
    if base.endswith("/chat/completions"):
        base = base[: -len("/chat/completions")]
    # a bare host for a known provider -> add the OpenAI-compatible prefix
    if provider == "GROQ" and "/openai" not in base:
        base = base + "/openai/v1"
    elif provider in ("OPENAI", "OPENROUTER") and not base.endswith("/v1"):
        if base.count("/") <= 2:           # just a host
            base = base + "/v1"
    return base


def _llm_call(question, cfg, context=None, system=None):
    """Call the configured provider. Returns (text, error). On success error is
    None; on failure text is None and error is a short human-readable reason."""
    from core.services.net import post_json
    key = cfg.llm_api_key
    if not key:
        return None, "No API key is set for the assistant."
    provider = cfg.llm_provider or "OPENAI"
    model = cfg.llm_model or _DEFAULT_MODEL.get(provider, "")
    if system is None:
        system = ("You are the treasurer's assistant for a church finance system. "
                  "Answer concisely using only the data summary provided. If the "
                  "answer isn't in the summary, say you don't have that figure.")
    if context is None:
        context = _data_context()
    user_msg = f"DATA SUMMARY:\n{context}\n\nQUESTION: {question}"
    try:
        if provider == "ANTHROPIC":
            url = (cfg.llm_base_url or "https://api.anthropic.com").rstrip("/")
            if url.endswith("/v1/messages"):
                pass
            elif url.endswith("/v1"):
                url = url + "/messages"
            else:
                url = url + "/v1/messages"
            status, body = post_json(url, {
                "model": model, "max_tokens": 400, "system": system,
                "messages": [{"role": "user", "content": user_msg}]},
                headers={"x-api-key": key, "anthropic-version": "2023-06-01"})
            data, perr = _parse_json(body, status)
            if perr:
                return None, perr
            if status >= 400:
                return None, _api_error(data, status)
            return "".join(b.get("text", "") for b in data.get("content", [])), None
        # OpenAI-compatible (OpenAI, Groq, OpenRouter, Gemini-openai, custom)
        base = _normalise_openai_base(cfg.llm_base_url or _OPENAI_BASES.get(provider, ""),
                                      provider)
        if not base:
            return None, f"No base URL is configured for provider {provider}."
        if not model:
            return None, "No model is set. Enter a model name in Settings (e.g. " \
                         "llama-3.3-70b-versatile for Groq)."
        status, body = post_json(base + "/chat/completions", {
            "model": model,
            "messages": [{"role": "system", "content": system},
                         {"role": "user", "content": user_msg}],
            "max_tokens": 400}, headers={"authorization": f"Bearer {key}"})
        data, perr = _parse_json(body, status)
        if perr:
            return None, perr
        if status >= 400:
            return None, _api_error(data, status)
        try:
            return data["choices"][0]["message"]["content"], None
        except (KeyError, IndexError, TypeError):
            return None, f"Unexpected response shape from the provider (HTTP {status})."
    except Exception as exc:  # noqa: BLE001
        return None, f"{type(exc).__name__}: {exc}"


def _api_error(data, status):
    msg = ""
    if isinstance(data, dict):
        err = data.get("error")
        if isinstance(err, dict):
            msg = err.get("message") or err.get("type") or ""
        elif isinstance(err, str):
            msg = err
    return f"HTTP {status}: {msg}" if msg else f"HTTP {status} from the provider."


def test_llm(cfg=None):
    """Make a tiny live call so the admin can confirm the assistant is wired up.
    Returns (ok, detail)."""
    from core.models import SiteConfig
    cfg = cfg or SiteConfig.get()
    if not cfg.llm_enabled:
        return False, "The assistant LLM is switched off in settings."
    text, err = _llm_call("Reply with the single word: OK.", cfg,
                          context="(connection test — no data)")
    if err:
        return False, err
    return True, (text or "").strip()[:200]


def _llm_answer(question, cfg):
    """Used as a fallback by answer(); returns text or None."""
    text, _err = _llm_call(question, cfg)
    return text


_ROUTE_INTENTS = {
    "tithe": "tithe {period}",
    "offering_summary": "offering summary {period}",
    "trust": "trust funds to remit {period}",
    "remittance": "remittance {period}",
    "dev_groups": "development groups progress {period}",
    "expenses": "expenses {fund} {period}",
    "budget": "budget {fund} {period}",
    "fund_balance": "balance of {fund}",
    "income_expenditure": "income vs expenditure {period}",
    "surplus": "surplus {period}",
    "assets": "fixed assets net book value",
    "financial_position": "statement of financial position",
    "top_givers": "top givers {period}",
    "cashbook": "cashbook {period}",
}


def _llm_route(question, cfg):
    """Use the LLM purely to classify a free-text question into one known report
    intent (plus an optional fund and period), then hand back to the rule engine
    with a canonical phrasing. Returns an answer dict, or None if it can't route."""
    import json as _json
    intents = ", ".join(_ROUTE_INTENTS)
    system = (
        "You classify a church treasurer's question into exactly one report intent "
        "for a finance system. Reply with ONLY a compact JSON object, no prose, of the "
        'form {"intent": "...", "fund": "...", "period": "..."}. '
        f"intent must be one of: {intents}. "
        "fund is the fund/ministry name if the question names one, else empty. "
        "period is a short phrase like 'last month', 'this year', 'last quarter', or empty. "
        'If the question is not about any of these reports, use {"intent": "none"}.')
    text, err = _llm_call(question, cfg, context="(intent routing — no data needed)",
                          system=system)
    if err or not text:
        return None
    raw = text.strip()
    if "```" in raw:
        raw = raw.split("```")[1].replace("json", "", 1).strip() if raw.count("```") >= 2 else raw
    try:
        obj = _json.loads(raw[raw.find("{"): raw.rfind("}") + 1])
    except (ValueError, TypeError):
        return None
    intent = (obj.get("intent") or "").strip()
    if intent not in _ROUTE_INTENTS:
        return None
    canonical = _ROUTE_INTENTS[intent].format(
        fund=(obj.get("fund") or "").strip(), period=(obj.get("period") or "").strip()).strip()
    routed = _answer_rules(canonical)
    if routed.get("_fallback"):
        return None
    routed.pop("_fallback", None)
    return routed


def answer(question, user=None):
    data = _answer_rules(question, user)
    # If the rule engine didn't understand, optionally defer to a configured LLM.
    if data.get("_fallback"):
        from core.models import SiteConfig
        cfg = SiteConfig.get()
        if cfg.llm_enabled:
            # first let the LLM pick the right report from natural phrasing…
            try:
                routed = _llm_route(question, cfg)
            except Exception:
                routed = None
            if routed:
                return routed
            # …otherwise fall back to a conversational answer from the data summary
            text = _llm_answer(question, cfg)
            if text:
                return {"text": text.strip()}
    data.pop("_fallback", None)
    return data


def _answer_rules(question, user=None):
    from giving.models import Transaction
    from cashbook.models import Expense
    from departments.models import Department, DevelopmentGroup
    from members.models import Member
    from reports.services import balances

    q = (question or "").strip()
    t = q.lower()
    if not t:
        return {"text": "Ask me something — try one of the suggestions below."}

    # help
    if t in ("help", "hi", "hello", "hey") or "what can you" in t or "help" == t:
        return {"text": "I can answer questions from the treasury data. For example:",
                "suggestions": SUGGESTIONS}

    # what's new / recent updates
    if ("what's new" in t or "whats new" in t or "what is new" in t
            or "recent update" in t or "new feature" in t or "what changed" in t
            or "changelog" in t or "latest version" in t):
        from core.version import WHATS_NEW, get_version
        items = list(WHATS_NEW.items())[:5]
        rows = [(f"v{ver}", note) for ver, note in items]
        return {"text": f"Recent updates (current version v{get_version()}):",
                "rows": rows or [("—", "No release notes recorded.")]}

    start, end, label = parse_period(q)

    # fund balance
    if ("balance" in t or "how much is in" in t or "how much does" in t) and _find_fund(t):
        return _fund_balance(_find_fund(t))

    # staff advances outstanding
    if ("advance" in t and ("outstanding" in t or "how much" in t or "total" in t
            or "owe" in t or "unaccounted" in t)) or t in ("staff advances", "advances"):
        from cashbook.views import outstanding_advances_total
        total = outstanding_advances_total()
        return {"text": f"Staff advances outstanding (issued but not yet accounted "
                f"for): {_money(total)}.",
                "link": reverse("advance_list"), "link_label": "Staff advances"}

    # petty cash float
    if "petty cash" in t or ("petty" in t and ("float" in t or "balance" in t)):
        import datetime as _dt
        from cashbook.views import _petty_balance_asof
        bal = _petty_balance_asof(_dt.date.today())
        return {"text": f"Petty cash float on hand: {_money(bal)}.",
                "link": reverse("petty_cash"), "link_label": "Petty cash register"}

    # tithe
    if "tithe" in t:
        from core.metrics import metrics
        total = metrics.tithe(start, end)
        return {"text": f"Tithe received in {label}: {_money(total)}.",
                "link": reverse("report_tithe"), "link_label": "Tithe report"}

    # trust / remittance
    if "trust" in t or "remit" in t:
        rows = balances.trust_summary(start, end)
        total = sum((r["to_remit"] for r in rows), Decimal(0))
        return {"text": f"Trust funds still to remit for {label}: {_money(total)}.",
                "rows": [(r["department"].name, _money(r["to_remit"])) for r in rows if r["to_remit"]],
                "link": reverse("report_remittance"), "link_label": "Remittance advice"}

    # ---- ledger / trial balance / cash / net assets ----
    if "trial balance" in t or "books balance" in t or ("ledger" in t and "balanc" in t) \
            or "are the books" in t or "do the books" in t:
        from ledger.services import posting
        if not posting.chart_ready():
            return {"text": "The general ledger hasn't been built yet. Open Trial balance and rebuild it.",
                    "link": reverse("trial_balance"), "link_label": "Trial balance"}
        _, tot = posting.trial_balance(None, end)
        ok = tot["debit"] == tot["credit"]
        return {"text": (f"The books balance ✓ — total debits and credits both {_money(tot['debit'])}."
                         if ok else
                         f"The books do NOT balance: debits {_money(tot['debit'])} vs credits {_money(tot['credit'])}. Try rebuilding the ledger."),
                "link": reverse("trial_balance"), "link_label": "Open trial balance"}

    if ("cash" in t and ("how much" in t or "balance" in t or "bank" in t or "have" in t or "do we" in t)) \
            or "cash on hand" in t or "cash and bank" in t or "bank balance" in t or "cash position" in t:
        from reports.services import balances
        cash = sum((r["closing"] for r in balances.department_summary(None, None)), Decimal(0))
        return {"text": f"Total cash & bank across all funds: {_money(cash)}.",
                "note": "This is the pooled balance of every fund.",
                "link": reverse("trial_balance"), "link_label": "Trial balance"}

    if "net asset" in t or "net worth" in t or "financial position" in t or "balance sheet" in t \
            or ("total" in t and ("asset" in t or "liabilit" in t)):
        from reports.services import balances
        rows = balances.department_summary(None, None)
        trust = sum((r["closing"] for r in rows if r["is_trust"]), Decimal(0))
        local = sum((r["closing"] for r in rows if not r["is_trust"]), Decimal(0))
        nbv = Decimal(0)
        try:
            from assets.models import FixedAsset
            import datetime as _d
            nbv = sum((a.net_book_value(_d.date.today()) for a in FixedAsset.objects.filter(disposed=False)), Decimal(0))
        except Exception:
            pass
        return {"text": f"Local fund balances total {_money(local)}, with {_money(trust)} held as trust liabilities.",
                "rows": [("Cash & bank", _money(local + trust)),
                         ("Fixed assets (NBV)", _money(nbv)),
                         ("Trust funds payable", _money(trust)),
                         ("Local fund balances", _money(local))],
                "link": reverse("report_financial_position"), "link_label": "Statement of financial position"}

    # ---- fixed assets / depreciation ----
    if "asset" in t or "net book" in t or "nbv" in t or "depreciat" in t or "equipment" in t:
        import datetime as _d
        from assets.models import FixedAsset
        live = FixedAsset.objects.filter(disposed=False)
        nbv = sum((a.net_book_value(_d.date.today()) for a in live), Decimal(0))
        cost = live.aggregate(t=Sum("cost"))["t"] or Decimal(0)
        return {"text": f"You have {live.count()} fixed asset(s) with a net book value of {_money(nbv)} "
                        f"(original cost {_money(cost)}).",
                "link": reverse("asset_list"), "link_label": "Asset register"}

    # ---- budget vs actual ----
    if "budget" in t:
        import datetime as _d
        from reports.services import budget as budget_svc
        year = start.year if start else _d.date.today().year
        fund = _find_fund(t)
        try:
            result = budget_svc.budget_vs_actual(year)
            rows = result.get("rows", [])
            totals = result.get("totals", {})
        except Exception:
            rows, totals = [], {}
        if fund:
            row = next((r for r in rows if r["department"].id == fund.id), None)
            if not row or not row.get("budget"):
                return {"text": f"No budget is set for {fund.name} in {year}.",
                        "link": reverse("budget"), "link_label": "Budgets"}
            var = row["budget"] - row["actual"]
            state = "under budget" if var >= 0 else "OVER budget"
            return {"text": f"{fund.name} {year}: budget {_money(row['budget'])}, "
                            f"actual {_money(row['actual'])} — {_money(abs(var))} {state}.",
                    "link": reverse("report_budget_vs_actual"), "link_label": "Budget vs actual"}
        tot_b = totals.get("budget") or sum((r["budget"] for r in rows), Decimal(0))
        tot_a = totals.get("actual") or sum((r["actual"] for r in rows), Decimal(0))
        var = tot_b - tot_a
        return {"text": f"{year} budget {_money(tot_b)} vs actual {_money(tot_a)} — "
                        f"{_money(abs(var))} {'remaining' if var >= 0 else 'over'}.",
                "rows": [(r["department"].name, f"{_money(r['actual'])} / {_money(r['budget'])}")
                         for r in rows if r.get("budget")][:8],
                "link": reverse("report_budget_vs_actual"), "link_label": "Budget vs actual"}

    # ---- surplus / deficit / income statement ----
    if "surplus" in t or "deficit" in t or "income statement" in t or "profit" in t \
            or "are we making" in t or "in the black" in t or "in the red" in t:
        from cashbook.models import Expense
        from reports.services import balances
        f = _credit_filter(start, end)
        rows = balances.department_summary(start, end)
        income = sum((r["receipts"] for r in rows if not r["is_trust"]), Decimal(0))
        ef = Q(status__in=[Expense.Status.APPROVED, Expense.Status.PAID],
               expenditure_type=Expense.ExpenditureType.RECURRENT)
        if start:
            ef &= Q(date__gte=start)
        if end:
            ef &= Q(date__lte=end)
        recurrent = (Expense.objects.filter(ef).exclude(doc_class=Expense.DocClass.LIABILITY)
                     .aggregate(t=Sum("amount"))["t"] or Decimal(0))
        op = income - recurrent
        word = "surplus" if op >= 0 else "deficit"
        return {"text": f"Operating {word} for {label}: {_money(abs(op))} "
                        f"(local income {_money(income)} less recurrent expenditure {_money(recurrent)}).",
                "link": reverse("report_income_statement"), "link_label": "Income & expenditure"}

    # ---- inter-fund transfers ----
    if "transfer" in t:
        from cashbook.models import FundTransfer
        qs = FundTransfer.objects.filter(is_reversal=False, is_reversed=False)
        if start:
            qs = qs.filter(date__gte=start)
        if end:
            qs = qs.filter(date__lte=end)
        recent = list(qs[:6])
        total = qs.aggregate(t=Sum("amount"))["t"] or Decimal(0)
        return {"text": f"{qs.count()} inter-fund transfer(s) in {label}, totalling {_money(total)}.",
                "rows": [(f"{x.source.name} → {x.destination.name}", _money(x.amount)) for x in recent],
                "link": reverse("transfer_list"), "link_label": "Fund transfers"}

    # ---- capital vs recurrent expenditure ----
    if "capital" in t and ("expen" in t or "spend" in t or "spent" in t or "spending" in t or "cost" in t):
        from cashbook.models import Expense
        f = Q(status__in=[Expense.Status.APPROVED, Expense.Status.PAID],
              expenditure_type=Expense.ExpenditureType.CAPITAL)
        if start:
            f &= Q(date__gte=start)
        if end:
            f &= Q(date__lte=end)
        agg = Expense.objects.filter(f).aggregate(t=Sum("amount"), n=Count("id"))
        return {"text": f"Capital expenditure in {label}: {_money(agg['t'] or 0)} "
                        f"across {agg['n'] or 0} item(s).",
                "link": reverse("expense_list") + "?type=CAPITAL", "link_label": "Capital expenses"}

    # ---- largest / biggest expense ----
    if ("biggest" in t or "largest" in t or "highest" in t) and ("expense" in t or "payment" in t or "spend" in t or "cost" in t):
        from cashbook.models import Expense
        f = Q(status__in=[Expense.Status.APPROVED, Expense.Status.PAID])
        if start:
            f &= Q(date__gte=start)
        if end:
            f &= Q(date__lte=end)
        rows = Expense.objects.filter(f).order_by("-amount")[:5]
        return {"text": f"Largest expenses in {label}:",
                "rows": [(f"{x.description} ({x.department.name})", _money(x.amount)) for x in rows]
                        or [("—", "none")],
                "link": reverse("report_expenses"), "link_label": "Expense report"}

    # ---- recent activity ----
    if "recent" in t or "latest" in t or "lately" in t or "what happened" in t or "last few" in t:
        from cashbook.models import Expense
        rcv = Transaction.objects.filter(_credit_filter(None, None)).order_by("-date", "-id")[:4]
        exp = Expense.objects.order_by("-date", "-id")[:4]
        rows = [(f"↓ {r.date:%d/%m} {r.payer_name or r.reference or 'Receipt'}", _money(r.amount)) for r in rcv]
        rows += [(f"↑ {x.date:%d/%m} {x.description}", _money(x.amount)) for x in exp]
        return {"text": "Most recent receipts (↓) and payments (↑):", "rows": rows}

    # ---- bank reconciliation ----
    if "reconcil" in t or "unmatched" in t or "matching" in t or "bank statement" in t:
        try:
            from statements.models import ReconciliationMatch
            pend = ReconciliationMatch.objects.filter(
                status__in=["AUTO", "REVIEW"]).count()
            return {"text": f"{pend} bank reconciliation match(es) await confirmation.",
                    "link": reverse("auto_reconcile"), "link_label": "Auto-reconciliation"}
        except Exception:
            return {"text": "Open the reconciliation screen to match bank lines.",
                    "link": reverse("reconciliation_list"), "link_label": "Reconciliations"}

    # expenses / outstanding
    if re.search(r"\boutstanding\b|\bunpaid\b", t) or ("pending" in t and "spend" not in t) or ("approve" in t and "expense" in t):
        qs = Expense.objects.filter(status__in=[Expense.Status.PENDING, Expense.Status.APPROVED])
        total = qs.aggregate(t=Sum("amount"))["t"] or Decimal(0)
        return {"text": f"There are {qs.count()} expense(s) awaiting payment, totalling {_money(total)}.",
                "link": reverse("expense_list") + "?status=PENDING", "link_label": "Open expenses"}
    if "expense" in t or "spent" in t or "spend" in t or "expenditure" in t:
        fund = _find_fund(t)
        f = Q(status__in=[Expense.Status.APPROVED, Expense.Status.PAID])
        if start:
            f &= Q(date__gte=start)
        if end:
            f &= Q(date__lte=end)
        if fund:
            f &= Q(department=fund)
        total = Expense.objects.filter(f).aggregate(t=Sum("amount"))["t"] or Decimal(0)
        where = f" on {fund.name}" if fund else ""
        return {"text": f"Expenses{where} in {label}: {_money(total)}.",
                "link": reverse("report_expenses"), "link_label": "Expense report"}

    # top givers
    if "top giver" in t or "top giving" in t or "biggest giver" in t or "who gave the most" in t:
        rows = (Transaction.objects.filter(_credit_filter(start, end), member__isnull=False)
                .values("member__name").annotate(t=Sum("amount")).order_by("-t")[:10])
        return {"text": f"Top givers in {label}:",
                "rows": [(r["member__name"], _money(r["t"])) for r in rows] or [("—", "no giving")]}

    # a person's giving: "how much did <name> give"
    m = re.search(r"(?:how much did|giving (?:for|by)|what did)\s+(.+?)\s+(?:give|given|contribute|donate)", t)
    if not m:
        m = re.search(r"(?:giving for|statement for|contributions? (?:of|for))\s+(.+)$", t)
    if m:
        name = m.group(1).strip().strip("?").title()
        member = (Member.objects.filter(name__icontains=name).first()
                  or Member.objects.filter(name__icontains=name.split()[0]).first())
        if not member:
            return {"text": f"I couldn't find a member matching “{name}”.",
                    "link": reverse("member_list"), "link_label": "Browse members"}
        rows = (Transaction.objects.filter(member=member, *[_credit_filter(start, end)])
                .values("department__name").annotate(t=Sum("amount")).order_by("-t"))
        total = sum((r["t"] for r in rows), Decimal(0))
        return {"text": f"{member.name} gave {_money(total)} in {label}.",
                "rows": [(r["department__name"] or "—", _money(r["t"])) for r in rows],
                "link": reverse("report_member", args=[member.id]), "link_label": "Member statement"}

    # members count
    if "member" in t and ("how many" in t or "count" in t or "number of" in t):
        total = Member.objects.count()
        active = Member.objects.filter(active=True).count()
        auto = Member.objects.filter(source=Member.Source.AUTO_BANK).count()
        return {"text": f"{total} members on file ({active} active). {auto} were created automatically from bank statements.",
                "link": reverse("member_list"), "link_label": "Members"}

    # review queue / debits
    if "queue" in t or "review" in t or "to allocate" in t or "unallocated" in t:
        n = Transaction.objects.filter(allocation_status=Transaction.Status.REVIEW,
                                       direction=Transaction.Direction.CREDIT).count()
        d = Transaction.objects.filter(allocation_status=Transaction.Status.REVIEW,
                                       direction=Transaction.Direction.DEBIT).count()
        return {"text": f"{n} giving item(s) await allocation and {d} bank debit(s) await classification.",
                "link": reverse("queue"), "link_label": "Review queue"}

    # development group
    m = re.search(r"(?:dev(?:elopment)?\s*(?:group|grp)?)\s*0*(\d+)", t)
    if m:
        num = int(m.group(1))
        grp = DevelopmentGroup.objects.filter(number=num).first()
        if not grp:
            return {"text": f"There's no development group {num}."}
        total = (Transaction.objects.filter(_credit_filter(None, None), dev_group=grp)
                 .aggregate(t=Sum("amount"))["t"] or Decimal(0))
        target = grp.target or Decimal(0)
        pct = (total / target * 100) if target else None
        msg = f"{grp.label} has collected {_money(total)}"
        if target:
            msg += f" of a {_money(target)} target ({pct:.0f}%)."
        return {"text": msg, "link": reverse("report_dev_groups"), "link_label": "Dev-group progress"}

    # envelopes
    if "envelope" in t:
        from envelopes.models import Envelope
        qs = Envelope.objects.all()
        if start:
            qs = qs.filter(date__gte=start)
        if end:
            qs = qs.filter(date__lte=end)
        total = qs.aggregate(t=Sum("total"))["t"] or Decimal(0)
        return {"text": f"{qs.count()} envelope(s) recorded in {label}, totalling {_money(total)}.",
                "link": reverse("envelope_list"), "link_label": "Envelopes"}

    # generic collections / totals
    if ("collect" in t or "received" in t or "total" in t or "giving" in t
            or "income" in t or "offering" in t):
        fund = _find_fund(t)
        f = _credit_filter(start, end)
        if fund:
            f &= Q(department=fund)
        agg = Transaction.objects.filter(f).aggregate(t=Sum("amount"), n=Count("id"))
        total = agg["t"] or Decimal(0)
        where = f" to {fund.name}" if fund else ""
        return {"text": f"Total received{where} in {label}: {_money(total)} across {agg['n'] or 0} entries.",
                "link": reverse("report_collections_summary"), "link_label": "Collections summary"}

    return {"text": "I didn't quite get that. Here are some things you can ask:",
            "suggestions": SUGGESTIONS, "_fallback": True}


SUGGESTIONS = [
    "Total collections this month",
    "Tithe last month",
    "Trust funds to remit",
    "Outstanding expenses",
    "Staff advances outstanding",
    "Petty cash balance",
    "Are the books balanced?",
    "Are we in surplus this year?",
    "How much cash do we have?",
    "What's our net book value of assets?",
    "Budget vs actual this year",
    "Recent activity",
    "Top givers this year",
    "What's in the review queue?",
    "What's new?",
]

# Grouped prompts shown on the empty chat screen
SUGGESTION_GROUPS = [
    {"label": "Money in", "icon": "↓", "items": [
        "Total collections this month", "Tithe last month",
        "Top givers this year", "Offerings this month"]},
    {"label": "Money out", "icon": "↑", "items": [
        "Outstanding expenses", "Staff advances outstanding",
        "Petty cash balance", "Biggest expenses this month"]},
    {"label": "Funds & balances", "icon": "▦", "items": [
        "How much cash do we have?", "Balance of Development",
        "Net assets", "Recent transfers"]},
    {"label": "Trust & compliance", "icon": "⚖", "items": [
        "Trust funds to remit", "Are the books balanced?",
        "Are we in surplus this year?", "Budget vs actual this year"]},
]


def board_report_narrative(context, period_label, cfg=None):
    """Narrative for the monthly board report (AI). Returns (text, error)."""
    from core.models import SiteConfig
    cfg = cfg or SiteConfig.get()
    if not cfg.llm_enabled:
        return None, "The AI assistant is switched off in settings."
    prompt = (
        f"You are a church treasurer preparing the {period_label} financial report "
        "for the church board. Using ONLY the data summary provided, write a clear, "
        "professional board narrative with these exact section headers on their own "
        "lines: 'Executive summary:', 'Key insights:', 'Trends:', and "
        "'Recommendations:'. Under 'Executive summary' write 2-3 sentences. Under the "
        "others use short bullets starting with '- '. Cite specific figures from the "
        "summary; never invent numbers. Be balanced and factual. Keep under 320 words.")
    return _llm_call(prompt, cfg, context=context)


def executive_insights(cfg=None):
    """Generate a short executive narrative (highlights, risks, recommendations)
    from the current financial summary, using the configured LLM. Returns
    (text, error)."""
    from core.models import SiteConfig
    cfg = cfg or SiteConfig.get()
    if not cfg.llm_enabled:
        return None, "The AI assistant is switched off in settings."
    prompt = ("You are a church finance advisor briefing the board. Based ONLY on "
              "the data summary, write a concise executive briefing with three short "
              "sections using these exact headers on their own lines: "
              "'Highlights:', 'Risks:', and 'Recommendations:'. Under each, give 2-3 "
              "short bullet points starting with '- '. Be specific with figures from "
              "the summary. Do not invent numbers. Keep it under 180 words.")
    return _llm_call(prompt, cfg)

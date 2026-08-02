"""Per-section explanations for engine reports: generated, AI-refined, editable.

Three layers, in the order they win:

1. **Generated** — a deterministic sentence or two built from the very figures
   the section shows, via the Financial Metrics Registry. Always available, no
   configuration, no network, byte-identical on a re-run.
2. **AI** — when the assistant is switched on, the treasurer can ask for a
   richer paragraph. The model is handed the section's own rows and totals and
   nothing else, so it explains the figures on the page rather than inventing
   any.
3. **Edited** — the treasurer's own words, stored per report / section /
   period. Whatever is stored is what the board reads and what prints.

The layering lives here rather than in the components so that any engine report
gains it without changing a single section, and so a section stays a pure
function of the context.
"""
from __future__ import annotations

from decimal import Decimal


def _n(v):
    return v if v is not None else Decimal(0)


def _money(v, places=0):
    """Figures inside prose match the tables: thousands separated, and the
    reader's own negative-number convention."""
    from core.numberstyle import negatives_style
    from core.templatetags.treasury_extras import _money as fmt
    return fmt(v, places, accounting=negatives_style() == "PARENS")


def _cur():
    from core.models import SiteConfig
    return SiteConfig.get().currency_symbol or "KES"


def _m(v, places=0):
    return f"{_cur()} {_money(v, places)}"


def _pct(part, whole):
    part, whole = _n(part), _n(whole)
    return f"{float(part) / float(whole) * 100:.1f}%" if whole else "—"


# ===========================================================================
# Generated explanations, one per section key
# ===========================================================================

def _explain_collections_summary(ctx, section):
    d = ctx.metric("collections_summary_monthly")
    t = d.get("totals") or {}
    if not t:
        return ""
    rows = d.get("rows") or []
    if not t.get("collections") and not t.get("expenditure"):
        # Nothing moved. Saying "collections exceeded spending by nil" three
        # ways is worse than saying so once.
        return ("Nothing was collected and nothing was spent in this period. "
                "On the as-reported basis this can also mean the entries had "
                "not yet been keyed in by the closing date."
                if ctx.as_reported_at else
                "Nothing was collected and nothing was spent in this period.")
    bits = [
        f"Collections for the period were {_m(t['collections'])}, made up of "
        f"{_m(t['local'])} of local funds and {_m(t['trust'])} of trust funds "
        f"({_pct(t['trust'], t['collections'])} of the total) held on behalf of "
        f"the conference.",
        f"Expenditure was {_m(t['expenditure'])}, so collections "
        f"{'exceeded' if t['net'] >= 0 else 'fell short of'} spending by "
        f"{_m(abs(t['net']))}.",
    ]
    if len(rows) > 1:
        best = max(rows, key=lambda r: r["collections"])
        worst = min(rows, key=lambda r: r["collections"])
        bits.append(f"The strongest month was {best['label']} "
                    f"({_m(best['collections'])}) and the weakest "
                    f"{worst['label']} ({_m(worst['collections'])}).")
        short = [r["label"] for r in rows if r["net"] < 0]
        if len(short) == len(rows):
            bits.append("Spending ran ahead of collections in every month of "
                        "the period.")
        elif short:
            named = ", ".join(short[:4])
            if len(short) > 4:
                named += f" and {len(short) - 4} other(s)"
            bits.append(f"Spending ran ahead of collections in {len(short)} of "
                        f"{len(rows)} months ({named}).")
    return " ".join(bits)


def _explain_trust_fund_summary(ctx, section):
    d = ctx.metric("trust_collections_monthly")
    rows = d.get("rows") or []
    if not rows:
        return ""
    grand = _n(d.get("grand"))
    top = rows[0]
    to_remit = _n(ctx.trust_to_remit())
    bits = [
        f"{len(rows)} trust fund(s) collected {_m(grand)} in the period, led by "
        f"{top['dept'].name} at {_m(top['total'])} "
        f"({_pct(top['total'], grand)} of trust collections).",
        "Trust money is the conference's, not the church's: it is a liability "
        "from the moment it is received until it is remitted.",
    ]
    if to_remit > 0:
        bits.append(f"{_m(to_remit)} remains to be remitted and should be "
                    f"cleared before it ages further.")
    else:
        bits.append("Nothing is outstanding for remittance at the period end.")
    return " ".join(bits)


def _explain_fund_balances(ctx, section):
    """Read from the statement's own rows rather than re-querying, so the
    commentary counts exactly the funds the board can see on the page — and
    still agrees when the report is run with sub-accounts itemised."""
    if section is None or section.total is None:
        return ""
    detail = [r for r in section.rows if not r.meta.get("level")]
    if not detail:
        return ""

    def name(r):
        return r.cells["fund"].strip()

    total = _n(section.total.cells.get("closing"))
    top = sorted(detail, key=lambda r: -_n(r.cells["closing"]))[:3]
    neg = [r for r in detail if _n(r.cells["closing"]) < 0]
    bits = [
        f"Funds stood at {_m(total)} at the period end across {len(detail)} "
        f"active fund(s).",
        "The largest holdings are " + ", ".join(
            f"{name(r)} ({_m(r.cells['closing'])})" for r in top) + ".",
    ]
    if neg:
        bits.append(
            f"{len(neg)} fund(s) closed overdrawn — "
            + ", ".join(f"{name(r)} ({_m(r.cells['closing'])})" for r in neg[:4])
            + ". An overdrawn fund is being carried by the others and needs a "
              "transfer or a spending pause.")
    return " ".join(bits)


def _explain_financial_position(ctx, section):
    rows = section.rows if section is not None else []
    # The summary and the full statement label their totals differently
    # ("Total assets" against "TOTAL ASSETS"), so the lookup is case-blind —
    # one commentary serves both rather than one of them silently getting none.
    get = {str(r.cells.get("label")).strip().lower(): _n(r.cells.get("value"))
           for r in rows}
    assets = get.get("total assets")
    liabilities = get.get("total liabilities")
    net = get.get("net assets")
    if net is None:
        return ""
    bits = [
        f"The church held {_m(assets)} of assets against {_m(liabilities)} of "
        f"liabilities at the period end, leaving net assets of {_m(net)}.",
    ]
    trust = (get.get("trust funds payable")
             or (_n(get.get("trust funds payable — receipted"))
                 + _n(get.get("trust funds payable — not yet receipted"))))
    if trust:
        bits.append(f"Of the liabilities, {_m(trust)} is trust money awaiting "
                    f"remittance to the conference.")
    loans = (get.get("outstanding loans")
             or (_n(get.get("loans payable — current"))
                 + _n(get.get("loans payable — long term"))))
    if loans:
        bits.append(f"Borrowings outstanding are {_m(loans)}.")
    property_ = get.get("net book value")
    if property_:
        bits.append(f"{_m(property_)} of that is the carrying value of "
                    f"property and equipment, which is not money the church "
                    f"can spend.")
    bits.append("Net assets is what would remain if every fund were settled "
                "today.")
    return " ".join(bits)


def _explain_cash_flow(ctx, section):
    rows = section.rows if section is not None else []
    get = {r.cells.get("line"): _n(r.cells.get("amount")) for r in rows}
    close = get.get("Cash & bank at end of period")
    if close is None:
        return ""
    op = get.get("Net cash from operating activities", Decimal(0))
    inv = get.get("Net cash used in investing activities", Decimal(0))
    fin = get.get("Net cash from financing activities", Decimal(0))
    change = get.get("Net increase/(decrease) in cash", Decimal(0))
    opening = get.get("Cash & bank at beginning of period", Decimal(0))
    bits = [
        f"Cash moved from {_m(opening)} to {_m(close)}, a "
        f"{'increase' if change >= 0 else 'decrease'} of {_m(abs(change))}.",
        f"Day-to-day activity {'generated' if op >= 0 else 'consumed'} "
        f"{_m(abs(op))}.",
    ]
    if inv:
        bits.append(f"{_m(abs(inv))} went into property and equipment.")
    if fin:
        bits.append(f"Financing {'brought in' if fin >= 0 else 'repaid'} "
                    f"{_m(abs(fin))}.")
    noncash = _n(ctx.metric("loan_retirement_income"))
    if noncash:
        bits.append(f"A further {_m(noncash)} of loans was converted to "
                    f"donations or written off. No money changed hands, so it "
                    f"is disclosed as a memo and is neither counted as a "
                    f"receipt nor netted against borrowings.")
    return " ".join(bits)


def _explain_trial_balance(ctx, section):
    _rows, totals = ctx.metric("trial_balance")
    debit, credit = _n(totals.get("debit")), _n(totals.get("credit"))
    if debit == credit:
        return (f"The ledger is in balance: debits of {_m(debit, 2)} equal "
                f"credits of {_m(credit, 2)}. Every figure in this pack is "
                f"drawn from these same accounts, so the statements above "
                f"agree with the books by construction.")
    return (f"The ledger does NOT balance — debits of {_m(debit, 2)} against "
            f"credits of {_m(credit, 2)}, a difference of "
            f"{_m(abs(debit - credit), 2)}. This must be resolved before the "
            f"board adopts these accounts.")


def _explain_bank_reconciliation(ctx, section):
    pos = ctx.metric("bank_position", ctx.end)
    stmt = pos.get("statement_balance")
    if stmt is None:
        return ("The bank statement covering this period end has not been "
                "imported, so the cash book is unreconciled. Reconciliation is "
                "the single strongest control over church cash and should be "
                "completed before the accounts are adopted.")
    diff = _n(pos.get("difference"))
    when = pos.get("statement_date")
    bits = [f"The bank reported {_m(stmt, 2)}"
            + (f" at {when:%d %b %Y}" if when else "")
            + f" against a cash book of {_m(pos.get('system_balance'), 2)}."]
    if diff:
        bits.append(f"A difference of {_m(abs(diff), 2)} remains unexplained "
                    f"after allowing for unpresented payments and deposits in "
                    f"transit, and should be investigated.")
    else:
        bits.append("The two agree once unpresented payments and deposits in "
                    "transit are allowed for.")
    return " ".join(bits)


#: section key -> builder. A section with no builder falls back to its own
#: ``note``, so adding a section never leaves a blank explanation box.
BUILDERS = {
    "collections_summary": _explain_collections_summary,
    "trust_fund_summary": _explain_trust_fund_summary,
    "fund_balances_statement": _explain_fund_balances,
    "financial_position_summary": _explain_financial_position,
    "financial_position_statement": _explain_financial_position,
    "cash_flow_statement": _explain_cash_flow,
    "trial_balance": _explain_trial_balance,
    "bank_reconciliation": _explain_bank_reconciliation,
}


def generate(section, ctx):
    """The deterministic explanation for one rendered section."""
    builder = BUILDERS.get(section.key)
    if builder is None:
        return ""
    try:
        return builder(ctx, section) or ""
    except Exception:  # noqa: BLE001 — commentary must never break a report
        return ""


# ===========================================================================
# Stored overrides
# ===========================================================================

def _lookup(report_key, start, end):
    from reports.models import ReportNarrative
    return {n.section_key: n for n in ReportNarrative.objects.filter(
        report_key=report_key, period_start=start, period_end=end)}


def annotate(rendered, report_key):
    """Attach an explanation to every section of a rendered report.

    Sets ``section.extra['explanation']`` (what to show),
    ``['explanation_source']`` and ``['explanation_edited']``. Sections that are
    themselves commentary keep their narrative text and are simply marked
    editable, so a treasurer edits the executive summary the same way they edit
    a table's explanation.
    """
    ctx = rendered.context
    stored = _lookup(report_key, ctx.start, ctx.end)
    for s in rendered.sections:
        override = stored.get(s.key)
        if s.kind == "commentary":
            auto = s.extra.get("text", "")
        else:
            # No fallback to ``note``: a note is the method caption printed
            # under the table, so borrowing it here would print the same
            # sentence twice with a heading between the copies.
            auto = generate(s, ctx)
        if override is not None and override.text.strip():
            text, source, edited = override.text, override.source, True
        else:
            text, source, edited = auto, "AUTO", False
        s.extra["explanation"] = text
        s.extra["explanation_auto"] = auto
        s.extra["explanation_source"] = source
        s.extra["explanation_edited"] = edited
        if s.kind == "commentary":
            s.extra["text"] = text
    return rendered


def save(report_key, section_key, start, end, text, user, source="MANUAL"):
    """Store (or clear) a treasurer's wording for one section and period."""
    from reports.models import ReportNarrative
    text = (text or "").strip()
    if not text:
        ReportNarrative.objects.filter(
            report_key=report_key, section_key=section_key,
            period_start=start, period_end=end).delete()
        return None
    obj, _ = ReportNarrative.objects.update_or_create(
        report_key=report_key, section_key=section_key,
        period_start=start, period_end=end,
        defaults={"text": text, "source": source,
                  "updated_by": user if user and user.is_authenticated else None})
    return obj


# ===========================================================================
# AI refinement
# ===========================================================================

def _section_facts(section, ctx):
    """The section's own figures, as plain text for the model. Only what is
    printed on the page goes in — no member data, no wider database."""
    lines = [f"SECTION: {section.title}"]
    if ctx.start and ctx.end:
        lines.append(f"PERIOD: {ctx.start:%d %b %Y} to {ctx.end:%d %b %Y}")
    if section.kind == "commentary":
        lines.append(section.extra.get("text", ""))
        return "\n".join(lines)
    cols = [c.label for c in section.columns]
    lines.append(" | ".join(cols))
    for r in list(section.rows)[:60]:
        lines.append(" | ".join(
            str(r.cells.get(c.key, "")) for c in section.columns))
    if section.total is not None:
        lines.append("TOTAL: " + " | ".join(
            str(section.total.cells.get(c.key, "")) for c in section.columns))
    if section.note:
        lines.append(f"NOTE: {section.note}")
    return "\n".join(lines)


AI_SYSTEM = (
    "You are a church treasurer writing the commentary that sits under a table "
    "in a board report. Explain what the figures mean for the church in two to "
    "four short sentences of plain English. Use only the figures given — never "
    "invent, estimate or extrapolate one. Lead with the single most important "
    "point, name any figure that needs the board's attention, and say plainly "
    "what it implies. No headings, no bullet points, no preamble, no flattery."
)


def ai_explain(section, ctx):
    """Ask the configured assistant to write this section's commentary.
    Returns (text, error); error is a short human-readable reason on failure."""
    from core.models import SiteConfig
    from core.services.assistant import _llm_call
    cfg = SiteConfig.get()
    if not cfg.llm_enabled:
        return None, ("The AI assistant is switched off. Turn it on under "
                      "Settings → Assistant to write commentary automatically; "
                      "the generated explanation below is always available.")
    facts = _section_facts(section, ctx)
    text, err = _llm_call(
        "Write the board commentary for this section.", cfg,
        context=facts, system=AI_SYSTEM)
    if err:
        return None, err
    return (text or "").strip(), None

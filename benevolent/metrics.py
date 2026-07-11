"""Benevolent metrics — registered into the Financial Metrics Registry.

Every figure this module publishes is registered here so it is discoverable in
the /metrics/ catalogue with its accounting definition, exactly like every other
financial concept in the system. Nothing else in the application should compute a
benevolent figure by hand; it should ask the registry.

Note what these implementations do and do not do. `benevolent_scheme_summary`
does not add up receipts and expenses — it asks the registry for `fund_summary`
and reads the rows for the scheme funds. `benevolent_payouts` asks for
`expenses_by_department`. The registry is not being duplicated here; it is being
projected onto the scheme dimension, which is the one thing the fund tables
cannot know about themselves.

Registration happens from BenevolentConfig.ready() (not at import time) so it
cannot create an import cycle with core.metrics.
"""
from core.metrics import Metric, metrics


def _r():
    from benevolent.services import reporting
    return reporting


def register_metrics():
    """Idempotent: safe to call more than once (the registry rejects duplicate
    keys, so we check first — a reloaded app registry must not crash startup)."""

    if not metrics.has("benevolent_scheme_summary"):
        metrics.register(Metric(
            "benevolent_scheme_summary", "Benevolent scheme summary", "Benevolent",
            "Per scheme: opening balance, contributions received, benefits paid and "
            "closing balance for the period, plus active members and open cases. The "
            "financial columns are the scheme FUND's rows taken from fund_summary — "
            "the same figures the Board Pack's fund statement shows.",
            "benevolent.services.reporting.scheme_summary", inputs="start, end",
            notes="Delegates entirely to metrics.fund_summary; adds only the "
                  "non-financial scheme context (members, open cases, commitments)."),
            lambda start=None, end=None: _r().scheme_summary(start, end))

    if not metrics.has("benevolent_contributions"):
        metrics.register(Metric(
            "benevolent_contributions", "Benevolent contributions (period)", "Benevolent",
            "Income credits received into the schemes' funds over the period. Uses the "
            "canonical income-credit definition (confirmed, non-reversed, not excluded "
            "from income), so it agrees exactly with what the income statement reports "
            "for those funds.",
            "benevolent.services.reporting.contributions_total", inputs="start, end, scheme"),
            lambda start=None, end=None, scheme=None:
                _r().contributions_total(start, end, scheme))

    if not metrics.has("benevolent_payouts"):
        metrics.register(Metric(
            "benevolent_payouts", "Benevolent benefits paid (period)", "Benevolent",
            "Approved/paid expenditure charged to the schemes' funds over the period, "
            "net of refunds. Taken straight from expenses_by_department, so it is the "
            "same figure the fund statement and the ledger show.",
            "benevolent.services.reporting.payouts_total", inputs="start, end, scheme"),
            lambda start=None, end=None, scheme=None: _r().payouts_total(start, end, scheme))

    if not metrics.has("benevolent_fund_balance"):
        metrics.register(Metric(
            "benevolent_fund_balance", "Benevolent scheme balance", "Benevolent",
            "A scheme's available cash: the closing balance of the fund that holds its "
            "money, from fund_balance. A scheme has no separately-maintained balance — "
            "this IS the fund's balance.",
            "benevolent.services.reporting.scheme_balance", inputs="scheme"),
            lambda scheme: _r().scheme_balance(scheme))

    if not metrics.has("benevolent_commitments"):
        metrics.register(Metric(
            "benevolent_commitments", "Benevolent benefits approved but unpaid", "Benevolent",
            "Benefits authorised by a case decision for which no effective payment "
            "voucher has yet been posted.",
            "benevolent.services.reporting.approved_unpaid_total", inputs="scheme",
            notes="A MEMORANDUM figure, deliberately not a balance-sheet liability. "
                  "Expenditure is recognised when the voucher is approved, at which "
                  "point it is already in the ledger and already reducing the fund; "
                  "this is what has been promised but not yet vouchered, so it neither "
                  "appears in nor contradicts the Statement of Financial Position."),
            lambda scheme=None: _r().approved_unpaid_total(scheme))

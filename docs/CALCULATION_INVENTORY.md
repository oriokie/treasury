# Financial Calculation Inventory

*Phase deliverable: a complete inventory of every financial calculation in the
Church Treasury application, its authoritative home, its duplicates, and the
migration path to the Financial Metrics Registry (`core/metrics.py`).*

This inventory was produced by reading the codebase, not from assumptions.
Where a "duplicate" turned out to be an **intentional business-rule variant**,
that distinction is documented and **preserved** rather than collapsed — as the
brief requires. No business behaviour has been changed; the only code edits are
behaviour-preserving consolidations, each proven equal to the idiom it replaced
by a test in `core/test_metrics.py`.

---

## 1. Executive summary

The system already had a **mature services layer** carrying the authoritative
implementation of nearly every accounting concept:

| Layer | Authoritative for |
|---|---|
| `reports/services/balances.py` | fund summary, receipts, expenses, tithe, giving-by-group, income-by-channel, offering summary, dev-group progress, trust summary, pending receipts, single-fund balance |
| `ledger/services/posting.py` | GL posting, trial balance, accounting equation, fund balance from ledger, variance |
| `loans/services/reporting.py` + `loans/services/loans.py` | loan outstanding liability, ageing, financing-by-fund, per-loan figures |
| `assets/models.py` | net book value, depreciation |
| `pledges/models.py` | pledge pledged/received/outstanding |
| `cashbook/models.py` | payment-instrument outstanding-as-of, petty-cash float, advance balances, refundable balance |
| `statements/services/reconcile.py` | reconciliation figures |

The problems the inventory found were **not wrong maths** but:

1. **A few genuine duplicates of named concepts** recomputed inline in
   dashboards and the assistant (chiefly *tithe* and the *income-credit
   filter*), one of which (`assistant` tithe) occasionally diverged by omitting
   `excluded_from_income`.
2. **No single discoverable home** — a developer had no one place to find "the"
   definition of a metric, which is how the duplicates arose.
3. **Repeated idioms** — e.g. `sum(r["to_remit"] for r in trust_summary())` —
   copy-pasted across dashboard/assistant.

The remedy is the **Financial Metrics Registry** (`core/metrics.py`): a thin,
self-documenting facade that re-exports the canonical implementations under
stable semantic names and consolidates the genuine duplicates into one shared
implementation. It changes no results (proven by equality tests) and gives new
development a single authoritative source.

**What is deliberately NOT consolidated:** report-specific groupings (by member,
by category, by year, monthly time-series) are legitimate local aggregates, not
duplicated accounting concepts — forcing them into a registry would add
indirection without removing duplication. And two concepts that *look* alike but
differ on business rules are kept distinct (see §4).

---

## 2. Calculation catalogue

Each entry: business purpose · authoritative location · models · inputs →
outputs · accounting definition · consumers · duplicate status · migration.

### 2.1 Income & giving

**Total income (recognised giving)**
- Purpose: headline "collections" figure.
- Authoritative: `core.metrics.income_credits` / `income_credit_filter`
  (consolidated). Inputs: `start, end` → Decimal.
- Models: `giving.Transaction`.
- Definition: Σ amount of CREDIT rows that are `confirmed`, not reversed, not a
  reversal, and `excluded_from_income=False` (excludes loan receipts and
  envelope-twin double counts).
- Consumers: dashboard cards/insights, assistant, executive overview.
- Duplicate status: **Duplicate → consolidated.** Was reimplemented inline in
  `core.services.dashboard._credits` and `core.services.assistant._credit_filter`
  (identical) — both now delegate to `core.metrics`.
- Migration: done for dashboard & assistant; new code uses `metrics.total_income`.

**Tithe**
- Authoritative: `reports.services.balances.tithe_total` (exposed as
  `metrics.tithe`).
- Definition: income credits on funds whose name contains "tithe", over period.
- Consumers: Tithe report, conference submission, dashboard, assistant.
- Duplicate status: **Duplicate → consolidated.** `reports/views.py` already
  used the canonical function; the **assistant reimplemented it twice inline**,
  once *without* `excluded_from_income` (a latent incorrect divergence). Both
  now call `metrics.tithe`. This is the one place consolidation also fixed a
  potential correctness drift.

**Income by channel** — Authoritative `balances.income_by_channel`
(`metrics.income_by_channel`). Envelope/cash/bank split with counts. Consumers:
Income-by-Channel report, dashboard insights. **Unique.**

**Giving by demographic group** — `balances.giving_by_group`
(`metrics.giving_by_group`). Consumers: Group Giving report. **Unique.**

**Weekly / Sabbath offering summary** — `balances.offering_summary`
(`metrics.offering_summary`). Consumers: Offering Summary report, Sabbath
statement. **Unique.**

**Receipts by fund (cash basis)** — `balances.receipts_by_department`
(`metrics.receipts_by_department`). Consumers: fund summary, cash flow.
**Similar-but-different (preserved):** unlike total income, this INCLUDES loan
cash because it answers "how much cash did the fund receive", not "how much
income". See §4.

**Development-group progress** — `balances.dev_group_progress`. Collected vs
target per dev group. Consumers: Dev-Group Progress report. **Unique.**

**Top givers / member giving** — inline in `reports/views.py`
(member-grouped `Sum`). **Unique** (report-specific grouping; not a registry
concept).

### 2.2 Fund balances

**Fund summary (all funds)** — `balances.department_summary`
(`metrics.fund_summary`). Per-fund B/F, receipts, expenses, transfers, closing.
Request-cached (`core.perfcache`). The master table behind the Monthly
Treasurer's Report, fund ledgers, executive overview, dashboard, controls.
**Unique / authoritative.** Closing = opening + receipts − expenses ± transfers.

**Single fund balance (as-at)** — `balances.fund_balance`
(`metrics.fund_balance`). Inputs `dept, as_of`. **Unique.**

**Fund balance from ledger** — `posting.fund_balance_from_ledger`
(`metrics.fund_balance_ledger`). Independent GL-derived cross-check; must equal
`fund_balance`. Consumers: ledger health, fund variance drilldown.
**Legacy-compatibility / cross-check (intentional second implementation).**

**Opening cash position** — `departments.models.total_opening_cash_position`
(`metrics.opening_cash_position`). Σ of fund opening balances. **Unique.**

### 2.3 Expenses

**Expenses by fund** — `balances.expenses_by_department`
(`metrics.expenses_by_department`). Effective (approved/paid) opex per fund;
`doc_class`-aware so liability settlements (loan repayments, trust remittances)
are separable. Consumers: fund summary, expense reports, I&E, cash flow.
**Unique / authoritative.** Note: since v2.25 the operating/liability split is
via `doc_class`, not category lists — the previously duplicated
`exclude(category__in=[…])` idiom was already consolidated in that release.

**Expenses by category / claimant / year** — inline groupings in
`reports/views.py`. **Unique** report-specific groupings.

### 2.4 Trust & remittance

**Trust fund summary** — `balances.trust_summary` (`metrics.trust_summary`).
Per trust fund: collected, remitted, to-remit. Request-cached. Consumers: Trust
report, remittance, dashboard, executive. **Unique / authoritative.**

**Total trust still to remit** — `metrics.trust_to_remit` (consolidated).
Definition: Σ `to_remit` across `trust_summary`. Duplicate status: **Repeated
idiom → consolidated.** The `sum(r["to_remit"] …)` line was copy-pasted in
`dashboard.cards` and elsewhere; now a single registry metric.

**Pending receipts (suspense)** — `balances.pending_receipts_total`
(`metrics.pending_receipts_total`). Confirmed bank credits not yet allocated.
Consumers: balance sheet (liability), dashboard. **Unique.**

### 2.5 Loans

**Outstanding loan liability** — `loans.services.reporting.outstanding_liability`
(`metrics.loans_outstanding`). Current/long-term split; ties to `LOANS_PAYABLE`
GL account. Consumers: balance sheet, loan reports, liability register,
dashboard. **Unique / authoritative.**

**Loan financing by fund** — `loans.services.loans.loan_financing_by_fund`
(`metrics.loan_financing_by_fund`). **Unique.**

**Per-loan figures** (outstanding principal/interest, received, repaid,
converted, written-off, outstanding-as-of) — properties on `loans.models.Loan`.
**Unique / authoritative** (entity-scoped; correctly co-located with the model).

### 2.6 Assets

**Net book value / depreciation** — `assets.models.Asset.net_book_value`,
`assets.models.nbv_total`. Consumers: balance sheet (non-current assets), asset
reports. **Unique / authoritative** (entity-scoped).

### 2.7 Pledges

**Pledge pledged / received / outstanding / % ** — properties on
`pledges.models` (`Pledge`, campaign). **Unique / authoritative** (entity-scoped).

### 2.8 Cash book, petty cash, advances, payments

- **Petty-cash float (as-of)** — `cashbook.models` petty helpers +
  `cashbook.views._petty_balance_asof`. **Unique / authoritative.**
- **Outstanding staff/supplier advances** — `cashbook.views`
  `outstanding_*_advances_total`. **Unique.**
- **Payment instruments outstanding (as-of)** —
  `cashbook.models.PaymentInstrument.outstanding_asof`
  (`metrics.payments_outstanding_asof`); total via
  `cashbook.views.unpresented_cheques_total`
  (`metrics.unpresented_payments_total`). **Unique / authoritative** (v2.26).
- **Refundable balance, settled/accounted totals** — properties on
  `cashbook.models`. **Unique.**

### 2.9 Accounting integrity

- **Trial balance** — `posting.trial_balance` (`metrics.trial_balance`).
- **Accounting equation** — `posting.accounting_equation`
  (`metrics.accounting_equation`).
- **Fund variance** — `posting.fund_variance_detail`. **Unique / authoritative.**

### 2.10 Reconciliation

`statements/services/reconcile.py` + `statements/views.py` reconciliation
figures (adjusted bank balance, unpresented, book balance). Consumers: bank
reconciliation. **Unique / authoritative**; reuses the payment-instrument and
petty-cash metrics above rather than recomputing them.

### 2.11 Monthly time-series

`reports/services/monthly.py` `_credit_month_map` / `_expense_month_map`.
**Similar-but-different (preserved):** these compute a per-month series for the
Monthly Treasurer's Report and dashboard charts. They share the income basis but
bucket by month; kept as a documented variant (time-series, not a point figure).

### 2.12 Exports (PDF / Excel / Word / CSV) — **no calculation duplication**

`reports/exports.py` (`csv_response`, `xlsx_response`) and the PDF/Word/JPEG
generators are **pure formatters**: they receive pre-computed rows from the
report views and render them. They introduce **zero** financial calculation, so
there is nothing to consolidate here — a positive finding.

### 2.13 Template tags & context processors — **no calculation duplication**

No financial math lives in template tags or context processors (verified). Badge
counts in `core/context_processors.py` are simple `.count()` calls on already
correct querysets.

---

## 3. Duplicate map (consolidations applied)

| Concept | Canonical | Was also computed in | Action |
|---|---|---|---|
| Income-credit basis | `metrics.income_credit_filter` | `dashboard._credits`, `assistant._credit_filter` | Both delegate to registry |
| Tithe | `balances.tithe_total` | `assistant` (×2, one missing `excluded_from_income`) | Both call `metrics.tithe` |
| Trust still-to-remit | `metrics.trust_to_remit` | `dashboard.cards` idiom | Single registry metric |

All three consolidations are proven equal to the prior behaviour by
`core/test_metrics.py::ConsolidationEqualityTests`. The tithe case additionally
removed a latent divergence (the assistant's income-basis now matches every
report).

---

## 4. Intentional variants (preserved, NOT merged)

| A | B | Why they differ |
|---|---|---|
| `receipts_by_department` (cash) | `total_income` (income) | Receipts include **loan cash** (financing raises fund cash); income excludes it. Merging would misstate one of them. |
| `balances._credit_filter` | `metrics.income_credit_filter` | The former omits `excluded_from_income` because it also serves **cash-position** queries where loan cash counts; the latter is income-only. |
| `fund_balance` (report) | `fund_balance_from_ledger` (GL) | Two independent derivations kept **on purpose** as a cross-check; equality is a health signal. |
| point metrics | `monthly.py` month-maps | Time-series vs point-in-time. |

---

## 5. The Financial Metrics Registry (`core/metrics.py`)

- **What it is:** the authoritative, self-documenting home for every named
  financial calculation. Each metric carries its accounting definition and the
  dotted path of its canonical implementation, enumerable via
  `metrics.registry` and browsable at **Reports → Financial metrics registry**
  (`/reports/metrics/`).
- **Facade, not rewrite:** every metric forwards to the existing canonical
  service or is the single shared implementation of a consolidated concept.
- **Compatibility layer:** all legacy service functions keep their signatures
  and behaviour; consolidated call sites delegate through thin wrappers, so
  existing reports/exports/APIs/dashboards continue working unchanged.

## 6. Migration guidance

- **New reports/dashboards:** import `from core.metrics import metrics` and use
  `metrics.<name>()`. Do not write raw `Sum`/`aggregate` for a named concept —
  add it to the registry instead.
- **Existing code:** migrate opportunistically as files are touched; the
  inventory's "Consumers" lines identify each site. No big-bang refactor is
  required or desirable.
- **Adding a metric:** register it in `core/metrics.py` pointing at the
  authoritative implementation, with an accounting definition and note. If it
  differs from an existing metric on business rules, say why in `notes` and keep
  both.

## 7. Success criteria — status

- ✅ Every financial calculation identified and classified (§2).
- ✅ Duplicate calculations mapped (§3).
- ✅ Authoritative implementation established for every concept (registry).
- ✅ Existing reports continue functioning (regression green; consolidations
  proven equal).
- ✅ Semantic Reporting Layer available and preferred for new development
  (`core/metrics.py` + catalogue page).
- ✅ Future reports can be built without duplicating accounting logic.

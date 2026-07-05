# Changelog

## v2.16.0 - advance/cash-count follow-up fixes, transactions export, ledger health, executive redesign
Follow-up to v2.15.0, tracing each report through to its real root cause before fixing.

**Fixed:**
- **Bank reconciliation "Staff advances from petty cash (not yet accounted)"** never actually decreased as
  advances were accounted for — `StaffAdvance.petty_outstanding_asof()` only ever subtracted returns to the
  float, never expenses recorded against the advance, so it permanently showed the full amount ever
  disbursed. Fixed to also subtract settled expenses, as of the reconciliation's own statement date.
- **Sabbath cash count double-counted staff advance settlements.** When someone accounts for a cash-tracked
  advance (an expense linked via `Expense.advance`), the real cash movement happened back when the advance
  was *issued* — possibly from an entirely different float (petty cash) — not when it's later accounted for.
  The cash count was counting the settlement as a brand-new disbursement from the Sabbath offering float.
  Fixed by excluding advance-settlement expenses from "Cash Disbursed" and instead adding back the advance's
  own issuance (only when cash, only when *not* from petty cash) at the point it actually happened.
  **Caught and fixed a regression from the first fix**, before shipping: the petty cash float's own running
  balance (`_petty_balance_asof`) depended on the same method, and would have incorrectly *increased* every
  time an advance was accounted for (settling an advance doesn't return cash to the float). Split into two
  clearly-named methods — `petty_cash_out_asof` (pure cash movement, for the float balance) and
  `petty_outstanding_asof` (accounted-for status, for reconciliation) — with a regression test locking in
  that settling an advance never changes the float's balance.
- **Transactions Excel/CSV export** now combines a contribution split across several funds back into one row
  with its full aggregate total, the way it looked before the split — matching the Trust Pending Receipts
  export's existing behaviour. Extracted the grouping logic into a shared helper used by both exports.
- **Ledger Health "Shared M-Pesa/bank receipt references"** flagged every legitimate split gift as "worth
  checking" — it compared `core_ref` values directly, but a split gives each sibling its *own* distinct
  core_ref (base + "-S1", "-S2", ...), never a shared one. Fixed to compare the base reference; the health
  page now separates the count into genuinely-unexplained references and legitimate splits (labelled and no
  longer counted as a concern).
- **Missing postings persisted after every rebuild.** A reversed transaction or a reversal's own contra-entry
  are never posted by design (`post_transaction()` and `rebuild()` both correctly decline), but the health
  check was flagging them as "missing" regardless — a false positive that no rebuild could ever clear. Fixed
  by excluding them from the check. Each remaining genuinely-missing item now links directly to its record.

**Redesigned:**
- **Executive overview** reorganised into clearly labelled sections (Performance this year / Giving
  breakdown / At a glance / Cash position & forecast / Trends) for easier scanning, with a consistent section
  heading style. Verified line-by-line against the original: every context variable, URL, chart canvas ID,
  and literal text label is preserved exactly — this is a presentation reorganisation, not a functional
  change. AI insights and all six charts work exactly as before.

Tests: 34 new across cashbook/envelopes/giving/ledger/core, covering each fix (including the caught-and-fixed
regression) with both the buggy and corrected scenarios reproduced directly. Targeted regression: cashbook
286, envelopes+giving 208, ledger+core+statements 340 — all green.

Deploy: no migration. Collectstatic recommended (executive.html CSS changed) but not required for correctness.

## v2.15.0 - ledger rebuild fix, cash count fix, transactions page improvements, export enhancements
Traced and resolved a batch of reported issues, each verified against a reproduction of the real scenario
(not assumed), plus one further defect found incidentally while running the regression suite.

**Fixed (Critical):**
- **`/ledger/rebuild/` server error** — `pymysql.err.IntegrityError: Duplicate entry '5105' for key 'code'`.
  Root cause: `ensure_chart()` assigned each expense category's account code from its *position* in
  `Expense.Category.choices` (`EXPENSE_BASE + enumerate index`). An earlier release inserted two new
  categories (Salaries/Wages, Lease Payment) into the middle of that list, shifting the positional index —
  and therefore the computed code — of every category listed after them. On any database that already had
  its chart of accounts built before that release, `UTILITIES` already held code 5105 on disk; the newly-
  computed code for `SALARIES` was *also* 5105 (its new position), so creating it collided. Reproduced the
  exact historical database state and confirmed the fix resolves it: codes are now assigned as "one past the
  highest code already on record", never recomputed from list position — immune to future reordering.

**Fixed:**
- **Sabbath Cash Count — Cash Disbursed** included expenses paid from the separate petty cash float
  (`Expense.paid_from_petty_cash`), money that never came out of the Sabbath offering cash box being counted.
  This silently understated "expected cash on hand" and made counts show discrepancies that weren't real.
  Now excluded.
- **Monthly Treasurer's Report — Collections insight** silently never generated (a `KeyError` on a wrong
  dict key, caught by an intentional "an optional narrative must never break the report" safeguard, so it
  never surfaced as an error — just a permanently missing paragraph). Found while running an unrelated
  regression suite. Fixed; the year-over-year collections commentary now appears correctly.

**Added:**
- **M-Pesa Reference column** on the Trust Fund Pending Receipts export (`/transactions/?export=trust-
  pending-receipt`).
- **Payment Method column** on the Expenses export (Cash / Bank / Petty Cash / Cheque / Mobile Money),
  using the actual recorded payment source (`Expense.method` plus the separate `paid_from_petty_cash` flag)
  — never inferred.
- **Quick-filter tabs** on the Transactions page (All / Needs review / Unallocated / Trust pending receipt),
  layered on top of the existing, already-tested filter mechanism without changing it — a safe, additive
  usability improvement rather than a full page rewrite, given how intricately the existing permission and
  status logic on this page is built. Caught and fixed a self-introduced duplicate-button/over-restrictive-
  permission bug while building this, by testing as an auditor as well as a treasurer before shipping.

**Reviewed, confirmed already correct (no change made):** the Staff Advances figure used during bank
reconciliation. Traced the full calculation chain (`_sync_managed_recon_items` ->
`outstanding_bank_advances_total`/`outstanding_petty_advances_total`) and confirmed it already computes
exactly "amount advanced minus expenses already settled against it, as of the reconciliation's own statement
date" — the Pending/Not-Accounted-For balance — cross-checked against the Statement of Financial Position
and dashboard, which use the same underlying concept. No competing or incorrect figure found anywhere.

Tests: 27 new across ledger/envelopes/giving/cashbook/reports/statements, 1 pre-existing test's header
assertion updated for the new export column. Targeted regression: ledger 55, envelopes 62, giving 136,
cashbook 278, reports 251, statements 77 — all green.

Deploy: no migration. Collectstatic recommended (new inline CSS for the quick-filter tabs).

## v2.14.0 - testing strategy review
Reviewed unit/integration test coverage, assertion quality, and long-term test reliability across the
application (135 test files, ~1,300 individual tests). No functional changes — this pass strengthens the
test suite itself.

**Added:**
- **A guardrail against test time-bombs.** An earlier review found and fixed a real bug class: `Pledge.
  start_date` defaults to `date.today()` (a moving target), and a test hardcoded an absolute contribution
  date that needed to fall within a window relative to it — the test passed when written and silently broke
  months later purely because real time had moved on, with no code change at all. This review added an
  automated scanner (`core.test_testing_review`) that inventories every `DateField`/`DateTimeField` across
  every app with this exact shape (a callable default, not `auto_now_add`) and fails if a new one appears
  without deliberate review. Verified it actually works: reintroduced a copy of the bug pattern in a
  throwaway model, confirmed the guardrail caught it with a clear, actionable failure message, then reverted.
- Confirmed, via the same scan, that `pledges/models.py` is the *only* file in the codebase with this
  pattern today, and that the only tests exercising the date-window-sensitive matching logic already pin
  their dates correctly (from the earlier fix) — no further live instances of the bug found.

**Assessed (no changes needed):** spot-checked the ~163 bare `status_code == 200` assertions across the test
suite for the "weak assertion" pattern flagged by this kind of review — found them to be legitimate in every
file sampled: either access-control tests where the status code itself *is* the meaningful assertion (a 200
vs. a 302 correctly proves who can and can't reach a page), or a "did it render" precondition followed by
real business-logic assertions on the same response. No systemic weak-assertion problem found.

**Recorded in `docs/recommendations.md`:** no CI/CD pipeline or code-coverage tooling exists (Medium-High —
arguably the highest-leverage testing investment available, converting every review's "run the tests" step
into an enforced gate rather than a manual habit); test files in `cashbook` (32 of them) mix feature-named and
version/session-named files, making coverage harder to locate by feature than necessary (Low); no genuine
concurrency/load testing exists, only careful code-review reasoning about concurrent-access risk (Low, given
current usage patterns).

Tests: 2 new (core.test_testing_review). Targeted regression: core 199, pledges+assets 45 — all green.

Deploy: no migration. Collectstatic not required.

## v2.13.0 - database integrity: transaction atomicity and referential protection
Reviewed schema design, foreign keys, cascade behaviour, and transaction handling across the whole
application. Two genuine risks found and fixed; further items recorded in `docs/recommendations.md`.

**Fixed:**
- **Splitting a contribution across several funds** (`Transaction.split_into()`) reduced the original entry's
  amount and then created the remaining sibling entries in separate, unwrapped database writes. A failure
  partway through — a constraint violation, a server restart — would leave the original entry permanently
  reduced with the remainder recorded nowhere, silently losing money from the books. Verified by forcing a
  real mid-split failure: before this fix, the original entry stayed wrongly reduced; after, it's fully
  restored to its original amount and fund.
- **Settling a trust remittance batch** (matching one bank payment to several trust funds' remittance lines)
  updated the batch, the expense lines, and the settling transaction in three separate, unwrapped writes.
  Now wrapped in one atomic block, so a failure partway through can't leave a batch marked "sent" with its
  expenses unpaid, or vice versa.
- **`DepartmentStatusLog`** (a fund's active/closed/archived history) and **`FundCarryForward`** (a fund's
  year-end closing-balance snapshot) both used `on_delete=CASCADE` on their department link, despite being
  audit-trail records by their own stated purpose. Changed to `PROTECT`, matching how `Transaction` and
  `Expense` already protect a fund from deletion — a department can't be deleted through any application view
  in the first place, so this only closes the narrow remaining gap (a fund with no financial activity left,
  but status or year-end history) rather than changing any current, working behaviour.

**Confirmed already correct (no changes needed):** every money-movement model's positivity validators (from
an earlier review), the dominant report-query indexes (from an earlier review), `DepartmentLeadership`'s
duplicate-prevention unique constraint, and `ATOMIC_REQUESTS` being intentionally off (checked to confirm no
other view silently relied on framework-level atomicity that isn't actually configured).

**Recorded in `docs/recommendations.md`:** two unrelated models are both named `BudgetLine` (one in
`departments`, one in `cashbook`) — confusing but not incorrect, and renaming either is an invasive,
multi-file migration better done as its own deliberate pass (Low priority).

Tests: 8 new (giving.test_atomicity, departments.test_referential_integrity) — including a test that forces
a real mid-operation failure and proves the database is left completely untouched, not partially written.
Targeted regression: departments 48, giving 127, core 197, cashbook 271 — all green.

Deploy: migrate (departments 0021, core 0048 — on_delete changes only, no data affected).

## v2.12.0 - accessibility: form labels and colour contrast
Reviewed navigation, forms, dashboards, and visual presentation against WCAG. The application already had a
strong accessibility foundation (skip link, ARIA landmarks, focus-visible styles with a high-contrast mode,
loading indicators, double-submit guards, `aria-live` flash/toast regions, correct viewport meta) — this
pass found and fixed two genuine, widespread gaps underneath that foundation.

**Fixed:**
- **Every form label in the application was missing its `for` attribute** — found first on the expense form,
  then confirmed across the entire app: the shared `partials/form_fields.html` template (used by most forms)
  and ten further custom form templates (expenses, obligations, petty cash, assets, members, pledges,
  campaigns, cash entries, settings, password change, the transactions filter bar, pledge import) all rendered
  a bare `<label>Field name</label>` with no programmatic link to its input. This meant a screen reader user
  had no way to know what a field was for beyond its position in the page, and clicking a label didn't focus
  its field for anyone. Fixed everywhere — including several hand-built (non-Django-field) inputs that needed
  a matching `id` added before they could be labelled correctly. Verified with zero remaining unassociated
  labels across every form template checked.
- **The amber "pending/warning" status colour failed WCAG AA contrast** — 3.99:1 in light mode and 3.28:1 in
  dark mode (both need 4.5:1), used throughout the app for pending/warning pills, flash messages, and toasts.
  Fixed to 4.76:1 (light) and 6.92:1 (dark, via a new theme-specific override) — both comfortably pass, and
  the colour still reads as the same warm amber/gold.

**Recorded in `docs/recommendations.md`:** two further items — data tables lack `scope="col"` on header cells
(Low-Medium, a broad mechanical cleanup better suited to its own pass than a partial sweep here), and no
dedicated mobile-breakpoint audit was performed this pass for the denser reporting pages (Low).

Tests: 12 new (core.test_label_accessibility), covering the shared partial directly and eight full-page
checks across the app finding zero unassociated labels. Targeted regression (every app with a touched
template): core 197, cashbook 271, giving 123, assets+pledges+accounts 91 — all green.

Deploy: no migration. Collectstatic recommended (CSS changed) but not required for correctness.

## v2.11.0 - critical: cash-position reporting and reconciliation fix
Reviewed every financial report, dashboard, chart, and export for calculation accuracy and reconciliation
with the General Ledger. Cross-checked collections, local funds statement, cash flow, and SOFP figures
against each other and against the ledger's own trial balance and accounting equation — all tied out exactly,
with one critical exception found and fixed.

**Fixed (Critical):**
- **Executive overview's "Cash & bank balance" KPI card, the Cash Flow Forecast, and the bank reconciliation
  book balance** were all computing "today's cash position" from `SiteConfig.opening_bank_balance` /
  `opening_cash_on_hand` / `opening_unremitted_trust` — fields populated only by the legacy-spreadsheet
  import tool as a one-time labelled snapshot, and left at zero for every normal deployment (including this
  one). The actual authoritative opening balance has always been `Department.opening_balance`, summed across
  every fund — the same source the ledger and the Statement of Financial Position correctly use. For this
  church's data, the bug understated "today's cash" by the full opening position (a multi-million-shilling
  discrepancy in testing), meaning: the Executive overview showed a false negative cash balance with no
  warning styling; the Cash Flow Forecast projected from a wrong starting point; and — most seriously — the
  bank reconciliation book balance could never tie to the actual bank statement, showing an unexplained gap
  on every single reconciliation. All three now use a new shared, correct helper
  (`departments.models.total_opening_cash_position()`) and were verified, after the fix, to tie out exactly
  to the Statement of Financial Position's own cash figure.
- The bank-reconciliation diagnostic view's "opening" breakdown (shown alongside its already-correct "book"
  figure, which came from a separate, unaffected calculation) is now internally consistent with that figure
  instead of contradicting it.

**Verified correct (no changes needed):** collections totals, local funds statement, trust fund summary,
cash flow opening/closing reconciliation, chart data vs. table data within the same report, and date-range
boundary handling (start/end dates correctly inclusive) — all cross-checked across multiple independent
calculation paths and found consistent.

**Recorded in `docs/recommendations.md`:** the Bank Position report depends on the same
`opening_bank_balance` field for a genuinely different figure (the bank account's own opening balance, which
isn't derivable from per-fund balances) and needs a treasurer to configure it explicitly — High priority,
operational rather than a code fix. Also recorded: the three legacy-import-only opening-balance fields remain
a duplicate, easily-misused source of truth that produced this exact mistake three times over several
releases — recommend renaming or documenting them more forcefully to prevent a fourth recurrence (Medium).

**Deploy note:** if bank reconciliation is in use, recompute any existing reconciliation worksheets (there's
a "recompute from ledger" action on each) so their stored book balance picks up the corrected figure — a
worksheet saved before this fix keeps its old, wrong stored value until recomputed.

Tests: 7 new (core.test_cash_position_fix), 1 existing performance test's query-count threshold adjusted
(+1 query — the correct opening-balance aggregate — a trivial cost for a critical correctness fix). Targeted
regression: core 185, statements 73, departments 44, reports 248, cashbook 271, giving 123 — all green.

Deploy: no migration. Collectstatic not required.

## v2.10.0 - trust pending-receipt export + architecture review
**New:** a "Trust pending receipt" download button on the Transactions page
(`/transactions/`) — an Excel export of trust-fund credits with no formal receipt yet
(Date, Phone, Member, Amount, Fund, Reference), independent of whatever filters are
currently applied to the list. A contribution split across several trust funds (e.g.
a Combined Offering split across two trust accounts) is recombined into one row —
summed amount, the split fund's name — rather than shown as separate partial lines,
since to the giver and the board it's one gift. The split fund's name is resolved via
the allocation rule that caused the split; falls back to a joined list of fund names
if no rule is found, so the export never breaks or shows a blank fund.

**Architecture & code-quality review** (Senior Architect / Django Expert lens):
codebase evaluated for maintainability, DRY compliance, dead code, and Django best
practices. Findings:
- Removed several confirmed-dead/fully-redundant imports (`reports/views.py`,
  `cashbook/views.py`, `giving/views.py`, `core/views.py`) — each verified to have
  zero usages, or to duplicate an import already present earlier in the same file,
  before removal. No behaviour changed.
- Reviewed all ~94 `except Exception` blocks in the codebase for silently-swallowed
  errors: the large majority are either explicitly marked `# noqa: BLE001` (a
  deliberate, previously-reviewed choice) or guard genuinely non-critical optional
  functionality (an update-availability check, an optional model import) where
  logging every transient failure would spam the log on every page load. No
  systemic error-handling issue found — a positive finding, not left for cleanup.
- Recorded two further items in `docs/recommendations.md`: `reports/views.py` (3,889
  lines) and `cashbook/views.py` (3,116 lines) have grown into "god files" that would
  benefit from splitting into logical sub-modules (Medium priority — a deliberate,
  dedicated refactor, not a quick fix); and the department-dropdown queryset pattern
  is repeated with minor variations across six form classes (Low priority).

Tests: 9 new (giving.test_trust_pending_receipt). Targeted regression (modules
touched): reports 248, cashbook 271, core 178, giving 123 — all green.

Deploy: no migration. Collectstatic not required.

## v2.9.0 - performance review, round two
Continued database/performance review. Two more N+1 patterns found and fixed, both more severe than
anything caught in the first pass; further items added to `docs/recommendations.md`.

**Fixed:**
- **Audit Log** (`/reports/audit/`) issued **~1,538 queries** for a church with a substantial history of
  allocation-rule edits (60 create+edit cycles in testing generated ~6,500 historical rows). Root cause:
  `h.instance` (a historical row reconstructed as a full model instance) carries no `select_related`, and
  `AllocationRule.__str__()` accesses `self.split_fund or self.department` — so calling `str(h.instance)` for
  every historical Allocation Rule row triggered a fresh FK lookup each time (1,426 Department queries + 90
  SplitFund queries observed). Fixed by building the display string directly from the historical row's own
  FK id columns against two name maps prefetched once, only for this one model (Transaction, Expense and
  Member history all use `h.instance` as before — their `__str__` methods don't touch relations, confirmed
  by testing). Verified identical display strings across 300 sampled historical rows. **~1,538 → ~24 queries.**
- **Every fund-picker dropdown built without `select_related("parent")`.** `Department.__str__()` shows
  "Parent / Name" for a sub-account — so rendering a `<select>` of departments calls `.parent` on each option,
  and eight form classes across cashbook, giving, assets, and core built their department queryset without
  prefetching it. The Payables & Accruals page alone (three such dropdowns) dropped from 44 to 23 queries;
  the fix applies equally to the expense, cash-entry, transaction, fund-transfer, payable, accrual,
  prepayment, and fixed-asset forms.

**Recorded in `docs/recommendations.md`:** one further item (`StaffAdvance.balance` computed per-row on the
advance list — Low priority, marginal at current scale), added to the five recorded in the first round.

Tests: 6 new (reports.test_performance2), asserting both correctness (display strings match the original
per-instance method) and query-count bounds. Targeted regression (the modules touched this round): reports
248, cashbook 271, giving 114, assets+core 197 — all green.

Deploy: no migration (queryset/view changes only). Collectstatic not required.

## v2.8.0 - performance & database review
Reviewed database access patterns, ORM usage, N+1 queries, indexing, and report generation. Two clear,
high-impact N+1 query patterns found and fixed; further architectural/infrastructure recommendations
recorded in `docs/recommendations.md` rather than implemented inline, per this review's scope.

**Fixed:**
- **General Ledger Health Check** (`/ledger/health/`) issued ~266 queries per load: `funds_out_of_balance()`
  called `fund_balance_from_ledger()` once per department (2 queries plus an implicit transaction each). New
  `fund_balances_from_ledger_bulk()` computes every fund's ledger balance with a small, constant number of
  grouped-aggregate queries instead. Verified byte-for-byte identical results against the original
  per-department function across every fund. Query count: ~266 → ~35.
- **Executive overview and budget-vs-actual reports** had the same pattern for budget figures:
  `budget_amount()` was called once per top-level fund (45 identical queries observed on the Executive
  overview alone). New `budget_amounts_bulk()` in `reports/services/budget.py` fixes both call sites
  (`ExpenseReportView`'s by-fund breakdown and `budget_vs_actual()`, which also feeds the Monthly Treasurer's
  Report). Verified identical results across every fund and every budget shape (legacy annual_budget, plain
  Budget.amount, Budget with breakdown lines, no budget at all). Executive overview: ~162 → ~114 queries;
  Monthly Treasurer's Report also dropped from ~175 to ~129 as a side effect of sharing the same fix.
- **New database indexes** for the two most common report-query shapes that weren't already covered:
  `Transaction(direction, confirmed, date)` — the shape behind `confirmed_credits()` filtered to a period,
  used by nearly every collections/income report — and `Expense(status, date)` — effective (approved/paid)
  expenses within a period, the dominant shape across expense reports.

**Caught during this review:** an earlier edit to `Transaction`'s model accidentally added a *second*
`class Meta` inside the same class body; Python silently keeps only the last one defined, which would have
discarded the pre-existing `ordering` and three existing indexes entirely. Caught before migrating by
checking `Transaction._meta.indexes` against the database rather than trusting the migration diff alone;
fixed by merging the new index into the correct, pre-existing `Meta` block.

**Recorded in `docs/recommendations.md` (not implemented this pass — see file for full detail):**
1. Monthly Treasurer's Report recomputes the same aggregates separately for several sections instead of once
   (Medium) — a report-internals refactor, not a quick fix.
2. `SiteConfig.get()` is uncached, costing 7-11 redundant identical queries per request (Medium) — the safe
   fix depends on an infrastructure decision (no shared cache backend is configured; the default per-process
   cache risks stale security/financial-control settings across workers).
3. No row-level locking on petty-cash-float balance checks — a narrow concurrency race (Low today, revisit
   if concurrent usage grows).
4. No systematic N+1/index audit against real production traffic, only synthetic profiling (Low).
5. Large file imports run synchronously with no background task queue (Low, given current usage patterns).

Tests: 9 new (ledger.test_performance, reports.test_performance), asserting both correctness (bulk functions
match the original per-item functions exactly) and query-count bounds. Targeted regression run (the four
modified apps only, per this review's instruction): ledger 49, reports 242, cashbook 271, giving 114 — all
green. Test suite run time also dropped noticeably (reports: ~150-170s -> ~78s) as a real-world side effect.

Deploy: migrate (cashbook 0033, giving 0022 — index-only, no data changes). Collectstatic not required.

## v2.7.0 - business logic & functional review
Comprehensive functional review of workflows, validations, calculations, and state transitions across every
module. Full findings, fixes, and regression results below; see the chat summary for the complete report.

**Fixed:**
- **Negative/zero amount validation (the main finding).** `Transaction.amount` already rejected non-positive
  values; `Expense.amount` and every other money-movement model did not — a real gap, not a deliberate
  design difference. A negative "expense" would post as an unreviewed credit to cash while still being
  categorised and reported as an expense, bypassing income recognition entirely. Now consistently enforced
  (`MinValueValidator(0.01)`) on: Expense, ExpenseRefund, FundTransfer, RecurringExpense, PettyCashTopUp,
  Payable, Accrual, Prepayment, StaffAdvance, AdvanceTopUp, ChequeRegister, PaymentInstrument, PledgePayment,
  PledgeMatchSuggestion, EnvelopeLine, and FixedAsset.cost (FixedAsset.salvage_value allows zero, the normal
  case, but not negative).
- **Future-date typo guard.** No form rejected a wildly future-dated entry — a year typo (2036 for 2026)
  would silently misfile a transaction with no error, since it just wouldn't appear in any report until that
  date arrived. Expense, cash entry, transaction-edit, and fund-transfer forms now reject a date more than a
  day ahead (a day of slack for timezone differences), with a clear message naming the likely mistake.
- **Pledge inline-matching test time-bomb.** Four tests failed with `paid` staying 0 / no match suggestions
  created. Root cause: a Pledge defaults `start_date` to *today*, but the tests hardcoded an absolute
  transaction date that was valid when written and has since drifted outside the matching window as real
  time passed — a test-fragility bug, not a defect in the matching logic itself (confirmed by testing the
  same code against the same scenario with an explicit, fixed `start_date`). Fixed by pinning `start_date`
  in the four affected tests.
- **Self-introduced regression, caught and fixed the same session:** an edit to `ExpenseForm` initially split
  `__init__` in two, leaving its accessibility styling call (`_style()`) and several field-setup steps
  (capitalized-asset queryset, petty-cash checkbox help text) as unreachable code with no visible error.
  Caught by the full regression run (`core.test_ux_a11y`), root-caused, and corrected; a regression test now
  guards against this exact class of mistake recurring silently.

**Documented for follow-up (not in scope for auto-fix this pass):**
- A deeper concurrency/race-condition review (e.g. simultaneous petty-cash top-ups both reading a stale
  float balance) was not performed in full — low real-world likelihood given typical single/few-treasurer
  usage, but worth a dedicated look if usage grows.
- A full N+1 query / performance audit across dashboards and reports was out of scope for this pass.

Tests: 33 new (cashbook.test_amount_validation, cashbook.test_future_date_validation, pledges/envelopes/
assets.test_amount_validation), 2 pre-existing pledge tests corrected. Full application regression, every
app: accounts 46, assets 19, cashbook 271, core 178, departments 44, envelopes 58, giving 114, ledger 44,
leaders 58, members 40, pledges 26, reports 238, statements 73 — 1,209 tests, all green.

Deploy: migrate (cashbook 0032, pledges 0004, envelopes 0004, assets 0004 — validator-only changes, no data
affected). Collectstatic not required.

## v2.6.0 - security & internal controls review
Reviewed against the OWASP Top 10, Django security best practices, least privilege, segregation of duties,
and internal-control principles for financial systems. Overall posture was already strong (Fernet-encrypted
credentials throughout, django-axes brute-force lockout on login, hardened production settings that fail
loudly on an insecure config, webhook endpoints authenticated with constant-time comparisons, no raw SQL
anywhere, every sensitive field explicitly listed rather than mass-assignable). Findings below.

**Fixed:**
- 2FA brute-force gap: the code-verification step (`/2fa/verify/`) had no rate limiting, unlike the password
  step (protected by django-axes) — a 6-digit TOTP could be guessed with unlimited attempts. Now locks the
  pending login out after 5 wrong codes and requires signing in again; the attempt counter is per-login and
  resets cleanly on a fresh sign-in.
- Last-active-Treasurer lockout protection: nothing stopped a Treasurer from demoting or deactivating the
  only active Treasurer account, which could leave the church with no one able to approve expenses, manage
  users, or unlock accounting periods. Now blocked with a clear message unless another active Treasurer
  exists; editing any other role is unaffected.
- Session lifetime: Django's default (2 weeks, no idle timeout) is longer than good practice for a financial
  system. Now a configurable 12-hour sliding session (renews on activity, so active users are never logged
  out mid-task) — override via `DJANGO_SESSION_COOKIE_AGE` if a different value is needed.
- Defence-in-depth: one chart-data JSON blob (leader dashboard) used a plain `json.dumps()` instead of the
  app's `safe_json()` (which escapes characters that could break out of a `<script>` tag) — switched for
  consistency, though the specific data involved was already free of user-controlled text. The manual journal
  entry form now handles a line with both a debit and a credit gracefully instead of raising, matching the
  new balanced-entry safeguard added in v2.4.0.

**Documented for review (policy decisions, not auto-fixed):**
- A department leader who leads any one development-fund can view the full contributor list (names, phones,
  amounts) for every development group church-wide, not just their own — confirmed as consistent, existing
  design throughout the leader area (not a new bug), but worth a policy decision on whether that shared
  visibility is acceptable for personally identifiable giving data.
- The bank-feed webhook's "None" authentication mode is already labelled "test environment only" and is not
  the default (Basic auth is) — low risk as configured, but worth confirming production never runs on it.

Tests: 11 new (accounts.test_security_audit, ledger.test_security_audit). Full application regression run:
accounts 46, cashbook 244, giving 114, leaders 58, statements 73, departments 44, members 40, core 178,
reports 238, envelopes 56, ledger 44, assets 15 — all green. Pledges: 35 of 39 pass; the remaining 4
(inline pledge-matching) were already failing before this review and are unrelated to it — flagged for
separate follow-up, not touched here.

Deploy: no migration. Collectstatic not required.

## v2.5.0 - general ledger health check, period-close checklist, permanent journal numbers
**1. General Ledger Health Check** (`/ledger/health/`, linked from the Accounting menu): a proactive integrity
dashboard showing trial balance status, unbalanced journals (should always be zero), orphan journals (a
posted entry whose source document no longer exists), missing postings (a source document that should have
been posted but wasn't), duplicate postings, shared M-Pesa/bank references worth a human look, and funds out
of balance between the fund-report engine and the general ledger. Every check is read-only; `ledger.services.health`
can also be used from a shell/management command for scripted monitoring.

**2. Accounting Period-Close Checklist**: the Treasury Controls page now shows a checklist for any open month
before it's locked — bank reconciliation complete, petty cash reconciled, advances cleared or explained, no
pending envelope allocations, no draft/pending journals, trial balance balances, fund balances reconcile, and
cash book equals bank plus cash on hand. Nothing here blocks locking outright (an advance spanning months is
often normal) — the lock button asks for confirmation if items are outstanding, so closing stays deliberate.

**3. Immutable journal sequence numbers**: every journal entry now gets a permanent reference (JV-2026-000001,
incrementing per year, assigned once and never reused). Since a correction to a source document replaces its
journal entry rather than editing it in place, the *original* entry's number is preserved in the Journal
Archive (added in v2.4.0) when it's superseded — the replacement gets its own new number. Existing entries
were backfilled in chronological order via a data migration. Shown on the Journal, General Ledger, Journal
Archive and Health Check pages, and included in the Journal's Excel/CSV export.

Tests: 16 new (ledger.test_health_and_numbering). Full regression green (cashbook 244, giving 114, core 178,
reports 238, statements 73, envelopes 56, ledger 42).

Deploy: migrate (ledger 0004-0005 — the 0005 migration backfills journal numbers for existing entries, safe
to run on a live database). Collectstatic not required.

## v2.4.0 - accounting-integrity review: customizable controls for every open recommendation
Implements the four Medium and two Low findings left open in the previous accounting-integrity review
(v2.3.0), each as a configurable setting where the recommendation involved a genuine trade-off, so every
existing deployment keeps its current behaviour until a treasurer deliberately opts in.

**New settings (Settings -> Approvals & financial controls):**
- **Require a different approver** (off by default) — blocks a treasurer from approving an expense they
  recorded themselves, for every expense, not just those above the dual-approval threshold. Applies to both
  the single-expense approval action and bulk approve.
- **Auto-lock on reconciliation** (off by default) — automatically locks the accounting month once a bank
  reconciliation for it balances. Independently of this setting, editing or adding an entry dated within an
  already-reconciled period now always shows a non-blocking warning.
- **Leader self-service delete window** (blank = unlimited, matching prior behaviour) — a department leader
  may only delete their own already-posted advance expense line within this many days of entering it;
  afterwards only a treasurer can remove it. Deleting now always requires a short reason (captured on the
  audit trail) and notifies all treasurers in-app.
- **Archive replaced ledger entries** (on by default) — snapshots a journal entry's detail whenever a
  correction (e.g. editing an expense's amount after posting) causes it to be replaced, so what the ledger
  said before the correction is never lost even though the live ledger only ever shows the current, correct
  posting. New Journal Archive page (`/ledger/journal/archive/`, linked from the Journal page) to review them.

**Always-on safety net:**
- Every journal entry is now validated to balance (debits == credits, no line with both a debit and a
  credit) at the single point all posting paths go through, raising `UnbalancedEntryError` if not. Every
  current posting path already balances by construction — this is defence-in-depth against a future mistake,
  not a behaviour change; full regression confirms it never triggers on any existing code path.

**Per-fund enhancement:**
- New optional `income_account` field on a fund: overrides the previous name-matching guess for which income
  account a local fund's receipts post to, for a fund whose name doesn't clearly say what it is (or after a
  rename). Blank (default) keeps the existing automatic guess.

Tests: 19 new regression tests (cashbook.test_audit_recommendations) covering every new setting in both its
default (off/unlimited) and opted-in state. 4 pre-existing tests updated to pass the now-required delete
reason. Full regression green (cashbook 244, giving 114, leaders 58, statements 73, departments 44, core 178,
reports 238, envelopes 56).

Deploy: migrate (core 0047, ledger 0003, departments 0020). Collectstatic not required.

## v2.3.0 - accounting-integrity & internal-controls review: two fixes, four documented for decision
A full review of the accounting core (general ledger, fund accounting, reconciliation, segregation of duties,
audit trail) against GAAP, IFRS for SMEs, and non-profit/church fund-accounting practice. See
`Treasury_Accounting_Controls_Review.docx` for the complete written findings register; summary below.

**Fixed this release:**
- General ledger date desync: correcting an envelope's Sabbath date bulk-updated its linked transactions'
  dates via a bulk `.update()`, which bypasses the signal that reposts entries to the ledger — the journal
  entry silently kept the old date. Now explicitly re-posted so the ledger follows the correction.
- Posted expenses were hard-deletable: any treasurer could permanently delete an APPROVED or PAID expense,
  removing it from the ledger with no trace, bypassing the app's own proper reversal mechanism (ExpenseRefund).
  Hard-delete is now restricted to PENDING expenses only; a posted expense must be reversed via a refund.

**Documented for review (not auto-fixed — policy or design decisions):**
- Leaders can hard-delete their own already-posted advance-accounting lines with no reason captured.
- No database/model-level safeguard that a journal entry's debits equal its credits (currently balanced by
  construction in every posting path, but nothing would catch a future mistake).
- The general ledger itself has no change history — only source documents do (a defensible, but
  under-documented, design choice).
- Bank reconciliation sign-off isn't linked to period locking, so a signed-off reconciliation can be silently
  invalidated by a later edit in its period.
- (Low priority) No system-level prevention of self-approval below the dual-approval threshold; income-account
  classification relies on fund-name text matching.

Tests: 5 new regression tests for the two fixes (cashbook.test_audit_findings). Full regression green
(cashbook 225, envelopes 56, giving 114). No migration; collectstatic not required.

## v2.2.1 - group contribution goals JPEG shows target/contributed/to go/progress
- The Group Contribution Goals JPEG chart (`/reports/fund/<id>/budget/group-goals.jpg`) now labels each
  group with Target, Contributed, To go, and percent progress — matching what's shown on the budget page —
  instead of just a collected/goal fraction. The all-groups footer total does the same.
- No tests changed this release (visual/label-only edit to the existing chart, already covered).

## v2.2.0 - receipt-strip wildcard fix, JPEG goal chart, member giving-count filter, board report case + charts
- Fix: the receipt strip-strings `*` wildcard was matching almost nothing. Two separate bugs: (1) whitespace
  in the configured phrase had to match the message exactly character-for-character, so a pattern typed with
  a double space against a message with a single space (a very easy copy-paste slip) silently failed to
  match at all; (2) the wildcard was non-greedy, so over an amount containing its own decimal point (e.g.
  "499,900.00") it stopped at the *first* period it found instead of the sentence's real full stop, leaving
  a fragment like "00." behind. Whitespace in a configured phrase now matches any run of whitespace in the
  message, and the wildcard is greedy so it correctly consumes the whole varying value.
- New server-side JPEG chart for a fund's Group Contribution Goals (`/reports/fund/<id>/budget/group-goals.jpg`):
  a proper per-group progress bar chart rendered with Pillow, not a table screenshot — same everywhere it's
  downloaded from, no client-side rendering needed.
- Members SMS page (`/members/sms/`) gained a "minimum contributions on record" filter, layered on top of
  any criterion (including the plain broadcast), to exclude one-time givers who may not be church members.
- Fix: `_camp_goal_records()` picked whichever CAMP_EXPENSE-flagged fund an unordered `.first()` happened to
  return, which is not guaranteed stable — if more than one fund was (mis)flagged, or the one returned had no
  `year_goal` set, the Camp Meeting Expense Goal would silently disappear from every report. Now deterministic:
  prefers the fund that actually has a goal set.
- Monthly Treasurer's Report (HTML and Word) and the classic board report now display fund/member names in
  Sentence case via a new `sentence_fund` filter, instead of the ALL CAPS many were originally entered in
  (short acronyms like AMM/LCB/PF are kept as-is).
- Word export gained three chart images (income vs expenditure, collections local/trust split, Camp Meeting
  Expense Goal progress when applicable), rendered server-side with Pillow and embedded as base64 — Word
  can't run the on-screen report's JS charts — each with a short AI-analysis caption (server-side, LLM-
  enriched with a rule-based fallback, same pattern as the per-section narratives).
- Tests: ~35 new tests across cashbook, reports, members. Full regression green (cashbook 220, reports 238,
  members 40, giving 114, core 178).
- Deploy: no migration; collectstatic not required.

## v2.1.0 - expense categories, member SMS, remittance-batch fix, camp goal bug fix, dynamic receipt stripping
- New expense categories: Salaries/Wages, Lease Payment (Stationery/Printing already existed).
- Bank/transaction charges (category BANK_CHARGE) no longer show a "no receipt" pill on the expense list, and
  the expense detail page shows "No receipt — none needed" instead of implying one is missing (the missing-
  receipts queue already excluded them; this closes the remaining inline UI gaps).
- New Members page SMS button (`/members/sms/`): send a message to members matching a criterion — not yet
  contributed to a given campaign (e.g. Camp Meeting), have an outstanding pledge, haven't given in the last
  N days, belong to a demographic group, or a plain broadcast to everyone with a phone. Recipients preview
  live before sending; message supports {name}/{church}/{campaign}/{amount} placeholders.
- Fix: the "settle a batch (multi-fund)" remittance option on the debits queue never populated any batches —
  its query ordered by `-created_at`, a field RemittanceBatch doesn't have. Django templates swallow that
  FieldError silently when iterating, so the dropdown just rendered empty with no visible error. Now orders
  by the model's actual `date`/`id` fields.
- Fix: the fund budget page (`/reports/fund/<id>/budget/`) has two independent forms — the fund's own
  expense goal, and per-group contribution goals — that both posted to the same `save_goals` flag; the
  shared handler unconditionally rewrote every field regardless of which form was submitted, so saving a
  per-group goal silently reset the fund's overall expense goal (and goal_type) to blank. Split into
  `save_expense_goal` / `save_group_goals`, each touching only its own fields.
- The Monthly Treasurer's Report (`/reports/board/`) already aggregates the Camp Meeting Expense Goal across
  the fund and all its sub-groups (never a per-group breakdown) — confirmed working now that the goal no
  longer gets wiped by the bug above.
- Receipt strip-strings (Settings → Branding) now support a `*` wildcard for parts that vary every message —
  an M-Pesa balance, a transaction cost, a promo link — so one configured line strips the whole sentence
  regardless of the actual figures.
- Tests: ~30 new tests across cashbook, members, reports; 4 pre-existing tests updated for the split budget
  forms. Full regression green (cashbook 212, giving 114, reports 225, members 36, core 178).
- Deploy: migrate (cashbook 0031, core 0046 — category/help-text changes only), collectstatic not required.

## v2.0.0 - reconciliation fix, board report rework, remittance batch matching, camp goal settings, and more
- **Bank reconciliation fix**: petty-cash-funded outstanding staff advances were never added to the
  reconciliation worksheet — the petty float already subtracted them (cash had left the box), but nothing
  added them back as their own item, so that money silently vanished from the reconciliation. New
  `outstanding_petty_advances_total()` adds a "Staff advances from petty cash (not yet accounted)" managed
  item; `outstanding_bank_advances_total()` also now respects top-up dates properly.
- **Monthly Treasurer's Report — RTF removed**, Excel rebuilt with full detail (every fund listed, not the
  on-screen top-10), native charts (pie/bar/line) on Collections, Trust Trend, Local Funds, Expenditure and
  Financial Position, and a KPI-card Executive Summary sheet. Word export rewritten to mirror the on-screen
  report's structure (Executive Summary, each management section, Board Decisions Required) and now carries a
  per-section analysis paragraph — computed server-side (`_ai_narratives`), LLM-enriched in one batched call
  when the assistant is enabled, always falling back to the same rule-based text already used on screen.
- **Trust remittance — batch matching**: a new "settle a batch (multi-fund)" option on the debits queue
  matches a bank payment to an open remittance batch, marking every trust fund's line PAID and charging each
  fund its own share, instead of forcing the whole payment onto one fund. Amount mismatches are rejected with
  a clear message.
- Fund Ledger sub-accounts now sort by closing balance (largest first) instead of receipts.
- **Camp Meeting Offering goal moved to Settings** (new Goals tab): a single church-wide Trust-fund figure,
  no longer set per fund. The Camp Meeting *Expense* goal and every fund's own budget/goals stay on that
  fund's own page. Data migration moves any existing configuration across; migrations core 0043-0045.
- New Settings field: strings to strip from saved bank/M-Pesa receipt messages (e.g. "never share your PIN"
  boilerplate), applied on every save, plus a "clean up already-saved messages" button to re-run it over
  everything already imported.
- Receipt/supporting-document uploads capped at 1MB (was 10MB) across every upload point.
- Supporting Documents PDF now only includes expenses with an actual file attachment (text/link-only
  attachments are covered by the Receipts view instead). Fixed a Django ORM gotcha along the way:
  `.exclude()` on a to-many relation excludes the *parent* if it has *any* matching related row, so an
  expense with one real file and one text-only attachment was being wrongly dropped entirely.
- Treasurer dashboard: the Latest Sabbath date now follows your font setting instead of a hardcoded font; the
  combined receipts-vs-expenses-by-month chart moved to the Executive overview showing the full year (was
  just the dashboard's selected period); its old spot is now a local-vs-trust pie chart for the selected
  month.
- Tests: ~55 new tests across statements, giving, reports, cashbook and core; 5 pre-existing tests updated
  for the redesign. Full regression green (statements 73, giving 114, cashbook 198, reports 225, core 178,
  leaders+members 85).
- Deploy: migrate (core 0043-0045), collectstatic (CSS/template changes).

## v1.99.1 - fix attachment popovers opening by default on the expense list
- Fix: `.clip-pop{display:flex}` was an unconditional CSS rule, so it overrode the browser's default
  `[hidden]{display:none}` styling on every popover (author styles beat user-agent styles at equal
  specificity). Every receipt/M-Pesa-text popover on the Expense Register rendered open immediately instead
  of staying hidden until its paperclip was clicked. Added `.clip-pop[hidden]{display:none}`.
- Improvement: opening a popover now closes any other one that's open, so only one shows at a time; clicking
  anywhere else on the page also closes it.
- Tests: cashbook/test_clip_popover (4). Full cashbook suite green (199 tests).
- No migration; deploy needs collectstatic (CSS/JS changed).

## v1.99.0 - advance top-up charges, debit "already accounted for", remittance crash fix
- Staff advance top-ups gain an optional Charge field (advance detail page): the bank/M-Pesa cost of sending
  the top-up, booked as a BANK_CHARGE expense against the fund (church's own cost) but NOT added to what the
  holder must account for. Reversing a top-up now also removes its linked charge expense. Migration
  cashbook 0030 (AdvanceTopUp.charge, charge_expense).
- Bank debits queue (/debits/): new "Already accounted for" resolution — for a payment already recorded
  another way (or that shouldn't hit the books at all). Resolves the queue item with a required reason, and
  creates no expense and touches no fund balance.
- Fix: resolving a debit as a Trust fund remittance crashed with "Field 'id' expected a number but got ''"
  when the fund dropdown was left blank — its display toggle had been left out of the kind-switch JS, so a
  hidden select posted an empty string straight into a raw Department.objects.filter(pk=...) query. Added a
  _dept_from_post() helper used across all department lookups in the debit-resolve flow, and fixed the JS to
  reveal the fund selector for the remittance option.
- Enhancement: resolving a debit as a remittance now links the new expense to the most recent open (DRAFT or
  APPROVED) RemittanceBatch, if one exists, and reports which batch it was linked to.
- Tests: cashbook/test_topup_charge (4), giving/test_debit_queue_fixes (9). cashbook 195 / giving 109 green.
- Deploy: migrate (cashbook 0030), collectstatic not required.

## v1.98.0 - Monthly Treasurer's Report redesigned for the Church Board
- Executive summary (page 1): 8 KPI cards (Total collections, Local fund receipts, Trust fund receipts,
  Total expenses, Monthly surplus/(deficit), Cash & bank balance, Net assets, Trust funds outstanding), a
  Key Highlights panel (4-6 rule-based/AI-enriched lines), and an Items Requiring Board Attention panel.
- New _board_focus() computation (reports/views.py): rule-based detection of an unbalanced/missing bank
  reconciliation, negative local-fund balances, trust funds collected but not yet receipted, and budget
  overruns for the month (via reports.services.budget.budget_vs_actual). Feeds both the attention panel and
  a new Board Decisions Required closing section (always includes financial-statement approval and, when
  applicable, trust-remittance approval and a resolve-item per high-severity issue).
- Management report reorganised and renamed in sentence case: Collections summary, Trust fund performance,
  Local fund performance, Expenditure summary, Budget & goal tracking, Statement of financial position,
  Five-year trend, Cash flow, Bank reconciliation.
- Two new trend charts: receipted-trust 3-month line chart and a top-local-funds bar chart, alongside the
  existing 5-year, income/expenditure and fund-composition charts.
- Long tables (collections detail, local funds statement) show the top 10 with a collapsible Appendix for
  the full listing; collections detail gains a % of total column; negative fund balances and budget-overrun
  rows are visually flagged.
- All existing calculations, figures and the Excel/Word/RTF exports are unchanged — this is a presentation
  and organisation redesign only, built on the same context data.
- Tests: reports/test_board_report_redesign (12); updated 6 pre-existing tests whose assertions named the
  old section titles. Full regression green (reports 210, core 171, cashbook 191).
- No migration; collectstatic not required (no static asset changes, only template/view Python).

## v1.97.1 - fund ledger: brought-forward opening balance, collection-only payments column
- Fix: FundLedgerView used each fund's raw `opening_balance` field directly, so a fund with real activity
  before the report period (but a zero founding balance) showed an opening of 0.00 instead of its true
  brought-forward balance. Added `balances.brought_forward()` / `brought_forward_map()` (founding balance +
  all net movement strictly before the period start) and wired it into both the main fund and every
  sub-account on the Fund Ledger, matching what the dashboard already computed correctly.
- Fix: the sub-accounts table's Payments column was always shown, even when every fund displayed was
  collection-only (which never takes expenses). It now hides — showing Opening / Receipts / Closing only —
  when this fund and all its sub-accounts are collection-only, and the Excel/CSV export matches.
- Tests: reports/test_fund_ledger_bf (8). Full regression green (reports 198, core 171, cashbook 191).
- No migration; deploy is a straight code update + collectstatic not required (no static changes).

## v1.97.0 - leader receipts + awaiting-receipts queue, statement debit fix, cheque auto-reconcile
- Debit-import fix: cheque/transfer rows previously used the narration's first word ("CHQ", a payee name) as
  the bank-receipt dedup key, so after the first cheque every later "CHQ No..." row was flagged a duplicate.
  Shape-C parsing now only uses a genuine M-Pesa-style receipt; cheques dedup on their unique Core Ref. A real
  receipt column ("Receipt No") is used as a fallback dedup key when the narration has none.
- Cheque auto-reconcile: the reconciler now extracts cheque numbers from the statement narration
  ("CHQ No.000411") and matches them to the expense voucher/cheque number (leading zeros ignored). An exact
  amount plus a cheque-number match auto-links the bank debit to the expense and marks it paid/cleared, even
  when the cheque clears a few days after it was written.
- Leaders: new Receipts page and Awaiting-receipts queue in the sidebar, scoped to the funds they lead.
- Receipts archive shows each expense id (e.g. #543) for matching a receipt to its expense.
- Awaiting-receipts queue (leaders + treasurers): expenses with no supporting document; attaching a file or an
  M-Pesa message removes the expense from the queue. Dashboard cards on both roles track the count and value.
- Clicking the paperclip on the expense list reveals the attached M-Pesa message (text) or opens the document.
- Supporting Documents PDF now includes only expenses that have an attachment.
- Leader development-groups section: download-all Excel button; leader collections / expenses / unassigned-
  offerings pages share the same hero + filter layout.
- Fixed a URL collision (expenses/<pk>/attach/) that had shadowed the existing receipt-upload endpoint; the
  upload view now also lets a department leader attach to their own funds and honours a next redirect.
- Tests: cashbook/test_batch_v197 (11), statements/test_cheque_debits (6). Full regression green
  (statements 69, leaders 58, cashbook 191, core 171, giving 100, reports 190).
- Deploy: collectstatic (CSS/template changes). No new migrations.

## v1.96.0 - leader/dashboard polish, allocation rights, collection-only columns, sign-out page, church settings
- Fix: campaign list member/transaction counts used two Counts in one annotate, multiplying into a cartesian
  product (e.g. 800 -> ~16000). Now Count(..., distinct=True).
- Rights: added allocate_transactions and classify_debits granular rights + AllocateRequiredMixin /
  DebitClassifyRequiredMixin; review-queue and debit-queue views honour them, so a profile can grant
  allocation without the full data-entry role. (Item 7 answer: yes, allocation rights were missing.)
- Leader dashboard: development groups drop Target/Progress (show Opening/Receipts/Closing); sub-accounts sorted
  by closing balance desc; collection-only funds hide the Expenses column (opening/receipts/closing only) and
  their KPI cards omit expenses/net.
- Treasurer dashboard: local funds sorted by closing balance desc; collection-only funds hide the Expenses
  column.
- Leader expenses page: Method/Status/Category removed from the display (retained in the Excel export); leaders
  can attach a supporting file or an M-Pesa reference to any expense on their department.
- New sign-out page (SignOutView) with a rotating public-domain KJV verse; redesigned sign-in already shipped.
- New church-customization settings: church_address, church_contact, currency_symbol, report_footer_note
  (exposed as CURRENCY / CHURCH_ADDRESS / CHURCH_CONTACT / REPORT_FOOTER_NOTE in templates). Migrations
  core 0041 (currency/footer) and 0042.
- Tests: leaders/test_batch_v196 (10). leaders+accounts 95 / giving 100 / core 171 / cashbook 180 / reports 190.
- Deploy: migrate (core 0041, 0042), collectstatic.

## v1.95.0 - leader advances polish, dev-group balances, receivable fix, modern sign-in
- Leader advances list: added a "how to account for an advance" help panel and an explicit Open button per row.
- Leader advance detail: Excel download of the full statement (?export=xlsx).
- Leaders can now delete a transaction charge line directly (not only whole expenses). Deleting an expense
  removes its linked charge; advance settling charges are now linked via charge_for so the cascade is reliable;
  deleting an advance still removes its issuance charge (AdvanceDelete) and settling charges (advance cascade).
- Leader dashboard development groups now show Opening / Receipts / Closing (and Target / Progress); export
  updated to match.
- Fix: outstanding_advances_total (Staff advances receivable on the Statement of Financial Position) now
  excludes top-ups dated after the as-of date, keeping both sides of amount-less-settled as of the same date.
- Sign-in page redesigned: modern split brand/form layout, password show/hide toggle, light theme + system font.
- Tests: leaders/test_batch_v195 (9). leaders 48 / cashbook 180 / reports 190 / core 171 green.
- No migration; run collectstatic on deploy.

## v1.94.0 - leader area overhaul, supporting-docs PDF, light+system defaults, advance Excel import
- Leader dashboard: removed the Top contributors card and the Recent collections/expenses preview cards.
  Sub-accounts table now shows Opening / Receipts / Expenses / Closing for every account.
- Leaders: Collections and Expenses are now dedicated sidebar menus (leader_primary_dept); both pages gain
  a search box and pagination. A date filter + status filter were added to expenses.
- Leader staff-advance page: date filter, search over the statement, paginated statement (25/page),
  mobile-friendly stacked entry form, and a delete button for the leader's own expense lines while the
  period is open and the advance is still pending (not settled/closed).
- Advance Excel import (advance_import): a sample .xlsx download with the key fields, available to the
  treasurer/assistant and the owning department leader. The combined total (amount + charge) may not exceed
  the advance's remaining balance - an over-budget file is rejected in full.
- Expense Register: new "Supporting documents" export (export=support-pdf) building a single PDF with a
  voucher summary page per expense followed by its attachments (PDFs merged, images drawn, unsupported/
  missing handled gracefully). Requires reportlab + pypdf on the server; degrades with a message if absent.
- Appearance: UserPreference theme default LIGHT and font_family default SYSTEM (migration core 0041). The
  anonymous/login page now explicitly renders light + system font.
- Info: petty-cash top-ups and staff-advance top-ups do NOT post journal entries; they are reflected in the
  derived cash/float balances, not the general ledger.
- Tests: leaders/test_batch_v194 (13), cashbook/test_supporting_pdf (6). leaders 39 / cashbook 180 /
  core 171 / reports 190 green.
- Deploy: pip install -r requirements.txt (adds reportlab, pypdf), then migrate (core 0041), collectstatic.

## v1.93.0 - fund opening column, payables tabs, settings/sidebar persistence, advance top-up double-entry
- Fund ledger sub-accounts table now has an Opening column (opening + receipts - payments = closing),
  with matching totals rows and the subgroups export updated to include Opening.
- Payables page reorganised into Payables / Accruals / Prepayments tabs; the active tab is kept in the URL
  hash so it survives a refresh.
- Settings page: the active tab is carried through the save round-trip via ?tab=, so saving no longer
  bounces you back to the first tab (also applied to the SMS/email/assistant test buttons). Sidebar scroll
  position is remembered across page loads via sessionStorage.
- Advance top-ups now record a true double entry against the source: a petty-funded top-up appears as a
  dated outflow in the petty-cash register and reduces the float (the base advance line now shows only the
  base amount so top-ups are not double counted). Added a treasurer-only reverse action
  (advance_topup_reverse) that removes the top-up, decrements the advance total and restores the source.
- Tests: cashbook/test_batch_v193 (10). cashbook 174 / reports 190 / core 171 green.
- No migration; run collectstatic on deploy.

## v1.92.0 - expense ID sort/export, edit-charge fix, net-asset rename, RTF export, debit pill, section insights
- Expenses list ordered by expense ID (was date); ID shown in the table and added to the Excel/CSV export.
- Editing an expense now syncs the linked transaction-charge entry: creates it if newly added, updates it
  in place if present (never duplicates), deletes it if cleared. The charge is prefilled on the edit form.
  (Previously the charge was ignored entirely on edit.)
- Renamed net-asset classes across report, exports and financial position: Unallocated -> General net
  assets; Allocated -> Designated development funds.
- New RTF export for the Monthly Treasurer's Report (report_board_rtf, "RTF / Pages" button). RTF opens
  natively in Apple Pages, Word, LibreOffice and Google Docs.
- Per-section trend insights on the Monthly Treasurer's Report (rule-based, LLM-enriched when enabled):
  a line or two under collections, trust trend, financial position and cash flow.
- Dashboard: split the single "to allocate" pill into a giving pill (-> review queue) and a bank-debits
  pill (-> debit queue). Bank-statement debits now have their own notification linking to the right queue.
- Tests: reports/test_batch_v192 (11); updated test_board_batch_v191 for renamed labels.
- No migration; run collectstatic on deploy.

## v1.91.0 - Monthly Treasurer's Report polish + font fixes
- Fixed the month filter: an <input type="month"> submits "YYYY-MM" but the view parsed only full ISO
  dates, so it silently fell back to the current month. Now accepts YYYY-MM and YYYY-MM-DD. This also
  resolves the "SoFP shows no items" impression (the fallback month often had little data).
- Camp goals table: removed the Type column (report, Excel and Word exports).
- Added a compact Statement of changes in net assets (opening + surplus/(deficit) = closing) to the
  report and both exports; a full standalone version already exists at /reports/changes-net-assets/.
- Narration labels no longer forced uppercase; report uses the selected --font-body/--font-display.
- Dashboard/report cards: replaced hardcoded "Fraunces" with var(--font-display) on card headers,
  report-card titles and empty-state text, so the Appearance font preference applies to them (item 4).
- Clarifying note on net-asset classification: unallocated = general local funds not earmarked for a
  project; allocated = board-designated Development funds (item 2 reconfirmed and documented in-report).
- Local funds statement already sorted by closing balance (total) descending; confirmed.
- Fixed a latent bug in BoardReportView (FixedAsset.objects.filter(active=True) -> disposed=False) that
  was silently zeroing property NBV there.
- Tests: reports/test_board_batch_v191 (9). reports 179 / cashbook 164 / core 171 green.
- No migration; run collectstatic on deploy.

## v1.90.0 - richer backup + assistant, reconciliation clean-up, remittance reminder verified
- Backup Excel (full_excel_export_response): added Payments, Staff Advances, Remittances, Fund Transfers,
  Payables, Accruals, Pledges, Fixed Assets and Petty Cash Top-ups sheets (now 19 sheets total). All
  guarded so a missing model/field never breaks the export.
- Assistant LLM context (_data_context): enriched with this-month/last-month collections, income by
  channel, tithe YTD, trust remittance compliance + unreceipted, latest bank reconciliation status,
  unpresented payments, pledges, active member count, and top expense categories YTD.
- Bank reconciliation: _sync_managed_recon_items now also runs at reconciliation creation (not only on
  detail view), and each managed amount is computed defensively so one failure can't block the others.
  Removed the redundant Petty cash / Staff advances / Unpresented cheques informational panels and the
  manual "add" button — all three are auto-populated into the reconciliation statement.
- Verified (no code change needed): trust-remittance to_remit counts only RECEIPTED trust money, so
  unreceipted trust never raises the reminder, and it clears once remitted (aggregate cache is busted on
  Expense/RemittanceBatch save). Locked in with regression tests.
- Tests: core/test_batch_v190 (6); updated cashbook/test_cheque_register recon-wiring for the auto
  aggregate. 564 green. No migration; run collectstatic on deploy.

## v1.89.0 - nav follow-ups (People/Funds split, remittance in Banking, setup consolidated) + pay-at-entry
- Split "People & funds" into "People" (Members, Pledges, Campaigns) and "Funds & setup" (Funds &
  departments, Fund transfers, Budgeting, Fixed assets, Allocation rules, Development-group patterns).
  dev_patterns was previously unreachable from any nav menu; it now has a home.
- Moved "Trust remittance" (remittance_dashboard) from Reports into Banking, next to Payment register,
  since it's an operational workflow rather than a report. Remittance calendar stays in Reports.
- Updated core/context_processors.py breadcrumb map (_BREADCRUMBS) for all moved items.
- New: issue a payment instrument directly from expense entry. ExpenseCreate gained an optional "Issue a
  payment now" section (method/reference/date/bank account); on save it creates a linked, ISSUED
  PaymentInstrument (posts no journal entries, same as the existing framework) and approves the expense.
  Available whenever the expense will be approved (auto-approve orgs, or any treasurer, who implicitly
  self-approves by issuing payment) — hidden with an explanatory note otherwise. Expense detail page now
  shows the linked payment or a prefilled "Issue a payment" link into the register.
- Tests: cashbook/test_expense_entry_payment (7), core/test_nav_reorg (5). 558 green.
- No migration required; run collectstatic on deploy (templates/CSS only, no schema change).

## v1.88.0 - navigation & UX audit
- Breadcrumbs: new core.context_processors.breadcrumb maps url_name -> (section, page); base.html renders a
  Home / Section / Page trail on every mapped page, styled in app.css.
- Renamed for clarity/consistency: Giving "Ledger" -> "Transactions"; "Ask the books" -> "Assistant";
  Accounting "Ledger check" -> "Ledger integrity"; Reports "Board report" -> "Monthly Treasurer's Report"
  (matches the page title). Basic report_monthly removed from the sidebar (kept in the reports index as
  "Fund movement summary"); report index card "Bank reconciliation" -> "Reconciliation summary".
- Bug fix: report_reconciliation (ReconciliationView) crashed with TypeError when book_balance was None.
- Duplicate removed: report_board was listed twice in the reports index (executive pane card removed).
- Current page highlights (active class) verified after renames; parent nav group auto-opens and its
  summary highlights via :has(a.active). Quick-add "+ New" and Ctrl+K palette retained near the top.
- Tests: core/test_nav_audit (7). Full nav crawl: 48/48 links OK. 323 green across core/reports.
- No migration required (nav/template/context-processor/CSS only); run collectstatic on deploy.

## v1.87.0 - board report exports/goals, budget goals, appearance, reconciliation, filters
- #1 Monthly Treasurer's Report: added Camp Meeting goal records (expense + offering, fund-level, never
  group), income-vs-expenditure and fund-composition charts, and Excel (openpyxl, multi-sheet) + Word
  (Word-compatible HTML, no server library) downloads. New _camp_goal_records helper; export views/urls.
- #2 Fund budget page: offering goal shown only for CAMP_EXPENSE funds (cleared on save for others); each
  development group has its own contribution_goal with per-group progress; aggregate total row.
- #3 Appearance: font preference now drives --font-display so headings, church name and logo use it;
  added sidebar_style preference (core 0040: Forest/Midnight/Brass/Charcoal) with live apply.
- #4 Bank reconciliation auto-includes petty-cash float, outstanding staff advances AND unpresented
  cheques via _sync_managed_recon_items; removed the manual add_petty_cash / add_advances /
  add_unpresented_cheques actions and buttons.
- #5 Pagination preserves the current query string (Django 5.2 {% querystring %}) across all lists, so
  filters persist between pages.
- Tests: reports/test_board_exports (5), cashbook/test_budget_goals_v2 (7), statements/test_auto_recon (4);
  recon + fund-budget tests updated. 539 green.

## v1.86.0 - unified legacy remittance onto the PaymentInstrument workflow
- RemitTrustView (/reports/remittance/remit/) reworked: instead of embedding a cheque number in each
  expense, it now creates a RemittanceBatch (status REMITTED), raises the per-fund remittance expenses
  against it, and settles the whole batch with one generic PaymentInstrument (method/reference/date/bank
  account). The instrument posts no journal entries; clearing only flips status. Single payment
  architecture for both batch and one-step remittances.
- Remittance report form replaced cheque_no/cheque_date inputs with method + reference + date + bank
  account; RemittanceView passes bank_accounts. Legacy cheque fields kept in step for the CHEQUE method.
- Data migration 0029: back-fills historical standalone remittance expenses (REMITTANCE category, cheque
  voucher_no, no batch) by grouping them per cheque into batches and creating matching PaymentInstruments,
  so all historical remittances share the unified settlement architecture.
- Tests: reports/test_legacy_remit_payment (6). 370 green across cashbook/reports/statements.

## v1.85.0 - Payment Register route + remittance settlement workflow
- Renamed /cheques/ -> /payments/ (names payment_register / payment_outstanding / payment_print);
  /cheques/ paths kept as permanent (301) redirects for backward compatibility. Templates renamed to
  payment_*.html; UI labelled "Payment register" consistently; added to the Banking nav group.
- RemittanceBatch.payment FK (cashbook 0027) -> PaymentInstrument: the generic settlement record (method,
  reference, date, bank account, status, cleared date). Legacy cheque_no/cheque_date retained but
  superseded; existing values migrated into PaymentInstruments and linked (cashbook 0028).
- Remittance workflow now: Draft -> Approve -> Issue payment instrument -> (linked) -> Mark sent -> await
  clearance -> Cleared. RemittanceBatchRemitView refuses to mark a batch sent until an issued payment is
  linked (batch.is_settled). New RemittanceBatchIssuePaymentView issues + links the instrument; it posts
  no journal entries (the batch expenses already account for the liability). Clearing only flips status.
- Batch detail page: settlement-payment card, issue-payment form (method/reference/date/bank account),
  5-step wizard (selected/approved/issued/sent/cleared), and clearance guidance.
- Tests: reports/test_remittance_payment (6) + route/label coverage; payment tests repointed to /payments/.
  364 green across cashbook/reports/statements.

## v1.84.0 - payment-instrument framework (cheque register rework)
- New PaymentInstrument model (cashbook 0025) + PaymentAttachment: generic payment framework supporting
  CHEQUE, EFT, RTGS, MPESA, CASH and OTHER methods. Existing ChequeRegister rows ported over (cashbook
  0026); the legacy model is retained read-only for history.
- Every payment references its source obligation (Expense / RemittanceBatch / ExpenseRefund / FundTransfer)
  via typed FKs; clean() enforces a source unless it is an explicitly manual/supplier payment, which the
  view gates on treasurer rights.
- Accounting integrity: a payment instrument posts NO journal entries — the source already accounts for it.
  Issuing a cheque against a trust remittance settles that obligation; clearing during reconciliation only
  changes status. Verified by tests that assert journal-entry counts are unchanged on issue and clear.
- Lifecycle: Draft -> Approved -> Issued -> Outstanding -> Cleared, plus Voided / Stopped. Cleared
  instruments are immutable (is_locked) — edit/delete blocked; void or reverse instead.
- Dual signatories, approval (approved_by/at), cheque printing with amount-in-words (/cheques/<id>/print/),
  outstanding-payments report with Excel/CSV (/cheques/outstanding/), and bank-reconciliation integration
  repointed to PaymentInstrument (unpresented_cheques_total + ReconciliationDetailView + add_unpresented).
- Tests: cashbook/test_payment_instrument (11) + rewritten test_cheque_register (4). 537 green across
  cashbook/statements/ledger/reports/core.

## v1.83.1 - stable goal-type identifier (no name matching)
- Department.goal_type (departments 0019): classifies a fund's annual goal as a general goal or the Camp
  Meeting Expense goal, replacing the previous fund-name match in the board report and budget page. Labels
  now follow goal_type and the configured offering_fund link, so renaming a fund no longer changes the
  report. Goal type is set on the fund budget page's Edit goals form.
- Tests updated to set goal_type; added rename-resilience coverage.

## v1.83.0 - Camp Meeting goals, board report sections & settings, chart of accounts
- #3 Fund budget page: Camp Meeting Expense goal (Local) now aggregates collections across the fund and
  all its sub-groups; renamed from "Camp Meeting Goal (Year)" to "Camp Meeting Expense Goal". A separate
  Camp Meeting Offering goal (Trust fund) is configured on the same page and tracked independently — the
  two totals are never merged. Group Contribution goal shows each group's own sub-account collection.
  New Department.offering_fund / offering_goal (departments 0018).
- #3 Board report: new "Goals and targets" section with target, collected, variance and completion %,
  covering expense, offering and contribution goals, kept separate.
- #4 BoardReportSettingsView (/reports/board-settings/): choose which sections appear, drag to reorder,
  and add report notes (SiteConfig.board_config, core 0039). Board report rewritten to render sections in
  configured order with sentence-case headings, clearer hierarchy/spacing and print-ready styling.
- #5 Chart of accounts expanded with standard church accounts: petty cash, mobile money, staff advances,
  prepayments, other receivables, accumulated depreciation, accruals, payables, statutory deductions,
  designated/restricted funds, opening-balance equity, and interest / fundraising / donations income.
- Tests: reports/test_board_goals (6); fund-budget tests updated for the renamed goal fields. 611 green.

## v1.82.0 - transfer editing, expense refunds, fonts, balancing, dev-patterns, rule lifecycle
- #1 TransferEdit (/transfers/<id>/edit/): editing re-syncs balances, journals (post_save signal) and
  history; is_locked guard blocks reversed/reversal/locked-period transfers; Edit button on the list.
- #2 ExpenseRefund (cashbook 0024): contra-entry preserving the original expense; net_amount /
  refundable_balance; netted into fund_balance + expenses_by_department (date-aware, effective-only);
  post_refund ledger posting + signals + rebuild; petty-float restore; refund UI on the expense detail.
- #6 UserPreference.font_family (core 0038): per-user body typeface (Public Sans / System / Serif /
  Atkinson Hyperlegible / Mono), applied live via data-fontfamily and CSS --font-body.
- #7 _balanced_partition rewritten: size-capped greedy seed + local-search swaps balance both capability
  and member count; documents the inherent skew limit; spread cut markedly.
- #8 DevGroupPattern (giving 0019/0020, seeded defaults): configurable dev-group regexes with a manager
  page (add/edit/enable/disable/delete), regex validation, capture-group check, live tester; allocate()
  uses cached configured patterns (signal-invalidated) with a built-in fallback.
- #9 AllocationRule lifecycle (giving 0021): archived/archived_at + is_expired; archived rules excluded
  from allocation; active/expired/archived views; bulk archive-expired; archive/restore/permanent-delete;
  archive_expired_rules management command with a grace period for nightly cron.
- Tests: cashbook/test_transfer_refund (8), giving/test_patterns_lifecycle (13). 633 tests green.

## v1.81.0 - dev-group builder: download-first, live apply opt-in
- DevGroupBuilderView now exports the balanced proposal to Excel/CSV (group, member, phone, capability)
  via ?export=xlsx|csv, including member phone numbers where present; with no group count it exports the
  member list by capability.
- New SiteConfig.dev_group_builder_apply (core 0037, default False): the live "create groups & reassign
  members" action is disabled by default and gated on this flag; POST is blocked with a message when off.
  Toggle added to Settings → Channels (allocation card). The preview hides the create form when off.
- Tests: core/test_rights_batch updated (apply requires the setting) + download-only test.

## v1.80.0 - delegated rights, balanced dev-group builder, assistant + settings
- #1 removed the duplicate "Allocation & categories" (allocation rules) card from settings; the dev-group
  prefixes / numbered-fund-families card is retained.
- #4 three new assignable rights in core.rights: allocate_dev_offering, manage_advances, build_dev_groups
  (granted to Treasurer always; allocate/advances also to Assistant by default). Helpers in core.roles;
  AdvanceAccessMixin gates the advance views on manage_advances.
- #3 DevGroupUnassignedView now requires the allocate_dev_offering right (treasurers included); a leader
  granted it sees a sidebar "Allocate dev offering" item.
- #7 DevGroupBuilderView (/reports/dev-groups/build/): generate N groups balanced by members' historical
  development giving (greedy longest-processing-time partition); preview + create; gated on build_dev_groups.
  Buttons added to the dev-groups page.
- #5 a petty-cash-funded advance's sending charge is now paid_from_petty_cash (reduces the float too).
- #6 assistant: new staff-advances and petty-cash intents, enriched data context, refreshed suggestions.
- Tests: core/test_rights_batch (9); settings test updated for the removed card.

## v1.79.0 - advance refinements, by-member fund view, recon export, global search
- #9 sending charge no longer reduces the advance (church cost; posts to the fund, not linked via the
  advance FK). #7 per-line transaction charges when recording an expense DO reduce the advance. #4 an
  expense+charge can't exceed the advance balance. #3 AdvanceTopUp (cashbook 0023) to add cash to an
  open advance; petty timing + statement updated. #6 leaders can edit only their own expense lines (not
  the advance); #10 leaders attach receipts/M-Pesa messages to lines, no delete. #8 advance deletable
  only with no expenses.
- #11 FundMembersView (/reports/fund/<id>/members/): giving rolled up across the fund + all sub-accounts
  grouped by member, with Excel/CSV; buttons added to the fund ledger (original page retained).
- #5 closing a parent fund cascades to its sub-accounts (zero-balance guard on each); sub-accounts get
  their own close/reopen; reopen keeps parent/sub consistent; the main department list shows ACTIVE only
  (closed/archived live on /departments/historical/).
- #1 reconciliation: Excel/CSV export, print-only statement with a print header, diagnostic + management
  panels hidden from print, column alignment fixes.
- #2 GlobalSearchView (/search/): command palette now also searches members, funds, staff advances,
  expenses and receipts, merged under grouped headers with sublabels.
- Tests: departments/test_close_cascade (+FundMembers/ReconExport/GlobalSearch); advance tests updated.

## v1.78.0 - charge reduces advance + auto-populated reconciliation
- Bank/M-Pesa charge on an advance now REDUCES the advance: _sync_advance_charge() links the BANK_CHARGE
  expense via the `advance` FK, so it counts toward settled_total and lowers the balance to account for
  (rationale: advance sent to a personal account, holder incurs charges while spending). Shows in the
  advance statement. Petty float unaffected (charge is bank-paid, not petty-flagged); SoFP still ties
  (fund balance and advance receivable both drop by the charge).
- Bank reconciliation auto-populates the petty-cash float and outstanding bank-funded staff advances:
  _sync_managed_recon_items() upserts both as ADD items on view (for data-entry users), updates their
  amount as values change, and removes them when zero. New ReconciliationItem.auto flag (statements 0009);
  auto items can't be hand-deleted (show an 'auto' marker). Manual add_petty_cash/add_advances actions
  retained but the panels now show 'added automatically'.
- AdvanceDelete fixed for the new charge link (detach charge_expense before cascading adv.expenses).
- Tests: cashbook/test_advance_charges_edit updated (charge reduces advance) + AutoReconAndChargeTests.

## v1.77.0 - advance charges + edit/delete, recon advances, leader UX
- #1 StaffAdvance.bank_charge + charge_expense (cashbook 0022); _sync_advance_charge() books/updates/
  removes a BANK_CHARGE expense (excluded from settled_total). AdvanceCreate captures it.
- #3 AdvanceEdit + AdvanceDelete + apply_advance_edit() (end-to-end: charge re-synced, petty float
  recomputed, settling/charge expenses cascade on delete). Leaders may correct an OPEN advance via the
  leader detail page; closed advances are treasurer-only to amend.
- #2 outstanding_bank_advances_total() + recon 'add_advances' action + panel: bank-funded advances are
  added back as a reconciling item (cash out of bank, not yet expensed). Petty advances already sit in
  the petty-cash float, so they're excluded here. (Answer: petty = already accounted; bank = now added.)
- #6 petty-cash register shows petty-funded advance issuance (out) and returns (in); model simplified so
  the box loses the full advance at issuance (settling expenses no longer petty-flagged) and the register
  reconciles to _petty_balance_asof.
- #4 leader sidebar: 'Staff advances' menu item; single-department leaders redirect straight to their
  department (?stay=1 keeps the overview); button removed; nav label singular when one dept.
- #5 leader department page: gradient hero header, one-line KPI values, advances summary in Explore.
- #7 .kpi-grid .stat .value stays on one line (no cent wrap).
- #8 executive overview: 'Staff advances outstanding' + 'Petty cash remaining' tiles.
- Tests: cashbook/test_advance_charges_edit (10); leader tests updated for the single-dept redirect.

## v1.76.0 - staff advances: petty-cash funding + leader self-service
- StaffAdvance gains from_petty_cash + returned_to_petty (cashbook 0021) and helpers settled_asof(),
  accounted_total, petty_outstanding_asof(), balance now nets returned cash.
- Issuing an advance from petty cash reduces the float: _petty_balance_asof() subtracts each petty
  advance's outstanding (issued - settled - returned); settling expenses against it are flagged
  paid_from_petty_cash, so the float stays exact (advance-out reclassifies to spent, net zero).
  AdvanceCreate validates the float can cover a petty advance.
- Settling expenses are now APPROVED+PAID (paid_date set). Shared helper _record_advance_expense().
- Leaders: new LeaderAdvancesView (/leader/advances/) + LeaderAdvanceDetailView with a statement and an
  add-expense form, strictly scoped to departments_led_by(); claimant = the leader's name. Link added on
  the leader dashboard. The leader area stays read-only everywhere else.
- Advance detail now shows a running statement (issued -> settling lines -> still-to-account) and the
  petty-cash source; AdvanceClose captures surplus returned to petty cash.
- Financial-statement impact verified: the SoFP reclassifies the advance within assets (cash_on_hand +
  receivable), so totals tie regardless of source; only settling expenses hit I&E/cash-flow; the petty
  float + bank reconciliation reflect a petty advance as cash physically out of the box.
- Tests: cashbook/test_advance_petty_leader (10).

## v1.75.0 - Appearance & Preferences (per-user workspace customisation)
- New core.UserPreference model (OneToOne with User, migration core 0036): theme, accent (+custom),
  sidebar mode, font size, layout width, card style, dashboard widget order/visibility, landing page,
  rows-per-page, table density, table_state, high_contrast, reduced_motion, large_targets,
  focus_indicators, toasts_enabled, toast_duration, desktop_notifications. Helpers: get_for(),
  accent_hex, merged_widgets(), visible_widget_keys(), reset_to_defaults().
- Exposed app-wide via core.context_processors (prefs) and applied on <html> as data-* attributes +
  --pref-accent; all rendering handled in CSS (dark theme, accent override via color-mix, sidebar
  modes, font/width/cards, density, high-contrast, reduced-motion, large-targets, focus toggle).
- PreferencesView (/preferences/) with tabbed UI + UserPreferenceForm; PreferenceUpdateView
  (/preferences/update/) JSON endpoint persists each change live; static/js/preferences.js applies
  changes to <html> instantly and auto-saves (segmented controls, accent swatches/custom picker,
  toggles, selects, number inputs, drag-and-drop widget reorder). Reset-to-defaults via POST.
- Landing page: PostLoginRedirectView (/after-login/) honours pref; LOGIN_REDIRECT_URL -> after_login.
- Dashboard widgets: DashboardView exposes widget_visible/widget_order; dashboard.html wraps sections
  (attention/kpis/sabbath/charts/funds/trend/recent) in .dash-widgets with show/hide guards + CSS order
  (DOM-safe; charts use IDs). 
- Toasts: configurable toast system in base.html (window.toast), flash messages render as toasts when
  enabled (honouring duration + reduced motion + optional desktop Notification).
- Tables: PrefPaginationMixin (core.utils) honours rows_per_page on Transaction/Expense/Member lists;
  density applied globally via data-density.
- Links from the user menu and the Settings page. Tests: core/test_preferences (13).

## v1.74.0 - app-wide UX/UI & accessibility polish
All changes live in the shared design system (base.html + app.css + form mixin), so they apply across
every page without touching individual templates or any business logic.
- A11y: skip-to-content link + focusable <main id="main">; sr-only utility; ARIA labels on the menu
  toggle, search box (with aria-keyshortcuts) and notification bell; flash messages now sit in an
  aria-live region with role=alert/status; form widgets emit aria-required. Darkened --muted (#7a8a83 ->
  #677770) for WCAG-AA contrast on secondary text.
- Feedback states: global HTMX top loading bar (htmx:beforeRequest/afterRequest); success/info flashes
  auto-dismiss after 6s and all flashes get a dismiss (x) button; busy spinner state for submit buttons.
- Responsiveness: any data table not already wrapped is auto-wrapped in a horizontal scroll container
  (.table-scroll) so wide tables no longer break mobile layouts.
- Integrity/UX: double-submit guard marks the triggering button busy and blocks repeat submissions
  (skips GET forms, htmx, and cancelled confirm() dialogs) - prevents accidental double-posting.
- Tests: core/test_ux_a11y.

## v1.73.0 - configurable LCB departments + dashboard tile overflow fix
- SiteConfig.lcb_departments M2M (core 0035) + picker in Settings -> Channels -> Allocation & categories
  (local funds only, checkboxes). reports/services/treasurer _lcb_dept_ids/_lcb_depts use the configured
  set expanded to include sub-accounts (children), falling back to name matching when unconfigured.
  departments.lcb_fund() also honours the config (first selected dept).
- Dashboard: .stat .value now uses clamp() font-size with overflow-wrap so long values (e.g. Total
  receipts) no longer spill over the tile. (collectstatic on deploy.)
- Tests: reports/test_lcb_config.

## v1.72.0 - receipt archive (#2) + monthly treasurer report rework (#6)
- #2: ExpenseAttachment.file upload_to is now a callable (expense_receipt_path) filing by the expense's
  INCURRED year/month (cashbook 0020). New ReceiptArchiveView (/expenses/receipts/) groups receipts by
  month for printing, with a ZIP download of a period (organised by year/month + index.txt). Link added
  to the expense list.
- #6: monthly treasurer report reworked. (c) trust + LCB trends now 3 months (current + previous two).
  (d) all LCB accounts listed via name match (_lcb_depts), new ones appear automatically. (e) five-year
  trend rendered as a vendored Chart.js bar chart (yearly_json). (f) LCB expenditure fixed - was matching
  the wrong 'LCB Departments' fund via lcb_fund(); now aggregates all LCB-named departments
  (_lcb_dept_ids). (g) removed 'Local funds (with activity)' and the income & expenditure statement.
  (h) new local_funds_statement (opening/receipts/expenses/closing). (i) full SoFP (trust receipted/
  unreceipted split, advances, prepaid, pending, unallocated/allocated), full cash-flow (operating/
  investing), and full reconciliation (bank/adjusted/book/difference) mirroring the main reports.
- Tests: cashbook/test_receipt_archive, reports/test_treasurer_rework (+ updated test_item_batch &
  test_report_fixes for the new section names).

## v1.71.0 - reconciliation polish + petty cash + feed tile (#1,#3,#4,#5)
- #1: reconciliation_detail redesigned - KPI summary strip (bank/adjusted/book/difference+status),
  reconciling items grouped into Add/Less sections.
- #5: ReconciliationDetailView add_petty_cash action adds the petty-cash float (via _petty_balance_asof)
  as a CASH_AT_HAND reconciling item (ADD), idempotent; suggestion panel explains it isn't double-counted.
- #3: PettyCashView gains ?export=csv/xlsx + ui/period_selector.html; download buttons added.
- #4: DashboardView exposes live_balance (latest_cleared_balance from the CBS feed); dashboard.html
  shows a 'Bank balance (live feed)' stat tile when a feed balance exists.
- Tests: statements/test_recon_pettycash.

## v1.70.0 - report fixes & polish (#1-#8)
- #1: fixed broken card structure on /ledger/reconciliation/ (orphaned divs); wrapped equation +
  fund-vs-GL tables in proper cards; added eq.net to accounting_equation().
- #2: Excel/CSV export on JournalView (?export=) and ReconciliationReportView (?export=), with buttons.
- #3: MonthlyTreasurerReportView is now report-form & detailed - masthead, per-fund collections detail,
  itemised income statement (revenue lines + expense categories), sign-off block.
- #4: financial position trust payable now split via trust_summary - receipted (to_remit) vs
  not-yet-receipted (closing remainder); pending bank receipts shown separately as suspense; balance
  sheet still ties.
- #5: Historical data reachable from Reports index (card) and an Annual summary header button.
- #6: income statement section headings changed from BLOCK CAPS to normal case.
- #7: historical page - each year expands to show its months with per-month delete and a 'delete all
  YEAR data' action (HistoricalMonth.month_label added; delete_year_all action).
- #8: shared ui/period_selector.html (start/end + month/quarter/year presets) added to the income
  statement and changes-in-net-assets reports; parse_period gained ?period= presets.
- Tests: reports/test_report_fixes (+ updated test_item_batch wording).

## v1.69.0 - monthly historical records (A) + SoFP clarity (B) + monthly treasurer's report (C)
- ITEM A: HistoricalYearManageView extended with per-month records, Excel import + sample download
  (?sample=1), and automatic yearly-total recomputation from months (_recompute_year). HistoricalMonth
  model already existed.
- ITEM B: financial_position splits 'Trust funds payable' into receipted (trust dept closings) vs
  not-yet-receipted (pending bank suspense), adds trust_total_payable to context, and adds plain-language
  explanations of unallocated (general) vs allocated (Board-designated) net assets.
- ITEM C: new reports/services/treasurer.py + MonthlyTreasurerReportView at /reports/board/ (old board
  report kept at /board-classic/). 10 compact sections: collections summary; trust receipted 4-month
  trend; LCB sub-account 4-month trend; 5-year YTD trend (live actuals blended with monthly history);
  LCB expenses by category; local funds (sorted); income statement; financial position; cash-flow
  statement; latest reconciliation. Each has a one-line note; an AI headline (via _llm_call) with a
  rule-based fallback. New compact/printable template.
- Tests: reports/test_item_batch.

## v1.68.0 - report accuracy: bank position, cash flows, duplicate detection (#11-#14)
- #11: StatementOfCashFlowsView operating bucket = total non-remittance - capital, so the three
  sections always sum to total expenses and the statement reconciles even with untyped expenses.
  Financial-position identity verified (assets == liabilities + net assets).
- #12: BankPositionView subtracts bank-method PAID expenses NOT linked to a bank_transaction (avoids
  double-counting linked ones); new statements.services.importer.latest_cleared_balance() surfaces the
  real-time CBS feed balance with a difference line.
- #13: _duplicate_offerings collapses split siblings (shared core_ref base / mpesa_ref+date / ref+date)
  into one gift, so split halves aren't flagged as duplicates of each other or the receipting envelope.
- #14: bank+envelope duplicate detection now requires the two entries to fall within window_days (7)
  of each other instead of merely the same month - removes coincidental same-amount false positives.
- Tests: core/test_duplicate_logic, reports/test_position_reports.

## v1.67.0 - envelope collapse (#7) + campaign delete (#8) + dev-group SMS (#9) + rule edit (#10)
- #7: each Sabbath's envelope table is a collapsed <details> (head/totals/actions stay visible;
  auto-opens when a Sabbath filter is active).
- #8: CampaignDeleteView (/pledges/campaigns/<pk>/delete/), treasurer-only, blocked if the campaign has
  pledges; delete button on campaign detail. (Transfers already reverse via FundTransfer.reverse.)
- #9: DevGroupSmsView (/dev-groups/sms/ and /dev-groups/<pk>/sms/) sends a templated SMS
  ({name}/{group}/{church}) to dev-group members (all or one); buttons on the funds list.
- #10: RuleEditView (/rules/<pk>/edit/) + Edit button on the rules list; an 'Allocation & categories'
  card added to Settings -> Channels linking to the rules manager.
- Tests: departments/test_batch_b.

## v1.66.0 - safety & audit hardening (#1-#6)
- #1: DebitResolveView.post calls block_if_locked(txn.date) up front — debits can no longer post
  expenses/transfers into a locked period.
- #2: ExpenseApprove reject sets new Expense.rejected_by (cashbook 0019) and no longer sets approved_by.
- #4: rejecting an expense sends a 'REJECTION' notification to the original submitter (optional note).
- #3: new core.utils.log_exception(); a 'treasury' logger added to LOGGING (-> console + error_file).
  Broad excepts across cashbook/giving/departments/members/pledges/assets/core/statements/reports views
  + allocation/matching services now log a full traceback before showing the generic message (32 sites).
- #5: htmx vendored to static/vendor/htmx.min.js (1.9.12) and served locally; removed the unpkg CDN
  <script>. Run collectstatic on deploy.
- #6: _block_if_locked deduplicated — single core.utils.block_if_locked, imported by giving & cashbook.
- Tests: cashbook/test_safety_fixes.

## v1.65.0 - collection accounts + lifecycle (#1,#2) + cheque register (#3) + pending-receipts fix (#4)
- #1: Department.collection_only — receives income but excluded from expense pickers (save() forces
  show_in_expenses off). ConsolidateView (/departments/<pk>/consolidate/) creates one FundTransfer per
  non-zero sub-account into the parent in a single atomic op; children zero, history preserved.
- #2: Department.status (ACTIVE/CLOSED/ARCHIVED; save() derives active). DepartmentStatusLog audit
  trail. CloseAccountView (guards zero balance via fund_balance), ArchiveAccountView, ReopenAccountView,
  HistoricalAccountsView. Closed/archived excluded from income/expense pickers (active=False) but stay
  in reports. Department migration 0017.
- #3: ChequeRegister model (cashbook 0018) + ChequeRegisterView (/cheques/): add/clear/bounce/cancel/
  reopen, sync from CHEQUE-method expenses & cheque remittances. unpresented_cheques_total() wired into
  the bank reconciliation (lists unpresented cheques as at the statement date + one-click 'add as items').
- #4: pending_receipts_total excludes bank credits receipted via envelope (processed_via_envelope /
  manual_receipt / excluded_from_income) so they no longer appear as 'Receipts Pending Allocation'.
- Tests: departments/test_collection_lifecycle, cashbook/test_cheque_register, reports/test_pending_receipts.

## v1.64.0 - period-correct SoFP settlement (#4) + thank-contributors SMS (#5)
- #4: open_payables_total/open_accruals_total are period-based when given an as-of date — an item is a
  liability if incurred on/before the date and either not settled or settled after it (settled_on > as_of).
  Fixes the SoFP showing an item as paid when it was settled a day after the statement date.
- #5: new FundThankSmsView (/reports/fund/<pk>/thank-sms/) + button on the fund report. Lumps each
  member's confirmed giving to the fund AND its sub-accounts over the selected period, skips members
  with no phone, and sends a customizable templated SMS ({name}/{amount}/{fund}/{period}/{church}) via
  the existing SMS service; treasurer-only to send, read-access preview. Tests:
  cashbook/test_period_settlement, reports/test_thank_sms.

## v1.63.0 - fund cards include sub-accounts (#1) + debit->petty cash (#2) + delete recurring (#3)
- #1: FundLedgerView computes combined_opening/combined_receipts/combined_closing (parent + sub-accounts
  + dev groups); the top cards show these with an 'incl. sub-accounts' note when sub-accounts exist.
- #2: DebitResolveView gains a 'petty_cash' kind that records a PettyCashTopUp from the bank debit
  (moves bank->cash on hand, not booked as an expense); option added to the debits form.
- #3: new RecurringDelete view/URL + Delete button on the recurring list; generated expenses are kept.
  Tests: reports/test_fund_combined, giving/test_debit_petty, cashbook/test_recurring_delete.

## v1.62.0 - off-site backup storage (#5)
- #5: new SiteConfig.offsite_backup_enabled/url/user/password (migration core 0034). New
  backup.upload_offsite() does a dependency-free authenticated HTTPS PUT (WebDAV/Nextcloud/object
  stores). backup_db command gains --offsite (and auto-uploads when enabled); a "Send a backup
  off-site now" button (OffsiteBackupNowView /backup/offsite-now/) uploads an encrypted copy on
  demand. Backup emails now use the configured SiteConfig SMTP connection (so the port-465 fix
  applies). Settings -> Backup gains the off-site fields. Tests: core/test_offsite_backup.

## v1.61.0 - cash flow forecasting (#6) + executive forecast & pledges KPIs (#7)
- #6: new core/services/forecast.py projects cash position over 30/91/365 days from a 6-month giving
  run-rate, scheduled recurring expenses (precise due dates) + a discretionary spend run-rate, and
  outstanding pledge installments. New CashFlowForecastView (/reports/forecast/) with a Chart.js line
  chart and a per-horizon breakdown; linked from the reports index.
- #7: the existing executive overview already covered giving-this-month, budget compliance, department
  performance, giving trends and pie/bar/trend charts; added a Cash flow forecast section (30d/quarter/
  year projected positions) and an Outstanding pledges figure. Tests: core/test_forecast.

## v1.60.0 - payables/accruals/prepayments CRUD + link-existing settle (#1) + pledge delete (#4)
- #1: edit/delete for Payable, Accrual, Prepayment via _ObligationEditView/_ObligationDeleteView;
  settled payables/accruals are read-only and undeletable. New SettleAgainstExpenseView
  (/payables/<kind>/<pk>/settle-existing/) links an already-entered, unlinked expense to a
  payable/accrual and marks it settled without creating a second expense. New templates
  obligation_edit.html + settle_existing.html; action buttons added to accruals.html.
- #4: new treasurer-only PledgeDeleteView (/pledges/<pk>/delete/); PledgePayment links cascade but
  the underlying contributions remain in the ledger. Delete button on pledge detail; is_treasurer/
  can_enter_data added to PledgeDetailView context. Tests: cashbook/test_obligation_crud,
  pledges/test_pledge_delete.

## v1.59.0 - quarterly/yearly recurring expenses (#2) + tag-aware update check (#3)
- #2: RecurringExpense.Frequency gains QUARTERLY and YEARLY (frequency max_length 8->10; migration
  cashbook 0017). due_dates() generalised to step by 1/3/12 months anchored to the start month.
- #3: updates.latest_release() now falls back to the GitHub tags API (newest by semver) when no
  published Release exists, fixing 'Latest release seen (none)' for a tag-only repo. New _fetch_json
  helper; diagnostics updated. Tests: cashbook/test_recurring_freq, core/test_update_check.

## v1.58.0 - itemised camp budgets (#1) + email SSL fix (#2)
- #1: BudgetLine reworked from category-keyed to named items (new `name`; `category` now optional/
  informational; unique per department/year/name; migration cashbook 0016). New Expense.budget_line FK
  (nullable) tags an expense to its budget item. Expense form shows a 'Budget item' picker populated
  per selected fund via new BudgetItemsJSONView (/expenses/budget-items/). FundBudgetView now reports
  budget-vs-actual per item (actuals from tagged expenses) and notes untagged spend. Categories remain
  for overall expense categorisation.
- #2: core/services/email._connection now selects implicit SSL for port 465 (use_ssl) and STARTTLS for
  587 (use_tls); they're mutually exclusive. New SiteConfig.email_use_ssl (auto-enabled for 465;
  migration core 0033), surfaced in Settings -> Email. Fixes SMTPServerDisconnected/timeout on 465.
  Tests: cashbook/test_fund_budget, core/test_email_ssl.

## v1.57.0 - settle via editable expense form (#5) + camp/fund budgets & goals (#7)
- #5: settling a payable/accrual links to the expense form pre-filled (department, description, amount,
  category) via ?settle=payable:N / accrual:N. ExpenseCreate gained _settle_target/get_initial/
  get_context_data and a form_valid hook that, after saving (including any charge), marks the
  obligation settled and links settled_expense. The payables page settle buttons are now GET links;
  the form shows a banner. Old POST settle endpoints remain (unused).
- #7: new cashbook.BudgetLine (department, year, category, amount, note; unique per dept/year/category;
  migration cashbook 0015). New Department.contribution_goal and Department.year_goal (editable;
  migration departments 0016). FundBudgetView at /reports/fund/<pk>/budget/ shows per-category
  budget-vs-actual for a year and two goal cards (contribution goal + yearly goal) tracked against
  collected receipts, with forms to edit goals and add/update budget lines. Linked from the fund
  ledger. Tests: cashbook/test_settle_form, cashbook/test_fund_budget.

## v1.56.0 - bank-feed balance card, audit log filters/download, faster executive
- #1: BankFeedLogView extracts the latest ClearedBalance from event payloads (case-insensitive,
  nested-safe) and shows it as a card; each row can expand its pretty-printed raw JSON payload.
- #2: AuditLogView rewritten with search (q), filters (model, change type +/~/-, user, date range),
  pagination (50/page) and CSV export; user/model lists drive the dropdowns.
- #4: ExecutiveDashboardView no longer runs health.anomalies() (slow); added fast dashboard.quick_facts()
  (top fund this month, top spend category, givers this month, largest single gift) shown in an
  'At a glance' card. Tests: statements/test_feed_log, reports/test_audit_log, core/test_executive_facts.

## v1.55.0 - profile rights on leader pages + faster, smarter Controls duplicates
- #3: leader views called mask_phone() directly, bypassing the rights system, so a profile granting
  view_member_phone_full had no effect. All leader phone/identity output now goes through
  display_phone()/new display_giver(). Leaders keep seeing giver names by default (added to LEADER
  group rights) but phones stay masked unless a profile grants the right; a profile can also withhold
  identity. Fixed a NameError by threading `user` through _collection_rows().
- #6: ControlsView no longer computes duplicates on load (~887 queries -> ~24); _duplicate_expenses
  and _duplicate_offerings now run on demand via ControlsDuplicatesView (HTMX "Run check" buttons,
  /controls/check/<kind>/). _duplicate_offerings rewritten: no longer flags a shared allocation
  reference (distinct bank gifts each have a unique receipt); flags same giver+amount counted on
  both bank and envelope within a month, or an envelope re-typed in one Sabbath. Tests:
  core/test_rights_leader, core/test_controls_duplicates.

## v1.54.0 - clickable report links in Telegram replies
- New SiteConfig.site_base_url (migration core 0032), editable under Settings -> Telegram.
- The Telegram assistant formatter turns a report's relative link into a full clickable URL using
  that base; it adds https:// if the scheme is omitted and trims a trailing slash. With no base set,
  replies remain text-only (graceful). Tests: core/test_telegram_links.

## v1.53.0 - recategorise type, simpler leader view, fund sub-account sort + JPEG
- #1: ExpenseRecategorizeView download gains "Current type"/"New type (capital/recurrent)" columns;
  the re-import now updates expenditure_type as well as category (each optional, keyed on the ID).
- #2: leader department detail removes the Chart.js insight charts; the "Recent expenses" card is
  hidden for funds that aren't expense-eligible; the sub-accounts table shows just name + total
  contribution when no subgroup carries expenses; and a JPEG download (with a date/time stamp) is
  offered for the subgroups. expenses_eligible / any_sub_expenses flags added to the context.
- #11: FundLedgerView sorts sub-accounts and dev rows by receipts (descending); the fund report's
  sub-accounts table gains a JPEG download.
- New static/js/table_jpeg.js: a dependency-free table->JPEG export (canvas, no html2canvas/Pillow)
  with title, subtitle and a "current as of" timestamp, used by both the leader and fund pages.
  (Run collectstatic on deploy.) Tests: cashbook/test_recategorize_type, leaders/test_dashboard_simplified,
  reports/test_fund_subaccount_sort.

## v1.52.0 - split-confirm fix, split funds in the queue, smarter Telegram
- #8: AutoAllocationReviewView (the 'require confirmation' screen) silently re-pointed split
  components to the dropdown's first option, because split halves aren't selectable and so weren't
  pre-selected. The picker now always includes each row's current fund (pre-selected), split
  components are shown locked, and the POST never re-points a split component.
- #9: the queue's manual Split rows can now target a split fund (the combo offers them); a split-
  fund part is expanded across its components server-side, so e.g. 600 to Combined Offering becomes
  300 ENF + 300 LCB within the wider split.
- Telegram /balance with no fund now lists every (parent) fund's closing balance with a grand total;
  /balance <fund> still gives the full breakdown. The free-text handler now renders the assistant's
  rows and report link properly.
- Telegram LLM report routing: when the assistant LLM is enabled, free-text questions are first
  classified by the LLM into a known report intent (+fund/period) and routed to that report via the
  existing rule engine; otherwise it falls back to a conversational answer. _llm_call gained an
  optional system-prompt override. Tests: statements/test_split_confirm, giving/test_queue_split_funds.

## v1.51.0 - cash-form dev group requirement + petty cash mirrors the expense form
- #7: CashEntryForm gains a dev_group field, shown on the cash form only when a DEVELOPMENT fund
  is picked (fund search now returns a `dev` flag) and required by clean() — a development gift
  can't be saved without its group.
- #10: petty cash disbursements mirror the expense form — method (cash/bank/M-Pesa/cheque),
  voucher, and an M-Pesa/bank transaction charge (for floats held on M-Pesa/bank). The charge is
  a linked bank-charge expense (charge_for) that is also paid_from_petty_cash, so it reduces the
  float; the float check includes it. A petty-cash disbursement is a normal Expense flagged
  paid_from_petty_cash=True (that flag is exactly what differentiates it and reduces the float).
  The regular expense form gains a "Paid from petty cash" checkbox, and the expense import gains
  a "Paid from petty cash" column (imported petty expenses are recorded as PAID). The manual/import
  charge inherits the parent's petty-cash flag. 7 tests (giving/test_cash_devgroup,
  cashbook/test_petty_charge).

## v1.50.0 - statement dedup, unassigned-page crash, notifications, ledger export column
- #5: reports/dev_unassigned (and the sabbath queue + pledge suggestions) crashed with
  VariableDoesNotExist when a row had no member, because `default:t.member.name` still evaluates
  member.name. Replaced with an explicit {% if %} guard.
- #6: the statement parser took the first '~' segment as the receipt, so mobile/bank-channel
  narrations ("NNNNNN:MBANKING~<REAL RECEIPT> ...") yielded non-unique keys and distinct payments
  could be dropped as duplicates. It now extracts the genuine 10-char M-Pesa receipt code
  (letters+digits) anywhere in the narration, falling back to the first segment only when absent.
- #3: the notifications page now lists only unread items (so they disappear once read) and each
  has a Dismiss action; "Mark all read" empties the list.
- #4: the transactions ledger export (xlsx/csv) gains a "Receipt status" column — Receipted
  (envelope) / Receipted (manual) / Memo (reconciled to envelope) / Not receipted.

## v1.49.0 - split-fund allocation fix + M-Pesa charge on expenses
- Allocation (#1): AllocationRule.reference is not unique, so a reference could have two rules
  (e.g. a stray learned 'remember this' to one account, plus the real split-fund rule). _pick
  now prefers, among rules covering the date: period rules, then an explicit split_fund over a
  bare department, then the newest rule. Pattern matching gets the same split-fund/newest
  tiebreak. So a configured split fund (Combined Offering) is never overridden by an older
  single-account rule (13th Sabbath). Hardening: the legacy importer's split-rule seeding now
  update_or_creates (so it can't be blocked by a pre-existing department rule), and a learned
  department rule clears any stale split_fund. 4 tests in giving/test_split_priority.py.
- Expenses (#2): new Expense.charge_for self-link (migration cashbook 0014). The manual form's
  M-Pesa/bank charge now links the generated bank-charge expense to its parent; the expense
  import template gains an 'M-Pesa charge' column that does the same on import. The expense
  detail page shows linked charge(s) and, on a charge, the expense it was for. 5 tests in
  cashbook/test_mpesa_charge.py.

## v1.48.0 - run allocation rules on the review queue on demand
- giving.services.allocation.reallocate_pending(): re-runs allocate() (+ dev-group token and
  campaign fallback, via the importer's _resolve) over the credits still in the review queue and
  updates each in place when it now resolves to a fund. Skips locked periods and split-fund
  matches; returns a {scanned, allocated, remaining, skipped_locked, skipped_split} summary.
- RunRulesOnQueueView (POST /queue/run-rules/, data-entry right) with a clear result message.
- "Run rules on pending" button added to the review-queue toolbar (shown when there are items).
  Use case: add rules after importing a statement, then clear the matching queued items without
  re-importing the file.
- 5 tests in giving/test_reallocate.py (matching allocated/others left, no-rule no-op, locked-
  period skip, the view, button visibility).

## v1.47.0 - Telegram envelope entry (configurable)
- Bot (#3): new guided /envelope flow in core/services/telegram_bot.py — Sabbath -> member
  (name match; ambiguity prompts; optional new-member creation) -> amount per configured fund
  (0/- to skip) -> optional confirmation -> save. Records via the same envelopes.views._save_envelope
  used by the web ledger, so it posts ENVELOPE-channel income and flows into reconciliation/reports.
  Respects locked periods (entry_blocked) and attributes the entry to the signed-in user (personal
  PIN), behind the existing PIN gate.
- Parameters on Settings -> Telegram (SiteConfig, migration core 0031):
  telegram_envelope_enabled, telegram_allow_new_member, telegram_envelope_confirm,
  telegram_envelope_channel (cash/bank) and telegram_envelope_funds (which funds are offered;
  empty = active top-level funds). Surfaced on the settings page; saved with the config form.
- 9 tests in core/test_telegram_envelope.py: full flow, skip-fund, new-member gating on/off,
  feature disabled, locked-period block, confirm-off immediate save, PIN-required, attribution.

## v1.46.0 - executive/controls speed-ups, aggregate caching, query-regression guards
- Controls (#2): _duplicate_expenses grouped expenses by service_sabbath_for(), which queried
  SiteConfig + closed-Sabbath rules per row (~8,000 queries on 4k expenses). It now groups by the
  pure natural Sabbath (sabbath_of, no DB) — correct for dedup and 1 query. Controls: ~887 q /
  4.8s -> 29 q / 77 ms.
- Executive (#2): health.anomalies() did a per-expense fund-average query and also invoked the
  expensive dedup; fund averages are now computed once and the dedup fix carries through.
  Executive: ~670 q / 5.1s -> ~239 q / 325 ms.
- Caching (#1): core.perfcache caches department_summary/trust_summary keyed by a global data
  version that is bumped on any Transaction/Expense/RemittanceBatch/FundTransfer write, with a
  TTL backstop. Off by default (DASHBOARD_CACHE_TTL=0); set DJANGO_DASH_CACHE_TTL=60 in prod.
- Regression guards (#1): core/test_performance.py asserts the hot pages stay under a query
  ceiling on a seeded dataset (catches N+1 regressions) plus cache hit/bust/off-by-default tests.

## v1.45.0 - performance at high volume
- Expenses list: eliminated an N+1 (a per-row `attachments.exists()` query). The receipt
  indicator is now an annotated Count in the main query — measured 66 -> 16 queries on a
  50-row page over 5,000 expenses.
- Member list: added a database index on `name` (migration members 0004) so the default
  name-ordered listing and search don't sort-scan at tens of thousands of members.
- Audited the hot paths on an 18,142-transaction / 5,042-expense / 4,010-member dataset:
  transactions (16 q), members (13 q), dashboard (52 bounded aggregate q, ~87 ms), review queue,
  audit log, fund ledger, trust, reports — all query-light with no N+1. The transactions page's
  one-off ~400 ms first hit was template/app warmup (38 ms warm); no code change needed.

## v1.44.2 - error monitoring, email config, log files
- Logging: server errors (django.request / django.security) now go to a rotating file
  (logs/treasury-errors.log, 5x5MB; dir configurable via DJANGO_LOG_DIR) and to an
  AdminEmailHandler that emails ADMINS on 500s when configured (no-op until set, so nothing
  breaks by default).
- Email: configurable via DJANGO_EMAIL_HOST/PORT/USER/PASSWORD/TLS, DJANGO_FROM_EMAIL,
  DJANGO_SERVER_EMAIL and DJANGO_ADMINS; defaults to the console backend when no SMTP is set so
  the app and the backup emailer degrade gracefully. Also wires DEFAULT_FROM_EMAIL/SERVER_EMAIL.
- Optional Sentry: set SENTRY_DSN (and optionally SENTRY_TRACES/SENTRY_ENV) to enable; guarded
  import means a missing sentry-sdk never breaks startup.
- (The encrypted, rotated, off-site backup_db cron command was already present — documented in
  its module docstring.)

## v1.44.1 - audit fixes & hardening
- Security: dashboard/report chart JSON is now emitted through a safe_json() helper that escapes
  <, >, & and line separators, so user-set fund/member names can't break out of the <script>
  block (low-severity stored-XSS hardening; dashboards are staff-only).
- Stability: the in-app Telegram poller no longer starts (or queries the DB) during `check`,
  `showmigrations`, `sqlmigrate` or `createsuperuser` — removes a DB-access-at-init warning.
- Cleanup: removed a redundant cumulative-receipts query in trust_summary (no behaviour change).
- Tests: pledge matching tests pin an explicit pledge start_date so they no longer depend on the
  current date.

## v1.44.0 - configurable profiles & rights (layered on roles)
- core/rights.py: a catalogue of granular rights (data entry, money controls, setup, reports,
  sensitive data) and resolution layered on the role groups — superuser = all; a user with
  assigned profiles is bound by the union of those profiles (can restrict); a user with none
  falls back to their role group's implied rights (full backward compatibility).
- accounts.Profile model (name, description, rights JSON, users M2M, is_system). Migration
  accounts 0003 + 0004 (four default profiles mirroring the role groups).
- Profiles management page (/profiles/) — create/edit/delete profiles, tick rights grouped by
  area, assign users. Gated by the manage_profiles right. Nav link beside Users & roles.
- Phone masking: member phone numbers are shown full only to viewers with view_member_phone_full
  (treasurer/assistant/auditor groups keep it by default); otherwise masked (e.g. *********678)
  in the member list, member detail, duplicates, the member-search typeahead and envelope ledger.
- RightRequiredMixin + has_right() + context `rights`/`can`/`phone_full` for further wiring.
- 16 new tests covering rights resolution, masking, profile CRUD/assignment and backward compat.

## v1.43.0 - asset cost from expenses: idempotent, itemised, reclass-aware
- Accumulate (#1): AssetAccumulateView now only picks up capital expenses not already linked to
  an asset (capitalized_asset is null), links them, and adds their sum to the cost — so clicking
  twice can't double-count. The asset detail page lists every expense included in the cost with a
  linked total.
- Reclassify/delete (#3): cashbook signals keep the cost honest — reclassifying a linked expense
  to recurrent (or unlinking, reducing its amount, or deleting it) reduces the linked asset's cost
  by the right amount. A recurrent expense can never stay attached to an asset.
- Legacy importer (#2): creates a single "Church building" construction-in-progress asset and
  capitalises every development/construction expense onto it (expenditure_type=CAPITAL,
  capitalized_asset set); the building's cost is set to the sum of those expenses.
- Backup workbook Trust Funds + Summary sheets now show outstanding-to-remit (receipted) and
  unreceipted (pending) separately, consistent with the on-screen trust reports.

## v1.42.0 - trust receipted/unreceipted split, construction asset, ledger autocomplete fix, budget quarter
- Trust (#1): trust_summary now splits cumulative trust receipts by whether a formal receipt was
  issued (envelope channel or manual_receipt). `to_remit` = opening + receipted − remitted (the
  firm liability due to the field); new `unreceipted` line = confirmed trust money with no receipt
  yet (still a liability, held off remittance); `total_liability` = to_remit + unreceipted.
  Surfaced on the Trust Fund Report, Remittance advice, Conference submission export, remittance
  dashboard and main dashboard. Remittance batches (which use to_remit) therefore only remit
  receipted money. Tests updated to the new policy.
- Assets (#2): new FixedAsset category "Construction in progress" that never depreciates (Land
  also corrected to not depreciate); NBV = accumulated cost. AssetAccumulateView totals CAPITAL
  expenses (approved/paid) on a chosen fund over any date range — including prior years — to set
  or add the asset's cost; manual cost editing remains. Migration assets 0003.
- Envelope ledger (#3): name autocomplete dropdown was being clipped by the scrolling table
  wrapper (overflow:auto). The suggestion box is now position:fixed, positioned from the input,
  and hidden on scroll/resize.
- Budget (#3a, from 1.41 work): BudgetLine.quarter (Q1–Q4) for planned spend timing. Migration
  departments 0015.

## v1.41.0 - budget timing by quarter
- BudgetLine gained an optional `quarter` (Q1–Q4) for the period a fund foresees spending the
  line; surfaced in the budget-lines page (column + add-form dropdown) and carried over by
  "copy prior year". Blank = spread across the year. Migration departments 0015.

## v1.40.0 - subgroup export, structure-import flag, charge traceability, print fit, audit creator
- Fund ledger (#1): sub-accounts table now exports to Excel and CSV (ID, Subgroup, Type,
  Receipts, Payments, Closing) via ?export=subgroups[/-csv]; download buttons on the page.
- Fund structure import (#2): new "Show in expenses (Yes/No)" column (template, parser, apply);
  defaults to Yes, "No" hides the fund from the expense picker.
- Charge traceability (#3): the auto-created transaction-charge expense now references its parent
  ("... [for <voucher / exp #id>]") and copies the parent voucher.
- Offering/Collection summary (#4): prints to a single A4 landscape page — a measured scale
  factor shrinks the sheet to fit when there are many funds/Sabbaths.
- Backup audit (#5): backup workbook adds a "Created by" column (from simple-history's create
  record) to Transactions, Expenses, Members, Departments and Reconciliations. Audit-only — not
  shown anywhere in the UI or on-screen reports. No schema change.

## v1.39.0 - Collections Detail report
- New /reports/collections-detail/ (CollectionsDetailView, PeriodMixin): collections for any
  chosen period broken down by fund, with Trust/Local subtotals and a grand total. Uses the same
  definition as the Collections Summary (confirmed credits, excluded_from_income=False; trust via
  is_trust), so totals reconcile exactly for matching dates. Headline strip shows Collections,
  Trust, Local, Expenditure and Net for the period.
- Excel (.xlsx) and CSV downloads. Linked from the reports index and the Collections Summary page.
- monthly.collections_detail() service added.

## v1.38.1 - campaign fallback splits to subgroups (fix)
- campaign_allocate now returns the matched member's subgroup fund, not the campaign's parent.
  Campaign.subgroup_department() gets-or-creates a child Department named after the member's
  group (e.g. CAMP_1), parented to the campaign's department so it inherits fund_type/is_trust
  and rolls up in trust/local reports. A member with no group still routes to the parent fund;
  a trigger match with no member still routes to the parent for review.
- Updated CampaignFallbackTests to assert subgroup routing + parent fallback.

## v1.38.0 - campaigns polish + smart bulk buttons
- Campaigns (#1): redesigned page (clean create form + campaigns table with per-row member
  upload). New "Sample upload file" download (Name, Mobile, Group). Import is now tolerant —
  numeric phone cells handled, bad/empty rows skipped and counted, no abort on a single bad row.
- Phone overflow fix: CampaignMember.save() stores only a normalised 12-digit phone (or blank),
  so the import can never raise DataError 1406 ("Data too long for column 'phone'").
- Expenses (#2) & Transactions (#3): bulk action buttons moved into the filter toolbar beside
  "Apply filters" (via the form= attribute), disabled by default and enabled only when the
  selected rows include items eligible for that action (Approve↔PENDING, Reject↔PENDING/APPROVED,
  Pay↔APPROVED, Delete↔any; Reverse↔any reversible row).

## v1.37.0 - transactions list bulk reverse
- TransactionBulkReverseView reverses several selected ledger entries at once (contra
  postings, never hard delete; linked envelope receipts removed and their siblings reversed).
  Locked-period and already-reversed/reversal rows are skipped and counted.
- Transactions list gains row checkboxes + select-all + a "Reverse selected" bar; the per-row
  Reverse button is removed (Edit / Split / Receipt / cash Delete stay per row).

## v1.36.0 - expenses bulk actions + ledger/backup IDs
- Expenses (#2): row checkboxes + select-all and one action bar (Approve / Reject / Mark paid /
  Delete) via ExpenseBulkActionView; per-row buttons removed, Edit kept. Each item is guarded
  the same as the single action (locked periods and dual-approval-needed items are skipped and
  counted, not errored).
- Fund-ledger export (#4): added ID and Type (Receipt / Expense / Transfer) columns so every
  line is traceable to its source row.
- Backup workbook (#5): Transactions, Expenses and Reconciliations sheets now lead with the
  database ID (Departments/Members already did); money-column indexes shifted accordingly.

## v1.35.0 - campaign fallback allocation
- New Campaign + CampaignMember models (giving). A Campaign has a fund (department), a set of
  comma/line-separated trigger words, and an active flag; members carry name/phone/group.
- giving.services.allocation.campaign_allocate runs ONLY after the normal allocate() misses:
  if the reference contains a campaign trigger word, the payer is matched by phone (or a
  unique name) to a campaign member and the credit is allocated to the campaign's fund and
  tagged with the member's group (AUTO); trigger-but-no-member routes to the fund as REVIEW.
- Wired into both the file importer and the live CBS feed (ingest_event); Transaction gains
  campaign (SET_NULL) + campaign_group so the group is reportable and survives campaign delete.
- UI at /campaigns/: create/update a campaign, upload its Name/Mobile/Group sheet (.xlsx/.csv),
  delete a finished campaign (members removed; past allocations keep their group tag). Nav link
  added. Regression tests cover trigger gating, phone/name matching, no-member review, inactive.
- Migration: giving 0018.

## v1.34.3 - CBS webhook token auth hardening
- CbsEventWebhookView TOKEN auth now accepts the shared token whether the bank sends it as a
  bare Authorization header, with a Bearer/Token scheme, or via X-Auth-Token / X-Api-Key /
  Api-Key / Token headers, and compares it in constant time (hmac.compare_digest).
- Confirmed the feed allocates incoming credits via the same allocate() rules as the
  statement importer (member match, split funds, dev-group tag, dedup, confirmation gating).

## v1.34.2 - mark-receipted now memos the bank credit (fixes inflated collections)
- Transaction.mark_manual_receipt now, for BANK credits, also sets excluded_from_income=True
  and nulls the department (the legacy "Processed via envelope" memo) when marking, and
  re-includes on un-mark. Previously it only set the manual_receipt flag, so under the new
  income-from-envelope model the credit stayed as income and double-counted the envelope
  it duplicated - inflating the dashboard and collections summary.
- This fixes all three callers at once: the bulk MarkProcessedImportView, the per-credit
  toggle, and receipt-one-bank's "mark only" paper-receipt path.
- The exclusion applies even when the credit was already flagged manual_receipt, so re-running
  the bulk mark-processed file settles credits marked before this fix.
- Full suite (458 tests) green; no migrations.

## v1.34.1 - cash count + report consistency for the legacy model
- Cash count (_breakdown): a BANK envelope now posts an ENVELOPE-channel transaction, but
  that is bank money, not physical cash. The count now excludes ENVELOPE transactions that
  belong to a bank-channel envelope (in both the cash total and the duplicate-matching
  heuristic), so the float still balances.
- Income reports that don't group by department now exclude the receipted bank-credit memos
  (excluded_from_income): income_by_channel, giving_by_group, offering_summary, tithe_total,
  dev_group_progress. Department-grouped reports already self-correct because a memo'd credit
  has department=None.
- Verified consistent (counted once) across: dashboard, collections summary, trust report,
  member statement, income-by-channel and the cash count. Full suite (479 tests) green.

## v1.34.0 - legacy accounting model: envelope is income, bank credit is a memo
- `_save_envelope` now posts an income transaction for BANK envelopes too (previously only
  cash), so the envelope is the income for all giving, matching the legacy import's
  phase_envelopes.
- Sabbath reconciliation INVERTED to match legacy: applying a match / marking a credit
  receipted now excludes the BANK CREDIT from income and nulls its department (the legacy
  "Processed via envelope" memo) - it no longer excludes the envelope's transaction. The
  envelope keeps its income, so the gift is counted once.
- reconcile_sabbath status is now "receipted" (excluded memo) vs "income" (still counted);
  a matched pair whose credit is still income is flagged as the double-count to clear, and
  `balanced` means no such double-count remains.
- _reverse_envelope re-includes a memo'd credit (clears excluded_from_income) on undo.
- New regression test locks the invariant: bank envelope + matching credit = double until
  receipted, then counted once (income AND fund balance). Full suite green.

## v1.33.0 - reconciliation status actions (mark receipted, cash->bank)
- ReconcileApplyView accepts two new pairing-free actions: `mark_receipted` (sets a bank
  credit and its split siblings to manual_receipt=True as a confirmation, no envelope link,
  no ledger change) and `to_bank` (reclassifies a cash envelope to bank and excludes its
  ENVELOPE-channel transaction from income to avoid overstating).
- reconcile_sabbath flags matched pairs as `miscat` when the bank credit is unreceipted but
  the envelope was entered as cash (the double-count case), and returns `miscat_count`.
- Unmatched bank table gains per-credit "mark receipted" checkboxes; the success message
  reports linked/receipted/moved counts separately.

## v1.32.1 - trust_reconcile accuracy for reconciled-and-excluded lines
- An envelope line whose transaction is excluded_from_income but whose envelope is linked
  to a bank credit (env.bank_transaction) is no longer reported as "offering but not
  collections" - the bank credit is the ledger entry and is already counted in collections.

## v1.32.0 - shared-name reconciliation match + receipt-only apply
- reconcile_sabbath suggestions now include a shared-name-token rule: within one amount,
  a name token (e.g. a first name) carried by exactly one remaining bank credit and one
  remaining envelope is suggested ("ADAM KEN" <-> "ADAM NYAN" when there is only one Adam
  of that amount). Suggestions are de-duplicated so no credit/envelope appears twice.
- ReconcileApplyView now marks the matched bank credit (and split siblings) as receipted
  (processed_via_envelope) WITHOUT changing the ledger: the credit stays as income and no
  envelope transaction is created. The existing duplicate-cash exclusion still applies only
  when a cash envelope is being reclassified as bank.

## v1.31.0 - smarter Sabbath reconciliation matching
- reconcile_sabbath auto-match is now conservative: it pairs a bank credit to an envelope
  only when the name+amount match is unambiguous (exactly one candidate on each side), so
  duplicates (two givers of the same amount, repeated names) are never mis-paired and are
  left for manual resolution.
- New unique-amount suggestions: any amount that appears exactly once among the remaining
  bank credits and exactly once among the remaining envelopes is surfaced as a suggested
  match (even when names differ), each confirmable with one tick. Returned as `suggestions`
  (list); the single-suggestion field is kept for compatibility.
- The reconciliation remains a detector/suggester only — it never posts a second ledger
  entry; hand-typed bank envelopes stay the offering record and the imported bank credit
  stays the income.

## v1.30.1 - trust_reconcile accuracy: respect env.bank_transaction
- The diagnostic previously counted any envelope line with no line-level transaction as
  "no ledger transaction", even when its envelope was linked to the imported bank credit
  (env.bank_transaction) — overstating the orphan figure. It now treats those as
  reconciled (the bank credit is the ledger entry) on both sides of the comparison.

## v1.30.0 - statement purge window extended to a week
- The statement-import Purge / Unlink-and-purge buttons now remain available for a
  week after upload instead of only the same day (StatementImport.can_purge, mirroring
  the bank-reconciliation delete window). All existing safety checks are unchanged:
  refuses inside a locked period or when expenses are linked (unless unlink is chosen).

## v1.29.0 - undo envelope entries (bulk reversal)
- New EnvelopeReversalView (/envelopes/reverse/, treasurer only): filter envelopes by
  Sabbath date and optional channel, preview the count/total, and reverse the batch
  with a confirm. Mirrors the bank statement import undo and respects locked periods.
- Reversal logic extracted into a shared _reverse_envelope helper used by both the
  single-envelope delete and the bulk reversal: it removes the ENVELOPE-channel ledger
  entries a cash envelope created, and for bank envelopes unlinks (keeps) the real bank
  deposit and clears its processed_via_envelope flag so it returns to the receipt queue.
- "Undo entries" link added to the envelope list for treasurers.

## v1.28.1 — revert bank-envelope ledger entry (keep diagnostic)
- Reverted v1.28.0: bank envelopes no longer create their own ledger transaction.
  Creating one risked counting the same gift twice once the bank statement (the real
  source of that money) is imported. _save_envelope is back to its prior behaviour and
  the backfill command is removed.
- Kept: the trust_reconcile management command.

## v1.28.0 — bank envelopes reach the ledger (trust/collections discrepancy)
- Root cause (found via trust_reconcile): manually-entered BANK envelopes created an
  envelope line with no ledger transaction, so the money appeared in the offering
  summary but never reached the cash book / collections / general ledger — the entire
  trust gap was these orphan lines.
- envelopes _save_envelope now creates one ENVELOPE-channel transaction per line for
  bank envelopes too (matching cash), so the money always reaches the ledger. To
  receipt money already imported from the bank statement, use the receipt-as-envelope
  action on that transaction (it links to the existing credit, so nothing doubles).
- New command backfill_envelope_transactions (report, or --fix) creates and links the
  missing transaction for existing orphan envelope lines. Run trust_reconcile first to
  confirm the orphan total, then rebuild the ledger after backfilling.

## v1.27.1 — trust reconciliation diagnostic
- New management command trust_reconcile <year> <month> reconciles the Offering
  Summary trust total (envelope lines, by Sabbath) against the Collections Summary
  trust total (transactions, by date) and itemises the difference: envelope lines
  with no ledger transaction, lines whose transaction is excluded or dates to
  another month, and trust collected with no envelope line or counted on another
  month's Sabbath. Both reports already use the same is_trust classification, so
  this isolates timing/data differences from genuine errors.

## v1.27.0 — reconciliation delete/recompute + split-fund allocation guard
- Bank reconciliations can be deleted within a week of creation (treasurer only,
  with a confirm). Older worksheets are protected. Reconciliations do not post to
  the ledger, so deletion is safe.
- Reconciliation detail: a one-click "Recompute from ledger" button refreshes a
  stale cash-book balance to the current figure as of the statement date, and the
  manual "Update book balance" now confirms when it saves.
- Allocation-rule form: the fund picker now lists only directly-allocatable funds,
  excluding the internal halves of a split offering, so a rule cannot send split
  giving entirely to one component. Rules should target the split fund itself.
  Also fixed unreachable validation in the rule form (the not-both-targets and
  date-range checks now run).

## v1.26.0 — trust classification single source of truth
Trust vs local was read from two places: the authoritative fund_type field (reports,
balance engine) and a cached is_trust flag (general ledger posting, envelope summary,
some pickers). If the two drifted — a bulk update or import that bypassed save() —
trust money could post to an income account instead of the trust liability, the
reports and the envelope summary disagreed, and the reconciliation couldn't balance.
- The general ledger now classifies trust strictly by fund_type (single _is_trust
  helper), so the ledger and the balance engine can never disagree. Once a fund's
  Fund Type is correct, every figure agrees and the reconciliation balances.
- New command, audit_funds, reports any fund whose Fund Type and envelope-summary
  classification disagree, and repairs in the direction you confirm:
    audit_funds                # report only
    audit_funds --from-cache   # trust the envelope summary: set Fund Type from it
    audit_funds --fix          # trust the Fund Type settings: set the cache from it
  No classification is changed automatically — you choose which source is correct.
- Regression test pins that a trust credit posts to the trust liability even if the
  cache is stale.
After repairing, rebuild the general ledger (Ledger check -> Rebuild) so existing
entries re-post under the corrected classification.

## v1.25.2 — backup authentication & ledger date filter
- Database backup/restore: the dump and restore tools now authenticate via a
  temporary [client] defaults file over TCP to the same host the application uses.
  Previously they passed -h localhost, which the command-line client treats as a
  Unix socket and can be denied even when the app connects fine — the cause of the
  'Access denied for user ... when trying to connect' error. They now also prefer
  the modern mariadb-dump/mariadb tools (clearing the deprecation notice) and drop
  options that need privileges shared-hosting users usually lack (--routines,
  tablespaces). Credentials are written to a 0600 temp file and deleted immediately.
- Ledger date filter: From/To dates are now parsed into real date objects before
  filtering (more reliable across database drivers) and malformed values are
  ignored instead of raising, so the filter always applies cleanly.

## v1.25.1 — one-click ledger rebuild from the Ledger check
When any fund does not tie to the general ledger, the Ledger check overview now
shows a clear explanation and a Rebuild button (treasurers only; others get a note
to ask a treasurer). This is the direct fix for an entry that is counted by a fund
but missing from the general ledger — it now both surfaces on the overview and is
fixable in one click, without drilling into each fund. Template-only change.

## v1.25.0 — summary reconciliation, amount search, accurate assistant
- Envelope/Offering summary: funds that received giving directly are now always
  listed, even if they also have sub-accounts (e.g. VBS). Previously such direct
  giving was silently dropped, so the summary total did not match the envelopes
  counted for the Sabbath. Both the per-Sabbath statement and the monthly summary
  are fixed; funds with no direct giving still do not appear.
- Ledger search: the search box now also matches by amount (type 1250 or 1,250.50)
  and by M-Pesa / bank receipt code, alongside name and reference.
- Assistant: all collection, tithe, giving, top-giver and development-group figures
  now use the recognised-income basis (confirmed credits, excluding reversed and
  double-counted envelope-twin rows) so they agree with the reports. Added a
  What is new answer that lists recent releases.

## v1.24.0 — wording: gift to contribution
Every user-facing use of the word gift or gifts now reads contribution or
contributions: dashboard, review queue, receipts, leader and department views,
reports, and spreadsheet/CSV export headers. The change is purely wording — no
totals, accounting rules, or behaviour were touched, and the underlying data keys
were left intact so all figures render exactly as before. Includes a no-op field
help-text migration.

## v1.23.0 — Latest Sabbath dashboard snapshot
The executive dashboard now leads with a Latest Sabbath card: the most recent
Sabbath's recognised collection, the change versus the previous Sabbath (up/down),
the number of gifts and envelopes recorded, and the top funds for that Sabbath. It
uses the same recognised-income basis as every other report (confirmed credits,
excluding the envelope-twin rows) so it never double-counts, and it is built from
grouped queries. Shown only when there is data for the latest Sabbath.

## v1.22.0 — keyboard-friendly entry & mobile receipting grid
Weekly envelope receipting grid:
- Spreadsheet-style keyboard navigation — Up/Down arrows move between rows in a
  column (Enter still moves down and adds a row at the bottom); the focused cell
  selects its contents so you can overtype immediately. Arrow keys are left alone
  inside dropdowns.
- Mobile/tablet: momentum scrolling, larger touch targets in cells, full-width
  toolbar fields and action buttons, and a two-column fund picker.
Cash and expense entry forms:
- The member, fund and claimant lookups were mouse-only; they are now fully
  keyboard-navigable (Up/Down to highlight, Enter to choose without submitting the
  form, Escape to dismiss), and the cash form lands focused in the first field.
No accounting or posting logic changed; 185 entry-related tests pass.

## v1.21.0 — professional print / PDF output for reports
- A comprehensive print stylesheet: printing any page (or saving to PDF) now hides
  all on-screen chrome — sidebar, top bar, filters, buttons, toolbars, action items
  and the on-screen page header — and lays the document out full width in black on
  white, ink-friendly (no shadows or solid fills; status pills print as outlines).
- Tables repeat their header row on every printed page and never split a row across
  a page break.
- Fix: the new sticky-header scroll caps were undone for printing, so long fund
  ledgers and journals print in full instead of being cut off at one screen.
- Reports now carry a print-only letterhead (church name, report title, period and
  the date/user it was generated) on 18 key reports, and a print-only signature
  block (prepared / checked / approved) on the monthly statement, remittance
  schedule, board report and financial position.
On-screen layout is unchanged — all of this applies only when printing.

## v1.20.0 — final design-system polish
Continued the rollout into the import wizards, executive summary, controls and the
remaining secondary tools. App-wide inline styles fell from ~370 to ~242; of those,
19 are JS-toggled visibility and 18 are dynamic templated values that must stay
inline, leaving ~205 genuine one-offs. (Since the modernization began the codebase
has gone from ~908 inline styles to ~242.) 117 pages verified rendering under
production settings with no failures; no behaviour or accounting logic changed.

## v1.19.0 — design-system rollout across secondary screens
Extended the component/utility adoption from the ten priority screens to the rest of
the app. Repetitive inline styling was replaced with shared utility classes
(merging into existing classes), cutting app-wide inline styles from ~908 at the
start of the sweep to ~370 — the remainder being data-driven values (e.g.
progress-bar widths) and a few genuine one-offs (bespoke backgrounds, JS-toggled
visibility, fixed pixel widths). Notable: settings 64->9, leader department detail
33->6, accruals 39->13, pledge detail 25->8. 117 pages verified rendering under
production settings; no behaviour or accounting logic changed.

## v1.18.0 — UI modernization & component-adoption sweep (part 2 of 2)
Completes the ten-screen sweep begun in 1.17.0.
- Fund Ledger — utilities + sticky running-ledger header.
- Journal Entries — modernized header and sticky headers.
- Bank Reconciliation — status summary rebuilt as stat tiles; the inline total-row
  style moved to the stylesheet; sticky comparison table.
- Contributions / Receipts (weekly receipting ledger + bank-gift receipting) —
  converted to utilities/components; the frozen member-name column behaviour is
  preserved exactly.
Across all ten priority screens, the only inline styles that remain are data-driven
values (e.g. progress-bar widths). Verified: ledger reconciliation, journal balance,
fund balances and the dual-approval gate are all unchanged (129 tests pass).

## v1.17.0 — UI modernization & component-adoption sweep (part 1 of 2)
Shared design-system components (reused across screens): toolbars, alerts/callouts,
filter bars, a responsive KPI grid, sticky table headers, and a set of spacing/layout
utility classes — plus reusable page-header, stat-card and empty-state partials.

Screens rebuilt on the component library (inline styles removed; only data-driven
values like progress-bar widths remain inline):
- Executive Dashboard — responsive KPI tiles, alert-style action items, cleaner charts.
- Transactions list — utilities + sticky headers; filters unchanged.
- Expenses list — utilities + sticky headers; approval/delete actions unchanged.
- Expense detail & approval — rebuilt with components; now shows inline Approve / Reject
  / 2nd-approve / Mark-paid actions that reuse the existing endpoint and enforce the
  same dual-approval threshold (no logic change).
- Pledges dashboard and Reports dashboard — converted to utilities/components.

All accounting behaviour, filters, and the dual-approval gate verified unchanged.
Remaining screens (Contributions/Receipts, Fund Ledger, Journal Entries, Bank
Reconciliation) follow in part 2.

## v1.16.0 — design-system foundation, security hardening & responsive polish
Security & stability
- Production now fails loudly if DJANGO_SECRET_KEY is unset (no more silently
  running on the shipped development key), and warns when ALLOWED_HOSTS is a
  wildcard or TREASURY_ENCRYPTION_KEY is missing (the latter is what previously
  risked locking users out of two-factor if SECRET_KEY rotated). Dev is unchanged.
UI consistency & code quality
- Added reusable template partials (ui/page_header, ui/stat, ui/empty) and a set of
  spacing/layout/text utility classes, so pages can drop ad-hoc inline styles for
  named, consistent ones. Adopted on representative pages to establish the pattern.
Branding & visual polish
- The 403, 404 and 500 pages now share one premium, centred-card design with the
  church brand mark, consistent with the sign-in screen. The 500 page keeps inline
  fallback styling so it still looks right even if the stylesheet cannot load.
Mobile & responsiveness
- Added a defensive rule so any wide data table scrolls horizontally on small
  screens instead of stretching the page (the main ledgers already scrolled).
Tests
- Added a production-mode (DEBUG=False) render guard for the sign-in, error, and
  dashboard pages.

## v1.15.0 — SMS / email one-time codes for two-factor
- Two-factor authentication now offers three delivery methods: authenticator app
  (TOTP, as before), text message (SMS, via the existing Advanta integration), or
  email (via the configured mail server). Each user picks their method when setting
  up two-factor.
- At sign-in, SMS/email users land on a 'code sent to ***' screen with a
  rate-limited resend button. Codes are 6 digits, stored only as a hash, expire
  after 5 minutes, and lock out after 5 wrong attempts. Recovery codes continue to
  work for every method.
- SMS and email options only appear when they are configured (SMS credentials in
  Settings; a mail server for email).

## v1.14.0 — leader sub-group access + performance
- Group leaders: assigning a parent fund now grants its entire sub-tree at any
  depth (CAMP MEETING -> CAMP_1..CAMP_30 and deeper), with drill-down links from
  the leader landing page and department dashboard into each subgroup. A leader can
  still be assigned a single subgroup directly.
- Fixed: a leader assigned only a subgroup no longer sees a blank dashboard (their
  subgroup now heads the list); siblings remain out of scope.
- Performance: removed per-group / per-sub-account query loops that scaled with the
  number of development groups and sub-accounts. Development-group progress, the
  leader dashboard, and the Fund Ledger report now use single grouped queries
  (e.g. 46 groups went from 47 queries to 2), so the dashboard and reports stay
  fast as the CAMP_1..CAMP_30 structure grows.

## v1.13.1 — production constraint fix
- Fixed a MariaDB warning (W036): the unique guard on PledgePayment
  (pledge, transaction) used a condition MariaDB can't create, so on production it
  was silently skipped and the same contribution could be matched to a pledge more
  than once. Replaced with a plain unique constraint that behaves identically on
  SQLite, MariaDB, and Postgres (all treat NULL as distinct), so it blocks
  duplicates while still allowing many manual no-transaction payments.

## v1.13.0 — numbered fund families (easy camp/expense-group routing)
- Added a 'numbered fund family' setting: one line such as
  'expense, exp, expe = CAMP_{n}' routes EXPENSE1 / exp1 / expe1 to the fund named
  CAMP_1, EXPENSE30 to CAMP_30, and so on for all groups — no rule per group.
  Handles narration variations, distinguishes EXPENSE1 from EXPENSE10, and only
  applies when the target fund exists (otherwise the gift goes to review).
- This resolves ahead of the generic development-group prefix matcher so a
  configured family is not intercepted and sent to a development group by mistake.
- The allocation-rules page now points to this instead of per-group regex rules.

## v1.12.0 — period-aware leader insights, fair trend, cash delete
- Leader dashboard: development-group collected figures now respect the selected
  period (previously all-time), in step with the other cards, and a per-period
  group summary can be downloaded as CSV or Excel.
- Multi-year trend now compares January-to-current-month of every year (prior
  years from monthly history, the current year from the live ledger, annual-only
  years pro-rated and flagged), so a part-year is not measured against full years.
- Cash entries page gains delete. A cash entry is the same record as its ledger
  row, so deleting it removes the single entry (split parts together); bank,
  reversed, and envelope-receipted rows are protected, and edits remain at the
  ledger.

## v1.11.0 — leader dashboard revamp
- The department-leader page is now an insights dashboard: headline KPIs (closing
  balance, collections, expenses, net), a monthly collections-vs-expenses chart,
  an income-by-channel breakdown, top contributors, budget-vs-actual and
  pledge-fulfilment cards, and development-group standings with drilldown.
- Added an "Explore" set of quick links and a dedicated, downloadable pledges page
  (CSV/Excel) to sit beside the existing collections and expenses pages.
- All leader views remain strictly read-only and scoped to the leader's own
  departments; contributor phone numbers are masked on detail pages and not shown
  on the overview at all.

## v1.10.2 — two-factor verify page renders in all states
- The 2FA code-entry page is now fully standalone (it no longer extends the main
  layout). It previously went blank when reached while already logged in but not
  yet verified (the middleware path), because the main layout only fills its body
  for verified users. It now renders for fresh logins and re-verification alike.

## v1.10.1 — two-factor sign-in fixes
- The 2FA code-entry page no longer renders blank. It is shown before the user is
  logged in, so it now uses the unauthenticated sign-in layout (the authenticated
  layout suppressed its body, which locked everyone out).
- The enrolment QR code now renders using a pure-Python SVG generator, so it shows
  even though the image library (Pillow) isn't installed on the server.
- A recovery code continues to work directly in the verification box.

## v1.10.0 — importers, regex rules, reconciliation, fixes
- Allocation rules: bulk Excel import (template + review), and a new REGEX match
  type so one rule covers many narration variations like EXPENSE_1 / exp1 / expe1
  for camp/expense groups (items 1, 2).
- Split funds are selectable in the bulk-allocate dropdown and split each gift
  into its parts (item 3).
- Sabbath reconciliation: split-fund bank parts are regrouped into one gift so the
  total matches the single envelope, matched/unmatched envelopes show their fund
  allocation (Tithe, Development, ...), and selected matches can be applied in one
  click to mark them as bank giving (items 1, 4 across releases).
- Expenses: bulk Excel import at /expenses/ with a template, review, and the
  approval setting honoured (item 5).
- Remittance dashboard: recent batches labelled as last 10; a note clarifies that
  Outstanding is the cumulative running balance. The underlying fix makes trust
  'to remit' a true running liability (opening + collected to date - remitted to
  date), so cross-month timing reconciles (items 6, 8).
- Envelope import: an unrecognised fund column is no longer dropped silently — you
  map it to a fund, create one, or ignore it before importing (item 7).
- Fixes: loose cash dated to a closed Sabbath now counts for that Sabbath (not the
  next one); a reset_2fa management command recovers users locked out by an
  encryption-key change (set a stable TREASURY_ENCRYPTION_KEY in .env).

## v1.9.0 — reconciliation apply, statement Sabbath, dashboard refresh, campaign pledge import
- Sabbath reconciliation: a one-click 'apply match' on selected pairs (and the
  singleton suggestion) marks the matched envelope as a bank item, links it to the
  bank gift, and neutralises the duplicate cash income so the money is counted once
  via the bank (item 1).
- Statement import: an optional Sabbath that every entry in the file counts under,
  for imports done later than the Saturday. It takes precedence over the by-date
  assignment and isn't held for confirmation; leave it blank for the current
  per-date behaviour (item 2).
- Dashboard: the local-funds table has a small button to download it as a JPEG
  image (item 3); the 'Giving by group' card is replaced by 'How giving arrives',
  showing the bank / M-Pesa vs cash vs envelope mix with gift counts and shares
  (item 4).
- Pledges: an Import button on a campaign page loads pledges straight into that
  campaign — no Campaign column needed — reusing the review-and-approve flow, with
  pledges landing as drafts (item 5).

## v1.8.0 — Sabbath reconciliation, leader pages, 2FA fix
- New per-Sabbath reconciliation (Envelopes -> Reconcile Sabbath): lists a
  Sabbath's bank giving (receipted + manual) and the envelopes counted for it,
  matches them by contributor and amount with fuzzy matching to catch misspelt
  manual-receipt names, suggests the last unmatched pair when only one remains on
  each side, excludes cash envelopes from the bank balance, and flags bank entries
  that aren't assigned to any Sabbath (item 1).
- Leaders get detailed pages: a full, downloadable collections list (contributor,
  masked phone, reference, channel, amount), a downloadable expenses list, and a
  development-group drill-down with each group's performance and a downloadable
  per-contributor list — all scoped to the leader's departments and read-only
  (item 2).
- Two-factor authentication: signing in no longer throws a server error when the
  stored authenticator secret can't be read (e.g. after an encryption-key change);
  a recovery code now works directly in the verification box as a second form of
  sign-in, and a broken secret is regenerated on re-enrol (item 3).
- 'Receipt bank giving' can optionally be limited to a single Sabbath; leave the
  date blank to keep the whole-month behaviour (item 4).

## v1.7.0 — queue tools, trust accuracy, cash-count control, error pages
- Review queue: select several gifts and allocate them to one fund at once
  (item 1); a button fetches unallocated gifts sitting in the ledger (no fund,
  not in the queue) back into the queue for allocation (item 5).
- Trust 'to remit' now keys off the authoritative fund type, so a stale flag can
  no longer pull a local fund into the remittance total; a migration re-syncs the
  flag on existing data (item 4).
- Expense form: an entry larger than the fund's available balance is no longer
  silently dropped — a clear notice keeps the entry intact and offers the
  override, so M-Pesa charges and other expenses don't 'disappear' (item 3).
- M-Pesa / bank charges are kept out of duplicate-expense detection even when
  recorded under another category (item 8).
- Possible duplicates are sorted by payer and now include fuzzy near-matches, to
  catch a manual receipt typed with a slightly misspelt name (item 9).
- The allocation rules list is paginated, shows the match type, and drops the
  source column (item 6).
- Friendly 404 / 403 / 500 pages with a way back to the app; the admin can be
  alerted on an unexpected error by email, SMS or WhatsApp (item 2).
- Sabbath cash count reflects physical cash only: a cash-envelope row that
  duplicates a bank gift for the same contributor that Sabbath is excluded from
  the expected total, so the count can balance (item 7).

## v1.6.0 — manual receipts vs system receipts
- Split the single processed-via-envelope flag into two clear states:
  - Manual receipt: the gift was receipted on paper (e.g. a hand-written
    envelope) with no link to the ledger. No system envelope is created, and the
    gift is kept out of BOTH the review queue and the receipt-bank-giving pull so
    it is never receipted again. Reversible — untick manual receipt on the entry
    to make it eligible for a system receipt later.
  - Processed via envelope: a system envelope record exists (it was receipted in
    the app).
- The bulk Mark tool, the per-gift mark-only action, and the entry edit page now
  set the manual-receipt state; all of them cascade across the parts of a split
  gift. The two states show with distinct labels on the ledger.
- A data migration splits existing flags: a previously-processed gift with no
  envelope record becomes a manual receipt; one with an envelope stays a system
  receipt. Income totals are unaffected (the bank entry remains the income).

## v1.5.1 — fix
- Receipt bank giving: the bulk pull now excludes any gift that already has an
  envelope record, not only those flagged processed-via-envelope. Previously, if
  a gift had been receipted but its processed flag was not set (older data, a
  manual envelope, or a partially-receipted split), the pull would receipt it
  again. The single-gift receipt action was hardened the same way, so receipting
  one part of a split can never re-add a part that is already receipted.

## v1.5.0 — fund import, sub-accounts, and queue clearing
- New dedicated fund/department structure importer (Funds and departments ->
  Import funds and sub-accounts). Download a template that lists your existing
  funds, add one row per fund, and set a Parent to make a row a sub-account.
  Parents are created before their sub-accounts so row order does not matter, and
  sub-accounts inherit their parent fund type. Existing funds are never modified.
- The budget import template now comes pre-filled with one row per existing fund
  (with the current year budget as a starting point where set), so you enter
  amounts against funds already in the system instead of typing names.
- Marking a bank entry processed via envelope (in the bulk tool or on the edit
  page) now also removes it from the review queue, and cascades to every part of
  a split gift so the whole gift leaves the queue together.

## v1.4.2 — split funds in bulk mark-processed
- The bulk "mark processed via envelope" tool now understands split offerings.
  A split gift (e.g. Combined Offering) is posted as several ledger rows that
  share the reference with the amount divided across funds. Uploading the
  reference with the TOTAL the member gave now confirms the whole group by its
  sum and marks every part processed together. A wrong total, or a reference that
  matches unrelated rows, is still reported rather than applied.

## v1.4.1 — fixes
- Settings: the SMS card was rendering on every tab (it had slipped outside its
  tab pane); it now shows only under the SMS tab.
- Discoverability: the bulk fund/department import is now linked on the Funds &
  departments page, not only on the budgeting page.
- New bulk tool (Ledger -> Mark processed): for gifts written on a physical
  envelope that also appear on the bank statement. Upload just a reference and an
  amount; the reference finds the bank entry and the amount confirms it is the
  right record. Matched entries are marked processed via envelope — kept out of
  receipting and the review queue so they are not entered twice — without
  creating a duplicate receipt. Amount mismatches and ambiguous or unknown
  references are reported, not applied. The processed status now shows as a badge
  on the ledger.

## v1.4.0 — Department leaders & configurable encryption
- New "Department leader" role: a read-only login scoped to the department(s) a
  leader is assigned. They get their own dashboard showing collections, expenses,
  sub-accounts, development-group progress (for a development leader) and any
  pledges toward their department. Scoping is enforced server-side — a leader
  cannot reach another department or any office screen.
- Privacy: contact phone numbers are masked (e.g. *********678) everywhere a
  leader sees member, payer or pledge data.
- Assign leaders from the user screen: set the role to "Leader" and pick the
  department(s); changing the role away clears the links so access never goes
  stale.
- Configurable encryption: the application-layer key now comes from
  TREASURY_ENCRYPTION_KEY (falling back to SECRET_KEY), encryption can be toggled
  with TREASURY_ENCRYPTION_ENABLED, and a new check_encryption command reports
  status and re-encrypts secrets after a key change (key rotation).
- Pledges and the books are unaffected: all 44 financial-accuracy invariants pass.

## v1.3.0 — Security & oversight
- Automated encrypted backups: a `backup_db` management command for a nightly
  cron job. Dumps the database, encrypts it with the application key, keeps the
  newest N copies (rotating older ones away), and can email the backup off-site.
  See deploy/AUTOMATED_BACKUPS.md. Set the off-site address in Settings.
- Two-factor authentication (TOTP): enrol from the user menu (Security & 2FA)
  with a QR code, then logins require a 6-digit code. One-time recovery codes are
  issued for lost-phone access. A setting can require all treasurers to enrol.
- Dashboard revamp: a single "Needs attention" panel replaces scattered alert
  banners, surfacing — with counts and one-tap links — transactions to allocate,
  expenses and pledges awaiting approval, overdue or soon-due trust remittances,
  overdue pledges, and possible duplicates. Only non-zero items appear.
- Pledges remain informational throughout: none of the above changes how money
  is recognised, and all 44 financial-accuracy invariants still pass.

## v1.2.1
- Treasurer-only bulk pledge import (Pledges -> Import): downloadable template
  with dropdowns; members matched by name or phone and campaigns by name, with a
  review screen to map or create anything unmatched; rows with no campaign can be
  assigned a default. Imported pledges are saved as DRAFTS for approval and, like
  all pledges, never post to the ledger or change a fund balance.

## v1.2.0 — Inline pledge matching + public pledge form
- Inline matching: when a new contribution is recorded (manual entry or statement
  import) from a member who has an active pledge, the system acts per a new
  setting (Settings to Pledges to Pledge matching mode):
    * OFF — do nothing;
    * SUGGEST (default) — flag a likely match for a treasurer to confirm;
    * AUTO — apply the match automatically, capped at the pledge's outstanding.
  Two more parameters: restrict matching to the campaign's target fund, and how
  many days after a pledge's end date a gift may still be matched.
- New match-suggestions review queue (Pledges to Review suggestions) where a
  treasurer confirms or dismisses each flagged match. Confirming links the
  existing contribution to the pledge; it never moves money.
- Optional public pledge link (/pledge/, off by default; enable in Settings to
  Pledges). Members submit a pledge themselves; submissions are held as
  UNVERIFIED DRAFTS for treasurer approval. The form is write-only — it never
  exposes member data, balances, or other pledges — and is guarded by a spam
  honeypot, a submit-rate limit, an amount ceiling, and mandatory manual approval.
- ACCOUNTING unchanged: pledges remain informational. All 44 financial-accuracy
  invariants continue to pass.

## v1.1.0 — Pledge Management
- New module for recording and tracking pledges, integrated with members,
  contributions, SMS/WhatsApp, reporting, security and the audit trail.
- Pledge campaigns (giving drives) with goals, target fund, and progress
  (pledged vs received vs outstanding).
- Member pledges with one-off or recurring (weekly / monthly / quarterly /
  annual) frequencies and an informational installment schedule.
- Approval workflow: an assistant's pledge is a draft a treasurer approves; a
  treasurer's pledge is active immediately. Cancel / reactivate supported.
- Fulfilment by matching real, confirmed contributions to a pledge — one click
  auto-match per pledge, a bulk auto-match sweep, manual match of a specific
  contribution (with split), or a directly-recorded payment. A contribution is
  never matched twice, and auto-match never over-applies past the outstanding
  balance.
- Reminders reuse the existing SMS / WhatsApp services, respect a per-pledge
  opt-out and missing phones, and are logged. Single or batch (per campaign).
- Reports: campaign progress and pledges-by-status, exportable to Excel; plus a
  printable year-end per-member pledge statement.
- ACCOUNTING: pledges are commitments, not income. Nothing in the module posts
  to the general ledger or changes a fund balance — only the matched real
  contribution does, exactly as before. All 44 financial-accuracy invariants
  continue to pass unchanged.

## v1.0.19
- Budgets: a Download template button produces a ready-to-fill spreadsheet with
  one row per planned line (Department, Line item, Category, Amount, Funded by),
  with dropdowns. Re-import it on the Bulk import screen and each department's
  budget becomes the sum of its lines; a line financed by another fund (or from
  the department's own funds) records that funding source.
- Controls: duplicate detection tightened — duplicate expenses are now flagged
  within the same Sabbath (not the whole month); M-Pesa / bank charges are
  excluded; duplicate offerings are only flagged within the SAME channel (so a
  giver who gave once by cash and once by M-Pesa is not flagged); and re-typed
  envelopes (same giver + amount on one Sabbath) are now detected.
- Remittance calendar: generated deadlines default to the 1st of the following
  month; and a period is automatically marked remitted when a completed
  remittance batch covers it.

## v1.0.18
- Names are now stored in a consistent UPPERCASE register everywhere — bank
  imports, manual entry, and envelope entry — via the member, transaction and
  envelope models, so matching and receipts read consistently.
- Expenses: the Type filter is replaced with a Search box (matches description,
  claimant and voucher number).
- Expenses: a new "Re-categorise" route lets you download all expenses, edit only
  the category column offline, and re-import — every other field is left
  untouched, keyed on the expense ID.
- Trust remittance dashboard: instead of "oldest outstanding", it now shows a
  COUNTDOWN to the reporting Sabbath (the Saturday whose count must be remitted),
  driven by the per-month remittance deadlines. Those deadline dates are set
  freely per month on the remittance calendar — they are not assumed to fall on a
  fixed day — and the reporting Sabbath updates automatically when a deadline is
  midweek.
- New Bulk fund & budget import (Budgets - Bulk import): upload a budget workbook
  with a DEPARTMENTS sheet, and the wizard matches each fund to an existing
  department (fuzzy + known synonyms). Anything that does not match is flagged so
  you can map it to a department, create a new fund or sub-group, or skip it.
  Applying writes the per-year budget and an optional Jan-Dec monthly breakdown
  (taken from the projected-expense columns so it ties to the headline).

## v1.0.17
- Ledger (transactions) made more compact: tighter rows, summary strip and
  toolbar, and — the real fix — wide tables now scroll horizontally instead of
  clipping, so the right-hand action buttons (Edit / Split / Reverse / Receipt)
  are always reachable. This overflow fix applies to the Envelopes and Expenses
  tables too.
- The Remittance calendar (trust-fund deadline dates and their reporting
  Sabbaths) is now linked directly in the left navigation under Reports, not only
  on the Reports index — it was already built but hard to find.
- Settings: the "Restore from backup" card no longer appears on every tab — it is
  now correctly scoped to the About tab. The settings tabs are laid out as a
  single tidy row with light separators between the General / Messaging / System
  groups (scrolling horizontally on small screens).

## v1.0.16
- Visual redesign of the three core data screens — Ledger (transactions),
  Envelopes and Expenses — around a single, consistent "workspace" layout so they
  read as one professional product:
  * a ruled page header with title and primary actions;
  * a calm summary strip of metric cards (the lead metric marked with a thin
    brass keyline), replacing the divergent per-page stat/chip styles;
  * a single contained command toolbar grouping all filters with Apply / Clear
    and export actions;
  * refined data tables with tighter rhythm, a subtle brass margin-cursor on
    hover, and clearer numeric treatment;
  * dignified empty states that tell the user what to do next.
  The warm forest-green / brass / parchment identity and the Fraunces + Public
  Sans + IBM Plex Mono type system are preserved throughout. All filters,
  exports, approval actions, bank-receipting and SMS workflows are unchanged.

## v1.0.15
- Extended the financial-accuracy suite (reports/test_accuracy.py) with a second
  layer of 15 edge-case / adversarial tests targeting the real-world conditions
  that cause reconciliation gaps:
  * period-window boundaries are inclusive and adjacent periods neither overlap
    nor leave a gap;
  * unconfirmed receipts and pending (unapproved) expenses never reach a balance;
  * excluded-from-income receipts stay in the fund balance but out of income;
  * split offerings divide to the exact cent with no money lost or created;
  * empty/zero state yields zero totals (never None or error) and still balances;
  * Decimal arithmetic shows no floating-point drift over awkward sums;
  * a mis-keyed far-future value date is excluded by a bounded period window;
  * bank debits correctly reduce the bank position.
  Validated by fault injection. 44 accuracy tests in total (416 across the app).

## v1.0.14
- New financial-accuracy test suite (reports/test_accuracy.py, 29 tests) that
  asserts the accounting invariants the figures depend on, each against a fully
  hand-totalled scenario:
  * departmental balance identity (closing = opening + receipts − expenses
    + transfers in − transfers out) for every fund;
  * carry-forward continuity (a period's opening equals the prior period's
    closing; a split year equals the full year);
  * reconciliation (the fund engine balance equals the general-ledger balance,
    with no variance, and rebuild is idempotent);
  * ledger integrity (every journal entry balances; the trial balance balances;
    Assets = Liabilities + Funds);
  * Statement of Financial Position balances (Total Assets = Total Liabilities
    + Net Assets) with trust-payable equal to unremitted tithe;
  * Statement of Cash Flows reconciles (opening + net change = closing; the
    three categories sum to the net change; capital is investing, not operating);
  * transfers are zero-sum; reversals net to zero; remittances are never income
    or operating expense; receipting a bank gift as an envelope never inflates
    income; and consolidated parents equal own-plus-children.
  The suite was validated by fault injection — deliberately breaking a formula
  makes the relevant tests fail, confirming they genuinely catch errors.

## v1.0.13
- New interactive deployment installer: deploy/install.sh. Collects all settings
  through validated dialog prompts (whiptail/dialog if available, plain prompts
  otherwise — never echoes secrets), then sets up the .env (600 perms), MySQL
  database (utf8mb4), Python venv + migrations + static + superuser, a systemd
  gunicorn service, the Apache proxy include under the domain-owning cPanel user,
  nginx pass-through and AutoSSL, and verifies /healthz/ at each layer. Safe to
  re-run; reuses the existing secret key and backs up the previous .env. See
  deploy/INSTALL.md.

## v1.0.12
- Transactions page redesigned: summary cards (count, receipts, payments, net,
  in-review), a cleaner filter bar with a Clear button, channel colour-coding,
  service-Sabbath hints and payer phone shown inline.
- Fixed a reporting bug where trust remittances were counted as expenses in the
  annual summary and the board-report multi-year trend, overstating expenses for
  prior years. (Operating expense totals now exclude REMITTANCE everywhere, as
  intended — trust funds are liabilities, not expenditure.)
- New Remittance calendar (Reports - Remittance calendar): per-year trust-fund
  remittance deadlines, each mapped to its reporting Sabbath (the most recent
  Saturday on/before the deadline). If a deadline falls midweek, the previous
  Sabbath is the reporting Sabbath. Overdue and due-soon remittances are alerted
  on the dashboard.
- Bank receipting: you can now mark a bank gift as receipted WITHOUT creating a
  new envelope (for when the envelope was already written/typed by hand).
- Bulk bank receipting now lets you optionally set a starting receipt number.
- Settings page reorganised into General / Messaging / System groups with a
  cleaner navigation.

## v1.0.11
- Redesigned the envelope ledger entry screen (Record envelopes) for faster,
  clearer entry: a cleaner toolbar, a live summary bar showing the running
  contributor count, grand total, and per-fund subtotals as you type, a sticky
  column-totals footer row, a clearer Save button showing the total, and an
  inline duplicate-name flag. All existing behaviour (name autocomplete,
  auto-incrementing receipts, keyboard navigation, fund picker, Excel template)
  is preserved.
- Confirmed SMS/WhatsApp receipt buttons on the envelopes list appear only when
  the matching channel is enabled in settings.

## v1.0.10
- Per-Sabbath Excel sheet cleanups:
  - Receipt numbers display without the internal month/sabbath prefix (e.g.
    "JUN1-0421" now shows as "0421").
  - Combined Offering and Thanksgiving Offering appear as a single block (the
    full amount given) in the per-contributor entries table, but are split into
    their trust and local halves in the summary table.
  - The summary table now has cell borders, matching the entries table.

## v1.0.9
- Statement imports now capture the statement's own opening/closing running
  balance and date span.
- New "Bank position check" report (Reports → Bank position check): compares the
  system's computed bank balance (opening + bank receipts − bank payments) against
  the most recent statement's closing balance. A non-zero difference means an
  entry is on the statement but not in the app (or vice versa) — the report lists
  the likely culprits (unconfirmed, in-review, or unallocated bank entries) so
  they can be chased. Directly addresses un-entered bank entries going undetected.

## v1.0.8
- New per-transaction "Receipt" action on the transactions list: receipt a single
  bank/M-Pesa gift as an envelope on demand (the per-entry counterpart to the bulk
  monthly pull). Supports a user-entered receipt number for hybrid manual
  receipting, so the system record matches a hand-written receipt/envelope; leave
  it blank to auto-assign. Split parts of one gift are receipted together, the
  bank transaction is linked, and it is marked accounted-for so income is never
  double-counted. (Items 7 + 8.)

## v1.0.7
- Reconciliation variance finder rewritten to explain real-world differences:
  it now compares each fund's engine contribution against what is actually
  posted in the ledger, catching transactions that were re-allocated to another
  fund, edited, excluded, reversed, or unconfirmed after posting — not just
  entries that were never posted. The flagged amounts now sum to the variance,
  and a one-click "Rebuild ledger" button on the page re-posts everything from
  current source records to clear it.

## v1.0.6
- Transactions Excel export now includes M-Pesa ref, core ref, bank receipt,
  member, phone, dev group, service Sabbath and confirmed status.
- SMS and WhatsApp send buttons on the envelopes page appear only when those
  channels are enabled in settings.
- The per-Sabbath Excel sheet now carries the church name, has cell borders,
  number formatting, and a print-ready landscape layout (fit-to-width, repeating
  headers, page footer).
- New reconciliation variance finder: when a fund's engine balance differs from
  the general ledger, click "investigate" to see the actual transactions and
  expenses causing the difference.
- M-Pesa webhook ingest now normalises dedup keys to uppercase (collation-safe),
  consistent with the statement importer.
- Mobile layout: tables scroll within their cards instead of forcing the page
  wide; tighter padding and wrapping on small screens.

## v1.0.5
- Fixed a 500 error (FieldError on 'children') on the budget breakdown edit page,
  triggered when the Local Church Budget fund was matched by its full name rather
  than an 'LCB ' prefix. The query now uses the correct 'subgroups' relation.

## v1.0.4
- Update checker now authenticates with an optional GITHUB_TOKEN, so it can read
  releases from a PRIVATE GitHub repository (the unauthenticated API returns 404
  for private repos).
- Fixed: the release check was cached permanently per process, so a new release
  was not noticed until the app restarted. It now re-checks at most every 10
  minutes, and the update page forces a fresh check.

## v1.0.3
- Import dedup now also matches on the M-Pesa receipt (mpesa_ref), catching a
  repeated payment even when one row has a core_ref and another does not.
- New 'dedupe_transactions' management command finds and removes existing
  duplicate transactions sharing an M-Pesa receipt (keeps the better record,
  repoints envelopes/expenses). Dry-run by default; --apply to perform.
- Statement purge gained an 'Unlink & purge' option: it clears the
  reconciliation links on any expenses tied to the statement's debits (keeping
  the expenses) instead of refusing outright.

## v1.0.2
- Statement dedup keys (core_ref / M-Pesa receipt) are normalised to uppercase,
  so duplicate detection is exact regardless of the database collation. Fixes
  false/inconsistent duplicate counts on MySQL databases created with a
  case-insensitive collation such as latin1_swedish_ci.

## v1.0.1
- Test release to validate the in-app update mechanism.
- Added a visible "What's new" note on the Settings → About tab so an applied
  update is easy to confirm.
- Database backup is now engine-aware (SQLite file / MySQL & Postgres dump).
- Importer creates a system user automatically on a fresh database, so the
  legacy import no longer fails on a brand-new deployment.
- `.env` is auto-loaded by the app (no fragile shell `export` needed).
- Production: WhiteNoise static serving, health check at /healthz/, gunicorn
  config, logging, and cPanel/WHM deployment runbook.

## v1.0.0
- Initial release: full SDA church treasury system — member giving, fund
  allocation, bank/M-Pesa reconciliation, trust remittances, expenses,
  departmental reporting, and audit logging.

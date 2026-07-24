# Performance & Scalability Recommendations

Recorded during the Performance Engineering / Database review (see VERSION history
for the release this review shipped in). Items here were judged to need architectural
change, an infrastructure decision, or further investigation beyond a safe quick fix,
so they were documented rather than implemented immediately, per that review's brief.

Each entry: description, reason, expected benefit, priority.

---

## 1. Monthly Treasurer's Report recomputes the same aggregates multiple times per request

**Description.** `/reports/board/` issues ~130 queries per render (down from ~175 after
this review's N+1 fixes elsewhere reduced a shared dependency). Profiling shows the
remaining cost isn't classic per-row N+1 — every individual query is already a proper
`GROUP BY`/aggregate, not a loop — but several of the *same shape* of aggregate
(receipts by department, expenses by department, fund transfers by department) are
computed **separately** for different sections of the report (collections summary,
local funds statement, trust trend, cash flow, financial position, camp goals) rather
than being computed once and shared.

**Reason.** The report's context-building grew section by section over many releases,
each section calling into `reports.services.balances` / `reports.services.treasurer`
independently rather than through a shared, request-scoped computation.

**Expected benefit.** Restructuring the view to compute each distinct aggregate once
(e.g. one `department_summary()` call reused by every section that needs it, one
per-department receipts/expenses map reused everywhere) would likely cut the report's
query count by roughly half, with no change in the figures shown. This is a genuine
refactor of the report's internals — worth doing deliberately with its own test pass
rather than as a quick fix, since the report has many sections and export formats
(HTML, Word, Excel) that all share this context.

**Priority: Medium.** The report is not on the hot path for every page load (it's
opened deliberately, not polled), so the cost is real but bounded to when a treasurer
views or exports it.

---

## 3. Concurrency / race conditions were not fully audited this pass

**Description.** Several "read the current total, then act" sequences are not wrapped
in row-level locking — e.g. the petty-cash-float check when issuing a staff advance or
top-up (`if amount + charge > avail`) reads the current float balance and compares,
without a `select_for_update()` on the contributing rows. Under concurrent requests
(two treasurers/leaders acting at the same moment), this is a textbook
time-of-check-to-time-of-use (TOCTOU) gap.

**Reason not fixed immediately.** The petty-cash float isn't a single row that can be
locked directly — it's an aggregate computed across `PettyCashTopUp` and
`StaffAdvance`/`Expense` rows, so a correct fix means either locking the whole
contributing row set (heavier, and easy to get subtly wrong) or introducing a
dedicated ledger-style running-balance row that can be locked cleanly (an
architectural change). This is exactly the kind of change this review's brief asks to
record rather than implement inline.

**Expected benefit.** Removes a narrow window where two simultaneous top-ups or
advances could both pass a balance check based on stale data and jointly overdraw the
float.

**Priority: Low in practice today** (typical usage is one or two treasurers, rarely
acting at the exact same second), **but worth revisiting if the number of concurrent
users grows** (e.g. more department leaders self-serving advances at once).

---

## 4. No systematic N+1 / index audit against real production query logs

**Description.** This review profiled the highest-traffic pages (dashboard, executive
overview, expense/member/transaction lists, the monthly report, and the new ledger
health check) using Django's query-capture tooling against seeded demo data, and fixed
the two clearest, highest-impact N+1 patterns found (see the shipped changes). It did
not have access to real production query logs or traffic patterns, and did not
systematically walk every view in the application.

**Reason not fixed immediately.** A full audit of every view/report against real usage
patterns is a larger undertaking than this pass's scope, and prioritising further work
without real traffic data risks optimising the wrong things.

**Expected benefit.** Enabling Django's `django-silk` or a similar profiler in a
staging environment for a week of real usage would surface any remaining hot paths
precisely, rather than guessing from synthetic data.

**Priority: Low** — the two fixes made this pass addressed the largest, clearest wins
found; further gains are likely smaller and more scattered.

---

## 5. Large file imports (bank statements, envelope sheets) run synchronously

**Description.** Statement and envelope-sheet imports are processed inline within the
request/response cycle. For a single church's typical statement size (weeks to a few
months of transactions), this is fast and unremarkable. For an unusually large import
(e.g. importing several years of historical statements at once, or a bulk backfill),
this could tie up a web worker for an extended period and risk a request timeout.

**Reason not fixed immediately.** Moving imports to a background task queue (Celery,
Django-RQ, or similar) is a genuine architectural addition — a new infrastructure
dependency and deployment component — not a quick, low-risk change.

**Expected benefit.** Removes any risk of a large import timing out or blocking a web
worker, and would allow a progress indicator for long-running imports.

**Priority: Low** given this church's actual usage pattern (regular, modestly-sized
statement imports), but worth planning for if the application is adopted by larger
congregations with bigger transaction volumes, or if historical bulk backfills become
a common operation.

---

## 6. `StaffAdvance.balance` computed per-row on the advance list page

**Description.** The Staff Advances list page computes each advance's outstanding
balance via a property that runs its own aggregate query per advance (a small number
of `SUM(cashbook_expense.amount)` / `SUM(cashbook_advancetopup.amount)` queries per
row). At the current typical scale (a handful of open advances at a time) this is a
minor cost (~24 queries total, observed), not the dramatic kind of N+1 fixed elsewhere
in this review.

**Reason not fixed this round.** Bulk-computing balances for a list of advances would
follow the same pattern as `fund_balances_from_ledger_bulk()` /
`budget_amounts_bulk()` added earlier in this review, but the benefit is
proportionally small until a church has many more concurrently-open advances than is
typical today.

**Expected benefit.** Marginal at current scale; would matter more if a larger
organisation with many simultaneous staff advances adopted the application.

**Priority: Low.**

---

## 7. Two "god files" — `reports/views.py` and `cashbook/views.py`

**Progress (god-file refactor pass, v2.77.0).** A second, dedicated pass
continued extracting the pure-logic helpers that never belonged in a views
file into properly-named service modules, each behaviour-preserving and
independently regression-tested. New modules this pass:
`reports/services/goals.py` (`camp_goal_records`, `sentence_fund_name`),
`reports/services/remittance.py` (`days_outstanding`, `repost_to_ledger`,
`remittance_dashboard_rows`), `reports/services/devgroups.py`
(`balanced_partition`), `cashbook/services/receipts.py`
(`validate_receipt_upload`, `missing_receipts_queryset`, the two receipt
constants), `cashbook/services/cheque_words.py` (`amount_in_words`), and
`cashbook/services/advances.py` (`advance_detail_ctx`,
`record_advance_expense` — both also imported by leaders/views.py). Each views
module re-exports every moved symbol under its exact original (often
underscore-prefixed) name, so every existing `from reports.views import ...` /
`from cashbook.views import ...` and every `views.ClassName` reference in the
url confs keeps working byte-for-byte. `reports/views.py` went 4,180 → 4,034
and `cashbook/views.py` 3,508 → 3,358. Verified: full cashbook suite (402,
unchanged), full reports/statements/giving suites, and the targeted core
suites for every external importer — all green — plus a direct import-surface
check asserting every externally-imported symbol still resolves and both url
confs still import cleanly.

**Progress (earlier metrics-expansion pass).** The canonical accounting
helpers trapped in `cashbook/views.py` (`_petty_balance_asof`, the three
`outstanding_*_advances_total` functions, `unpresented_payments_qs`,
`unpresented_cheques_total`, the payables/accruals/prepayment totals) moved to
`cashbook/services/treasury_position.py`; the Bank Position calculation moved
to `reports.services.balances.bank_position`.

**What deliberately remains open.** The largest single clusters are still
*view* code, not pure helpers, so they were left alone this pass as
higher-risk-per-line: `MonthlyTreasurerReportView` + `_monthly_report_context`
(~530 lines) and the various board/position statement views in
`reports/views.py`; the expense / advance / petty-cash / obligations view
clusters in `cashbook/views.py`. A full package split (`reports/views/` with
topic submodules re-exported from `__init__.py`) is the eventual end state, but
it touches module-level import ordering and 66+ interdependent classes, so it
warrants its own dedicated pass with a full-suite run rather than being folded
into a helper-extraction pass. The incremental service-extraction approach
taken so far is lower-risk and has already removed ~300 lines of non-view logic
from the two files while leaving every import path intact.

**Expected benefit.** Easier navigation, lower cross-feature coupling, and —
the concrete architectural win — accounting/query logic now lives in testable
service modules instead of view files, consistent with the Financial Metrics
Registry direction the rest of the codebase is moving toward.

**Priority: Medium.** Not urgent — the code works and is well-tested — but worth
planning for before either file grows further.

---

## 8. Repeated (non-identical) department-dropdown queryset construction across forms

**Description.** Six form classes across `cashbook/forms.py`, `giving/forms.py`,
`assets/forms.py`, and `core/forms.py` each build a `Department` queryset for a
dropdown field inline, with three slightly different filter shapes depending on the
form's purpose (`active=True, is_trust=False`, plain `active=True`, and
`active=True, selectable=True`).

**Reason not refactored this pass.** The three variants are genuinely different
(each form legitimately needs a different subset of funds), so collapsing them into
one shared helper isn't a pure duplication removal — it would need a small parameterised
helper (e.g. `departments.models.dropdown_departments(trust=None, selectable_only=False)`)
designed carefully enough not to subtly change any one form's behaviour. Low risk, but
not the "obviously identical, safe to merge" case this pass prioritised.

**Expected benefit.** One shared, well-tested helper instead of six inline
near-duplicates; future changes to how fund dropdowns are built (e.g. adding
`select_related("parent")`, as this review's performance pass just did everywhere by
hand) would only need to happen in one place.

**Priority: Low.**

---

## 9. Bank Position report can be wrong if "Opening bank balance" was never configured

**Progress (metrics-expansion pass):** the calculation is now the
`bank_position` registry metric (`reports.services.balances.bank_position`),
which returns an explicit `opening_configured` flag; the Treasurer's Report's
Treasury Position section and the executive snapshot's bank-balance card show a
"opening bank balance not configured" caveat while the flag is false, so the
figure can no longer be silently trusted. The underlying operational/data-model
gap (configure the figure once, or track opening balances by physical location)
remains as recorded below.

**Description.** The Bank Position report (`/reports/bank-position/`) compares the
system's recorded bank movements against the actual bank statement's captured
closing balance, using `SiteConfig.opening_bank_balance` as its starting point. Unlike
the three critical fixes made in this review (which affected the *total system-wide*
cash figure and have a correct, always-available substitute in
`Department.opening_balance`), this report specifically needs the **bank account's
own** opening balance — a figure that genuinely isn't derivable from per-fund
opening balances, since those mix cash-on-hand, petty cash, and bank funds together
per fund rather than separating them by which physical account holds the money. For
this church's data, `opening_bank_balance` is still at its default of zero, so this
report would currently show a spurious gap equal to the true bank-only opening
balance.

**Reason not fixed this pass.** The field is genuinely configurable (Settings →
Financial Setup already exposes `opening_bank_balance`), so the immediate action is
operational, not a code fix: **a treasurer should set this figure once**, and the
report will be correct from then on. Automatically deriving a sensible default would
require either a new data field (e.g. tagging each fund's opening balance as
bank-held vs cash-held) or a one-time reconciliation exercise — a data-model decision,
not a safe quick fix.

**Expected benefit.** Either a short onboarding prompt ("Bank Position needs your
opening bank balance — set it here") shown the first time this report is opened while
the field is still zero, or a data-model change to track opening balances by physical
location (bank/cash/petty) rather than only by fund, would make this report reliable
without depending on a treasurer discovering the right settings field on their own.

**Priority: High** — this is a live, real problem for this deployment specifically
(unlike the three fixed issues, this one needs a decision, not just a code change),
and a treasurer relying on this report today would see a confusing, wrong gap.

---

## 10. Legacy-import-only opening-balance fields are a duplicate, easily-misused source of truth

**Description.** `SiteConfig.opening_bank_balance`, `opening_cash_on_hand`, and
`opening_unremitted_trust` exist only to receive a one-time snapshot from the
legacy-spreadsheet import tool, displayed thereafter as a labelled reference (in the
Statement of Financial Position and the backup/export Summary sheet). This review
found that **three separate, unrelated calculations** (Executive overview KPI, Cash
Flow Forecast, and bank reconciliation book balance) had, over several releases,
each independently — and incorrectly — reached for these fields as if they were the
authoritative "today's opening cash position", instead of the actual authoritative
source (`Department.opening_balance`, summed). All three are now fixed via one shared
helper (`departments.models.total_opening_cash_position()`), but the underlying
temptation remains: three same-looking, zero-by-default fields sitting on `SiteConfig`
that look like they should represent "the opening cash position" but don't, for any
deployment that didn't go through the legacy-import path.

**Reason not fixed further this pass.** Removing or renaming these fields would
affect the legacy-import tool and the two legitimate reference displays (SOFP,
backup Summary) that intentionally show "what was configured at setup" — a
data-model change with migration and tooling implications beyond this review's safe-fix
scope.

**Expected benefit.** Renaming the fields to make their limited purpose obvious (e.g.
`legacy_import_opening_bank_balance`) and/or adding a code comment or docstring
warning directly on the model fields (pointing future developers to
`total_opening_cash_position()` instead) would prevent this exact mistake from being
reintroduced a fourth time.

**Priority: Medium.**

---

## 11. Data tables lack `scope="col"` on header cells

**Description.** No table in the application uses `scope="col"` (or `scope="row"`)
on its header cells — checked across every table template. Most of this app's tables
are simple (a single header row, no row-spanning headers), so screen readers can
usually still infer the association reasonably well without it, but `scope` is a
WCAG best practice (1.3.1) for reliably associating a data cell with its column
header, especially valuable on the wider tables (the General Ledger, the Journal,
several reports) with 6+ columns.

**Reason not fixed this pass.** This app has dozens of table templates; doing this
properly and consistently (rather than a partial, inconsistent sweep) is a broader,
mechanical cleanup better done as its own dedicated pass than folded into this
review's targeted fixes.

**Expected benefit.** More reliable screen-reader navigation of wide, data-dense
tables (the ledger, journal, and financial reports especially).

**Priority: Low-Medium.** Lower severity than the label-association and colour-contrast
issues fixed in this review (which affect whether a control's purpose is
communicated *at all*, versus this one improving navigation of already-labelled data).

---

## 12. No dedicated mobile layout audit performed this pass

**Description.** The application already has solid responsive infrastructure (a
`table-scroll` auto-wrap script for tables on narrow viewports, a correct viewport
meta tag, and opt-in "large touch targets"/"reduced motion"/"high contrast" user
preferences) — but this review did not systematically test every page at common
mobile breakpoints (e.g. 375px, 390px) for overflow, cramped layouts, or awkward
wrapping, particularly on the denser reports (Monthly Treasurer's Report, General
Ledger Health Check) which were designed with desktop use as the primary case.

**Reason not fixed this pass.** A systematic, screen-by-screen mobile audit across
the whole application is a substantial undertaking better scoped as its own review.

**Expected benefit.** Confidence that the denser reporting pages remain usable for a
treasurer checking figures on a phone, not just the transactional/data-entry pages
that were more clearly designed mobile-first.

**Priority: Low.**

---

## 13. Two unrelated models are both named `BudgetLine`

**Description.** `departments.models.BudgetLine` (a line within a `Budget` — the
department's formal annual budget) and `cashbook.models.BudgetLine` (a named
budget item for a fund in a year, e.g. "Accommodation 50,000" under a Camp Meeting
fund, which `Expense.budget_line` tags spend against) are two entirely different
models that happen to share an identical class name, distinguished only by which
app they live in. Found while auditing every model's `on_delete` behaviour for this
review.

**Reason not fixed this pass.** Renaming either model is a genuine migration (Django's
`RenameModel`), plus updating every import, FK reference, template, and test that
touches it — a mechanical but invasive change spanning two apps, not a safe drop-in
fix. It also isn't a live bug (Django distinguishes them correctly via `app_label`
internally) — it's a maintainability/confusion risk for future developers (and for
an AI assistant, or a new hire, searching the codebase for "BudgetLine" and finding
two unrelated results).

**Expected benefit.** Renaming one of them (e.g. `cashbook.BudgetLine` →
`cashbook.BudgetItem`, since it's the newer, more specific concept) would remove the
ambiguity permanently.

**Priority: Low.** Confusing, not incorrect; worth doing during a quieter period
rather than as a targeted fix.

---

## 15. Test files are organised by when they were added, not what they cover

**Description.** Several apps — `cashbook` most notably, with 32 test files — mix
feature-named files (`test_amount_validation.py`, `test_transfer_refund.py`) with
version/session-named ones (`test_batch_v193.py`, `test_batch_v197.py`,
`test_batch_v2001.py`). The version-named files each cover a specific historical
batch of changes, which made sense when they were written, but makes it hard for a
future developer (or reviewer) to find "all the tests for staff advances" without
searching file contents rather than reading file names.

**Reason not fixed this pass.** Consolidating test files means moving test methods
between files — mechanical but with real risk of an accidental omission if rushed,
and better done as its own deliberate pass with a full regression run at the end,
per this review's brief.

**Expected benefit.** Faster navigation to relevant coverage when reviewing or
extending a feature; less risk of accidentally duplicating a test that already
exists in a version-named file no one thought to check.

**Priority: Low.** A maintainability nicety, not a coverage gap — every test still
runs and still catches what it's meant to.

---

## 16. No concurrency/load testing

**Description.** This review (and the database review before it) reasoned carefully
about concurrency risk (e.g. the petty-cash-float TOCTOU gap recorded earlier, and
the two atomicity fixes made in the database review) but neither could exercise
*genuine* concurrent access — Django's default test runner and SQLite don't
straightforwardly support multiple real threads/processes hitting the same test
database at once the way production traffic would.

**Reason not fixed this pass.** Proper concurrency testing needs either a
Postgres/MySQL-backed test environment with real multi-connection support and a
tool like `pytest-django` with `django_db(transaction=True)`, or a dedicated
load-testing tool (Locust, k6) run against a staging deployment — both are
infrastructure additions, not safe in-repo test changes.

**Expected benefit.** Direct evidence (not just reasoning from code review) about
how the application behaves under concurrent writes to the same fund, and how it
performs under realistic multi-user load — valuable if the application is adopted
by a larger congregation with more simultaneous users.

**Priority: Low**, given this church's actual current usage pattern (one or two
treasurers, rarely acting at the exact same second) — the same reasoning already
applied to the petty-cash TOCTOU finding.

---

## 17. Password-reset emails are not sent — by design, for now

**Description.** When an administrator resets a user's password, the new password is
shown once on-screen for the administrator to pass to the user directly, rather than
being emailed to them.

**Reason.** This application has no outbound email backend configured (no SMTP
settings, no `EMAIL_BACKEND` beyond Django's console default) — it's built around SMS
for outbound notifications, not email. Building a "forgot password" email flow without
a working mail transport would either silently fail or need a parallel SMS-based
reset flow (a real feature, but a separate one, since it would need a verified phone
number per user and a one-time-code mechanism much like the existing 2FA SMS delivery
path).

**Expected benefit.** If/when an SMTP backend is configured, add a genuine self-service
"forgot password" flow (Django's built-in `PasswordResetView` works out of the box
once `EMAIL_BACKEND` is set) alongside the admin-driven reset this review added.

**Priority: Low** — the admin-driven reset covers the operational need today.

---

## 18. Security questions were not implemented

**Description.** The review brief asked whether security questions should be
supported for account recovery.

**Reason.** Security questions are a widely-deprecated pattern (NIST and most current
security guidance recommend against them — answers are often guessable, publicly
discoverable, or forgotten, and they add an alternate, usually weaker authentication
path alongside the real one). This application already has a stronger recovery
mechanism for its strongest control (2FA recovery codes), and the admin-driven
password reset covers the "forgotten password" case without needing a second,
weaker factor.

**Recommendation: do not implement.** If self-service account recovery becomes a
priority, prefer an SMS-based one-time-code reset (reusing the existing 2FA SMS
delivery path) over security questions.

---

## 19. Per-session detail (not just count + bulk terminate)

**Description.** This review added an active-session **count** on a user's Security
tab and a **"force logout everywhere"** bulk action, by decoding
`django.contrib.sessions.models.Session` rows. It did not build a per-session list
(device/browser, IP, last-active time, an individual "end this one session" control).

**Reason not built this pass.** Django's session data doesn't store device/user-agent
information by default — only what a session's own middleware chain records. Building
a genuinely useful per-session table (recognisable device names, accurate "last seen"
times) needs a small amount of additional tracking (e.g. stamping user-agent and last-
seen-at into the session data or a companion model) that's a reasonable follow-up but
distinct from what this pass covered.

**Expected benefit.** Lets an administrator (or, later, a user managing their own
sessions) end one suspicious session without logging out every device.

**Priority: Low-Medium.**

---

## 20. Password expiry is tracked but not yet enforced

**Description.** `UserProfile.password_changed_at` is now stamped automatically
whenever a password changes (self-service or admin-reset), and shown on the Security
tab — but there is no `SiteConfig`-level "maximum password age" setting, and no
enforcement (a middleware forcing a change once a password is older than N days), the
way `require_2fa_for_treasurers` is enforced today.

**Reason not built this pass.** Whether to enforce password expiry at all is a policy
decision (current security guidance is actually mixed on forced periodic rotation —
NIST's more recent guidance argues *against* mandatory rotation in favour of length/
breach-checking), so this was left as a recorded decision point rather than assumed.

**Expected benefit, if wanted.** A `SiteConfig.password_max_age_days` (blank/0 =
disabled) plus a small middleware extension of the same shape as
`ForcePasswordChangeMiddleware` this review added, checking `password_changed_at`
against it.

**Priority: Low**, pending a policy decision on whether periodic rotation is wanted.

---

## 21. "Copy permissions between two existing users" not implemented

**Description.** The review brief asked about copying a user's permissions to another
*existing* account (distinct from **cloning**, which this review did implement — creating
a *new* account with the same role/profiles/led-departments as an existing one).

**Reason not built this pass.** Overwriting an existing, possibly-customised user's
rights from another user's is a more destructive operation than cloning into a new
account (there's no "undo" beyond checking the audit log and manually reverting), and
needs its own careful confirmation UX. Cloning covers the far more common real
scenario ("set up another Assistant like Jane") safely.

**Expected benefit.** A rarely-needed convenience; low priority relative to the risk
of a rushed implementation encouraging accidental overwrites.

**Priority: Low.**

---

## 22. "Archive" is intentionally the same as deactivate, not a separate feature

**Description.** The review brief asked about archiving a user "while preserving
history." This application already deactivates (`is_active=False`) rather than
deletes, and nothing about deactivation removes the account's history — the full
audit trail (`UserAdminLogEntry`), `django-simple-history` records, and every
transaction/expense/approval the account ever touched all remain exactly as they
were. There is deliberately no user-delete feature anywhere in this application (a
finding from an earlier review), so "archived" and "deactivated" would describe
exactly the same state.

**Recommendation: no separate "archive" feature needed** — deactivation already *is*
archiving, in every sense that matters here.

---

## Summary table

| # | Item | Priority |
|---|---|---|
| 1 | Monthly Treasurer's Report recomputes aggregates per-section instead of once | **Addressed (v2.28)** — request-scoped memo in `perfcache` dedupes same-arg aggregates per render; 133→120 queries, no figure change |
| 3 | No row-level locking on petty-cash-float checks (TOCTOU race) | Low (today) |
| 4 | No systematic N+1/index audit against real production traffic | Low |
| 5 | Large file imports run synchronously, no background task queue | Low |
| 6 | `StaffAdvance.balance` computed per-row on the advance list | Low |
| 7 | `reports/views.py` and `cashbook/views.py` have grown into "god files" | Medium |
| 8 | Department-dropdown queryset construction repeated (non-identically) across 6 forms | Low |
| 9 | Bank Position report wrong until "Opening bank balance" is configured (operational + data-model gap) | High |
| 10 | Legacy-import-only opening-balance fields are a duplicate, easily-misused source of truth | Medium |
| 11 | Data tables lack `scope="col"` on header cells | Low-Medium |
| 12 | No dedicated mobile layout audit performed | Low |
| 13 | Two unrelated models are both named `BudgetLine` (departments vs cashbook) | Low |
| 15 | Test files organised by when added, not what they cover (cashbook: 32 files) | Low |
| 16 | No concurrency/load testing (SQLite/default test runner limitation) | Low |
| 17 | Password-reset emails not sent — no email backend configured | Low |
| 18 | Security questions — deliberately not implemented (deprecated pattern) | N/A (documented) |
| 19 | Per-session detail (device/IP/last-seen, individual termination) not built | Low-Medium |
| 20 | Password expiry tracked but not enforced — pending a rotation policy decision | Low |
| 21 | Copy permissions between two existing users not implemented (cloning covers the common case) | Low |
| 22 | "Archive" intentionally not a separate feature from deactivate | N/A (documented) |

---

## Enhancements identified during the Report Engine phase (v2.28) — deferred

These were found while building the Semantic Reporting Layer and Generic Report
Engine. They are out of scope for the engine-foundation phase (which explicitly
does not redesign existing reports) and are recorded for after the review phases.

## 24. Dashboards assemble figures directly rather than via ReportContext — PARTLY ADDRESSED (Phase 6)

**Status: Partly addressed.** The main `DashboardView` now obtains its headline
figures (fund summary, trust summary, trust-to-remit, giving by group, income by
channel, tithe) through a single `ReportContext`, so they equal the reports'
metrics by construction (verified by reconciliation test). The executive
dashboard's blended live+historical trend and the leader dashboards remain on
their bespoke paths — a larger, separate migration.

*(original)* `core/views.py` (executive) and `leaders/views.py` built headline
figures with their own aggregates. **Priority: Medium.**

## Enhancements identified during the Component Library phase (v2.29) — deferred

Found while building the component library, chart engine, rendering framework and
dependency map. Out of scope for this phase (which builds reusable machinery, not
report migrations or new UI); recorded for after the review phases.

## 29. `html` section kind not yet used

**Description.** `SectionData` supports `kind="html"` for arbitrary safe HTML
fragments, but no component emits it yet (the library covers its needs with
table/keyvalue/kpi/chart/commentary/info/signature).

**Recommendation.** Add a `RawHtmlComponent` if/when a report needs bespoke
markup (e.g. a formatted legal notice); render it in the template with
appropriate escaping/sanitisation.

**Priority: Low.**

---

## Enhancements identified during the Narrative Engine phase (Phase 6) — deferred

## 30. Remaining reports to migrate onto the engine — UPDATED (Phase 7)

**Progress (Phase 7).** Now migrated: Cash Flow Statement (`cash_flow_v2`),
Statement of Fund Balances (`fund_balances_v2`), Budget vs Actual
(`budget_vs_actual_v2`) — joining Phase 6's Income & Expenditure, Trial Balance,
Financial Position summary and Board Report. **Still on legacy:** the *detailed*
Financial Position (NBV/prepayments/advances), comparative/multi-period/prior-year
statement wrappers, and the operational + member/ministry registers (Cash Book,
Bank Reconciliation, Payment/Receipt/Instrument/Cheque registers, Staff Advances,
Petty Cash, Expense/Journal/Ledger, Asset/Depreciation, Liability, Loan, Trust,
Envelope, Giving, Pledge, Department, Development Project, Audit; Member/
Contribution statements, Giving history, Donor, Ministry, Leader reports).

**Recommendation.** Migrate each by composing existing components + narratives,
proving figure-equivalence against the legacy view first. The machinery exists;
these are compositions, not new infrastructure.

**Priority: Medium.**

## 31. Executive dashboard & leader dashboards still compute figures inline — UPDATED (Phase 7)

**Progress (Phase 7).** Confirmed the executive dashboard already draws income
through `core.metrics.income_credits` (the definition `total_income` wraps), so
its headline figures **reconcile with the reports by construction** (verified by
test). The remaining work is structural, not correctness: routing the executive
dashboard's blended live+historical trend and the leader dashboards through
`ReportContext` for provenance/memoization consistency.

**Recommendation.** Introduce period/scope-aware metrics for the historical-trend
figures, then route these dashboards through `ReportContext`. Larger than the
main-dashboard migration because of the historical-data blending and leader
scoping.

**Priority: Medium** (correctness already holds; this is consolidation).

## 32. Legacy statement/report views can be retired once engine versions are adopted

**Description.** The migrated reports run in parallel with the legacy views
(`IncomeStatementView`, `TrialBalanceView`, `FinancialPositionView`, the Monthly/
Board report) to preserve URLs and allow verification. Once the engine versions
are adopted as the primary reports, the legacy views and templates can be retired
(or reduced to thin redirects) to remove duplicated presentation code.

**Recommendation.** After a review period, point the existing report URLs at the
engine reports (via a small adapter) and delete the superseded view/template
code. Keep the export byte-compatibility in mind for anyone scripting downloads.

**Priority: Low-Medium.**

## 33. Narrative localisation / templating

**Description.** Narrative text is composed in English in code. A future need for
other languages, or for churches to customise wording, would benefit from
externalising the sentence templates.

**Recommendation.** If localisation is required, move narrative sentence fragments
into templates/catalogues keyed by narrative + style, keeping the metric-sourced
values as substitutions. Determinism must be preserved.

**Priority: Low.**

---

## Enhancements identified during Phase 7 (statement migration, snapshot foundation) — deferred

## 35. Snapshot integrity for non-deterministic export formats

**Description.** Only the payload checksum and CSV export are byte-deterministic;
xlsx/docx embed timestamps and pdf embeds metadata, so their bytes vary between
identical renders. The snapshot service therefore checksums the payload (canonical
anchor) and CSV, and treats other formats as point-in-time copies.

**Recommendation.** If byte-stable archival of xlsx/pdf is required, normalise
their embedded timestamps/metadata at render time (e.g. fixed creation date) so
their checksums become deterministic and can be used for drift detection.

**Priority: Low.**

## Enhancements identified during Phase 8 (Report Administration Platform) — deferred

## 37. Drag-and-drop Report Designer canvas

**Description.** The designer persists reports as data and validates them, but the
editor is a JSON-backed section editor with the component palette surfaced, not a
visual drag-and-drop canvas. The persistence and compile/validate layers are
complete, so a canvas is purely a front-end addition.

**Recommendation.** Build a JS canvas that reads the component palette and writes
the same `sections` JSON (component + params + LayoutMeta), with live preview via
the existing render endpoint. No backend change needed.

**Priority: Low (UX polish).**

## 38. Actual report distribution (email/notification sending)

**Description.** Schedules carry recipients and every generated snapshot is linked
to its run, but the actual sending of the snapshot (email attachment / internal
notification) is not wired. Approval-before-send is modelled (`require_approval`)
but not enforced in a send pipeline.

**Recommendation.** Add a distribution step after `execute_schedule` that, for
schedules with recipients, emails the snapshot's export (or a link) via the
existing email backend, honouring `require_approval`. Record a delivery history.

**Priority: Low-Medium.**

## 39. Snapshot retention policy & background scheduler

**Description.** Scheduling execution and manual/'due' running exist, but there is
no background worker invoking `run_due_schedules` on a timer, and no retention
policy pruning old snapshots.

**Recommendation.** Add a management command (or Celery beat task) calling
`run_due_schedules` periodically, and a retention setting (keep N per report, or
age-based) applied after each run.

**Priority: Low (operational).**

---

## Financial Intelligence Platform (Phase 9)

The Financial Intelligence Platform was built on the reporting architecture: a
Financial Intelligence Engine (structured, explainable Insights), 15 insight
modules across the seven categories, a Recommendation Engine, a Financial Health
Score (9 transparently-weighted indicators), a Trend & Forecast Engine, a
Financial Knowledge Service (the AI-Treasurer knowledge backend), a Treasurer
Workspace, and JSON Analytics APIs. Everything reads only the Financial Metrics
Registry via the Semantic Reporting Layer; no accounting calculation is
duplicated, and every insight/indicator/recommendation is explainable (reason +
metrics + thresholds + services + transactions). See docs/INTELLIGENCE_PLATFORM.md.

### Enhancements identified during Phase 9 — deferred

## 40. Conversational AI Treasurer (on the Knowledge Service)

**Description.** Phase 9 built the structured Knowledge Service (`knowledge_for`,
`full_briefing`) that a conversational assistant would consume, but deliberately
no chatbot. A future phase could add a natural-language layer that answers
treasurer questions by calling the Knowledge Service — with every answer grounded
in, and citing, the registry metrics and insights it returned.

**Recommendation.** Add an LLM front-end that maps questions to concepts/periods,
calls `knowledge_for`/`full_briefing`, and renders the structured result as prose
with citations back to metrics and snapshots. The knowledge layer already
guarantees explainability; the assistant must never introduce a figure the
Knowledge Service didn't provide.

**Priority: Low (future phase).**

## 41. Advanced forecasting (seasonality-aware)

**Description.** The Trend & Forecast Engine uses a transparent linear projection.
For income with strong seasonality (e.g. camp-meeting months), a seasonality-aware
model would project more accurately while remaining explainable.

**Recommendation.** Add an optional seasonal-decomposition forecast (still
deterministic and labelled a projection), keeping the linear model as the default
transparent baseline.

**Priority: Low.**

## 42. Persisted insight snapshots for trend-of-insights

**Description.** Insights are computed live each request (correct, always current).
Persisting a periodic snapshot of the insight set would enable "insight trends"
(e.g. how many criticals over time) and alerting on new criticals.

**Recommendation.** On a schedule (reusing the Phase 8 scheduler), persist the
insight set as a snapshot and add a small trend-of-insights view. Keep live
computation as the source of truth; the snapshot is for history/alerting only.

**Priority: Low.**

---

## Treasurer's Report + Report-Aware AI phase

The existing AI assistant was extended (not replaced) to consume the Financial
Knowledge Service, becoming report-context-aware: the /assistant/ page and
/assistant/ask/ endpoint accept a report/period/element context, and every engine
report gained top + per-section "Ask AI" affordances that open the assistant
already aware of what the user was viewing. The Treasurer's Report was rebuilt as a
comprehensive engine report (AI briefing, health score, KPIs, statements, budget,
trust, reconciliation, insights, recommendations) composed only from the engine +
components + metrics registry + narrative + intelligence. See docs/TREASURER_REPORT.md.

### Enhancements identified during this phase — deferred

## 44b. Report-Designer editing of a report's presentation template — NEW

**Description.** `Report.html_template` now lets a registered report opt into a
purpose-built presentation template (the Treasurer's board pack) while keeping
identical section data and the generic engine template as the default. The
Report Designer persists *sections/layout* as data but does not yet let an
administrator choose a presentation template per designed report.

**Recommendation.** Expose a small "presentation style" choice on the designer
(generic grid vs. board pack) that maps to `html_template`, so designed reports
can also use the richer presentation without code.

**Priority: Low.**

## 45. Deep drill-down from a figure to its supporting transactions in-chat

**Description.** The assistant answers "which transactions make up this amount?"
from the knowledge context at a summary level. A deeper drill could return the
actual supporting journal/transaction rows for a specific metric and period via
the dependency graph.

**Recommendation.** Extend the Knowledge Service with an optional transaction-level
drill for a given metric+period, surfaced by the assistant when asked, still
read-only and registry-sourced.

**Priority: Low-Medium.**

---

## Metrics-expansion pass — coverage decisions

## 46. Treasury-position concepts now registry metrics; two brief items deliberately not modelled

**Status: metrics registered.** Ten metrics were added so every figure the
board pack shows is registry-sourced: `petty_cash_balance`,
`staff_advances_outstanding`, `bank_position`, `cash_in_transit`,
`pending_expense_claims`, `total_payments`, `budget_vs_actual`,
`dev_group_progress`, `negative_fund_balances`, `dormant_funds`. The registry
itself gained `has()`/`get()` lookups and `validate_authoritative()` (every
metric's documented implementation path is verified by test, so the catalogue
can no longer drift from the code), and `ReportContext.metric()` now
auto-applies the period end to `as_of`-keyed metrics the way it already did
`start/end` — removing the "forgot to pass ctx.end" footgun.

**Deliberately not modelled (would require inventing data the app doesn't
have):**
- **Pending journal entries** — journals in this system post immediately and
  atomically (`ledger.services.posting`); there is no draft state, so "pending
  journals" is structurally zero. If a draft/approve journal workflow is ever
  added, register the metric then.
- **Month-end checklist status** — no checklist model exists. The period-close
  service (`core/services/period_close.py`) is the nearest concept; a formal
  close-checklist would be its own small feature before it can be reported on.

**Priority: N/A (documented decisions).**

## 50. Follow-ups noted during the maker-checker redesign — NEW

* **Live-browser verification.** The DOM-harness testing above is strong but
  not a substitute for clicking through the grid (especially drag-reorder,
  resize, and pin) in a real browser before/soon after this ships.
* **Three-way segregation of duties.** `require_different_approver` currently
  only requires Post's actor to differ from the batch's *creator*, not also
  from the *approver* — a treasurer could approve then post the same batch
  themselves. A stricter three-actor mode (maker ≠ checker ≠ poster) would be
  a small, config-gated addition on top of the same pattern if wanted.
  **Priority: Low** (the current rule already matches the Expense-approval
  convention this app uses elsewhere).
* **Column resize persistence granularity.** Widths save per user per grid;
  no per-device variant, so a very different screen size on a second device
  will reuse the same saved widths (usually fine, occasionally cramped).
  **Priority: Low.**
* **Bulk actions on the Review Queue.** Approve/return/reject/post are
  currently per-batch; a treasurer processing many small batches at once has
  no bulk action. **Priority: Low-Medium**, revisit once real usage volume is
  known.

## 54. Follow-ups noted during this review — NEW

* **Client-side canvas export duplication.** The dashboard's
  `downloadLocalFundsPng()` (templates/dashboard.html) is a ~60-line
  near-duplicate of `static/js/table_png.js`'s `tableToPng()` — both fixed
  identically (2x → 4x) for this pass, but they should really be ONE
  implementation. Consolidating would mean generalising `tableToPng` to read
  its title/subtitle from `data-*` attributes as a fallback when `opts`
  doesn't supply them (the dashboard's only real difference). **Priority:
  Low-Medium** — cosmetic/maintainability, not a correctness issue.
* **`toDataURL('image/png')` can't embed DPI metadata.** A Canvas API
  limitation (no way to write a PNG `pHYs` chunk client-side) — the
  client-side exporters rely on pixel count alone for perceived quality,
  unlike the two Pillow-generated file types which now also carry explicit
  300 DPI tags. Not practically limiting at 4x scale, but worth knowing if a
  future export ever needs an exact physical print size. **Priority: Low.**
* **Three-way segregation of duties for loan retirement.** Not investigated
  this pass but worth checking: does `convert_to_donation`/`write_off`
  respect the same `require_different_approver` pattern used elsewhere?
  **Priority: Low-Medium**, flagged for a future review.

## 56. Benevolent Scheme Engine — Phase 1 shipped; follow-ups — NEW

The Benevolent Module shipped as a configurable **Benevolent Scheme Engine**
(see `docs/BENEVOLENT_MODULE.md`). Phase 1 delivered the data model, the policy
engine, the case workflow, services, rights, navigation, admin, a read-only JSON
API, seed data and 43 tests. The following were deliberately deferred rather than
half-built.

**56a. Per-case levy collection screen.** The model (`BenevolentContribution.case`)
and the working list (`benevolent.services.contributions.raise_case_levy`) exist
and are exercised at the service layer, but there is no UI to run a levy round —
issue the levy to every active member, track who has paid, chase the rest. Today a
levy is collected by recording ordinary contributions against the case. *Priority:
Medium* — only matters for schemes whose policy uses `PER_CASE_LEVY`.

**56b. Benevolent sections on the Report Engine.** Benevolent figures are all
registry metrics, so they are *available* to the engine, but no `ComponentSection`
has been written. A "Welfare schemes" section on the Board Pack (balances,
contributions, benefits paid, open cases, commitments) is a small piece of work
now that the metrics exist, and would put welfare in front of the board without a
separate report. *Priority: Medium.*

**56c. Bank-narration intake for dues.** The loans module recognises loan money on
a bank statement via `LoanNarrationPattern`. There is no equivalent for scheme
dues, so a member paying `BEN` by M-Pesa lands in the review queue and is
allocated by hand. The pattern engine is already generic enough to extend.
*Priority: Medium.*

**56d. Arrears reminders.** `schemes.refresh_arrears_status()` marks members
LAPSED / reinstates them, but nothing schedules it and nobody is told. Wiring it
to the existing SMS/WhatsApp channels (as pledges already does with
`pledges/services/reminders.py`) is the obvious next step. *Priority: Low.*

**56e. Dependant-aware benefit rules.** A different benefit for a spouse vs a
child is currently expressed by creating separate event types
("Bereavement — spouse", "Bereavement — parent"), which works but is blunt: the
relationship is already on `SchemeDependant`, so a benefit rule could key off it
directly. *Priority: Low* — the current approach is workable and no data would be
lost by changing later.

---

## 63. `post_batch` silently drops a line if its fund was deactivated between entry and posting — NEW

**Description.** While investigating the envelope-ledger column/data-loss bug (#64, this fix), `envelopes.services.batches.post_batch` was found to build its fund-resolution dict as `{d.id: d for d in Department.objects.filter(active=True)}`. `_expand_lines` then silently `continue`s past any `amounts` key whose fund id is not in that dict. If a fund is deactivated in the window between a batch being drafted/approved and actually posted, the line for it is posted as if it never existed — no error, no row flagged, and no visible sign that money was dropped.

**Why it wasn't the reported bug.** The fund in the reported case (a real, active fund merely outside the "preferred" quick-pick defaults) was never deactivated — this is a distinct, narrower edge case, not a different cause for the same symptom.

**Recommendation.** Either (a) resolve `funds`/`splits` in `post_batch` without the `active=True` filter (a fund referenced by an already-approved batch should still post; deactivating a fund should stop *new* entries against it, not un-post historical ones), or (b) have `validate_batch_for_post` explicitly flag any row whose `amounts` references a fund/split that no longer resolves, so it surfaces as a blocking error instead of a silent drop.

**Priority: Medium.** Rare in practice (a fund would have to be deactivated mid-flight on a specific batch), but it is a silent-money-loss shape and cheap to close.

---

---

## 119. Enterprise Asset Management (EAM) — phased build — IN PROGRESS

Redesigning the flat Fixed-Asset Register into a full lifecycle EAM module
(design: ASSET_EAM_DESIGN.md). Phased so nothing lands as a big-bang rewrite.

**Phase 0 — Foundation (DONE, v2.99).** Non-breaking data foundation:
`core.Organization` (multi-church scaffold, nullable everywhere, defaults to the
single implicit church); `assets.AssetClass` (configurable classification +
depreciation policy + optional per-class ledger account keys, seeded 1:1 from
the Category enum); `assets.Location` (hierarchical Campus→Building→Room);
extended the asset record with lifecycle `status`, `tag`, `serial_no`,
`in_service_on` (commissioning date), `custodian`, `church`, `parent`
(componentisation), useful life, heritage/donated flags. `Asset` is now a
first-class alias of `FixedAsset` (table unchanged; external imports intact).
Data migration backfills every asset consistently. Asset figures (NBV, cost,
accumulated depreciation, period depreciation, by-class) are now registered in
the Financial Metrics Registry, reusing `nbv_total()` as the authoritative NBV
impl — so they resolve through the registry, not ad-hoc sums. No figures
changed; the financial statements are byte-identical.

**Phase 1 — Ledger backbone (DONE, v3.00).** Assets now post to the general
ledger. Decisions taken: monthly depreciation (§9.2); capital purchases only via
the Acquisition workflow, with an optional convert-expense wizard (§9.3); opening
balances only, no historical reconstruction (§9.6). Delivered: a monthly
depreciation engine (`assets/services/depreciation.py`, keyed off `in_service_on`,
class-aware, salvage-capped); `DepreciationRun`/`DepreciationLine` with a
generate/post service, a `run_depreciation` management command, and a
treasurer-gated UI at `/assets/depreciation/runs/`; ledger postings
`post_depreciation_run` (Dr depreciation expense / Cr accumulated depreciation,
idempotent) and a self-reconciling `post_asset_opening` (delta to the register,
robust to legacy capital-expense balances); capital spend routed to CWIP
(unlinked) or Fixed assets (linked to a register asset) — never the P&L; new
chart accounts (CWIP, depreciation, disposal gain/loss, impairment, revaluation
reserve). A `register_vs_ledger` metric proves the control accounts equal the
register exactly (cost, accumulated depreciation, NBV all tie; trial balance and
accounting equation balanced). Monthly depreciation changed NBV figures as agreed.

**Phase 2a — Disposal on the ledger (DONE, v3.01).** Disposals are now proper
journals. The asset metrics are temporal (an asset is present in the register
until its disposal date, so the register and ledger agree both before and after
a disposal, rather than a disposed asset retroactively vanishing). `post_disposal`
reclassifies the proceeds receipt: it removes the asset's cost and accumulated
depreciation from the control accounts and recognises the balancing gain/(loss)
in the disposal gain/loss accounts, leaving only the true gain/(loss) in the
income result. The cash proceeds stay a fund receipt (so fund balances and the
Transaction-based reports are unchanged), the proceeds income leg nets to zero,
and the register↔ledger reconciliation stays exact through a mid-year disposal
(gain, loss and scrap all covered by tests). Depreciation runs now include
disposed assets so the final month's charge is captured before write-off.

**Phase 2b — Acquisition intake & the capital bridge (DONE, v3.02).** Every asset
now records how it came to exist. New `Acquisition` model (purchase / donation /
construction / transfer-in / opening) linking the asset to the `Expense` that paid
for it, the fund, and the donor. Only a donation posts a journal of its own —
`post_acquisition()` books Dr fixed assets / Cr donated-asset income (new account
`4610`) at fair value with the fund on the line — because a purchase is paid by an
Expense that already carries the cash side; posting again would double-count.
Acquisitions post before the asset opening so its self-reconciling delta nets them.
A "Convert Expense to Asset" wizard (`/assets/capitalise/<id>/`, linked from the
expense page, treasurer-only) creates the register record at the amount paid and
links the expense, which moves that expense's debit out of capital
work-in-progress into fixed assets with no second entry; it can also add a payment
to an existing asset (the construction/improvement case), so that logic lives in
one place. The capitalisation threshold (§9.1) is now configuration —
`SiteConfig.capitalisation_threshold`, default 0 (off), so no figures move until
it is set — enforced on both the asset form and the wizard. The disposal fund is
now mandatory: it receives any proceeds AND carries the gain or loss, so every
disposal is attributable to a fund (this also closes the v3.01 edge case where
proceeds without a nominated fund were not reclassified in the ledger). Fixed a
Phase 1 defect: `post_run` wrote a naive `posted_at` under active timezone support.

_Treasurer's decision (§9.4), applied in v3.03:_ donated assets are NOT income.
They are credited to net assets — `DESIGNATED_FUNDS` where the receiving fund is
restricted (the system's existing rule: trust money is restricted), otherwise
`CAPITAL_FUND` — with the fund carried on the journal line either way. The
short-lived `4610` donated-asset income account is retired automatically by
`ensure_chart()` if nothing was ever posted to it, so no history is lost. The
Income & Expenditure statement stays transaction-based and instead gained a
**Non-cash contributions — donated assets** section (schedule plus total, from
the register), deliberately outside the cash income total and the net result.
While wiring it, the statement's ad-hoc `disposal_gain_loss` aggregate — a
calculation living outside the registry — was consolidated into a registered
`disposal_gain_loss` metric, and an unclosed `card` div in the template was
fixed.

_Remaining (Phase 2c):_ the lifecycle state-machine UI and guarded transitions,
transfers/assignments/locations, the Asset 360 profile, and the Kanban board.
Also: register cost is temporal on disposal but not yet on ACQUISITION, so a
mid-year asset still sits in opening cost from the opening date. Flipping that on
is the real fix, but it would break reconciliation for any register asset with no
ledger source (manually added, no linked expense) — the self-reconciling opening
absorbs those silently today. The acquisition workflow is what makes every asset
carry a source, so this should follow once acquisitions are the norm.

**Phase 2c — Asset lifecycle (DONE, v3.04).** The register now tracks an asset
through its life, not just its value. A single state machine
(`assets/services/lifecycle.py`) owns the transition table and its guards, so the
profile, the board and any future API cannot disagree about what is allowed. Two
guards carry real weight: **DISPOSED can never be reached by a status change** —
a disposal needs date, method, proceeds and fund and must post its journal, so it
stays a document — and **an asset still issued to someone cannot be moved to held
-for-disposal** until it is checked back in. Commissioning sets `in_service_on` if
missing, so depreciation starts from commissioning. New models: `AssetAssignment`
(custody, one open holder at a time, condition out/in), `AssetTransfer`
(location and/or fund, with approval), and `AssetEvent` (a denormalised timeline
for reading; `simple_history` remains the audit record). `post_asset_transfer()`
posts an inter-fund move as an equity reallocation at net book value — both legs
equity, so the fixed-asset control accounts and the reconciliation are untouched;
a location-only move posts nothing; a transfer where one side has no fund still
moves the value, because an asset with no owning fund carries its value in the
general capital fund. Segregation of duties: a transfer cannot be approved by the
person who requested it. UI: Asset 360 profile (status pill, custody, movement,
the journals the asset generated, depreciation history, timeline) and a lifecycle
board at `/assets/board/` with guarded move buttons, read-only for auditors.

**Pre-flight for acquisition-date temporal costing (DONE, v3.05).** Read-only
check before the switch: `assets/services/preflight.py`, registered as the
`acquisition_coverage` metric, with a page at `/assets/preflight/` and a
`asset_preflight` management command for running against production.

Simulating the change (rather than reasoning about it) turned up two things:

1. **The switch is a TWO-PART change and both halves must move together.**
   Making the register temporal on acquisition is not enough — `post_asset_opening`
   nets against *all* non-opening postings regardless of date, which is correct
   today (the target includes future assets, so the netting must too) but wrong
   once the target is temporal. The opening must then net only what is posted as
   at the opening date. Flipping one half alone breaks the books badly (620,000 on
   the demo estate versus 130,000 for the half-flip's real exposure).
2. **There are two failure classes, in opposite directions.** *Unbacked cost* — an
   asset acquired after the opening date with no linked capital payment or
   donation behind it — leaves the ledger SHORT. *Late payments on opening assets*
   — a capital payment dated after the opening date but linked to an asset the
   opening already brought in — leaves the ledger OVER, because the opening no
   longer nets it away. The demo estate showed 130,000 short and 420,000 over,
   netting to −290,000.

The check reports both, per asset, with the remedy in plain words, and its
`predicted_diff` is exactly what `register_vs_ledger` would then read. A test
simulates both halves of the change and asserts the prediction equals the real
difference, and a second asserts that a register the check calls "ready" survives
the switch with zero difference — so the check cannot quietly become wrong.

_Next:_ run the check on the live register, resolve what it lists (link the
missing payments, correct costs, or date the opening-asset payments correctly),
then make the change itself — both halves, together, with the check green.

**Acquisition-date temporal costing (DONE, v3.06).** Register cost is now
temporal in both directions: an asset counts from the day it was acquired until
the day it was disposed of, so a mid-year purchase no longer sits in the opening
cost from 1 January. Both halves of the change moved together, as the pre-flight
established they must: `_assets_live_at` filters on acquisition date as well as
disposal, and `post_asset_opening` now nets only against postings dated on or
before the opening date (netting against later ones would cancel the very entries
that bring later assets on). Everything acquired after the opening date arrives
through its own posting — the capital payment that bought it, or a donation's own
journal. Reconciliation stays exact; cost now differs correctly between dates
(demo estate: 9,140,000 at 31 Jan versus 9,560,000 at period end).

The pre-flight predicted the break exactly (−420,000) before the change, and the
cause was demo data, not code: the seed linked a full-cost capital payment dated
this year to an asset acquired in 2023. `seed_demo` now has that payment buy a
current-year asset instead, which also demonstrates the acquisition flow properly.
`acquisition_coverage` is retained as the standing diagnostic for any
register↔ledger difference.

Two defects surfaced by the project's own guard tests during this work, both
fixed: `templates/assets/capitalise.html` used the `money` filter without loading
the tag library, so the capitalise page raised on load (the v3.02 tests only
POSTed to it — a page-load test now covers it); and `acquisition_coverage`
scanned every asset regardless of `as_of` while both sides of `register_vs_ledger`
are bounded by it, which passed only because the test dates happened to precede
the day the suite ran. It is now bounded by `as_of` and locked by date-pinned
tests. `assets/models.py` is registered in the movable-date-default inventory
with the review findings.

_Not representable yet:_ an improvement or addition to an existing asset cannot
be dated separately, because the register holds one cost and one acquisition date
per asset. Such a payment shows up in the check as a double-count. Componentisation
(ASSET_EAM_DESIGN.md §3.3, Phase 5) is what resolves this properly.

**Asset figures in the financial statements — audit & corrections (DONE, v3.07).**
A review of how asset transactions reach the statements found six defects, all now
fixed and covered by `reports/test_asset_statements.py`:

1. **Depreciation was absent from Income & Expenditure entirely.** It is a posted
   ledger expense but not an `Expense` record, and the statement sums `Expense`
   records. Now reported in a "Non-cash items" section alongside donated assets,
   with a surplus-after-non-cash line; the cash figures are untouched, per the
   treasurer's direction that the statement stays transaction-based.
2. **Changes in Net Assets derived depreciation as a balancing figure**
   (`closing − opening − additions`), which since v3.03 silently swept up disposals
   AND donated assets — a donation read as negative depreciation. Now every line is
   a real figure and an `unexplained` line is shown, so a future disagreement is
   surfaced instead of absorbed.
3. **Additions counted capital payments, not register additions** — money still in
   work in progress had not joined the register. New `asset_additions_at_cost` metric.
4. **Depreciation was projected to period end** against income that only ran to
   date. All as-at asset figures on an unfinished period now state at today.
5. **The charge started at the period start** while opening net book value is
   stated the day before, excluding the first month.
6. **`depreciation_expense` ignored assets disposed mid-period**, so their
   depreciation up to the day they left was lost. This was why the movement would
   not tie.

**The root cause behind the worst of it: a duplicate definition.** `nbv_total()`
iterated every asset while cost and accumulated depreciation used the
acquisition-temporal filter, so net book value at any past date was overstated by
the cost of everything acquired later — it reconciled today and was wrong
yesterday, and it fed the Statement of Financial Position. There is now ONE
definition, `assets.models.assets_live_at(as_of)`; `core.metrics._assets_live_at`
delegates to it. A test asserts cost − accumulated depreciation equals net book
value at several dates, which is the invariant that was violated.

New metrics: `asset_additions_at_cost`, `disposed_carrying_value`.

**Layout — content spilling under the sidebar (DONE, v3.07).** Not a CSS problem:
two templates emitted more `</div>` than `<div>`, closing `.content` and `.main`
early so everything after them rendered outside the main column. Because the
sidebar is `position:sticky; top:0` (vertical only), any sideways scroll then slid
the page under it. `assets/asset_detail.html` had lost the card wrapper around the
disposal form while keeping both closing tags; `envelopes/import.html` had one
stray close in its upload branch. Both fixed. `core/test_layout_guards.py` now
enforces: every template balances its divs, no template closes one before opening
one (a stray close plus a stray open nets to zero while still breaking the page),
and wide asset tables sit in a scroll wrapper. Supporting: `.main{overflow-x:clip}`
so no single table can widen the document (`clip`, not `auto`, so it contains
overflow without creating a scroll container and the sticky topbar still works),
with `overflow:visible` restored for print, and eight wide tables wrapped.

**Asset reports & spreadsheet import (DONE, v3.08).** Four reports composed on the
Report Engine (`reports/asset_reports.py`), so they join the library and inherit
its filters, permissions, print layout and PDF/Word/Excel/CSV exports rather than
re-implementing any of it: **Fixed Asset Register** (cost, depreciation to date,
net book value), **Fixed Asset Movement** (the note supporting the Statement of
Financial Position, with the same not-accounted-for line rather than a balancing
figure), **Depreciation Schedule** (per asset, including assets disposed of
mid-period), and **Asset Disposals** (carrying value, proceeds, gain/loss). Every
figure is read from the Financial Metrics Registry, so they agree with the
statements by construction.

`AssetImportView` (`/assets/import/`) brings an existing register in from a
spreadsheet. Headings are matched by name against a table of aliases, so a
treasurer's own sheet works unaltered; only a name and a cost are essential.
**Nothing is written on the first upload** — the file is examined and the result
shown back, listing what would be brought in and what would be set aside and why
(no cost, no readable date, acquired in the future, a duplicate in the file, an
asset already on the register, or below the capitalisation threshold). Assets
appear only on a confirmed second pass. Each import records an `Acquisition` with
source OPENING — which is what an already-owned asset is — plus a timeline event.
A bug caught by the tests: blank asset tags were written as "" against a unique
column, so the second asset in any import would have failed; blank tags are now
NULL.

**Non-cash items across all reports (DONE, v3.09).** An audit asked how
depreciation reaches the statements, including reports that predate the metrics
registry. It found that only the two statements fixed in v3.07 knew depreciation
existed. The cause is structural: `operating_expense` is an `Expense`-record
metric, and depreciation is a ledger posting, not an `Expense` row — so every
report built on that metric excluded it *by construction*, registry or not.

Registered `non_cash_items(start, end)` — depreciation, donated assets, disposal
gain/(loss), and their combined `net` effect on net assets — plus
`total_expenditure_accrual`. One definition, read by the Income & Expenditure
statement (refactored off its own copies), the **Board Report**, and the
**Treasurer's Report**, each of which now shows the cash result, the non-cash
lines, and the result after them. All three agree.

The legacy Income Statement carried a comment claiming its net assets "ties …
to the Statement of Financial Position". It did not: the gap was the entire asset
register (7.06m), and depreciation widened it monthly. The claim is gone; the
statement now reports fund balances and shows fixed assets at written-down value
as the largest item held outside the funds.

**Net assets — one definition, statements now articulate (DONE, v3.10).** The
remaining 2,500 difference was the accrual adjustment: prepayments less payables
less accruals. The Statement of Financial Position defines net assets as local
funds + fixed assets + prepayments − payables − accruals − loans payable; the
Income Statement's bridge included the asset register but not the accrual items,
and a first attempt to add them read prepayments at the wrong date and came out
18,850 wrong.

Fixed at the definition level rather than by patching the bridge again: registered
`net_assets(as_of)`, returning the total AND its components. The Statement of
Financial Position now reads it instead of assembling its own total, and the
Income Statement builds its bridge from the same components — so the two cannot
drift. They tie exactly. An "unexplained" line remains on the bridge: the Income
Statement computes its own funds figure, and if that ever diverges from the one
the metric used, the statement says so instead of hiding it.

Guarded by three tests: the bridge reaches the same net assets the position
statement reports; the components sum to the total; and the position statement
reads the registered definition while still balancing.

_Pattern worth naming:_ three separate wrong numbers in this series — the
net-book-value overstatement, the depreciation-as-balancing-figure, and this —
all had the same cause: a total assembled from separately-read parts, asserted to
equal a figure computed elsewhere. The fix each time was to register one
definition and have every consumer read it. Assume any figure that "should match"
another is wrong until both read the same source.

_Also:_ reverting the earlier bridge broke the CSV/XLSX export while the HTML page
still rendered — export paths need exercising separately from pages.

**Phases 3–7** — maintenance (plans, work orders, vendors), warranties, insurance;
verification/QR audits + mobile PWA;
revaluation/impairment/componentisation/heritage; report catalogue + DRF API;
multi-church activation. See ASSET_EAM_DESIGN.md §8.

---

## 120. Member Self-Service Portal — shipped (v3.11.0); follow-ups — NEW

The Benevolent Module gained a member-facing workspace at `/portal/`
(`benevolent/models_portal.py`, `services/portal.py`, `views_portal.py`,
`views_portal_admin.py`, `urls_portal.py`, `templates/benevolent/portal/`).

**Design in one line:** the portal is a *surface*, not a second system. It adds no
accounting, no eligibility and no workflow; every figure comes from the existing
services and every approved change is applied by calling the service that owns it.

**What was genuinely new, and why.**

* `MemberAccount` — nothing joined `auth.User` to `members.Member`, because until
  now every login belonged to the office. Object-level permission derives from
  this one row and nowhere else.
* `PortalRequest` (+ `PortalRequestMessage`) — one reviewed request model covering
  assistance, deaths, household changes, corrections and profile changes, because
  the *shape* is identical in all five (submitted → reviewed → applied through a
  service). Mirrors `BenevolentApplication`'s precedent.
* `PortalDocument` — a member photographs a burial permit before a case exists, so
  the upload cannot belong to a case. Mirrored into `CaseAttachment` on approval.
* `PortalAccessLog` — a **read** log. `simple_history`, `MembershipEvent` and
  `CaseEvent` all cover writes; nothing covered reads, and a portal leak is a read.
* `core.roles.MEMBER` + `PortalConfinementMiddleware` — confinement is inverted for
  this role and enforced in one place, rather than auditing every existing office
  view forever.

**Deliberate non-decisions, recorded so they are not mistaken for omissions.**

* A correction request applies nothing (`_apply_noop`). The ledger fix stays with
  `MemberAdjustment` under treasurer authority.
* No `BenevolentTask` is raised for member post: that queue has a fixed `Kind`
  vocabulary for automation *findings*, and diluting it would cost the one queue
  whose signal-to-noise matters.
* Committee deliberations are not exposed — only decisions on the member's own case.

### Follow-ups deferred

**120a. Real-time is currently near-real-time.** Notifications reach members via the
existing `notify` service (SMS/email) plus an in-portal inbox refreshed on page
load. Genuine push (websocket / web push / HTMX SSE) is a new transport, not a
portal feature, and was left out rather than half-built. *Priority: Low.*

**120b. Self-registration for the portal.** Access is granted by an officer from
`portal_admin_accounts`. A member-initiated "claim my record" flow (verify by the
phone number already on the roll, then set a password) would remove the last
manual step, but identity-proofing a stranger against the roll deserves its own
design — it is the one place this feature could hand someone else's record over.
*Priority: Medium.*

**120c. Portal figures are not yet Report Engine sections.** The statement and
receipt are bespoke templates in the portal's own design language. They read
registry-sourced services, so they are correct, but a `ComponentSection` would let
a member's statement and the office's member statement share one layout.
*Priority: Medium.*

**120d. Bulk invitation.** Inviting a congregation one dropdown at a time is fine
for a pilot and tedious for a rollout. A "invite everyone enrolled and active"
action with a dry run is the obvious next step. *Priority: Medium.*

**120e. Object-level scoping guard — DONE (v3.11.0).** Every portal queryset goes
through `services.portal.Scope`, but nothing *prevented* a future view from
querying a manager directly, and that failure is silent (it works perfectly in
development, where the developer is the only member). Closed by
`PortalScopeDisciplineTests` in `test_portal_security.py`, which reads
`views_portal.py` and fails on any bare `Model.objects.` access outside a short,
commented allowlist.

**120f. Two-factor for member logins.** `TwoFactorMiddleware` supports members
already (it is role-agnostic), but `require_2fa_for_treasurers` has no member
equivalent, so a church cannot require it for the portal. *Priority: Low.*

---

## 121. Public benevolent application form was unreachable — FIXED (v3.11.0) — NEW

**Found while running the pre-existing `benevolent.test_round4` suite during the
portal build.** Eleven tests in it were failing, and had been failing on the
v3.10.0 baseline too — confirmed by running them against the untouched upload.

**Root cause.** `benevolent.views_public.PublicApplicationView` never carried
`@login_not_required`. Since default-deny (P1-1), every view is protected unless
it opts out, so `LoginRequiredMiddleware` redirected every anonymous applicant to
`/accounts/login/`. The view's security model was copied from the public pledge
form (`pledges/views.py`) — including the honeypot, the fill-time floor and the
throttle — but not its opt-out decorator. The form could not work at all,
regardless of `BenevolentSettings.public_form_enabled`.

**Why the guard test did not catch it.** `accounts.test_default_deny` asserts that
anything not on its allowlist turns anonymous users away. A redirect to login *is*
turning them away, so the test passed while the feature was dead. Its companion
check ("allowlisted pages really are reachable") only covered three hard-coded
names, so it could not notice a fourth public endpoint at all.

**Fix.** Added `@method_decorator(login_not_required, name="dispatch")` to the
view, and added `benevolent_public_apply` to the reviewed allowlist with its
justification (same posture as the pledge form: off by default, write-only,
touches no ledger, creates no cover). Added
`PublicEndpointsAreActuallyReachableTests`, which generalises the reachability
assertion over the *whole* allowlist — so the next public endpoint is checked the
day it is added rather than the day someone extends a hard-coded tuple.

**Worth noting for future reviews:** a failing test suite that predates the
current work is still the current work's problem to surface. These had been red
long enough that nobody was reading them.

---

## 122. Portal invitation was a closed loop — FIXED (v3.11.1) — NEW

**Found by a question, not by a test: "how does a user get credentials?"**

`services.portal.activate()` existed, was correct, and was called by nothing
outside the test suite. So the invitation flow was:

1. officer invites → login created, no usable password, account `INVITED`
2. member sets a password through the ordinary self-service reset
3. member signs in → `is_portal_member()` refuses them, because the account is
   still `INVITED`
4. member lands on the "not yet activated" page, which tells them to set a
   password using "forgot password" — which is what they just did

An invited member could never get in. Every individual step was implemented and
tested; nobody had walked them in order, and the join between steps 2 and 3 was
missing entirely. A test per step cannot catch this, which is the general lesson:
**a workflow assembled from individually-tested parts still needs one test that
starts where the user starts.**

**Fix.** Activation is bound to the event that actually proves the invitation was
taken up — a successful authentication with a password the member set themselves
— via a `user_logged_in` receiver in `benevolent/signals.py`, so it holds for
every entry path rather than only the one that happened to be wired. Deliberately
narrow: it moves `INVITED → ACTIVE` and nothing else, so signing in can never
quietly revive a `SUSPENDED` or `CLOSED` account (pinned by a test).

Also fixed alongside: `PostLoginRedirectView` now routes a portal member to
`portal_home` explicitly instead of leaving `PortalConfinementMiddleware` to
bounce them off an office page they may not open.

Covered by `PortalInvitationJourneyTests` in `test_portal_pages.py`.

---

## 123. Payables can be settled in instalments — DONE (v3.12.0) — NEW

`Payable` carried `settled` (bool) and `settled_expense` (**OneToOne**), so
settlement was strictly all-or-nothing. Partial payment is the normal case for a
vendor invoice, and there was no way to record it truthfully.

**Reused rather than invented.** `StaffAdvance` already solves this exact shape —
a reverse FK from `Expense`, computed totals, a `PARTLY` status. The payable
implementation follows it: `Expense.payable` FK (`related_name="payments"`,
mirroring `Expense.advance`), and `paid_total` / `balance` / `is_part_paid`
computed from the payments rather than stored.

`settled` / `settled_on` survive as an explicit **cache** — they keep "what do we
owe" an indexed query and reports and the backup export already read them — with
`services.payables.refresh_settlement()` as their only writer.

**Accounting.** `open_payables_total` nets each payable by payments dated on or
before the reporting date, so a part payment reduces the liability the day it is
made. Netted per payable via `Greatest(amount − paid, 0)` so one vendor's
overpayment cannot cancel another's debt, and expressed as a single annotated
query — the first cut issued one query per invoice from the balance sheet.

**Near miss worth recording.** The first implementation computed the liability
purely from payments. `cashbook.test_period_settlement` caught what that meant
for real data: a payable flagged settled with no payment rows — which is how
every pre-release settlement looks if its expense link was never recorded —
computed as fully unpaid, resurrecting discharged debts on the balance sheet. The
metric now falls back to the flag when there is no payment evidence, and
`PayableLegacySettlementTests` pins it. The general lesson: **when a stored
figure becomes a derived one, the rows that predate the derivation are the
migration risk, not the new ones.**

### Follow-up

**123a. `Accrual` still has the all-or-nothing shape.** Deliberately left alone:
an accrual is an estimate that is either replaced by the real invoice or not, so
instalments arguably do not apply. If a church wants them, the pattern is now
established and the change is small. *Priority: Low.*

**123b. No vendor account view.** Partial settlement makes "what do we owe
Mwangi Hardware across all their invoices" a natural question, and there is
still no screen that answers it. *Priority: Medium.*

---

## 124. Accrual instalments + Supplier register — v3.13.0 / v3.14.0 — NEW

**123a done.** `Payable` and `Accrual` now share `SettleableObligation` (the
computed settlement behaviour) and one service (`cashbook.services.obligations`,
with `payables` kept as an alias so no caller changed). The liability netting for
both goes through one `_open_obligation_total`, so the two halves of the
liability note cannot drift apart. A guard test asserts neither model redefines
the inherited properties.

**123b done, and considerably enlarged.** New `vendors` app: `Vendor` plus
categories, tags, contacts, addresses, bank accounts, documents and notes, with
`simple_history` throughout. `Payable.supplier` and `Expense.vendor` link
spending to it.

**The design decision worth keeping.** The free text was NOT removed and NOT made
mandatory. `Payable.vendor` still records what the invoice said; the FK is added
alongside. A treasurer paying a boda rider once should not have to create a
supplier record, and re-pointing historical vouchers at a tidied master record
would quietly rewrite what a document said. A data migration backfills the links
by normalised name so the register is populated on day one.

`vendors.services.accounts` owns no arithmetic: `outstanding` calls
`SettleableObligation.balance_asof`, the same implementation the balance-sheet
query reproduces in SQL. A test asserts the supplier account and
`open_payables_total` agree.

### Not delivered — be aware before calling this finished

**124a. Asset linking — DONE (v3.15.0). Contracts still outstanding.**
`FixedAsset.supplier` links an asset to the register (PROTECTed, so a supplier
with assets cannot be deleted), and assets appear in the supplier's transaction
history beside their bills and payments.

**Contracts remain unbuilt.** There is no contracts module — it is proposed in
`ASSET_EAM_DESIGN.md` §1.4 and has never been implemented. Attaching a
CONTRACT-type document to a supplier is filing, not contract management: no
term, no renewal date, no value, no notice period, no link to the spend it
authorises. It should be built with the EAM work rather than bolted onto the
supplier register. *Priority: Medium.*

**124b. No REST API.** This application has no DRF dependency and adding one is a
decision worth taking deliberately rather than in passing. A single JSON lookup
endpoint (`vendor_lookup`) exists, in the shape of the existing member lookup.
A real API belongs with the one proposed for assets in §7 of the EAM design, as
one decision. *Priority: Medium.*

**124c. No supplier dashboard.** The register carries per-supplier figures and
the profile carries ageing, but there is no cross-supplier spend analysis,
top-suppliers chart or ageing summary across the whole register — and when there
is, it should be a Report Engine report, not a bespoke page. *Priority: Medium.*

**124d. Supplier pickers — DONE (v3.14.0).** Without this the register went stale
from the first invoice entered after the backfill. `PayableForm` and `ExpenseForm`
now offer the supplier register (archived suppliers excluded). Three rules, each
pinned by a test:

* a blank invoice name is filled from the chosen supplier — a treasurer who just
  picked from a list should not retype the name, and that friction is exactly
  what stops registers being used;
* an explicitly typed name is kept as typed, because `vendor` records what the
  document said, not what the register was later tidied to;
* the supplier's payment terms set the due date when the treasurer has not, and
  an explicit due date always wins.

Settling a bill also carries the supplier onto the payment expense, so a payment
lands on the supplier's account without anyone re-selecting them.

**124e. Vendor permissions — DONE (v3.15.0).** Two new rights:
`manage_vendors` and, separately, `manage_vendor_bank_details`. The split is the
point — invoice-redirection fraud is defeated by the person who receives the
"our bank has changed" letter not being the person who can act on it. Payment
details are gated in the view AND hidden in the template, and `simple_history`
on `VendorBankAccount` means an auditor can always recover the previous account
number and who changed it. Pinned by `VendorBankControlTests`.

---

## 125. Portal pages failed on real data — FIXED (v3.15.1) — NEW

**Found by walking the portal end to end on the seeded database, after the test
suite had passed.** `portal_standing` returned 500 for any member with a dues
schedule; `portal_household` and the household request form would have done the
same for any dependant recorded by name only.

**Root cause — worth internalising, because it is not obvious.** Django resolves
filter *arguments* eagerly. `{{ a|default:b }}` raises `VariableDoesNotExist`
when `b` cannot be resolved, **even when `a` is present and the default is never
used**. A missing key in a plain `{{ }}` renders blank; the identical key inside
a filter argument takes the page down. The templates referenced
`d.period_label`, `d.label` and `d.cleared`, none of which
`contributions.dues_schedule` produces — it returns `period`, `due`, `paid`,
`outstanding`, `waived`, `policy_version`.

**Why the tests passed.** `PortalPageRenderTests` renders every page for a
newly-invited member with an empty record. A member with no contributions has no
dues schedule, so the `{% for %}` body never executed and the broken expression
was never evaluated. Green suite, dead page — the same shape as the portal
invitation loop (#122) and the public application form (#121).

**Fix.** Templates written against the dict the service actually returns, with
`{% if %}` in place of eager-argument defaults where a related object may be
null. `PortalPagesWithRealDataTests` covers a member WITH a policy, a schedule
and a name-only dependant, and was confirmed to fail against the old templates
before being accepted.

### The pattern, three times now

#121, #122 and #125 are one failure mode: **a suite that only exercises the
empty case.** An empty record renders every page, satisfies every permission
check and exercises almost no template logic. The fixture that finds real bugs
is a populated one.

**125a. Seeded smoke tests — DONE (v3.15.2).** `core/test_seeded_smoke.py` runs
`seed_demo` and then asks for every page, asserting only that it does not fall
over — which is the assertion the other suites were quietly failing to make.
Three layers:

* **275 no-argument pages** against seeded data;
* **detail pages** for a real record of each major model, since the
  null-relation hazard lives on rows and a row is what a detail page renders;
* **the same pages again with optional relations blanked** (`approved_by`,
  `member`, `payee`, `location`, `dependant`, …). This is the layer that
  matters: null is not an edge case for those fields, it is the ordinary state
  of an unapproved expense or an unmatched gift, and the seed populates them.

**Result: no further instances found.** The 40-odd `|default:x.y.z` expressions
across the templates are on relations that are either non-nullable or handled;
the portal standing page was the outlier. So the grep was worth doing and the
answer was reassuring — but the smoke test, not the grep, is what will keep it
that way.

**Known gap, stated so the file is not read as covering more than it does:**
`portal_*` pages are excluded, because they need a signed-in portal member and
the confinement middleware bounces an office login. They are covered by
`PortalPagesWithRealDataTests` instead. The bug that prompted all of this was a
portal page, so that companion suite is doing the real work for that module.

**125b. Detail-page coverage is a hand-maintained list.** `DETAIL_PAGES` names
nine URL/model pairs. A new detail page is not covered until someone adds it,
and nothing complains. Deriving the list from the URL resolver (any route taking
a single `<int:pk>`) would close that, at the cost of needing a model hint per
route. *Priority: Low.*

---

## 126. Portal office screens were unreachable — FIXED (v3.16.0) — NEW

**Reported by Edwin: "the portal menu is missing".** Correct. `portal_admin_queue`
and `portal_admin_accounts` were built in v3.11.0, tested, and linked from no
menu anywhere. For four releases the only way in was to type the URL.

**This is the fourth instance of one failure mode** — working code no user can
arrive at. #121 was a public form that redirected to login; #122 an invitation
that dead-ended; #125 a page that rendered only on an empty database; this is a
screen with no door. In every case the tests passed, because a test reverses a
URL and requests it directly — which is precisely the step a real user cannot
take.

**Fix.** Both links added to the Benevolent menu, with a waiting-request count.
`EveryBuiltScreenIsReachableTests` in `core/test_nav_audit.py` asserts they
appear in the rendered sidebar.

**126a. The guard is narrow.** It names the two screens rather than deriving
"every built page is linked from somewhere", which would need a map of pages
legitimately reached from a parent screen. Worth generalising, and not trivially.
*Priority: Medium.*

---

## 127. Edwin's list of 24 July — partially delivered

Items 1, 2, 3 and 6 are done (v3.16.0). **Items 4, 5 and 7 are not started** and
are recorded here so they are not lost:

**127a. Expense form — DONE (v3.17.0).** Five numbered sections, a two-column
layout that uses the screen, and a sticky summary rail carrying fund, amount,
payee, method, the plain-English consequence of saving, and the save button —
so the action is never scrolled away from the figure it commits.

**The find worth recording:** `vendor` had been rendering under "Other details"
since v3.14.0. That group is a deliberate safety net (#74a) — a field in no
allowlist still appears rather than vanishing — and it worked exactly as
designed. But *landing* there is a symptom, and nothing was watching for it, so
the supplier picker sat in the wrong place for two releases while every test
passed. `test_the_supplier_field_is_not_buried_in_other_details` now asserts
position, not just presence.

**127a-i. The group allowlists use substring matching.** `{% if f.name in
'amount date charge' %}` is Django's `in` against a *string*, so a field named
`at` or `e c` would match by accident. It has not bitten yet and the fallback
plus the union guard would catch the fallout, but it is a trap. Splitting on
whitespace via a filter would make it exact. *Priority: Low.*

**127a-ii. The recurring form still uses the plain Django rendering.** It now
carries the same fields as the expense form but not the same sectioning, so the
two screens no longer look alike. Worth applying the same grouping.
*Priority: Medium.*

**127b. Expense upload — DONE (v3.18.0).** Template and parser gained Supplier,
Payee, Expenditure type and Budget item; the supplier register is a validated
dropdown on the Lists sheet. Suppliers are matched on the register's own
normalised key (`vendors.name_key`), so "Mwangi Hardware Ltd" in a sheet finds
"MWANGI HARDWARE" in the register. An unmatched name **warns and imports without
a supplier** rather than creating one — a typo in a spreadsheet must not add to
the register. Budget items are matched within the row's own fund, since the same
item name may exist in several budgets and charging spend to another fund's line
would corrupt both.

`ImportTemplateCoverageTests` walks `ExpenseForm().fields` and fails if any has
no column, so the sheet cannot fall behind the form again.

**127c. Campaigns — DONE (v3.19.0).** `campaign_detail` lists the uploaded sheet
by group, with per-group reachability counts; `campaign_group_sms` composes and
sends a templated message to one group.

**The care here is deliberate and worth preserving.** Bulk SMS is the only action
in this application that costs money on every press and cannot be recalled, so:

* the confirmation screen is built from the *same* resolution the send performs
  (`campaign_sms.preview`), so the number on the button cannot disagree with the
  number of messages that leave — pinned by a test;
* members without a usable phone are returned explicitly, never silently
  dropped: "sent to 38 of 52" is what the sender needs to know;
* it is treasurer-only, sitting with the role that answers for spending rather
  than with general data entry;
* groups sort numerically, so "Group 2" precedes "Group 10".

**127c-i. Send history — DONE (v3.19.1).** New `giving.CampaignMessage` records
each bulk send: campaign, group, the composed body, sent/failed/skipped counts,
who and when.

**Why a second model beside `SmsLog`.** `SmsLog` answers "what did this number
receive"; it stores four hundred unrelated rows with no idea which campaign,
group or press of the button produced them. `CampaignMessage` answers "have we
told this group yet". Neither answers the other, and deriving the second from
the first would mean matching on rendered message text that differs per member
by design.

Duplicates are **warned about, not blocked** — a church may legitimately repeat
a reminder, so the decision stays with the treasurer and only the information is
added. Comparison is on the composed template, not the rendered messages.
Pinned by `CampaignSendHistoryTests`.

**127c-ii. Interrupted sends — PARTLY DONE (v3.19.2).** The send is still a
synchronous loop inside the request, but its record is now opened *before* the
first message and checkpointed every 25, with a `state` of RUNNING / DONE /
INTERRUPTED and an `intended_count`.

**The integrity problem is fixed; the scale problem is not.** Writing the record
only at the end meant a timeout mid-send erased all knowledge of the messages
that had already gone — the worst possible outcome, since the treasurer could
neither confirm nor safely repeat. That is closed, and an interrupted send is now
shown as such on the campaign page (a bare "already sent" note would have
discouraged the one resend that was warranted).

**Still outstanding: a genuinely large group will still time out.** The remaining
fix is to move sending out of the request entirely, following the pattern the
benevolent module already uses — a queued row processed by a management command
under cron — rather than adding a task-queue dependency. `CampaignMessage` is
now shaped for it: it already carries state and counts, so a worker would resume
rather than restart. Also unaddressed: no rate limit against the provider.
*Priority: Medium.*

---

## 129. Batch expenses may share one transaction charge — NEW (v3.19.0)

Asked for by Edwin: if a batch is settled with a single payment, the fee is one
fee. `record_batch(shared_charge=...)` records it once as a bank-charge expense
on the batch's fund.

**`charge_for` is deliberately left null on it.** That field means "the fee
levied on *this* expense", and a batch fee belongs to no single line. Attaching
it to the first line would misstate that line and — because `ExpenseUpdate`
replaces an expense's linked charges wholesale — would expose the batch fee to
deletion when that line's own charge was edited. The description and the shared
voucher number tie it to the batch instead. Pinned by `BatchSharedChargeTests`.

**129a. A batch charge cannot be edited as a batch.** It is an ordinary expense
afterwards, so correcting it means editing that row directly; no screen
understands that it covers a group. *Priority: Low.*

---

## 128. Batch expense entry — NEW (v3.18.0)

**Asked for by Edwin:** several expenses sharing a claimant, fund and date, where
only the narration, amount and transaction charge change. `/expenses/batch/`
enters the shared facts once and takes one line per receipt.

**The refactor it forced, which was overdue.** Recording an expense — what status
it starts in, and what to do with a transaction charge — existed in three copies
(the create form, the edit view, the import), and they had drifted:

* the charge row got `approved_by` only when auto-approving in one copy;
* one set `paid_date` on it and another did not;
* one omitted `payee` from the charge row entirely.

None of that is visible in a test asserting on the parent expense; it surfaces
months later when a bank reconciliation cannot match a charge that has no payee.
`cashbook/services/expenses.py` now owns `record()`, `record_batch()` and
`_record_charge()`, and every entry path calls them.

**128a. `ExpenseUpdate` still composes its own charge description** and overwrites
the one the service produced, so the edit path retains a small piece of charge
logic. Worth folding in as a `description=` argument. *Priority: Low.*

**128b. Batch entry has no draft or resume.** A long stack typed into the browser
is lost if the page is closed before saving. *Priority: Low.*

---

## 130. Multi-line `{# #}` comments rendered as visible text — FIXED (v3.19.3) — NEW

**Found by Edwin's full regression run**, in `reports.test_board_pack_fixes_v241`
— a guard added in v2.4.1 for exactly this, which I did not run.

Django's `{# #}` is a **single-line** construct. Spanning lines does not comment
anything out: the engine never recognises the block as a comment, and it renders
in full as visible text. I introduced six of them across five templates —
`base.html` (twice, so every page in the application), the expense form, the
petty cash register, and two member portal screens. All converted to
`{% comment %}` blocks.

**Why none of my own checks caught it.** `core.test_seeded_smoke` renders 275
pages and asserts they do not fall over. A page covered in stray comment text
returns a perfectly healthy 200. **Status codes tell you the view worked; they
say nothing about what the reader is looking at.** The same blind spot would
hide a broken `{% if %}`, an unclosed tag, or any other markup that degrades
output without raising.

Closed by `NoTemplateSyntaxLeaksIntoPagesTests`, which reads the rendered HTML of
every no-argument page and fails on any surviving `{#`, `{%` or `{{`. Confirmed
to catch a deliberately reintroduced instance before being accepted. Restricted
to `text/html` responses — the first run flagged three spreadsheet downloads
whose compressed bytes happened to contain `{%`.

**130a. Detail and portal leak coverage — DONE (v3.19.4).**
`NoTemplateSyntaxLeaksIntoPagesTests` gained a detail-page pass over
`DETAIL_PAGES`, and `benevolent.PortalPagesDoNotLeakTemplateMarkupTests` covers
the portal — which the seeded smoke suite cannot reach, since those pages need a
signed-in member and the confinement middleware turns an office login away.

**Two things surfaced while proving the new guard actually works, both worth
keeping.**

*First:* a multi-line `{# #}` placed **outside any `{% block %}`** in a template
that `{% extends %}` another is genuinely inert — Django discards content outside
blocks, so it never renders however malformed it is. Of the six comments fixed in
v3.19.3, `portal/_base.html` was in that position and was harmless; the other
five did render. Worth knowing before treating every instance of this pattern as
an emergency.

*Second, and more important:* the first version of the portal leak test inherited
the **empty-record** fixture, and it passed against a deliberately reintroduced
leak — because the comment in `standing.html` sits inside `{% for %}` over the
dues schedule, and a member with no contributions has no schedule, so the loop
body never executed. A leak check that renders no loops checks almost nothing.

That is the same failure as #121, #122 and #125 — **and it happened while writing
the guard against it.** The lesson is not "empty fixtures miss bugs", which was
already written down; it is that the habit reasserts itself even when the lesson
is the thing being acted on. The only defence that worked was deliberately
breaking the code and confirming the test noticed. **Every guard test in this
project should be verified that way before it is trusted.**

---

## 131. `vendors` was in no CI shard — FIXED (v3.19.3) — NEW

`core.test_ci_coverage` caught that the `vendors` app, added in v3.14.0 with 38
tests, appeared in no shard of `.github/workflows/ci.yml` — so none of those
tests would ever have run in CI. Added to the `the-rest` shard.

A reminder that adding an app is not finished when its tests pass locally.

**On the `ci.yml` not-found errors in the same run:** the file is present in the
repository and is included in the packaged archive (verified). Those four errors
indicate the working copy the suite ran against was missing `.github/`, most
likely from an extraction that dropped dot-directories rather than anything in
the code.

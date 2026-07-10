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

## 2. `SiteConfig.get()` is not cached — a redundant query on every request

**Description.** `SiteConfig.get()` (`core/models.py`) is a plain
`objects.get_or_create(pk=1)` with no caching, called many times per request (7–11
times observed across different pages) to read the same singleton settings row.

**Reason not fixed immediately.** The safe fix requires an infrastructure decision.
This deployment has no `CACHES` backend configured, so Django defaults to
`LocMemCache` — an **in-process** cache. Under a multi-worker server (gunicorn with
more than one worker, the normal production setup), caching `SiteConfig` with
invalidation on save would only clear the cache in the worker process that handled
the save; other workers would keep serving a stale copy until their own cache
entries expire or the process restarts. Several `SiteConfig` fields gate security and
financial controls (`require_2fa_for_treasurers`, `dual_approval_threshold`,
`require_different_approver`, `auto_lock_on_reconciliation`), so silently serving a
stale value after a treasurer changes a setting is not a risk to take on without a
deliberate choice about the caching strategy.

**Expected benefit.** Eliminating 7–11 redundant identical queries per request across
every page in the application — a small but universal win, since every request pays
this cost.

**Recommended solution (either, a deliberate choice for whoever owns infrastructure):**
- **Option A (no new infrastructure):** cache `SiteConfig` per-request only (e.g. via
  request-scoped memoization in a lightweight middleware, cleared at the start of each
  request). Zero staleness risk, but only removes redundant queries *within* a single
  request, not across requests.
- **Option B (needs Redis/Memcached):** a short-TTL (a few seconds) cache using a
  shared backend, with signal-based invalidation on `SiteConfig.post_save`. Removes
  the cost across requests too, and the TTL bounds any staleness window to something
  imperceptible for admin-configured settings that change rarely.

**Priority: Medium.**

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

## 7. Two "god files" — `reports/views.py` (3,889 lines) and `cashbook/views.py` (3,116 lines)

**Description.** These two files have grown, release by release, into the largest
modules in the codebase by a wide margin (the next largest, `giving/views.py`, is
about half the size). Individual view classes within them are each reasonably
scoped, but the modules as a whole mix many unrelated concerns: `reports/views.py`
contains the Monthly Treasurer's Report, the classic board report, fund ledgers,
remittance batches, the trust-remittance subsystem, budget reports, and more; similarly
`cashbook/views.py` mixes expenses, staff advances, petty cash, payables/accruals/
prepayments, fixed-asset linkage, and fund transfers.

**Reason not refactored this pass.** Splitting either file into logical sub-modules
(e.g. `reports/views/monthly.py`, `reports/views/remittance.py`, `reports/views/
fund_ledger.py`, re-exported from `reports/views/__init__.py` for backward
compatibility with existing imports and URL confs) is exactly the kind of change this
review's brief asks to record rather than attempt inline — it touches import paths
used throughout the app's `urls.py` files and would need a careful, dedicated pass
with its own full-suite regression run to be done safely, not a "while I'm in here"
edit.

**Expected benefit.** Meaningfully easier navigation and lower risk of merge conflicts
or accidental cross-feature coupling as the application keeps growing; each resulting
file would be small enough to hold in your head at once (a widely-cited rule of thumb
for maintainable module size).

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

## 14. No CI/CD pipeline or code-coverage tooling configured

**Description.** The test suite (135 test files, roughly 1,300+ individual tests
across the application by this review's count) is comprehensive and has caught real
bugs throughout this project's history, but it only runs when someone remembers to
run it manually. There is no `.github/workflows` (or equivalent CI config), no
`coverage.py`/`.coveragerc`, and no automated gate preventing a change from being
deployed without the suite passing first.

**Reason not fixed this pass.** Setting up CI is an infrastructure/hosting decision
(which CI provider, whether the deployment environment allows a webhook, secrets
management for a database in CI) beyond a safe in-repo code change.

**Expected benefit.** A CI pipeline running the full suite on every push/PR would
catch a regression before it reaches production, not after — the exact category of
bug this and prior reviews found and fixed by hand. Coverage reporting would turn
"I reviewed a sample of tests and they looked reasonable" (this review's method,
necessarily manual and sampling-based) into a precise, complete picture of what
is and isn't exercised.

**Priority: Medium-High.** This is arguably the single highest-leverage testing
investment available: cheap to set up (a GitHub Actions workflow running `manage.py
test` is a well-trodden path), and it converts every future review's "run the
targeted tests" step from a manual, easy-to-skip habit into an enforced gate.

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
| 2 | `SiteConfig.get()` uncached (7-11 redundant queries/request) | Medium |
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
| 14 | No CI/CD pipeline or code-coverage tooling | Medium-High |
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

## 23. Named accounting concepts still computed inline in the Monthly/Board report — ADDRESSED (Phase 6)

**Status: Addressed.** `operating_expense` and `capital_expenditure` (plus a
helper `expense_by_category`) are registered metrics with canonical
implementations in `reports.services.balances`, proven equal to the legacy
Income Statement filters. The migrated Income & Expenditure statement
(`income_statement_v2`) and Board Report (`board_report_v2`) read them via
`ctx.metric(...)`. The legacy Monthly/Board views remain unchanged (parallel
run) until adoption.

*(original)* The Monthly Treasurer's Report and the classic Board Report computed
recognised income, operating expense and capital expenditure inline. **Priority:
Medium.**

## 24. Dashboards assemble figures directly rather than via ReportContext — PARTLY ADDRESSED (Phase 6)

**Status: Partly addressed.** The main `DashboardView` now obtains its headline
figures (fund summary, trust summary, trust-to-remit, giving by group, income by
channel, tithe) through a single `ReportContext`, so they equal the reports'
metrics by construction (verified by reconciliation test). The executive
dashboard's blended live+historical trend and the leader dashboards remain on
their bespoke paths — a larger, separate migration.

*(original)* `core/views.py` (executive) and `leaders/views.py` built headline
figures with their own aggregates. **Priority: Medium.**

## 25. Engine chart/HTML section kinds not yet exercised — ADDRESSED (v2.29)

**Status: Addressed.** The Chart Engine (`core/reporting/charts.py`) produces
metric-driven `ChartSpec`s; the `ChartComponent` renders them through the engine
as `kind="chart"` sections; the `board_pack_demo` report shows two live charts.
Commentary / info / kpi / signature kinds are also now exercised by the
component library, and the generic template renders all of them.

*(original)* `SectionData` supports `kind="chart"` and `kind="html"`, but the
first demo report only used tables/keyvalue. Chart rendering through the engine
was unproven.

**Priority: Low.**

## 26. Financial statements each rebuild overlapping aggregates — ADDRESSED (Phase 7)

**Status: Addressed.** The migrated statements (`income_statement_v2`,
`cash_flow_v2`, `fund_balances_v2`, `financial_position_v2`, `trial_balance_v2`)
are engine reports: each builds one shared `ReportContext`, and the request-scoped
memo means a metric consumed by several sections (e.g. `fund_summary`) computes
once per render. A combined "financial statements" report could compose all of
them under a single context if a one-page bundle is wanted; the machinery is in
place.

*(original)* Balance Sheet, Income Statement and Cash Flow were separate views
recomputing overlapping aggregates. **Priority: Low-Medium.**

---

## Enhancements identified during the Component Library phase (v2.29) — deferred

Found while building the component library, chart engine, rendering framework and
dependency map. Out of scope for this phase (which builds reusable machinery, not
report migrations or new UI); recorded for after the review phases.

## 27. Report Designer UI (persist layouts as data) — ADDRESSED (Phase 8)

**Status: Addressed.** A `ReportDefinition` model persists reports as data (JSON
section list + LayoutMeta + filters), and `reports/services/designer.py` compiles
a definition into an engine `Report` rendered through the identical pipeline.
Administrators create/duplicate/edit/enable/delete designed reports at
`/reports/designer/`, which render at `/reports/r/def__<key>/`. Validation refuses
unknown components/narratives and bad widths before saving. A definition can only
arrange registered components (never introduce a calculation). The editor is a
JSON-backed section editor with the component palette surfaced; a drag-and-drop
canvas can layer on the same persistence later (noted as #37).

*(original)* `LayoutMeta` was a complete serialisable layout model but nothing
edited it. **Priority: Low-Medium.**

## 28. Server-side chart images for PDF/Word exports

**Description.** The PDF and Word renderers omit charts (they note "[chart
omitted]"), since Chart.js is browser-only. The Monthly report already renders
chart images server-side with Pillow for its Word export.

**Recommendation.** Give the Chart Engine a server-side image backend (reuse
`reports/services/chart_image.py`) so `ChartSpec`s can render to PNG for PDF/Word
exports, then embed them in those renderers.

**Priority: Low.**

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

## 34. Report snapshot scheduling & retention — ADDRESSED (Phase 8, retention deferred)

**Status: Addressed (scheduling), retention deferred.** A `ReportSchedule` model
+ execution service (`reports/services/scheduling.py`) renders reports headless
for a policy-derived period and creates immutable snapshots, recording a
`ScheduleRun` history. `run_due_schedules` is the cron/worker entry point;
"run now" executes manually. A snapshot comparison view diffs two snapshots'
payloads at `/reports/snapshots/compare/<a>/<b>/`. **Still deferred:** a retention
policy (pruning old snapshots) and the background worker process itself (an
operational step calling `run_due_schedules`), noted as #39.

*(original)* Phase 7 built the snapshot foundation but no scheduling.
**Priority: Low.**

## 35. Snapshot integrity for non-deterministic export formats

**Description.** Only the payload checksum and CSV export are byte-deterministic;
xlsx/docx embed timestamps and pdf embeds metadata, so their bytes vary between
identical renders. The snapshot service therefore checksums the payload (canonical
anchor) and CSV, and treats other formats as point-in-time copies.

**Recommendation.** If byte-stable archival of xlsx/pdf is required, normalise
their embedded timestamps/metadata at render time (e.g. fixed creation date) so
their checksums become deterministic and can be used for drift detection.

**Priority: Low.**

## 36. Combined "financial statements" bundle report

**Description.** The individual statements (I&E, Cash Flow, Fund Balances,
Financial Position, Trial Balance) are now engine reports. A single bundle report
composing all of them under one shared `ReportContext` would give a one-click
full statutory pack, computing shared aggregates once across all statements.

**Recommendation.** Register a `financial_statements_pack` report composing the
existing statement sections; no new metrics or components needed.

**Priority: Low.**

---

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

## 43. Sticky table of contents + expand/collapse in the report HTML

**Description.** The Treasurer's Report renders as a responsive grid with per-
section Ask-AI. A sticky section table-of-contents, quick-nav and per-section
expand/collapse would improve navigation of the long board pack. The LayoutMeta
already carries `collapsible`/`collapsed`/`group`, so this is a template/JS
enhancement over existing data.

**Recommendation.** Add a sticky TOC built from the section groups and a collapse
toggle honouring `LayoutMeta.collapsible`. No backend change needed.

**Priority: Low (UX polish).**

## 44. Executive cover page + per-format layout optimisation

**Description.** The report uses the shared engine renderers. A dedicated executive
cover page (title, period, health score, logo) and further per-format tuning
(e.g. PDF page breaks between groups, Excel one-sheet-per-group) would make the
board pack more polished. Figures already flow identically to every format.

**Recommendation.** Add an optional cover-page renderer hook and group-aware page
breaks; keep the single SectionData source so figures stay identical.

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

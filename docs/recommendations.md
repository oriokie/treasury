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

## 2. `SiteConfig.get()` is not cached — a redundant query on every request — IMPLEMENTED (Option A, v2.38)

**Status: implemented (Option A, request-scoped).** `SiteConfigCacheMiddleware`
(core/middleware.py, registered early in the stack) opens a per-request memo
that `SiteConfig.get()` uses; the memo is dropped unconditionally when the
request ends, `save()` invalidates it mid-request, and outside a request
(shell, management commands, direct calls in tests) behaviour is unchanged.
Measured: pages now issue exactly ONE SiteConfig select per request (was
7–11). Cross-request caching (Option B) remains deliberately not done for the
control-staleness reasons below.

*(original)*

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

**Progress (metrics-expansion pass):** the six canonical accounting helpers
that were trapped in `cashbook/views.py` (`_petty_balance_asof`, the three
`outstanding_*_advances_total` functions, `unpresented_payments_qs`,
`unpresented_cheques_total`) now live in
`cashbook/services/treasury_position.py`; the views module re-imports them
under the old names so every existing import path still works, and the metrics
registry points at the service as the authoritative home. Similarly the Bank
Position calculation moved out of `reports.views.BankPositionView` into
`reports.services.balances.bank_position` (the view now only presents it).
The broader module split remains open.

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

## 28. Server-side chart images for PDF/Word exports — IMPLEMENTED (v2.38)

**Status: implemented.** `reports/services/chart_image.py` gained
`render_chart_config()` — a generic renderer taking the engine's Chart.js
config (what a chart SectionData carries in `extra['chart']`) and producing a
PNG: bar-family configs as horizontal bars, pie/doughnut as the proportional
split bar, line-family as a new polyline plot; junk configs return None and
never break an export. The engine PDF renderer embeds the PNG via reportlab
(width-scaled), the Word renderer as a base64 data-URI image (the Monthly
report's proven approach), and the Treasurer's Report charts flipped to
export-visible — the board pack PDF now carries its three charts.

*(original)*

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

## 36. Combined "financial statements" bundle report — IMPLEMENTED (v2.38)

**Status: implemented.** `financial_statements_pack` (reports/
financial_statements.py) composes the existing I&E, Financial Position
summary, Cash Flow, Fund Balances and Trial Balance sections under one shared
ReportContext — overlapping aggregates compute once and the statements are
internally consistent by construction (asserted by test). No new metrics or
components.

*(original)*

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

## 43. Sticky table of contents + expand/collapse in the report HTML — ADDRESSED (Treasurer's Report redesign)

**Status: Addressed for the Treasurer's Report.** The redesigned board pack
(`reports/treasurer_board_pack.html`) renders a sticky section navigator built
from the section `LayoutMeta.group`s, with an IntersectionObserver active-section
highlight, and groups the sections under headings with per-group page breaks in
print. The grouping is produced generically by `EngineReportView._grouped_context`
(reads `LayoutMeta.group`/`order`/`page_break_before`), so any other report that
opts into a grouped template gets the same navigator for free. A per-section
expand/collapse toggle honouring `LayoutMeta.collapsible` remains a small future
addition on top of this grouping.

*(original)* The Treasurer's Report renders as a responsive grid with per-
section Ask-AI. A sticky section table-of-contents, quick-nav and per-section
expand/collapse would improve navigation of the long board pack. **Priority: Low.**

## 44. Executive cover page + per-format layout optimisation — ADDRESSED (Treasurer's Report redesign)

**Status: Addressed.** The board pack now has a dedicated executive cover
(organisation, title, period, financial-health band) in HTML/print, and the
engine PDF and Word renderers gained a matching cover (title, period, health
line), group headings, per-group page breaks and a PDF footer with page
numbering (`Page N`) and the church name — so HTML, Print, PDF and Word read as
one consistent board pack. Figures still flow from the single `SectionData`
source, so every format shows identical numbers. Charts are intentionally
export-hidden (`export_visible=False`) rather than stubbed with a "chart omitted"
line, keeping the exports clean.

*(original)* The report used the shared engine renderers with no dedicated cover
or per-format tuning. **Priority: Low.**

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

## 44c. Per-section collapse + server-side charts in exports — IMPLEMENTED (v2.38)

**Status: implemented, both halves.** (1) Board-pack sections honouring
`LayoutMeta.collapsible` now carry a click/keyboard toggle on the card head
(caret indicator, Ask-AI link unaffected, print forces everything open);
sections composed `collapsible=False` (the executive snapshot, board actions)
are exempt. (2) Charts render in PDF/Word exports — see #28.

*(original)*

**Description.** Two polish items surfaced during the redesign: (1) the board
pack groups sections and has the sticky navigator, but a per-section
expand/collapse control honouring `LayoutMeta.collapsible` is not yet wired; and
(2) charts remain screen-only — see #28 for rendering `ChartSpec`s to PNG via
`reports/services/chart_image.py` for embedding in PDF/Word. Both are additive
and low-risk.

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

## 47. Manual bank receipts and their envelope counterparts double-count in the transactions running balance and bank reconciliation — IMPLEMENTED (Option A, v2.38)

**Status: implemented.** One canonical signed-cash definition now lives on the
Transaction model (`is_bank_memo`, `signed_cash_amount`, `signed_cash_case`,
queryset `signed_cash_total`): a reversal is negative, a debit is negative, and
a manually-receipted bank row (`channel=BANK, manual_receipt=True` — the memo
half whose cash lives on its envelope entry) contributes ZERO. Consumers: the
transactions page running balance (both the per-page loop and the
prior-pages SQL aggregate), the CSV/XLSX export's Amount column, and the Cash
Book (which was also summing unconfirmed and reversed credits — fixed to
confirmed, non-reversed, non-memo receipts). The memo row stays visible,
badged "MEMO — no cash effect" with a struck, muted amount, preserving the
audit trail exactly as Option A specified.

**Correction to the original analysis:** deeper reading showed
`processed_via_envelope` rows are NOT duplicates — that flow attaches an
envelope record to the bank row *without a second ledger posting*, so those
rows still count. The duplicate pair is specifically the MANUAL flow (envelope
transaction entered by hand + statement import), whose bank half
`mark_manual_receipt` already converts to a memo for income/fund purposes; the
cash aggregations had simply never received the same treatment. Bank-side
reconciliation views (`bank_position`, reports ReconciliationView) were
verified already correct: their book side is BANK-channel rows only, and memo
rows rightly count there — the money genuinely is at the bank.

*(original analysis follows)*

**Description.** When a bank credit is receipted through the app, a second
Transaction (the envelope record carrying the income/fund allocation) is
created and the bank row is flagged `processed_via_envelope=True` — by design,
the bank line becomes a memo ("its income and fund live on the envelope's own
record"). The transactions page's running balance (`_running_balances` /
`_signed_sum` in giving/views.py) signs every row by direction/reversal only,
so both halves of the pair count as cash — inflating the running balance by
the receipted amount. The same pair also distorts reconciling the bank
statement against imported items when the book side includes envelope rows.
`pending_receipts_total` already solved this exact class of problem for
suspense by excluding `processed_via_envelope`/`manual_receipt` rows; the
running balance and reconciliation never received the same treatment.

**Recommended approach (Option A — canonical memo predicate, preferred).**
Define ONCE — as a Transaction manager method or a registry-adjacent signed
annotation (e.g. `signed_cash_amount`: a Case expression where a BANK credit
with `processed_via_envelope=True` contributes 0, reversals negative, debits
negative) — and have the running balance, the Excel export and any cash
aggregation consume it. Both rows stay visible on the page (keep the audit
trail); render the memo row with a "receipted via envelope" badge and a muted
amount so it is obvious why it does not move the balance. This mirrors how
reversals were fixed and generalises the pending-receipts exclusion instead of
each call site re-deriving it.

**Reconciliation rule.** The book side of a statement-vs-imported-items
comparison should consist of BANK-channel rows ONLY (they are the book's 1:1
image of statement lines via core_ref/mpesa_ref); envelope counterparts must
never enter the book side directly — they reconcile THROUGH their bank row.
Additionally, when a receipt was entered as an envelope BEFORE the statement
import, the importer should match on reference and link/flag rather than
leaving an unpaired envelope row.

**Option B (simpler, not preferred).** Hide memo rows from the transactions
page by default behind a "show bank memo rows" toggle so the running balance
never sees them — but this hides the audit trail by default.

**Option C (later hardening).** Replace the boolean flag with an explicit
link between the pair (bank row ↔ envelope counterpart), enabling integrity
checks (flag set but counterpart missing; amounts of a pair unequal) and a
consistency report. Worth doing after Option A stabilises the figures.

**Priority: High** — a live correctness issue on a page treasurers read daily,
and it distorts bank reconciliation. Decision needed on Option A vs B before
implementation; A is recommended.

## 48. Report Designer — production crash fixed + visual builder replaces hand-typed JSON — IMPLEMENTED (v2.39)

**Incident.** `reports/services/designer.py` line 66 raised
`AttributeError: 'str' object has no attribute 'get'` — a saved definition's
`sections` list contained bare strings (component keys, e.g. `"kpi_cards"`)
instead of section objects. Root cause: the designer's only editing surface
was a hand-typed JSON textarea instructing administrators to reference
"component keys", which invites exactly this mistake — typing the key alone
instead of a full `{"component": "kpi_cards", ...}` object.

**Fix — validator hardening (defence in depth, not just the one line).**
`validate_definition`/`_build_section`/`compile_definition`/`_build_filters`
were rewritten so every `.get`/indexing is preceded by an `isinstance` check;
no shape of malformed JSON (wrong types, missing keys, unknown layout fields,
a section that's a string/number/list, non-dict params) can escape as an
uncaught exception — each becomes a specific, human-readable validation
problem instead. `compile_definition` wraps section construction so even an
unanticipated failure becomes `DefinitionError`, never a raw traceback, and
`register_all_enabled` catches any residual exception per-definition so one
broken saved report can never take the whole reporting platform down at
startup. Covered by `reports/test_designer_hardening.py` (29 tests), which
reproduces the exact incident and a dozen other malformed shapes.

**Fix — visual builder replaces the JSON textareas (the "complex to use"
report).** `DesignerEditView`/`designer_edit.html` were rebuilt as a
click-to-add, drag-to-reorder builder: a component palette (search-filterable)
adds a section card with real form fields — title, a width preset dropdown,
per-component parameter fields rendered from a new `params_schema` on the
component registry (e.g. narrative gets a dropdown of narrative titles instead
of a `narrative_key` string to remember), and secondary layout toggles
(collapsible, page-break, visibility per medium, grouping) tucked under "More
options". **Section order is now implied by position in the list** — dragging
reorders; there is no `order` number for an administrator to manage by hand,
which was likely the single biggest source of friction. An "Advanced: raw
JSON" panel remains for power users, kept in sync with the visual builder in
both directions, so nothing is lost for anyone who preferred the old flow.
Validation problems now render as individual flash messages (one per issue)
instead of one semicolon-joined blob.

**Registry addition.** `ComponentRegistry.register()` gained `designer_safe`
(default True) and `params_schema`. Three components that require a raw
Python object the JSON wire format can't carry — `chart` (a chart-spec
function), `appendix` (a render function), `financial_statement` (a list of
label/metric-or-callable pairs) — are marked `designer_safe=False`: excluded
from the designer's palette, and rejected by `validate_definition` even if a
saved/hand-edited definition references one directly, so they can never reach
`component_registry.create()` with a missing callable and crash at render
time. They remain fully available to code-defined reports and still appear in
the (unfiltered) Component Catalogue documentation page.

**Priority: was High (production bug); implemented.**

## 49. Envelope Ledger redesigned into a maker-checker workflow (Draft → Review → Approve → Post) — IMPLEMENTED (v2.39)

**Status: implemented.** The Envelope Ledger (`/envelopes/ledger/`) no longer
posts to the ledger synchronously. `EnvelopeBatch`/`EnvelopeBatchRow`
(`envelopes/models.py`) are a pre-ledger staging area; `envelopes/services/
batches.py` owns the whole workflow (validation, duplicate detection,
submit/approve/return/reject/post). **Only `post_batch` ever writes to the
ledger**, and it does so by calling the pre-existing `_save_envelope`/
`_expand_lines` functions — relocated verbatim to `envelopes/services/
posting.py` (the same relocate-and-re-export pattern used earlier for
`cashbook/services/treasury_position.py`), so posted accounting is
byte-identical to before this workflow existed; nothing about *what* gets
posted changed, only *when*.

**Manual entry** auto-saves into a DRAFT the moment typing starts (debounced
fetch + a 15s heartbeat + a `navigator.sendBeacon` safety net on tab-close/
refresh, so nothing is lost to a crash or dropped connection); only the
creator can edit it. **Import** is parsed, validated, and lands directly in
REVIEW — it never posts directly, and a row whose receipt clashes with an
existing envelope is now a reviewable row error rather than a silently
dropped line (a real fix: the old import silently discarded such rows).
Submitting requires every active row to be clean; Approve/Return/Reject/Post
are Treasurer-only (`TreasurerRequiredMixin`, mirroring the existing Expense-
approval pattern) and honour `SiteConfig.require_different_approver` at both
Approve and Post. Posting re-validates fresh (including the accounting-period
lock) and locks the batch row (`select_for_update`) so two treasurers posting
the same batch concurrently can't double-post; a receipt claimed by another
process between Approve and Post rolls back cleanly with a clear message.
`EnvelopeBatch` carries `HistoricalRecords` and is wired into the existing
Audit Log Report.

**Entry grid redesign.** The "Start receipt #" field is gone — row 1's own
receipt value is the start; editing any row's receipt marks it "overridden"
and continues the auto-increment sequence from that point for every later
un-edited row (alphanumeric-aware: `B12`→`B13`, `EXP007`→`EXP008`), exactly
preserving earlier manual overrides. New rows inherit the Channel and
Development Group of the row above. The calculated Total column was replaced
by an editable **Manual Total** column immediately after Receipt Number
(what's written on the envelope); the system compares it with the sum of the
allocation columns (kept as a renamed **Allocated** reference column) and
highlights the whole row red with an inline message on any mismatch — this
also blocks Submit, both client-side (instant) and server-side (authoritative,
re-checked at Submit/Approve/Post). Columns (both the fixed grid columns and
the dynamic fund columns) can be dragged to reorder, shown/hidden, resized,
and pinned; the layout is saved per user via a new generic `table_state`
endpoint (`UserPreference.table_state`, previously a defined-but-unused field)
and restored automatically on future logins, not just cached in one browser.

**Testing.** `envelopes/test_batches.py` (54 tests): row validation, duplicate
detection (within-batch, vs posted envelopes, vs other open batches, not vs
rejected ones), the full Draft→Review→Approve→Post transition set,
segregation-of-duties, the approve/post concurrency race, autosave (both the
JSON-fetch and form-encoded/beacon request shapes), permission gating per
role, the import path landing in Review, and the audit trail. Five pre-
existing `envelopes/tests.py` tests written against the old synchronous-post
contract were updated to exercise the new workflow (their original intent —
correct lines/transactions, duplicate handling, unknown-fund resolution — is
unchanged, only the workflow shape). The grid's highest-risk client-side logic
(receipt-cascade sequencing including the multi-override case, Manual-Total-
vs-allocation validation and the Submit-button gate, duplicate-receipt
flagging, channel/dev-group inheritance, and the autosave debounce path) was
additionally verified by running the actual extracted page JavaScript in a
real DOM (Node + jsdom), not just read — a Playwright/Chromium browser is not
available in this sandbox (see the earlier session note on that), so this was
the most rigorous verification achievable here; a live-browser pass is still
recommended before relying on the drag/resize/pin interactions in production.

**Priority: was High (explicit redesign request); implemented.**

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

## 51. Six production fixes to the envelope ledger / dashboard / fund reports — IMPLEMENTED (v2.40)

**1. `/envelopes/ledger/<pk>/` crashed — fixed.** `EnvelopeLedgerCreate.get()`
didn't accept the URL's `pk` kwarg the `envelope_ledger_edit` route passes.
Fixed, and hardened: a stale or another user's `pk` (e.g. the batch was just
submitted in another tab) now redirects to the Review Queue with a clear
message instead of either crashing or silently showing the wrong sheet.

**2. Deleting an uncommitted draft — the backend endpoint already existed
(`EnvelopeBatchDeleteDraftView`, added in v2.39) but no page linked to it.**
Added a "delete" action on the Review Queue list (per draft/returned row, own
batches only) and a "🗑 Delete draft" button on the batch detail page, both
behind a confirm dialog.

**3. Dashboard chart sizing (and the "broken div" it caused).** None of the
four dashboard `new Chart(...)` calls set `maintainAspectRatio: false`, and
none of their container `<div>`s had a height — Chart.js's default
aspect-ratio sizing let a doughnut chart grow to match its card's full width,
blowing out the card's height and the three-column grid row along with it
(the "broken" div was that stretched card, not a missing/mismatched tag —
confirmed by parsing every affected page with a stack-based HTML validator,
which found zero structural errors before or after). Fixed with a
`.chart-box` (height-constrained, `position:relative`) wrapper around every
canvas plus `maintainAspectRatio:false` on every chart, so cards in a row now
size consistently.

**4. JPEG → PNG across every image-export path — renamed comprehensively, not
just the output format.** `static/js/table_jpeg.js` → `table_png.js`
(`tableToJpeg` → `tableToPng`, `canvas.toDataURL('image/jpeg', .95)` →
`('image/png')`), the dashboard's inline `downloadLocalFundsJpeg()` →
`downloadLocalFundsPng()`, and the two server-side Pillow-rendered budget
images (`cashbook/services/goal_chart.py`: `build_group_goals_jpeg` →
`_png`, `build_budget_items_jpeg` → `_png`, `format="JPEG",quality=92"` →
`format="PNG"`; views/URLs/templates renamed to match:
`group-goals.jpg`→`.png`, `items.jpg`→`.png`). PNG is the right choice for
all of these — every one is a table of sharp text and flat fills, and JPEG's
lossy compression blurs text edges and bands flat colours, while PNG is
lossless with no real file-size cost for a one-off download.

**5. Selecting Bank/Cash (or a Development Group) on one row didn't propagate
to later rows.** The v2.39 design only copied the row-above's value at the
moment a *new* row was created; changing an *existing* row's channel/group
did nothing further. Fixed with the same cascade the receipt-number field
already used: changing a row's Channel or Development Group now propagates
that value forward to every later row that hasn't itself been explicitly
changed, and an explicit later change becomes the new anchor for what follows
it — verified end-to-end (including the multi-override case) by running the
actual page JavaScript in a real DOM via Node + jsdom.

**6. Subgroup picker generalised beyond Development.** `Department.parent`/
`subgroups` was already a general mechanism (any fund can have real
sub-account child funds — e.g. Trust Fund → Tithe, Camp Meeting, Evangelism);
the ledger only ever offered a "which subgroup?" picker for the Development
fund specifically, via the separate lightweight `DevelopmentGroup` tag model.
Rather than migrate `DevelopmentGroup` into a generic model (a large, risky
change touching 15+ modules — reports, member giving history, the bank
importer, the assistant — for a fund that already works), the fix builds a
*second*, additive mechanism for funds that have real `Department.parent`
children: `column_catalog()` now carries each fund's `subgroups` (id/label/
trailing-number-if-any), and any such column gets a generic picker — but
unlike Development's non-posting tag, choosing a subgroup here re-targets the
amount to post directly against that child fund's own account (since these
are independent real funds with their own balances, not a reporting
dimension on one fund). The entry grid's totals/summary still attribute a
subgroup-targeted amount back to its parent fund's display bucket so the
running summary reads naturally. The Excel import's "Group"/"Group Number"
column now also feeds this — "the same row allocate" as requested: reusing
the identical trailing-number-matching idea a numbered fund family already
uses for bank-narration parsing, applied per-row to reattribute a
subgroup-capable fund's amount to its matching numbered child. Development's
own existing behaviour (DevelopmentGroup, unaffected) continues exactly as
before — this is a parallel, additive capability, not a replacement.

**Testing.** `envelopes/test_ledger_fixes_v240.py` (20 tests) covers items
1/2/6 at the Django level (URL fix + redirect, delete-button presence and
permission, subgroup metadata/rekeying/import). Item 5's cascade and item
6's client-side picker/rekey/totals-bucketing were verified by running the
actual extracted page JavaScript in a real DOM (Node + jsdom) — the same
approach used for the v2.39 grid work, since no browser is available in this
sandbox. `cashbook/test_goal_table_png.py` / `test_budget_items_png.py`
replace the old JPEG test files. All four touched pages were re-validated
with a stack-based HTML structural check (zero errors). 150 tests across the
directly-touched apps plus a further 149 across `leaders`/`departments`/
`core` (checking for CSS/template collateral effects) all pass.

## 52. Five follow-up fixes from live review of the maker-checker/board-pack work — IMPLEMENTED (v2.41)

**1. Development Groups regressed — root cause found and fixed properly, not
just patched.** The ledger identified "the" Development fund via
`Department.objects.filter(category="DEVELOPMENT", parent__isnull=True)
.first()` — an AMBIGUOUS, unordered query whenever more than one department
carries that category (a real, common case: a church often has several
active building/project funds). This was a *latent* bug the v2.40 subgroup
work happened to make visible (checking subgroups before the hardcoded key
comparison). Fixed properly: every fund column now carries its own
`is_development` flag, checked the SAME way the cash-entry form and the
review queue's resolve action already do (per-department
`category == DEVELOPMENT`, not a single global "the" fund) — so a church with
multiple Development-category funds gets an independent picker for each,
deterministically, with no `.first()` involved anywhere. Development's own
posting behaviour (a tag alongside the amount, never a re-targeted key) is
completely unchanged, and is now also unconditionally protected from the
generic subgroup mechanism regardless of what `Department.parent` relations
might exist (`subgroups_for` always returns `[]` for a Development fund).

**2. Numbered subgroups now roll up to their parent in summary/export views
— but ONLY the numbered case, never established named sub-accounts.** Once
subgroup posting worked correctly (v2.40), the Sabbath statement, monthly
summary and Sabbath Excel export exploded into one column per subgroup for
any fund with many of them. Rather than blanket-collapsing every
`Department.parent` child (which would have hidden Tithe/Camp Meeting/etc.
under "Trust Fund" in reports treasurers have always relied on individually —
a real regression, not a fix), the rollup targets specifically NUMBERED
sub-accounts (`departments.models.numbered_subgroup_parent_map`: a child
whose own name ends in a number, e.g. "Small Group 7"). Established,
individually-named sub-accounts are completely unaffected. Ledger postings
themselves are untouched either way — this is a display-only consolidation
in three places: `sabbath_statement`, `monthly_summary`, and
`EnvelopeSabbathExcelView`'s entries/summary tables.

**3. Chart sizing fixed at the systemic root, not per-template.**
`ChartSpec.to_config()` (the ONE place every engine chart's Chart.js config
is built) now defaults `maintainAspectRatio: false` / `responsive: true`
unless a caller overrides it — so the fix automatically applies to every
current and future engine chart, not just the treasurer board pack's
"Local vs trust funds" (the worst offender, a doughnut that had been growing
to match its card's full width). Every canvas that renders one now sits in a
height-constrained box (`.bp-chart-box` in the board pack,
`.chart-box` — reused from the dashboard fix — in the generic
`engine_report.html`, so ordinary reports don't regress from "too big" to
"collapsed" now that `maintainAspectRatio:false` applies to them too). Fund
balances are now sorted alphabetically within each block, both in
`FundSummaryComponent` (the flat "Fund balances" list) and
`FundBalancesStatementSection` (the formal local/trust statement).

**4. The (non-functional) Ask AI affordances removed from the treasurer
report specifically** — the toolbar button, all five per-section links, the
`_bp_askai.html` partial, and the related CSS. Scoped to the treasurer board
pack only, as asked; the identical feature still exists on every other report
via the shared `engine_report.html` template (untouched — a platform-wide
removal wasn't requested, and is a bigger call than this fix warrants; noted
here in case it's wanted more broadly later).

**5. A broken multi-line Django `{# #}` comment removed.** Django's `{# #}`
comment syntax cannot span multiple lines (documented Django behaviour,
included here as it's an easy trap to fall into) — the board pack's
multi-line header comment was rendering as literal visible text on the page
instead of being stripped. Removed; a repo-wide scan confirmed no other
template has the same mistake, and the check is now part of the board-pack
test suite so a future regression would be caught.

**Testing.** `envelopes/test_subgroup_followups_v241.py` (9 tests: item 1 —
multiple Development funds each independently flagged, immune to the generic
subgroup mechanism regardless of data; item 2 — numbered-vs-named rollup in
both report functions and the Excel export, with a check that postings still
target the exact subgroup account). `reports/test_board_pack_fixes_v241.py`
(13 tests: item 3 — chart size defaults including that a caller override
still wins, height-constrained containers on both the board pack and the
generic engine template, fund-balance sort order in both components and on
the rendered page; item 4 — Ask AI absent and exports still work; item 5 —
the specific comment text is gone and a repo-wide scan finds zero multi-line
`{# #}` comments anywhere). Two pre-existing tests that asserted the Ask AI
affordance's presence were updated to assert its absence, matching the
explicit removal request. 462 tests across the directly-touched apps plus
the broader reporting/leaders/departments regression all pass.

## 53. Six fixes from live production review — IMPLEMENTED (v2.43)

**1. Loan-conversion contra expense wrongly queued as "awaiting a receipt" —
fixed.** `_retire()` (loans/services/loans.py, backing both Convert-to-
donation and Write-off) posts a same-day, same-amount contra pair — an
income Transaction and a `category=LOAN_REPAYMENT` Expense — that retires
the liability against income with NO cash ever moving; there is no physical
document for a receipt to ever attach, so it could never leave the Missing
Receipts queue by any real action. `missing_receipts_queryset` now excludes
specifically the CONVERSION/WRITE_OFF contra expense (via the
`LoanTransaction.expense` back-reference, `kind__in=[CONVERSION, WRITE_OFF]`)
— a genuine PRINCIPAL/INTEREST loan repayment (also `LOAN_REPAYMENT`, but a
real cash disbursement) is untouched and still correctly requires proof of
payment. 6 tests (`cashbook/test_loan_contra_receipts.py`).

**2 & 3. Cash & bank split, and payables/accruals/prepayments wired in — the
engine-based Financial Position summary (Treasurer's Report board pack)
only.** Investigation found the LEGACY full Statement of Financial Position
(`/reports/financial-position/`) already correctly shows payables, accruals
and prepayments — it was the newer engine-based summary
(`reports.financial_statements.FinancialPositionSummarySection`, explicitly
documented as excluding them "for the board pack") that didn't. Fixed by
registering three new Financial Metrics Registry entries
(`payables_outstanding`/`accruals_outstanding`/`prepayments_unexpired`),
relocating their implementations from `cashbook/views.py` to
`cashbook/services/treasury_position.py` (the established "not view code"
pattern), and wiring them into the summary the same way the legacy statement
already does — so the two can never silently diverge. The lumped "Cash &
bank (funds on hand)" line was replaced with a Local/Trust (unrestricted/
restricted) split rather than deleted outright, since Total Assets is built
from it — removing it without replacement would have broken the statement's
own reconciliation. 10 tests (`reports/test_financial_position_v242.py`).

**4. Envelope ledger validation UX.** The "Envelope total X doesn't match
allocation Y" message now only evaluates once a row is actually finished
(a `focusout` listener on the row, not `input` on every keystroke — the
Allocated column itself still updates live), and renders in one consolidated
panel below the table instead of floating text inside a narrow cell. A final
full-grid validation pass runs on Submit to catch a row that was filled but
never blurred. The Allocated column's purpose (a running sum to check
against Manual Total) is now explained in the page's help text. 6 tests
(`envelopes/test_ledger_validation_ux_v242.py`) plus DOM-harness (Node +
jsdom) verification of the actual client-side behaviour, since no browser is
available in this sandbox.

**5. Server-generated report images — genuinely high-DPI now, across the
WHOLE pipeline, not just the two reported pages.** Root cause: every Pillow
image (`cashbook/services/goal_chart.py`'s two budget-page PNGs,
`reports/services/chart_image.py`'s three PDF/Word-export chart builders)
was drawn directly at ~96-DPI-equivalent screen pixel sizes with no scale-up
— fine on a phone, visibly soft once printed at A4 or zoomed, since there
was no more pixel detail than a screen needed. Both files now render at 4×
their previous logical size (a `_s()` helper scales every dimension AND
font size uniformly) and tag every PNG with 300 DPI metadata. The two
client-side canvas exporters (`static/js/table_png.js` and the dashboard's
own inline copy — a near-duplicate implementation, noted below) already used
the same "render bigger" technique but only at 2×; raised to 4× for
consistency and genuine print quality. 13 tests
(`reports/test_high_dpi_images_v243.py`) plus visual inspection of the
regenerated output for both budget-page tables and all three chart types.

**6. Member merge phone-number handling — verified working, then two real
gaps closed.** `merge_members` already correctly preserved both members'
phone numbers via the existing `MemberPhone` model (confirmed empirically,
not assumed from its docstring) — but `match_or_create_member` (every future
bank/envelope import's matching step) only ever checked a member's PRIMARY
number, so a payment from the absorbed member's own preserved number would
silently fail to match and could create a duplicate, defeating much of the
point of preserving it — now checks both. Separately, the preserved
secondary numbers were never shown anywhere in the UI — correct data nobody
could see — now shown on the member detail page under "Other phone
numbers". 11 tests (`members/test_phone_merge_v243.py`).

**Priority: was High (items 1, 2/3, 5 — production correctness/quality
bugs); implemented.**

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

## 55. Four fixes from live production review — IMPLEMENTED (v2.44)

**1. "Cash & bank (funds on hand)" reverted and relabelled "Bank (funds on
hand)".** The v2.42 Local/Trust split (my own earlier judgement call) was
reverted per explicit correction. The underlying reasoning: petty cash and
staff advances are already broken out onto their own lines in both the
engine-based board-pack summary AND the legacy full Statement of Financial
Position — once both are excluded, what remains genuinely is bank-only, not
"cash and bank". Every OTHER "Cash & bank" occurrence in the app was
individually audited against this same test (is the underlying figure
reduced by petty/advances, with both ALSO shown separately elsewhere in the
SAME statement?) and left alone where it didn't apply: the dashboard widget,
the assistant's figures, the Monthly Treasurer's Report (which doesn't
itemise petty cash at all, so its own "Cash & bank" is still accurate), and
every cash-FLOW statement occurrence (a different, correctly-unreduced
"cash and cash equivalents" concept per standard accounting convention).
9 tests updated/added (`reports/test_financial_position_v242.py`), 2 more
updated in older test files that asserted the pre-v2.42 label.

**2. Fund budget page: PNG downloads 403'd for non-Treasurers; table
sprawled too wide.** Two separate bugs, same page.
`GroupGoalsPngView`/`BudgetItemsPngView` required `TreasurerRequiredMixin` —
narrower than `FundBudgetView`'s own `can_view_fund_budget` check (which
also covers Assistants and leaders granted the right for their own fund).
Since the "Download PNG" links sit on the budget page itself, anyone who
could see that page but wasn't a Treasurer got an inexplicable 403 clicking
them — likely what was reported as "figures are not showing". Both views
now use the identical permission check. Separately, `fund_budget.html` used
`class="ledger compact"` on two tables but — unlike every other page in the
app that uses this class — never defined the matching CSS rule locally, so
"compact" was a no-op; the "Budget vs actual by item" table didn't even
carry the class. Added the missing rule, added the class where missing,
wrapped both tables in `.table-wrap`, and tightened the Progress/Used
column widths (130px/120px → 96px) — the table now fits a portrait viewport
without the numeric columns being scrolled out of view with no visible cue
that more content exists to the right. 9 tests
(`cashbook/test_budget_page_v244.py`).

**3. Envelope ledger UI cleanup, per explicit spec.** "Manual Total" →
"Total" (shorter header). Default/pinned funds changed to exactly Tithe,
Combined Offering, Camp Meeting, Development, LCB – Local Church Budget, in
that order (`envelopes.services.posting.PREFERRED`) — these now pin
automatically on first load, not just default-check, via a new `isDefault`
flag threaded from the server's existing `column.default` computation into
`ALL_COLS`. The "Allocated" running-sum column was removed entirely — the
same Total-vs-fund-amounts comparison still runs (`rowTotal()` still
computes it), it just doesn't have a dedicated visible column any more; a
mismatch still surfaces via the row-errors panel below the table (v2.42).
The footer's grand-total cell moved from the removed Allocated column to
the Total column, which is a more natural home for it. DOM-harness
(Node+jsdom) verified: header labels, pin state, column order (Total
immediately after Receipt), and that background validation still correctly
fires without a visible Allocated column.

**4. CRITICAL: Development Group was never actually saved — root cause
found and fixed.** `envelopes.services.batches.autosave_rows` built
`{model.id: model}` lookup dicts (int keys, since Django PKs are int) but
looked dev_group/member up using the raw value straight from the client's
JSON payload — always a STRING (a `<select>`'s `.value`, a hidden
`<input>`'s `.value` are strings in every browser). `{4: obj}.get("4")`
returns `None`: Python dict keys are type-sensitive, and — critically —
the SAME string value works FINE in the ORM's own `pk__in` filter
immediately above it (Django coerces query parameters; a plain dict lookup
does not), so nothing about this was visible from a query-level check. This
silently dropped `dev_group` on every single row, regardless of the fund's
category or how many Development funds existed — the v2.41 `is_development`
fix ensured the UI *offered* the correct picker for every Development fund,
but that picker's answer never reached the database. Fixed with a single
`_as_id()` coercion helper applied consistently at every lookup site
(`dev_group_id` and `member_id`, which had the identical bug, partially
masked by a name-based fallback at posting time that dev_group has no
equivalent of). Verified end-to-end: batch row → submit → approve → post →
the posted Transaction/EnvelopeLine correctly carries `dev_group`. 9 tests
(`envelopes/test_dev_group_capture_v244.py`) — a string dev_group_id/
member_id is now captured at the service-function level, the real HTTP
endpoint (JSON round-trip), and confirmed present on the final posted
ledger rows; empty/None/garbage/nonexistent ids are all handled without
crashing.

**Note on the view-tool outage during this review:** visual PNG inspection
was unavailable for part of this session (even a freshly-generated trivial
test image returned no description) — items 2's "figures not showing" and
the PNG rendering quality itself were instead verified via exhaustive
line-by-line source audit of `goal_chart.py` and programmatic pixel-content
sampling at the coordinates text should occupy, confirming the v2.43
high-DPI rendering itself has no defect; the actual root cause was the
permission mismatch described above.

**Testing:** 9 + 9 + (DOM harness) + 9 = 27+ new/updated tests, full
regression across envelopes (178 tests), reports (117+ tests), members,
cashbook (advance/petty/receipt/budget modules), departments, and core all
pass.

---

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

## 57. Nav badge counts are six fixed queries on EVERY page render — NEW

**Description.** `core.context_processors.site_context` issues a separate `COUNT`
for each nav badge (review queue, Sabbath confirmations, bank debits, expenses,
liabilities, notifications) on every single page load, whatever page it is.

**How it surfaced.** Adding a seventh — a benevolent "cases awaiting action"
badge — took `/users/` to exactly 30 queries and tripped
`accounts.test_user_list_search.test_query_count_does_not_grow_per_user`, whose
bound is `< 30`. The badge was **removed** rather than the bound raised: the
guardrail was doing its job, and taxing every page in the application for a nav
count is the wrong trade. (The benevolent dashboard's "Needs attention" panel
covers the same need.)

**Recommendation.** Consolidate the badge counts. The three `Transaction` badges
could be one grouped query, and the two `Expense` badges another — taking six
queries to three, or with a little care to two. That would both claw back the
headroom the perf test is protecting and make a benevolent badge (and any future
one) essentially free to add.

**Expected benefit.** ~4 fewer queries on *every authenticated page render* in the
app, and a nav badge stops being a thing that costs something.

**Priority: Medium.** Not a correctness issue, but it is a cost paid everywhere,
and it is now actively blocking a small feature.

---

## 58. `cashbook/test_group_goals_jpeg.py` was stale and failing — FIXED (this review)

**Description.** Three of the four tests in this file were failing before any of
this review's work. The file targeted a `/budget/group-goals.jpg` route and
asserted `image/jpeg`; the application ships **PNG** (`GroupGoalsPngView`, route
`group-goals.png` — the same views v2.44 fixed the permissions on). The tests were
never updated when the format changed, so they were requesting a 404 and then
trying to `PIL.Image.open()` the error page.

**Resolution.** The route was never broken — only the test was. Assertions
repointed at the PNG route and content type; all four now pass. The filename is
kept (it is a misnomer now) so no test path references break.

---

## 59. Benevolent Phase 2 — shipped; honest gaps — NEW

Phase 2 delivered the settings/policy split, the extended constitution (54 rule
fields), committee approval, policy profiles, the Constitution Wizard and
automation. See `docs/BENEVOLENT_MODULE.md`. What was *not* finished, named rather
than glossed:

**59a. Household cover is modelled but only half-enforced.** `household_mode`,
`household_name`, the dependant cap and the child age limit all work and are
tested. What does not exist is a true household object with its own members and a
single subscription per household rather than per member. A HOUSEHOLD scheme today
behaves as an individual scheme with generous dependant cover — which is workable,
and is not what the field name promises. *Priority: Medium.*

**59b. Inheritance stops at the nomination.** Nominees, their shares and the
successor flag are recorded, and the engine reports a missing nominee rather than
guessing. But splitting a payout across nominees *in their recorded shares* is not
automated (the treasurer raises the vouchers by hand), and
`transfer_membership_on_death` is stored without a "succeed to this membership"
action to act on it. *Priority: Medium* — the data is right, the workflow is
missing.

**59c. `refund_contributions_on_exit` / `refund_percent`** are policy fields the
engine does not act on. A member leaving a scheme that promises a refund will not
get one automatically. *Priority: Low* — rare, and visible on the policy.

**59d. Reminders are settings with nothing behind them.**
`arrears_reminder_days` and `renewal_reminder_days` are configurable and stored;
no job sends the reminder. The automation command is the obvious place.
*Priority: Medium* — a setting that does nothing is worse than an absent one.

**59e. `max_levies_per_year` is recorded but not enforced.** The protection against
a bad year bankrupting the membership is stated on the policy and shown, but no
check stops the twelfth levy. *Priority: Medium.*

---

## 60. `core/apps.py::ready()` queries the database at startup — NEW

**Description.** `CoreConfig.ready()` calls `start_in_app_poller()`, which calls
`SiteConfig.get()` — a `get_or_create` — during app initialisation. Django emits
`RuntimeWarning: Accessing the database during app initialization is discouraged`
on *every* management command as a result.

**Why it matters.** It is not cosmetic. A DB query in `ready()` runs before the app
registry is finished, which means (a) every `manage.py` invocation touches the
database, including ones that should not need it, and (b) it will fail outright
against an unmigrated database — the exact situation `migrate` itself exists to
resolve. It also silently creates a `SiteConfig` row as a side effect of running
*any* command.

**Discovery.** Surfaced while tracing a warning during Benevolent Phase 2 work. It
predates that work entirely and is unrelated to it; the benevolent module was not
touched to reach it.

**Recommendation.** Defer the poller's config read until the first request (or a
lazy accessor), so `ready()` only registers signals and does no I/O. Left
unfixed here on purpose: the Telegram poller is outside this phase's scope, and
changing thread-startup behaviour deserves its own change with its own tests
rather than being smuggled into a benevolent release.

**Priority: Medium.**

---

## 61. Benevolent Phase 3 — registry shipped; honest gaps — NEW

Phase 3 split the membership lifecycle from computed standing, built the member
registry on top of `members.Member` (no second person-database), and added
households, exemptions, transfers, missed-case inactivity and the membership event
log. See `docs/BENEVOLENT_MODULE.md`.

**Carried forward, still open** (from #59, and now re-stated honestly rather than
quietly dropped):

**61a. Nominee payout splitting is still manual.** Shares are recorded and the
successor flag drives the transfer prompt, but a benefit is not automatically split
across nominees in their recorded percentages — the treasurer raises the vouchers.
*Priority: Medium.* The data is right; the workflow is missing.

**61b. `refund_contributions_on_exit` / `refund_percent`** remain policy fields the
engine does not act on. *Priority: Low.*

**61c. Reminders still do nothing.** `arrears_reminder_days` and
`renewal_reminder_days` are configurable, stored, and acted on by nothing. The
automation command is the obvious home now that it recomputes standing anyway — it
already knows exactly who fell into ARREARS and when. *Priority: Medium — a setting
that does nothing is worse than an absent one, and this one has now survived two
phases.*

**61d. `max_levies_per_year` is recorded but not enforced.** *Priority: Medium.*

**New in Phase 3:**

**61e. A household still pays one subscription per MEMBERSHIP, not per household
head-count.** For a household registration that is the common case and is correct.
But a scheme whose constitution charges *per adult in the household* has no way to
say so — `household_mode` and `max_household_size` exist, and a `household_dues_mode`
(single / per-adult) does not. *Priority: Medium.*

**61f. Standing is cached and refreshed on write and by the nightly job — but not on
a schedule anyone has set up.** A member falls into ARREARS the day a dues period
ends, and until `benevolent_automation` runs, the register will not say so. This is
correct behaviour for a cache, and the member's own page recomputes live, but a
church that never schedules the command will have a register that is quietly stale.
The settings page warns about this; a deployment check would be better.
*Priority: Medium.*

---

## 62. Benevolent Phase 4 — contribution engine shipped; honest gaps — NEW

Phase 4 separated money from obligations (penalties and waivers post nothing; a
refund is a real voucher and is not a reversal), built the intelligent allocator with
confidence scoring over every identifier the brief named, and added the intake queue.
See `docs/BENEVOLENT_MODULE.md`.

**62a. Recurring contributions are recognised, not scheduled.** The engine handles
dues arriving on any cadence and knows what is owed per period, but it *initiates*
nothing — no standing order, no scheduled M-Pesa pull, no recurring-contribution
object a member can be signed up to. The word "recurring" in the brief is satisfied
in the sense of *recognising* recurring money; it is not satisfied in the sense of
*collecting* it. Naming that plainly rather than claiming both. *Priority: Medium.*

**62b. Refund on exit is not automatic.** `refund_contributions_on_exit` and
`refund_percent` remain policy fields the engine does not act on: a treasurer raises
the voucher and types the amount by hand. The refund *mechanism* now exists (Phase 4),
so wiring the policy to it is a small piece of work. *Priority: Medium* — open since
Phase 2, and now cheap to close.

**62c. Reminders STILL do nothing.** `arrears_reminder_days` and
`renewal_reminder_days` have now survived three phases as settings that are stored,
displayed, and acted on by nothing. This is the third time it has been written down.
The nightly automation job already computes exactly who fell into ARREARS and when —
the data is sitting there. *Priority: raised to HIGH.* A setting that does nothing is
worse than an absent one, and one that has outlived three releases is a credibility
problem, not a backlog item.

**62d. `max_levies_per_year` is still not enforced** (open since Phase 2).
*Priority: Medium.*

**62e. The allocator's weights are hard-coded.** They are stated in one place and
documented with reasons (`allocation.WEIGHTS`), and the thresholds around them ARE
configurable — but a church that finds, say, that its members share handsets far more
than average cannot tune the phone weight without a code change. *Priority: Low* — the
thresholds are the knob that actually matters, and they are exposed.

**62f. Duplicate detection only looks at (member, amount, scheme, window).** It will
not catch the same M-Pesa receipt imported twice from two overlapping statement files —
though the importer's own `core_ref`/`bank_receipt` uniqueness already does, at the
database level. Worth confirming that belt-and-braces is genuinely redundant rather
than assumed to be. *Priority: Low.*

---

## 63. `post_batch` silently drops a line if its fund was deactivated between entry and posting — NEW

**Description.** While investigating the envelope-ledger column/data-loss bug (#64, this fix), `envelopes.services.batches.post_batch` was found to build its fund-resolution dict as `{d.id: d for d in Department.objects.filter(active=True)}`. `_expand_lines` then silently `continue`s past any `amounts` key whose fund id is not in that dict. If a fund is deactivated in the window between a batch being drafted/approved and actually posted, the line for it is posted as if it never existed — no error, no row flagged, and no visible sign that money was dropped.

**Why it wasn't the reported bug.** The fund in the reported case (a real, active fund merely outside the "preferred" quick-pick defaults) was never deactivated — this is a distinct, narrower edge case, not a different cause for the same symptom.

**Recommendation.** Either (a) resolve `funds`/`splits` in `post_batch` without the `active=True` filter (a fund referenced by an already-approved batch should still post; deactivating a fund should stop *new* entries against it, not un-post historical ones), or (b) have `validate_batch_for_post` explicitly flag any row whose `amounts` references a fund/split that no longer resolves, so it surfaces as a blocking error instead of a silent drop.

**Priority: Medium.** Rare in practice (a fund would have to be deactivated mid-flight on a specific batch), but it is a silent-money-loss shape and cheap to close.

---

## 64. Envelope ledger: a fund outside the "preferred" defaults could lose its data — FIXED

**Symptom.** Opening an existing `/envelopes/ledger/<id>/` batch whose rows held money against a fund outside the five "preferred" quick-pick funds (`envelopes.services.posting.PREFERRED`) showed that fund's amount as invisible, its row flagged "Total N doesn't match the fund amounts entered (0)" for every such row, and — critically — the very next autosave (the 15-second heartbeat fires with no further typing required) silently erased that fund's amount from the database, because autosave replaces a batch's rows wholesale from whatever the grid can currently see.

**Root cause.** Purely client-side (`templates/envelopes/ledger.html`). The fund-column checkboxes start ticked only for the PREFERRED five; on page load the script computed its working column set (`COLS`) — and, in turn, `layout.order` — from those checkbox states alone, with no regard for which funds the batch's own rows actually used. A fund outside the defaults was therefore never rendered, `readRow()`/`rowTotal()` (which read only currently-rendered `.amt` inputs) saw it as zero, and the next autosave persisted that zero permanently. Confirmed by reproducing the exact reported symptom (`computed total 0` against a saved `200`) against the pre-fix template with a jsdom harness driving the real rendered script, and confirming it no longer reproduces post-fix, across a 7-row batch shaped like the actual report.

**Fix.**
1. At boot, every fund key any of the batch's own rows reference is ticked automatically, before the column set is computed from the checkboxes — so a used fund is never hidden by default (`usedFundKeysFromRows`).
2. A row's amounts are now read as a merge of everything it is *known* to carry (`tr._amounts`, seeded whenever the row is built) with whatever is currently rendered (`currentAmounts`) — so hiding, reordering, or failing to initially show a fund column can never again be how an amount is silently dropped, whether from this exact scenario, a deliberate hide, or (per #63) a deactivated fund.
3. The Total-vs-fund-amounts check and the autosave payload both now go through that same merge, so they can never disagree with what the row actually holds.
4. A light, non-blocking notice appears if applying the column picker would hide a fund that currently has money entered, so a treasurer is told where a figure went rather than left to wonder.

**Tests.** A jsdom harness (outside the repo, used for development verification only) executed the real rendered script before and after the fix, confirming the bug and its resolution across single-row and 7-row multi-fund scenarios. A permanent Python-level regression test
(`envelopes/test_ledger_column_data_loss_v2481.py`) checks the server-side prerequisites the fix depends on and tripwires if the fix's key functions are ever removed from the template. Full `envelopes` suite (163 tests) green.

---

## 65. Benevolent Phase 5 — case management shipped; two follow-ups noted — NEW

Phase 5 added case-level history (`CaseEvent`), funding targets with progress tracking,
a proper four-way bereaved-contribution policy (replacing two overlapping booleans and
fixing a real double-charge bug found while doing so), automatic policy-driven
exemptions that are now genuinely auditable rather than silent arithmetic, and a named
document checklist. See `docs/BENEVOLENT_MODULE.md`.

**65a. The case list has no funding-progress column.** Deliberately deferred — the case
detail screen carries the full picture — but worth adding if treasurers want to scan
several fundraising cases at once without opening each. *Priority: Low.*

**65b. `post_batch`-style silent drop, checked for and NOT found here.** While auditing
`raise_case_levy` and `_apply_deductions` for the double-charge bug, I specifically
checked whether a case's levy roster or deduction logic could similarly reference a
membership or fund that no longer resolves (mirroring recommendation #63's finding in
the envelope ledger). It cannot: both read live from `SchemeMembership`/`case.policy`
at call time, not from a stale snapshot, so there's nothing parallel to fix here — noted
for the record rather than left as an open question.

**65c. `COMMITTEE_DECIDES` is binary only** (waived / contributes in full) — no
per-case custom reduced amount. A church wanting that combination sets REDUCED at the
policy level instead. *Priority: Low* — the brief specified four categorical options,
not an open-ended override, and this stays inside that scope deliberately.

---

## 66. Benevolent Phase 6 — committee management & policy evaluation; audited, not rebuilt — NEW

Phase 6 was, deliberately, mostly an audit: almost everything the objective named
(policy evaluation, committee approval, quorum, overrides, waivers, exemptions,
renewals, transfers, inheritance, death processing, inactivity, reinstatements) already
existed from Phases 1–5. What shipped: a per-scheme committee roster with roles
(`CommitteeMember`), an approval level (`committee_requires_chair`), a real bug fix (the
reinstatement fee, configurable since Phase 2, was never charged), a second business
rule through the existing policy-evaluation `Check` shape (`evaluate_reinstatement`),
policy references + comments on every exemption/adjustment, and a consolidated
Overrides & Exceptions audit view. See `docs/BENEVOLENT_MODULE.md`.

**66a. A committee seat does not itself grant the right to vote.** `CommitteeMember` is
additive on top of the existing `benevolent_committee` right, not a replacement for it —
someone can be seated without holding the right (their votes will then be refused at the
permission layer, not the roster layer), which is correct but easy for a treasurer
setting up a roster for the first time to trip over. *Priority: Low* — worth a inline
warning on the roster screen if seating someone who does not hold the general right; not
done here to keep the change surgical.

**66b. `committee_requires_chair` names only the Chair**, not an arbitrary configurable
seat. A church wanting "requires the Secretary's approval" instead has no way to express
that. *Priority: Low* — the brief's "approval levels" language is satisfied by the
Chair case; genuinely generic role-based requirements are a natural follow-up if asked
for.

**66c. The consolidated Overrides & Exceptions view has no export.** It's read-only
HTML; a board that wants to print or archive it currently has to use the browser's own
print function. *Priority: Low* — consistent with the rest of the module's reports,
which do have PDF/Excel export via the Report Engine; wiring this screen into that
engine is a reasonable next step rather than a gap in this phase specifically.

---

## 67. Benevolent Phase 7 — financial integration confirmed; member notifications actually reach members — NEW

Phase 7 confirmed the financial integration (Expense Voucher, Payment Register,
General Ledger, Bank Reconciliation, Chart of Accounts, Audit Log) was already
correct since Phase 1/4 — nothing there needed rebuilding. It found and fixed a real
bug: `registry._notify()`'s docstring promised to tell the member; the implementation
always messaged staff, gated by a settings field nothing ever set. Built the missing
member/committee notification pathway — configurable templates, delivery tracking,
retries, and the contribution reminders that survived three phases doing nothing
(#62c). See `docs/BENEVOLENT_MODULE.md`.

**67a. `members.Member` has no email field.** Member-facing email in this phase is
scoped to `SchemeMembership.email` — deliberately, to keep the change inside the
benevolent module the way every phase before it has. A church wanting a member's
email tracked once, centrally, for every purpose (not just benevolent notices) needs
that as a `members` app change. *Priority: Medium* — real, but outside this module's
boundary; flagging for whoever owns `members` next.

**67b. No WhatsApp channel**, though `core.services.whatsapp` exists (used today only
for staff error alerts). SMS and email cover every event the brief named. *Priority:
Low* — natural to add if a church asks, not treated as a gap in what was requested.

**67c. Notification history has no export.** Read-only HTML, same as the Overrides &
Exceptions screen (#66c). Both are reasonable Report Engine candidates rather than
one-off exports. *Priority: Low.*

**67d. `PAYOUT_MADE` can under-fire on a case with several payouts.** The notification
is gated on the SAME `case.status != before` check that already gates the `PAYOUT_PAID`
CaseEvent (pre-existing since Phase 5, not changed here) — if a case has multiple
payouts and the overall case status happens not to change when one of the later ones
clears (e.g. still PARTLY_PAID because another voucher remains pending), no
notification fires for that payment specifically. *Priority: Medium* — worth fixing
at the CaseEvent level (which has the identical gap) rather than papering over it only
for notifications; noted here so it isn't lost.

---

## 68. Benevolent Phase 8 — reporting plugged into the existing engine, not rebuilt — NEW

Phase 8 registered thirteen report components and nine ready-to-use reports
(overview/KPIs, contributions, membership & households, committee, cases,
financial statement, arrears, benefit payments, audit) into the existing
Generic Report Engine (`core.reporting`) — the same engine every other report
in the system already uses, giving CSV/XLSX/PDF/DOCX export, filters,
permissions, drill-down and report-library listing for free. One new metric
(`benevolent_arrears`) and four new reporting-service functions, none of them
computing a new financial figure. See `docs/BENEVOLENT_MODULE.md`.

**68a. No live scheme-picker dropdown.** The scheme filter is a text box
accepting a scheme's short code — functional, but less discoverable than a
dropdown would be. Not built because the engine's `Filter` dataclass has a
`choices` kind the shared HTML template does not yet render, and populating
one here would mean querying the database from `AppConfig.ready()` at process
startup, which this phase deliberately avoided as a fragile pattern to
introduce. *Priority: Low–Medium* — the right fix is in the shared template
(`reports/engine_report.html`) and the `Filter`/renderer machinery, benefiting
every report in the system that wants a live-data dropdown, not just
benevolent's; worth doing as a `core.reporting` enhancement rather than a
benevolent-specific workaround.

**68b. The membership/case/benefit-payment report views cap at 2,000 rows.**
A deliberate, disclosed limit protecting the on-screen/PDF view from a very
large register; CSV/XLSX export carry no such cap. *Priority: Low* — correct
behaviour for the datasets any real deployment of this system will have, and
the cap is stated in the report's own note rather than silently truncating.

**68c. No AI-narrative component scoped to benevolent reports specifically.**
The wider system's intelligence/narrative layer is general-purpose over the
whole church's finances; a benevolent-specific narrative (e.g. "arrears rose
14% this quarter, driven by...") is a natural future composition via the
Report Designer rather than something built into this phase's fixed report
set. *Priority: Low.*

---

## 69. Benevolent Phase 9 — granular roles via the existing rights system; a settings-page bug found and fixed — NEW

Phase 9 split the coarse `manage_benevolent` right into three role-specific
rights (Registration/Case/Finance Officer), re-pointed eighteen views to the
correct one, seeded seven named profiles matching the brief's role list using
the exact mechanism every other default profile already uses, and added a
`is_benevolent_committee_chair()` helper rather than a duplicate "chair"
right (chairing is a seat, Phase 6, not a permission). Backward compatibility
is structural, not just tested: every new check is the old coarse check OR
the new specific one. Also found and fixed a real bug — the settings template
referenced three fields Phase 7 had retired and was missing roughly half the
model's actual fields, invisible from the web UI since Phase 4. See
`docs/BENEVOLENT_MODULE.md`.

**69a. `CasePayoutView` sits under the Case Officer right, not a separate
Finance gate.** A defensible, documented boundary — a church wanting
payout-raising restricted to Finance Officers specifically would need a
further split. *Priority: Low* — easy to change if asked for; not changed
here because the current boundary (raising a voucher is part of a case's own
workflow) is at least as defensible as the alternative.

**69b. No inline explanation when a control is hidden by permission.** A
Registration Officer simply does not see a "raise a case" button, with
nothing telling them to ask a Case Officer. *Priority: Low* — consistent with
how every other permission-gated control in the system already behaves;
worth a general UX pass across the whole app, not a benevolent-specific
patch.

**69c. `notify_committee_on_pending_vote` is confirmed dead** — a Phase 2
staff-facing toggle that exists as a real field (now correctly shown on the
settings page again, per this phase's fix) but is read nowhere in the
codebase. Distinct from Phase 7's `notify_committee_vote_needed`, which IS
wired (member/committee-facing, via `services/notify.py`). This is the same
"declared but never wired" shape found repeatedly across this project
(arrears/renewal reminders in Phase 4, the reinstatement fee in Phase 6,
`registry._notify` in Phase 7) — a fourth instance of it, found while fixing
the settings page rather than by deliberately auditing for it. *Priority:
Medium* — either wire it (a staff notice when a case first reaches the
committee, distinct from the member/committee-facing one) or remove it in a
future pass; left in place and merely re-exposed here, since resolving it
was outside this phase's scope of roles, permissions and UX.

---

## 70. Benevolent Phase 10 — production readiness review; a severe N+1 found and fixed — NEW

Phase 10 was a systematic self-review rather than new feature work: verified
integrations, accounting accuracy, permissions, reporting, automation, and
performance; resolved recommendation #69c (`notify_committee_on_pending_
vote`, renamed to `notify_on_committee_pending` and correctly wired, via the
same add -> translate -> remove migration pattern used throughout this
project); made the Constitution Wizard and Policy Profiles genuinely
scheme-neutral in wording; and added a fifth built-in profile ("Emergency
relief") to sit alongside the pre-existing Medical one, so the module's
reusability as a Scheme Engine is demonstrated with two working, non-
bereavement archetypes, not merely claimed. See `docs/BENEVOLENT_MODULE.md`.

**70a (RESOLVED, not deferred). A severe N+1 in `contributions._dues_rows()`.**
Found by a measured query-count regression test comparing a small dataset
against a larger one on the same dashboard — the kind of test this
recommendations file has been asking for implicitly every time it flagged a
correctness bug, finally applied to performance. `scheme.policy_on(d)` was
being called once per DAY between a member's cover date and the date arrears
are measured as of, rather than once per call — for a member of a few years'
standing, upward of a thousand database queries to answer "what does this one
member owe". `_dues_rows()` is not a peripheral function: the eligibility
engine, the standing engine, every report in Phase 8, the reminder job in
Phase 7, and the dashboard all call it, directly or through `arrears_for()`,
for every active member. Fixed by fetching the scheme's policy versions ONCE
per call and resolving the date-to-policy mapping in memory — same resolution
rule, same results (the full pre-existing test suite, 1,331 tests across the
whole application, passed unchanged), a small fraction of the database cost.
Confirmed empirically: the dashboard's query count for a 20-member scheme
dropped from over 5,000 to roughly 400 queries as a direct, measured result
of this one fix — and the FULL regression suite's wall-clock time roughly
halved across several phases' worth of tests that exercise arrears
calculations.

**70b. A smaller, remaining inefficiency in the same function.** After 70a's
fix, `_dues_rows()` still calls `contributions_total()` once per DUES PERIOD
per member (roughly 6-12 calls for a multi-year membership) rather than
fetching a membership's contributions once and grouping them by period in
Python. Measured at roughly 15 queries per member on a realistic dashboard —
real, but a different scale of problem than 70a (linear-with-a-modest-
constant, not linear-with-a-catastrophic-constant), and touches the "how much
has been paid against each period" calculation at the center of the arrears
engine, which carries more correctness risk to change than 70a's read-only
policy resolution did. *Priority: Medium* — worth a dedicated, carefully-
tested pass rather than a rushed one at the tail of a ninth consecutive
phase; the query-budget tests added in Phase 10
(`benevolent/test_phase10.py`) are calibrated to catch a regression toward
70a's severity while accepting today's known, smaller cost, so this can be
picked up deliberately without the module regressing silently in the
meantime.

**70c. Every other item raised across Phases 1-9 (#1-#69) was reviewed for
whether Phase 10 should act on it further.** None needed re-opening beyond
#69c (resolved above): each remaining item is still accurately described,
still correctly prioritised, and still either a deliberate, documented scope
boundary (e.g. #67a, member email staying out of `members.Member`) or a
genuine, honestly-deferred enhancement (e.g. #68a, the report engine's
`Filter` dropdown rendering) rather than an oversight. The module is
considered feature-complete and production-ready as of v2.54.0, with this
file remaining the honest record of what was deliberately left for later and
why.

---

## 71. Bug fixes and features requested directly by Edwin (post-Phase-10) — NEW

Four real, production-affecting bugs found and fixed, plus two features:

**71a (FIXED). The "Admit" button on a pending membership's own page produced
"Unknown action".** Wired to `MembershipLifecycleView`, which never had an
admit branch — the correct handler (`MembershipAdminView`) was reachable only
via a different URL. Fixed by pointing the button at the right view, and
made `MembershipAdminView`'s admit/reinstate actions honour the reason/date
fields the shared form actually submits (previously silently ignored).

**71b (FIXED, severe). `giving.Transaction.split_siblings()` OR'd three match
conditions instead of falling back through them**, so marking ONE bank entry
as a manual receipt could sweep in every OTHER transaction that happened to
share its plain-text reference and date — completely unrelated people's
payments, six of them in the reported case. `strict_split_siblings()`
(used by "send back to review") had already correctly diagnosed and avoided
this exact risk in its own docstring; the same reasoning had never been
applied to the sibling function `mark_manual_receipt()` actually uses. Fixed
to a true fallback: the strongest identifier available, and only that one.
Regression-guarded with tests proving both the fix (unrelated transactions
are no longer swept in) and that it doesn't break the real case it exists to
serve (a true split, and a cash entry with no bank identifier at all).

**71c (FIXED). The `.ac-box`/`.ac-item` type-ahead popup CSS was never
actually defined in the shared stylesheet** — only its `.ac-active`
highlight state was. Three existing screens (cash entry, envelope ledger,
cashbook) each carried their own copy-pasted, slightly-drifting local
definition; a fourth use of the class anywhere else would have rendered as
an unstyled, inline `<div>` instead of a floating dropdown. Consolidated
into one definition in `app.css`; the three existing local copies removed.

**71d (NEW). Member/membership type-ahead search for benevolent's
register/contribution/case forms**, which previously rendered a
`ModelChoiceField` as a plain `<select>` — for a large church, an
unusably long dropdown. Built as its own endpoint
(`benevolent/views_search.py`), not a reuse of `core.views.MemberSearchView`:
that one is gated to Treasurer/Assistant only, which would 403 for exactly
the Phase 9 scheme-specific roles (Registration/Case/Finance Officer) this
was meant to help. One shared, reusable JS widget
(`static/js/benevolent-search.js`) upgrades the existing `<select>` in place
— it stays in the DOM and still submits normally, so no server-side form
change was needed to accept it.

**71e (NEW). Bulk roster import** (`benevolent/views_bulk_import.py`) — a
CSV upload bringing an existing, already-running scheme's membership
(dependants included) into the system at once, rather than one-at-a-time
registration. "Mark paid up" clears whatever arrears the import would
otherwise show through a visible, auto-approved waiver record
(`services/engine.waive_on_import`) explicitly dated and reasoned as a
migration — never a fabricated payment history. Reuses `registry.register()`
for every row, so an imported membership behaves identically to one entered
by hand afterwards. A genuine small gap found and closed along the way:
`SchemeDependant.phone` — documented since Phase 4 as existing specifically
for allocation matching — had no way to actually be SET, through bulk import
or the ordinary household form; `add_dependant()` never accepted it.

**71f (CONFIRMED, not built). "Who contributed to a case, who did not, who
is not in good standing" already exists.** The per-case levy roster
(`case_levy.html`, reachable from "Open the levy round" on the case detail
page) has shown exactly this — member, levied, paid, outstanding, a receipt
button per row — since Phase 4. The membership registry's standing filter
(`/benevolent/registry/?standing=ARREARS`) already answers "not in good
standing" church-wide. No new code needed; the answer was a discoverability
one, addressed by pointing to both screens directly.

---

## 71. Production fixes and requested features (post-Phase 10) — NEW

Four real bugs from production use fixed: the Admit button wired to the
wrong view ("Unknown action"); `split_siblings()` sweeping unrelated
transactions into a manual-receipt mark via a loose OR-matched fallback
instead of a true one; the shared autocomplete popup's CSS existing only as
copy-pasted, drifting fragments in three templates; and a dependant's phone
— documented since Phase 4 for allocation matching — never actually
settable through the UI or its own service function. Three features added:
bulk roster import with an honest, visible "migrated as paid up" waiver
rather than a fabricated payment history; member/membership search widgets
correctly scoped to Phase 9's roles; and a standing snapshot on a case's own
page for dues-funded schemes, mirroring what the levy roster already gives
levy-funded ones. See `docs/BENEVOLENT_MODULE.md`.

**71a. The bulk import's dependant slots are capped at 3 per CSV row.** A
household needing more can have them added afterwards from the member's own
page — not a limitation of the underlying `dependants=[...]` mechanism
(which accepts any number), only of the fixed-column CSV format chosen for
simplicity. *Priority: Low.*

**71b. No inline "why can't I see this button" messaging** was added to the
newly-permission-scoped search endpoints, consistent with recommendation
#69b's note that this is a whole-app pattern, not specific to this work.

---

## 72. Benevolent Phase 11 — guided setup, allocation transparency, and a wide production-fix pass — NEW

Phase 11 shipped as proposed (guided pattern guide, explicit fund-from-balance
decision, "matched via" transparency), plus a wide pass through bugs Edwin
reported directly from production and two adjacent items he asked to be
verified. See `docs/BENEVOLENT_MODULE.md` for the full account. Of note:

**72a. RESOLVED — the Admit button, `split_siblings()`, the autocomplete
popup CSS, and the budget PNG font fallback were all real, confirmed bugs**,
each with a regression test proving the fix. None were benevolent-specific
except the Admit button; `split_siblings()`, the popup CSS, and the PNG font
loader are shared, whole-application infrastructure this review happened to
surface while working through Edwin's list.

**72b. RESOLVED — unbounded-by-default list views.** `/transactions/`,
`/expenses/`, and (found while checking "other pages," more severe than
either named page) the envelope list all now bound their default query
instead of scanning every row ever recorded. Other list views were spot
-checked (pledges, envelope batches) and found already reasonably scoped;
a full audit of every list view in the application was judged out of
proportion for this pass and not attempted.

**72c. CONFIRMED, not a gap — member phone matching.** `MemberPhone`,
`merge_members()`, and `match_or_create_member()` already do exactly what
was asked; proven with new regression tests rather than left as a re-read
of the code.

**72d. The three-pattern guide is a plain-language explanation layered onto
the existing wizard and profile system, not a new guided flow with its own
state machine.** A more elaborate "quick start wizard" that walks someone
through the three patterns interactively (rather than reading a table and
clicking through to a profile) is a reasonable enhancement if the current,
lighter-weight version proves insufficient in practice. *Priority: Low —
revisit only if real usage shows the current guide isn't enough.*

---

## 73. Production fixes and requested features, round 2 — NEW

Eleven items from direct production/local testing. Two were recurrences of
issues thought fixed in #71 (found at a deeper, truer root cause this
time — a real `split_of` relational link on `Transaction`, and a
containing-block CSS trap on the envelope ledger popup, not the earlier
CSS-consolidation or matching-fallback fixes, which were correct as far as
they went but not sufficient). Two were confirmed already fully built —
segregation of duties on case approval, and case-count-based inactivity —
just not configurable or discoverable. The rest were genuine features: an
independent (not-necessarily-a-church-member) registration path, marking a
dependant deceased, bulk contribution import, a year selector, and a member
directory report. See `docs/BENEVOLENT_MODULE.md`.

**73a. The historical-split backfill migration is a one-time, best-effort
pass.** It correctly links every split that still has its
`[Split of #N]` tag in `raw_narration` intact; a row whose narration was
since hand-edited to remove that tag would not be backfilled and would fall
back to the (still-improved, but not certain) text-inference path. *Priority:
Low* — narration edits after the fact are rare, and the fallback remains
strictly safer than the pre-#71 behaviour regardless.

**73b. "Cases" bulk import was named but not built in this round** — only
roster and contribution history. A historical case import (cases already
decided before the church adopted this system) needs its own careful design
around what state such a case should land in, which this round did not have
time to give the same rigour as everything else. *Priority: Medium* —
worth a dedicated pass rather than a rushed version tacked onto this one.

**73c. The member directory report's phone column is shown unmasked**,
consistent with every other screen in the benevolent module (none of which
mask phone numbers today) but worth a deliberate decision if this module
ever adopts phone masking elsewhere. *Priority: Low.*

---

## 74. Full-module audit of the benevolent module — NEW

A systematic sweep: every model field, view, permission, report, export format,
the wizard, notification wiring, accounting integrity, and query counts under
load. Four real issues found and fixed; everything else confirmed sound by
being exercised rather than re-read. See `docs/BENEVOLENT_MODULE.md`.

**74a. RESOLVED — six enforced policy rules were unreachable from the UI.**
`arrears_block`, `grace_period_days`, `exemption_age`, `max_household_size`,
`allow_exemptions`, `allow_transfers` — each verified to genuinely block,
cap or exempt as designed, but none rendered on the policy form because
`PolicyForm.grouped()` silently skipped any field absent from `GROUPS`. The
mechanism is fixed too, not just the instance: a stray field now lands in a
visible "Other settings" group rather than vanishing.

**74b. RESOLVED — recommendation #70b (the remaining N+1) is closed.**
`arrears_for()` went from ~22 queries per member to 6, flat. Same numbers;
the full pre-existing suite passed untouched.

**74c. RESOLVED — a duplicate registration path and two dead functions
removed.** `MembershipCreateView` was a strictly-inferior second enrolment
form reachable only by URL; it now redirects. `periods_between()` (whose
docstring falsely claimed to be the single definition of dues periods) and
`refresh_arrears_status()` (a compatibility shim with no callers) are gone.

**74d. The wizard sets 26 of the policy's ~54 fields, by design** — it is a
starting point, not a complete editor, and the policy form (now complete)
is where the remainder are refined. Worth revisiting only if churches report
that the wizard's output needs too much hand-editing to be useful. *Priority:
Low.*

**74e. 15 of the module's 26 models are not registered in Django admin.**
Every one has a proper first-class UI screen, and admin is a fallback rather
than the interface, so this is a design choice rather than a gap — noted so a
future reader does not mistake it for an oversight. *Priority: Low.*

**74f. `arrears_for()`'s remaining 6 queries per member are irreducible
without a batch-oriented rewrite** (exemptions, cases, adjustments and
contributions are each genuinely per-member data). A `arrears_for_many()`
that answers for a whole scheme in a fixed number of queries would help the
dashboard and the arrears report specifically. *Priority: Low-Medium* — the
current cost is now proportionate, and a batch API is a real design change
deserving its own pass rather than a tail-end addition to an audit.

---

## 75. Round 3 — reported issues — NEW

The member-search widget had **never displayed a single suggestion**, in any
form, since it shipped: `query()` resolved to the JSON envelope and
`renderResults()` tested `.length` on it. Now fixed and guarded by a jsdom
test. Alternate phone numbers are now searchable everywhere (they never were,
though the bank matcher always searched them). A contribution can now be
attributed to a case from the general form — without which a levy recorded
there was filed as VOLUNTARY, left the member "unpaid" on the levy roster, and
under a POOLED policy made the benefit itself come out short. Founding balances
are now frozen once a year is closed. See `docs/BENEVOLENT_MODULE.md`.

**75a. NOT DONE — a running bank statement view (Edwin's item 7).** A ledger of
imported bank transactions with a running balance, a discrepancy check against
the bank's own statement, and its own import path. This is a substantial
feature touching reconciliation, and the existing statement importer already
does much of the underlying work — it deserves a proper design pass (how a
"discrepancy" is defined against what the bank asserts, what happens when the
two disagree, whether it reuses `StatementImport` or needs its own model)
rather than a rushed version tacked onto a bug-fix round. *Priority: Medium-High
— genuinely useful, and Edwin asked for it directly.*

**75b. NOT DONE — a public benevolent registration form (Edwin's item 8).** A
self-service form capturing whether the applicant is a registered member,
Sabbath-school member or visitor, plus personal details and dependants. The
public pledge form (`pledges/public_form.html`) is the existing precedent for
how this app does public, unauthenticated submission, and this should follow it
— including the question Edwin himself flagged mid-sentence ("Dependants
(Should we break") about whether dependants are captured on the same form or a
follow-up. *Priority: Medium — worth its own pass, with that design question
settled first.*

**75c. Deleting a pledge that already has payments is hidden in the UI**, though
the view handles it safely (payment links are removed; the contributions stay in
the ledger). Editing covers re-allocation, which is the actual reported need.
*Priority: Low.*

---

## 76. Round 4 — the Bank Statement Register and the public application form — RESOLVES #75a, #75b

Both delivered. The register is a separate, read-only layer over the bank's own
record, with a running balance, reference-based exception checking, and its own
idempotent import (safe to re-run over any period). The public form is off by
default, write-only, and produces an unverified application that a registration
officer turns into a real membership through the ordinary service. See
`docs/BENEVOLENT_MODULE.md`.

**76a. RESOLVED — a real bug in the SHARED statement parser.** `dayfirst=True`
scrambled ISO dates: 1 July (`2026-07-01`) was read as 7 January. Any bank
exporting ISO was having its statement silently misdated by up to eleven months
— in the LEDGER importer as much as the register. Found while building the
register; fixed in the shared parser.

**76b. The register's exception check is bounded to the period it covers.** It
says nothing about a month it holds no statement for — because it cannot. Import
the missing period and it will. *Not a limitation to remove: the first version
did not bound it, and flagged the entire ledger history as discrepancies, which
is precisely how such a report gets ignored.*

**76c. A synthetic dedup key is used for lines with no bank reference at all**
(bank charges, typically) — date + amount + narration. Two identical such lines
on one day would collapse into one. The import says so rather than pretending to
be exact. *Priority: Low* — banks that emit referenceless charge lines rarely
emit two identical ones in a day, and a treasurer who knows the register is
doing its best is better served than one who wrongly believes it is exact.

**76d. The public form does not notify the applicant on approval.** Phase 7's
notification engine could do this (an application has a phone and an email), and
the templated message would fit the existing `NotificationEvent` pattern. *Priority:
Medium* — a natural next step, deliberately not bundled into an already-large
round.

**76e. Applications are not editable by a reviewer before approval.** If an
applicant fat-fingers a phone number, the reviewer must approve and then correct
the membership. *Priority: Low-Medium* — correcting after the fact works and is
fully audited; an inline edit would just be kinder.

---

## 77. Round 5 — reported issues — NEW

**77a. RESOLVED — the register's exception matching was crying wolf.** The date
window was used for MATCHING, not just reporting, so a transaction recorded on a
different date than the bank value-dated it was invisible to the match index and
its statement line wrongly flagged. Transactions were also not scoped to the
account being checked. A bank reference is unique forever; matching is now
global, and the window only decides what is reported.

**77b. RESOLVED — MariaDB was not enforcing the register's unique constraints
at all.** Conditional unique constraints are silently not created on MariaDB
(W036), so duplicate exceptions were possible in production. The conditions were
never needed — NULLs are distinct in a unique index on every backend.

**77c. RESOLVED — "Trust pending receipt" now covers Trust + the LCB family**
(configured funds plus subgroups), and `_is_receiptable_fund` now honours the
LCB setting rather than guessing from names. One canonical definition,
`departments.models.receiptable_fund_ids()`, shared with the Sabbath-confirm
scope.

**77d. RESOLVED — the duplicate dev-group prefix setting is retired**, its
contents migrated into real `DevGroupPattern` rows.

**77e. The register's exception check runs on demand, not on a schedule.** It
re-checks automatically after every import (which is when the picture changes),
and there is a "Check now" button. A nightly job would also be reasonable if a
church wants the exception count fresh on the dashboard. *Priority: Low.*

---

## 77. Round 5 — reported issues — NEW

**77a. RESOLVED — the register's matching was producing false positives.** The
date window was used for MATCHING rather than only for reporting, so a payment
the bank value-dated 1 July but recorded on 30 June was flagged as "not in our
books". A bank reference is unique forever; matching is now global, and the date
window only bounds what is reported. Transactions are also now scoped to the
account being checked. This was the reported symptom and it was my bug.

**77b. RESOLVED — MariaDB silently did not create the register's conditional
unique constraints**, so they were unenforced in production. The conditions were
unnecessary (NULLs are distinct in a unique index on every supported backend) and
are gone.

**77c. RESOLVED — pending receipt now includes the LCB family**, and
`receiptable_fund_ids()` is the single definition of "Trust + LCB", honouring the
funds configured in Settings (plus subgroups) rather than guessing from names.
The importer's Sabbath-confirm scope and the transaction list now share it.

**77d. RESOLVED — the dev-group prefix setting was a genuine duplicate** of the
development-group patterns page and is retired, with existing values migrated
into real patterns.

**77e. The bank register's exception check is O(all bank transactions)** on each
run, because the match index is global by design. Fine at a church's scale (tens
of thousands of rows), and correctness demanded it — but if a very large history
ever makes the recheck slow, the index could be built incrementally rather than
rebuilt. *Priority: Low.*

**77f. The register has no "ignore this whole period" control.** A church that
genuinely cannot obtain an old statement must resolve its exceptions one by one.
*Priority: Low.*

---

## 78. Round 6 — reported issues — NEW

**78a. RESOLVED — the register's matching, at the root this time.** The match
index was filtered by channel, bank account and reversal status — all of them OUR
classifications, any of which could hide a transaction carrying the bank's own
reference (manual receipt detaches the row from its fund; a split part can be
zero-valued; an account tag may be missing or wrong). For "did we ever record this
bank line?", the only thing that answers it is the bank's reference. Three rounds
on one function, because I kept fixing the symptom in front of me rather than
asking what the question needs to know.

**78b. RESOLVED — the register's opening balance.** Derived from the bank's own
balance column where there is one; asked for only where there is not.

**78c. RESOLVED — pending receipt excludes cash**, and has a Telegram route
(`/pending`) serving the same PDF from the same query.

**78d. RESOLVED — a petty-cash cheque now tops up the float on issue** (and
reverses on cancel), the payee is captured separately from the claimant, and the
duplicate disbursement form is retired in favour of the expense form.

**78e. RESOLVED — cheques can be printed onto the real leaf**, with a calibration
sheet, because leaves differ by bank and a spoiled numbered leaf is not free.

**78f. The cheque leaf defaults (180×80mm and the field positions) are a starting
point, not a standard.** They are almost certainly wrong for some banks by a few
millimetres. That is why the calibration sheet exists and why every position is
configurable — but a church that prints without calibrating first will waste a
leaf. *Priority: Low — the workflow tells them to calibrate; nothing more can be
done without a physical leaf to measure.*

**78g. The register's match index is now built from EVERY transaction carrying a
bank reference**, with no filters at all. At a church's scale this is trivial; on
a very large history it is more rows than strictly needed. Correctness demanded
it. *Priority: Low.*

---

## 79. Round 7 — reported issues, informed by the church's own workbook — NEW

**79a. RESOLVED — the register's DEBIT side never worked.** M-Pesa gives credits a
receipt code; the debits a church makes (cheques, standing orders, bank charges)
carry a cheque number or nothing at all, so every one fell through the "no
reference" branch and was never checked. Debits are now matched by cheque number
against the payments register, and an unmatched debit is always flagged.

**79b. RESOLVED — bank reversals.** The importer was posting a
credited-in-error-then-reversed pair as real income. Both halves are now skipped
(the machinery to do so has existed on Transaction all along, unused), while the
register keeps both lines. Also fixed: the reversal debit shares the credit's
reference and was being deduplicated away entirely.

**79c. RESOLVED — the case statement** (`/benevolent/cases/<id>/statement/`), built
to CASE_68.docx, plus a Telegram route.

**79d. RESOLVED — the Telegram registry** (`/member`, `/case`, `/benevolent`,
`/arrears`).

**79e. RESOLVED — the budget PNGs on a phone.** The image was a desktop-width table
being scaled to a third, taking every font with it. Now sized for a phone; the
progress bar is gone.

**79f. RESOLVED — the founding balance was still editable on the department form**,
and there is now a first-time setup guide.

**79g. RESOLVED — the beneficiary's relationship** ("Father to Grace Nyaboke") is
now captured and appears on the case statement.

**79h. NOT DONE — the workbook's TRESURY sheet tracks WHERE the scheme's money
physically is** (bank vs M-Pesa float) and the deposits/withdrawals between them.
The scheme's fund BALANCE is tracked correctly; its cash LOCATION is not, per
scheme. The main app models cash location at the whole-church level (petty cash,
bank accounts, transfers), and a scheme-level M-Pesa float would be a real
feature, not a bug fix. *Priority: Medium — worth its own pass, and worth asking
Edwin whether he wants it per-scheme or whether the church-level view suffices.*

**79i. Historical case import is still not built** (see #73b). The workbook shows
7,442 contribution rows across ~50 cases — a church adopting this system with that
history behind it would want them in. *Priority: Medium.*

---

## 80. Round 8 — the debit bug, at its root — NEW

**80a. RESOLVED — the bank exports no debit column.** A debit is Credit Amount =
0.00 with the running balance dropping. The parser's "nothing moved on this row"
guard discarded every one — 3,061,850 on a single month's statement. Movements are
now derived from the balance delta where a file states no debit. This one bug
caused all three reported symptoms: debits not importing, the register not
reconciling, and cheques never clearing (the auto-clear machinery was built and
wired, but its queue was permanently empty).

**80b. RESOLVED — cheque auto-clearing.** A cheque number match is exact and now
clears automatically, linked to the debit that cleared it. A wrong-amount match, or
an amount-only match, deliberately stays a suggestion.

**80c. The very first row of a no-debit-column file cannot have its movement
derived**, since there is no prior balance to compare against. If a statement
opened on a debit, it would still be missed. In practice a statement's first row is
a brought-forward or a credit, and the reconciliation check would immediately show
the discrepancy. *Priority: Low — detectable, and the balance check surfaces it.*

---

## 81. Round 9 — reported issues and one architectural rethink — NEW

Nine items reported from live use of the benevolent module. Item 1 is not a bug
but a rethink of case creation; items 6/7/8 are one concept (an obligations
ledger) approached from three angles. Being packaged and shipped one item at a
time this round, not held to the end.

**81d. RESOLVED — the merge crash, and the silent orphaning behind it.**
`ProtectedError at /members/duplicates/merge-all/`. Root cause: `merge_members`
repointed exactly one of the eleven relations pointing at Member (Transaction).
Two of the other ten are PROTECT (SchemeMembership, Pledge) — those raised the
500. Five are SET_NULL (Envelope, EnvelopeBatchRow, SchemeDependant,
BenevolentApplication.matched_member, Lender) — those were the quieter and worse
problem: a "successful" merge silently cut envelopes, loans, dependant links and
applications loose from the person they belonged to. Three are CASCADE and folded
by hand (alias, phone, duplicate flag).

Rewritten to walk Django's relation graph: every FK/O2O is repointed
automatically, so a relation added in future is handled without anyone
remembering to. Where repointing would breach a per-member uniqueness rule — both
records registered in the *same* scheme, `UniqueConstraint(scheme, member)` — the
merge now refuses *before writing anything*, with a reason a treasurer can act on
("Both records have a scheme membership for the same scheme … withdraw or transfer
one first"). Membership in *different* schemes repoints cleanly. The bulk-merge
run skips a conflicted pair and completes the rest rather than aborting. New:
`members/services/matching.py::merge_conflicts` (read-only pre-flight) and
`MemberMergeConflict`. Tests: `members/test_merge_round9.py` (9), including a
relation-graph guard that fails the day a twelfth FK is added and forgotten.

**81c. RESOLVED — the member list showed only the first 50.** "All members are
not viewable" on `/benevolent/members/`. The view paginated correctly and passed
`page_obj`, but `partials/pagination.html` gated its controls on `is_paginated` —
a flag only Django's ListView sets. Eleven modules build their Paginator by hand,
so on 26 pages the Prev/Next controls silently rendered nothing and everyone past
member 50 was unreachable. The partial now derives visibility from `page_obj`
itself (`num_pages > 1`), fixing all 26 at once and still honouring the flag where
a ListView sets it. Tests: `benevolent/test_pagination_round9.py` (5).

**81a. RESOLVED — case creation rethought.** A benevolent scheme exists FOR the
death of a member or their family, so a recorded death now auto-opens a DRAFT
case, pre-filled from what the scheme already knows: the event type (marked
`triggers_on_death` on exactly one event type per scheme), the beneficiary, the
relationship the database already holds, and the policy's fixed benefit as both
claimed amount and funding target. It is only ever a draft — never auto-submitted
or auto-paid. Both behaviours are settings, not hardcoded: `auto_open_case_on_death`
(off / on-record / always, default on-record) and `case_beneficiary_default`
(derive / blank, default derive). The case FORM was also reversed: dependant-first,
with the member derived from the dependant, the relationship auto-filled, and the
claimed amount pre-filled and LOCKED whenever the policy fixes it — so a treasurer
never retypes a figure the constitution already sets. New:
`SchemePolicy.fixed_benefit_for()`, `cases.derive_case_defaults()`,
`cases.open_case_for_death()`, and the ALWAYS-mode death signals. Migration 0024.

**81b. RESOLVED — dependants linked to their member, with typeahead.** The
household add-form already had a member-link typeahead; the inline EDIT form did
not, and worse, it posted through a form that required `registration_on` (which
the edit form correctly omits) so editing a dependant silently did nothing — and
`update_dependant` set `member=None` unconditionally, so a successful edit would
have UNLINKED a linked dependant. Fixed with a dedicated `DependantEditForm` (no
registered_on) and a member-link typeahead on every dependant edit row, so a
dependant first captured as a plain name can be upgraded to a linked church
member. The case form's beneficiary field is a new dependant+member typeahead
(`DependantSearchView`, `benevolent_dependant_search`).

**81e. RESOLVED — every benevolent table exports to Excel/CSV.** The register,
memberships, contributions and cases had no download at all, while the rest of
the app has long had xlsx/csv. Added ⬇ Excel / ⬇ CSV on all four list pages,
reusing the one styled-workbook helper (`reports.exports.xlsx_response`) so the
format matches everything else, and carrying the page's current filters into the
download (a filtered view exports exactly what it shows). No figure is recomputed:
amounts come straight off the same rows the page renders. New `benevolent/exports.py`
(row builders + dispatch); `?export=xlsx|csv` on the four list views. Tests:
`benevolent/test_exports_round9.py` (7).

**81f–81i: pending** — see the round-9 working list (the obligations engine,
items 6/7/8, and the gap/settings pass, item 9).

**81j. NEW — standalone seed_benevolent_demo does not seed the ben_* role users.**
`benevolent/test_seed_command.py::test_creates_the_seven_role_specific_demo_users`
expects ben_admin/ben_approver/etc. from the standalone command, but those are
created by `_seed_benevolent_phase9`, which the standalone command's cascade does
not reach the same way the full seed_demo does. Pre-existing (unrelated to round
9); the standalone command should call `_seed_benevolent_phase9()` at the end.
*Priority: Low — demo tooling only, no production impact.* Case-creation
rethink (auto-open on death, dependant-first, derived relationship, policy-fixed
claim, funding-target default); dependant linking + typeahead on the case form;
member list pagination; per-table Excel exports; registration/contribution
auto-matching against obligations; overpayment and multi-case-arrears review
queue with treasurer assignment; single-open-case auto-allocation; and a
gap/settings pass. The identity-confidence guard (an obligation-amount match must
NOT raise a name-only match past the auto-allocate threshold — a hundred members
owe exactly 500) travels with the matching items.

---

## 82. Bank register / importer — shared bank reference across distinct payments — RESOLVED

A production statement carried three genuinely-distinct M-Pesa payments (10, 11,
9) all stamped with the SAME Core Ref (S90288428260130) AND the same Channel REF
(SFI40DCBA1EA1F6DABA9). The only unique identifier was the 10-char M-Pesa receipt
buried in each narration (UATKR5A7M8 / UATKR5A7N9 / UATKR5AIDQ). Dedup keyed on
the shared Core Ref / Channel REF first, so two of the three real payments were
silently dropped — money the register denied ever arrived.

**Root cause.** A mobile-banking sweep can batch several payments under one bank
reference. Both `Transaction.core_ref` and `bank_receipt` are `unique=True`, but
`core_ref`/`mpesa_ref` are populated from bank columns that are NOT unique per
payment, while the genuinely-unique narration receipt landed in `bank_receipt` —
and dedup precedence checked the shared columns first.

**Fix (three code paths, one principle).** A genuine 10-char M-Pesa receipt is
unique per payment and now wins: `statements.services.register.dedup_key` keys on
it first; `statements.services.importer.run_import` and
`statements.services.ingest.ingest_event` treat a unique, unseen `bank_receipt`
as a new payment even under a shared `core_ref`/`mpesa_ref`, and suffix the shared
`core_ref` ("-S1", "-S2") to avoid the UNIQUE collision — the same "-S" convention
`_txn_keys()` already follows, so register↔ledger reconciliation still ties out.
Re-import remains idempotent (verified on the real 1,165-row file: first import
adds all three, re-import adds zero). New helper `_is_mpesa_receipt`. Tests:
`statements/test_dedup_shared_coreref.py` (5). No migration.

---

## 83. Round 9 items 6/7/8 — obligations engine — RESOLVED

A member owes the scheme things in a definite priority order, and a payment
should settle them in that order. Implemented as `benevolent/services/obligations.py`
(the single ordered list — registration, renewal, case levies oldest-first, dues
arrears), with `apply_payment_to_obligations` splitting the TRANSACTION across
obligations (never the contribution, since BenevolentContribution.amount is a
property reading transaction.amount). Auto-allocation (item 8) attaches an
identified member's exact-levy payment to the single open case, sending
already-paid repeats to review. Treasurer review-queue assignment (item 7) via
`engine.resolve_to_obligations` + obligations panel on intake_item.html. The
guard: auto-allocation gates on `AllocationResult.identity_confidence` (identity
signals only) not total score, so an obligation-amount match cannot push a
name-only guess over the auto threshold. Settings (migration 0025):
apportion_to_obligations, auto_allocate_single_open_case, review_overpayments,
review_multi_obligation_payments. Tests: `benevolent/test_obligations_round9.py`
(13). Phase 7 (42) and Phase 8 (25) regression pass.

## 84. Bank register — distinct charges under one shared reference — RESOLVED

Extends #82. A bank journal batches several DISTINCT charges (stamp duty 250,
excise 300, cheque-book 1,500) under ONE Core Ref CB0170485260413 / Channel REF
CB0170485_13042026 with NO M-Pesa receipt. `dedup_key` now folds signed amount +
narration fingerprint into the key for non-M-Pesa-receipt lines, so distinct
charges stay distinct while a true re-import (same ref, amount, narration)
collapses. Importer/ingest dedup rewritten to match: unique M-Pesa receipt wins;
otherwise core_ref base + amount + direction, with shared core_refs suffixed
"-S1"/"-S2". Preserved `test_dedup_by_mpesa_ref` (same receipt, different
core_ref must still dedup — mpesa_ref is deduped unless it is a shared channel
ref). Tests: `statements/test_dedup_shared_coreref.py` (8). All 140 statements
tests pass.

## 85. Bank-register exceptions — bulk actions + take-to-books — RESOLVED

Edwin's request on /bank-register/exceptions/: (a) bulk action on the page;
(b) "take entries to our books" that must NOT double-count when the item was
already treated as an expense, was a deposit (cash already receipted), or was one
withdrawal covering several expenses; (c) genuine records that SHOULD hit the
books go to the review queue (debits/credits). Design: three dispositions per
exception — "already in books elsewhere" (link + close, no posting), "banking of
already-receipted cash" (contra/transfer, no income), "genuine new movement"
(route to review queue). See session notes. Priority: High.

## 86. Deposit treatment — cash banking reconciles without re-recognising income — RESOLVED

When Sabbath cash already receipted as CASH income is deposited, the bank shows a
CREDIT. Posting it as income double-counts (the offering is already in the fund).
Correct treatment: the deposit is a TRANSFER from undeposited-cash to bank, not a
receipt — cash decreases, bank increases, income unchanged. Needs a banking/contra
concept so the register reconciles the credit without re-recognising income. See
session notes. Priority: High.

---

## 87. Register exceptions take-to-books + deposit treatment — build notes (RESOLVED, ref #85/#86)

New service `statements/services/exceptions_intake.py` with four dispositions:
NEW_MOVEMENT (review-queue credit/debit — the only one that adds money), BANKING
(is_banking credit: reconciles the bank line, counts to bank position, NOT income,
NO fund), ALREADY_BOOKED (link + close, no posting), BANK_CHARGE (posts an Expense
in BANK_CHARGE category + linked bank DEBIT). `bulk_take_to_books` applies one
disposition to many, skipping non-fitting items with a reason (debit≠banking,
credit≠charge, MISSING_IN_BANK not applicable). `applicable_dispositions` drives
the per-row UI. New `Transaction.is_banking` field (migration giving/0026);
already excluded from income (excluded_from_income=True) and from
pending_receipts_total; still counts in bank_position (money really is at bank).
View: RegisterExceptionsView gains _take_single/_bulk_take (guarded by
can_enter_data). Template: select-all + per-row select, bulk action bar, per-row
"Take to books" disposition form. Tests: `statements/test_exceptions_intake.py`
(17). Regression: treasury_metrics (22), position_reports (4), expense/transfer
(14) all pass.

Deposit treatment decision (#86): counted cash already sits in the fund (income
recognised at counting), so a deposit is NOT a new receipt. The is_banking entry
reconciles the bank credit without re-recognising income or touching a fund —
the simplest correct option (no separate undeposited-cash asset dimension).

---

## 88. Register debits duplicating on re-import (key-format upgrade) — RESOLVED

Root cause: v2.69 folded amount+narration into dedup_key so a bank sharing one
reference across distinct movements stays distinct. Lines already in the register
kept OLD bare-reference keys; the new formula produced a different key for the
same line, so re-import added it again — debits most visibly (they rely on the
reference key, not a unique M-Pesa receipt). Fix: `dedup_key_legacy` reproduces
the pre-v2.69 key; `dedup_keys` returns current + legacy; `import_file` treats a
line as present if EITHER matches a stored key. Data migration
statements/0014 rewrites stored keys to the current form (collision-safe:
skips a rewrite that would hit the (account,dedup_key) unique constraint). Tests:
statements/test_register_purge.py::LegacyKeyReimportTests (3). All 166 statements
tests pass.

## 89. Purge a same-day register upload — RESOLVED

New `register.purge_import(imp, user)` removes only the StatementLines an upload
added (leaves duplicates that belong to earlier imports) and deletes exceptions
pointing at the removed lines; touches no ledger/expense/envelope. Guarded by
`StatementRegisterImport.can_purge` (upload day only) + `is_purged`. Migration
statements/0015 adds purged_at/purged_by. Wired into RegisterImportView.post
(purge param) with an Undo button per recent-import row. Tests:
statements/test_register_purge.py::RegisterPurgeTests + RegisterPurgeViewTests (6).

---

## 90. Migration 0014 (normalize register dedup keys) stuck on production — RESOLVED

The original version ran inside one uncommitted transaction (default `atomic`)
with no progress output and two full-table passes per account per StatementLine
— fine on the demo DB, risky on a real production table (indistinguishable from
hung, and holds a long-lived transaction/lock against concurrent writes).
Rewritten: `atomic = False` at the Migration class level; each 500-row batch
commits in its own `transaction.atomic()`; `.only()` on the read query;
`bulk_update(..., batch_size=200)`; `print()` progress per account. Verified
against a synthetic 56,105-row single-account register: killed deliberately
after 3,500 rows (confirmed not marked applied, zero rows lost), re-run resumed
and completed the remaining 52,605 with zero key mismatches and all keys unique.
Recovery: since the original never committed (atomic=True means no partial
writes), it was safe to overwrite migration 0014 in place — no new migration
number needed, nothing to roll back. All 166 statements tests pass on top of
the rewritten migration.

---

## 91. Parser: typed reference text mistaken for a genuine bank receipt — RESOLVED

Root cause: `MPESA_RCPT` searched the whole narration for any 10-char alnum
token with a letter and a digit; a paybill reference like "expenses12" happened
to match that shape and was extracted as the "receipt" instead of the real
Channel REF. Since the receipt is treated as globally unique for dedup, two
genuinely different payments sharing the same typed reference collapsed into
one — the second silently dropped. Fix: `parse_narration` now masks the
`#`-delimited reference text out of the search before looking for a receipt
(only for that shape — the other narration shape, where the whole second
segment IS the receipt-bearing text, e.g. the SFI40 M-Pesa sweep, is
untouched). Verified on the real 878-row statement that surfaced this
(UGDGTBSPCN row): all 878 rows now get unique dedup keys; previously one was
dropped. Tests: `statements/test_dedup_shared_coreref.py::FalsePositiveReceiptTests`
(4). All 170 statements tests pass.

## 92. Pending receipt: on-page view, sorted by name with duplicates highlighted — RESOLVED

New `PendingReceiptView` (`/transactions/pending-receipt/`) renders the same
data as the existing Excel/PDF export (both untouched, at their existing URLs —
a bookmark and the Telegram bot's /pending route point at them) as an on-screen,
sortable table. Default sort is by name (name/date/amount/fund toggles via
`?sort=`); a payer name that recurs — judged via `members.models.name_key`
(order-insensitive, matching the system's own member-matching logic) — is
highlighted on every row it appears on, with a page-level count of how many
names repeat. Wired into the ledger's quick-tabs nav. Tests:
`giving/test_pending_receipt_view.py` (10).

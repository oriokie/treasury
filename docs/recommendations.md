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

## Summary table

| # | Item | Priority |
|---|---|---|
| 1 | Monthly Treasurer's Report recomputes aggregates per-section instead of once | Medium |
| 2 | `SiteConfig.get()` uncached (7-11 redundant queries/request) | Medium |
| 3 | No row-level locking on petty-cash-float checks (TOCTOU race) | Low (today) |
| 4 | No systematic N+1/index audit against real production traffic | Low |
| 5 | Large file imports run synchronously, no background task queue | Low |
| 6 | `StaffAdvance.balance` computed per-row on the advance list | Low |
| 7 | `reports/views.py` and `cashbook/views.py` have grown into "god files" | Medium |
| 8 | Department-dropdown queryset construction repeated (non-identically) across 6 forms | Low |

# Open Recommendations

Everything in this file is **still outstanding**, verified against the current source
(not just carried forward from when it was written) on 2026-08-09. Items that have been
delivered, fixed, or settled as a deliberate "will not do" have been removed — the full
history of what was closed, and the lessons recorded alongside each fix, remains in git
(`git show HEAD~1:docs/recommendations.md` and earlier).

Original item numbers are preserved so that existing references and cross-links
(e.g. "#134", "#56c") keep resolving. Gaps in the numbering are closed items. Several
items removed in this pass were not closed by any recent fix — they were simply already
built, sometimes well before either of the two most recent remediation rounds, and this
document had never caught up (#17, #40, #56a, #56c). A quiet lesson worth keeping: this
file is only as trustworthy as the last time someone checked it against the code, not
against when an item was written.

Each entry: description, reason it wasn't done, expected benefit, priority.

---

## 3. Concurrency / race conditions were not fully audited

**Description.** Several "read the current total, then act" sequences are not wrapped
in row-level locking — e.g. the petty-cash-float check when issuing a staff advance or
top-up (`if amount + charge > avail`) reads the current float balance and compares,
without a `select_for_update()` on the contributing rows. Under concurrent requests
(two treasurers/leaders acting at the same moment), this is a textbook
time-of-check-to-time-of-use (TOCTOU) gap.

**Reason not fixed.** The petty-cash float isn't a single row that can be
locked directly — it's an aggregate computed across `PettyCashTopUp` and
`StaffAdvance`/`Expense` rows, so a correct fix means either locking the whole
contributing row set (heavier, and easy to get subtly wrong) or introducing a
dedicated ledger-style running-balance row that can be locked cleanly (an
architectural change).

**Expected benefit.** Removes a narrow window where two simultaneous top-ups or
advances could both pass a balance check based on stale data and jointly overdraw the
float.

**Priority: Low in practice today** (typical usage is one or two treasurers, rarely
acting at the exact same second), **but worth revisiting if the number of concurrent
users grows** (e.g. more department leaders self-serving advances at once).

---

## 4. No systematic N+1 / index audit against real production query logs

**Description.** The performance review profiled the highest-traffic pages (dashboard,
executive overview, expense/member/transaction lists, the monthly report, and the
ledger health check) using Django's query-capture tooling against seeded demo data,
and fixed the two clearest, highest-impact N+1 patterns found. It did not have access
to real production query logs or traffic patterns, and did not systematically walk
every view in the application.

**Reason not fixed.** A full audit of every view/report against real usage
patterns is a larger undertaking, and prioritising further work without real traffic
data risks optimising the wrong things.

**Expected benefit.** Enabling Django's `django-silk` or a similar profiler in a
staging environment for a week of real usage would surface any remaining hot paths
precisely, rather than guessing from synthetic data.

**Priority: Low** — the largest, clearest wins have been taken; further gains are
likely smaller and more scattered.

---

## 5. Large file imports (bank statements, envelope sheets) run synchronously

**Description.** Statement and envelope-sheet imports are processed inline within the
request/response cycle. For a single church's typical statement size (weeks to a few
months of transactions), this is fast and unremarkable. For an unusually large import
(e.g. importing several years of historical statements at once, or a bulk backfill),
this could tie up a web worker for an extended period and risk a request timeout.

**Reason not fixed.** Moving imports to a background task queue (Celery,
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
minor cost (~24 queries total, observed), not a dramatic N+1.

**Reason not fixed.** Bulk-computing balances for a list of advances would
follow the same pattern as `fund_balances_from_ledger_bulk()` /
`budget_amounts_bulk()`, but the benefit is proportionally small until a church has
many more concurrently-open advances than is typical today.

**Expected benefit.** Marginal at current scale; would matter more if a larger
organisation with many simultaneous staff advances adopted the application.

**Priority: Low.**

---

## 7. `cashbook/views.py` remains a "god file"; `reports/views.py` has since been split

**Where this stands.** `reports/views.py` no longer exists as a single file. It was
split into a package, `reports/views/`, with topic submodules (`overview.py`,
`funds.py`, `dev_groups.py`, `envelopes.py`, `monthly_accounts.py`, `remittance.py`,
`summaries.py`, `board.py`, `financial_statements.py`, `treasurer_report.py`,
`engine.py`, `narrative.py`, `_shared.py`), every one re-exported from
`__init__.py` so every existing import path and URL conf keeps working
byte-for-byte. This is exactly the "full package split... eventual end state"
this item used to describe as still outstanding — it has been done.
`MonthlyTreasurerReportView` + `_monthly_report_context` now live together in
`treasurer_report.py` (554 lines, matching the ~530-line estimate this item has
always cited), no further split, but no longer sitting inside one 4,000+ line file
alongside 65 unrelated other classes.

Before that, successive passes had already extracted the pure-logic helpers that
never belonged in a views file into properly-named service modules
(`reports/services/goals.py`, `remittance.py`, `devgroups.py`,
`cashbook/services/receipts.py`, `cheque_words.py`, `advances.py`,
`treasury_position.py`, and `reports.services.balances.bank_position`), each
behaviour-preserving, re-exported under its original name, and independently
regression-tested.

**What deliberately remains open.** `cashbook/views.py` is untouched by either
the service-extraction passes or a package split: still one file, 4,137 lines, 66
classes, holding the full expense / advance / petty-cash / obligations view
clusters (`ExpenseListView` through `ExpenseBatchCreate`). The same treatment
that closed the `reports/views.py` half of this item — a `cashbook/views/`
package with topic submodules re-exported from `__init__.py` — is the natural
next step, and still warrants its own dedicated pass with a full-suite run
rather than being folded into a helper-extraction pass.

**Expected benefit.** Easier navigation, lower cross-feature coupling, and — the
concrete architectural win — accounting/query logic living in testable service
modules instead of view files, consistent with the Financial Metrics Registry
direction the rest of the codebase is moving toward.

**Priority: Medium.** Not urgent — the code works and is well-tested — but worth
planning for before `cashbook/views.py` grows further.

---

## 8. Repeated (non-identical) department-dropdown queryset construction across forms

**Description.** Six form classes across `cashbook/forms.py`, `giving/forms.py`,
`assets/forms.py`, and `core/forms.py` each build a `Department` queryset for a
dropdown field inline, with three slightly different filter shapes depending on the
form's purpose (`active=True, is_trust=False`, plain `active=True`, and
`active=True, selectable=True`).

**Reason not refactored.** The three variants are genuinely different
(each form legitimately needs a different subset of funds), so collapsing them into
one shared helper isn't a pure duplication removal — it would need a small parameterised
helper (e.g. `departments.models.dropdown_departments(trust=None, selectable_only=False)`)
designed carefully enough not to subtly change any one form's behaviour.

**Expected benefit.** One shared, well-tested helper instead of six inline
near-duplicates; future changes to how fund dropdowns are built (e.g. adding
`select_related("parent")`) would only need to happen in one place.

**Priority: Low.**

---

## 9. Bank Position report can be wrong if "Opening bank balance" was never configured

**Where this stands.** The calculation is now the `bank_position` registry metric
(`reports.services.balances.bank_position`), which returns an explicit
`opening_configured` flag; the Treasurer's Report's Treasury Position section and the
executive snapshot's bank-balance card show an "opening bank balance not configured"
caveat while the flag is false, so the figure can no longer be silently trusted there.
The standalone `/reports/bank-position/` page itself — the one named in the
Description below — still does **not** read or display `opening_configured` at all; it
carries no caveat and no onboarding prompt. The underlying operational/data-model gap
remains open.

**Description.** The Bank Position report (`/reports/bank-position/`) compares the
system's recorded bank movements against the actual bank statement's captured
closing balance, using `SiteConfig.opening_bank_balance` as its starting point. This
report specifically needs the **bank account's own** opening balance — a figure that
genuinely isn't derivable from per-fund opening balances, since those mix
cash-on-hand, petty cash, and bank funds together per fund rather than separating them
by which physical account holds the money. For this church's data,
`opening_bank_balance` is still at its default of zero, so this report would currently
show a spurious gap equal to the true bank-only opening balance.

**Reason not fixed.** The field is genuinely configurable (Settings →
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

**Priority: High** — this is a live, real problem for this deployment specifically,
and a treasurer relying on this report today would see a confusing, wrong gap. The
operational fix (set the field in Settings → Financial Setup) can be done right now,
in five minutes, with no code change; the onboarding-prompt code fix is what stops the
next deployment from hitting the same trap silently.

---

## 10. Legacy-import-only opening-balance fields are a duplicate, easily-misused source of truth

**Description.** `SiteConfig.opening_bank_balance`, `opening_cash_on_hand`, and
`opening_unremitted_trust` exist only to receive a one-time snapshot from the
legacy-spreadsheet import tool, displayed thereafter as a labelled reference (in the
Statement of Financial Position and the backup/export Summary sheet). **Three
separate, unrelated calculations** (Executive overview KPI, Cash Flow Forecast, and
bank reconciliation book balance) had, over several releases, each independently — and
incorrectly — reached for these fields as if they were the authoritative "today's
opening cash position", instead of the actual authoritative source
(`Department.opening_balance`, summed). All three are fixed via one shared helper
(`departments.models.total_opening_cash_position()`), but the underlying temptation
remains: three same-looking, zero-by-default fields sitting on `SiteConfig` that look
like they should represent "the opening cash position" but don't, for any deployment
that didn't go through the legacy-import path. The fields have not been renamed, and
no docstring warning has been added directly on them — the only warning text lives on
`total_opening_cash_position()` itself, not on the fields a future developer would
actually stumble across first.

**Reason not fixed further.** Removing or renaming these fields would
affect the legacy-import tool and the two legitimate reference displays (SOFP,
backup Summary) that intentionally show "what was configured at setup" — a
data-model change with migration and tooling implications.

**Expected benefit.** Renaming the fields to make their limited purpose obvious (e.g.
`legacy_import_opening_bank_balance`) and/or adding a code comment or docstring
warning directly on the model fields (pointing future developers to
`total_opening_cash_position()` instead) would prevent this exact mistake from being
reintroduced a fourth time.

**Priority: Medium.**

---

## 11. Data tables lack `scope="col"` on header cells

**Description.** Not "no table anywhere" any longer: `templates/envelopes/batch_detail.html`
(both its row table and its audit-trail table), `batch_list.html`, and the two
board-pack templates `templates/reports/treasurer_board_pack.html` (which also puts
`scope="row"` on its statement-line labels) and `board_pack_min.html` already carry
`scope="col"`. None of this came from either recent remediation pass — it was written
in when those templates were first built, well before both. That still leaves 212 of
the 216 templates with a `<th>` unmarked, including the General Ledger, the Journal,
and nearly every other report, so the substance of the finding — most of the app's
tables, including its widest and most data-dense ones, give a screen reader no
explicit column association — is unchanged; only the absolute framing needed
correcting.

**Reason not fixed.** This app has dozens of table templates; doing this
properly and consistently (rather than a partial, inconsistent sweep) is a broader,
mechanical cleanup better done as its own dedicated pass.

**Expected benefit.** More reliable screen-reader navigation of wide, data-dense
tables (the ledger, journal, and financial reports especially) — the four templates
that already do this correctly (envelope batches, board packs) are a working example
to copy the pattern from rather than a pattern to invent.

**Priority: Low-Medium.** Lower severity than label-association and colour-contrast
issues (which affect whether a control's purpose is communicated *at all*, versus this
one improving navigation of already-labelled data).

---

## 12. No dedicated mobile layout audit performed

**Description.** The application already has solid responsive infrastructure (a
`table-scroll` auto-wrap script for tables on narrow viewports, a correct viewport
meta tag, and opt-in "large touch targets"/"reduced motion"/"high contrast" user
preferences) — but no review has systematically tested every page at common
mobile breakpoints (e.g. 375px, 390px) for overflow, cramped layouts, or awkward
wrapping, particularly on the denser reports (Monthly Treasurer's Report, General
Ledger Health Check) which were designed with desktop use as the primary case.

**Reason not fixed.** A systematic, screen-by-screen mobile audit across
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
app they live in.

**Reason not fixed.** Renaming either model is a genuine migration (Django's
`RenameModel`), plus updating every import, FK reference, template, and test that
touches it — a mechanical but invasive change spanning two apps. It also isn't a live
bug (Django distinguishes them correctly via `app_label` internally) — it's a
maintainability/confusion risk for future developers (and for an AI assistant, or a
new hire, searching the codebase for "BudgetLine" and finding two unrelated results).

**Expected benefit.** Renaming one of them (e.g. `cashbook.BudgetLine` →
`cashbook.BudgetItem`, since it's the newer, more specific concept) would remove the
ambiguity permanently.

**Priority: Low.** Confusing, not incorrect; worth doing during a quieter period
rather than as a targeted fix.

---

## 15. Test files are organised by when they were added, not what they cover

**Description.** Several apps — `cashbook` most notably, now with 55 test files (32
when this was first written; both recent remediation passes added more of the same
feature-named kind — `test_advances_closed_asof.py`, `test_payable_partial.py`,
`test_recurring_petty_status.py` from the 31-defect audit, and
`test_settlement_flag_and_petty_guard.py` from the e2e suite — consistent with the
existing convention, not a new instance of the problem) — mix feature-named files
(`test_amount_validation.py`, `test_transfer_refund.py`) with a handful of
version/session-named ones (`test_batch_v193.py`, `test_batch_v197.py`,
`test_batch_v2001.py`, `test_budget_goals_v2.py`, `test_budget_page_v244.py`). The
version-named files each cover a specific historical batch of changes, which made
sense when they were written, but makes it hard for a future developer (or reviewer)
to find "all the tests for staff advances" without searching file contents rather
than reading file names.

**Reason not fixed.** Consolidating test files means moving test methods
between files — mechanical but with real risk of an accidental omission if rushed,
and better done as its own deliberate pass with a full regression run at the end.

**Expected benefit.** Faster navigation to relevant coverage when reviewing or
extending a feature; less risk of accidentally duplicating a test that already
exists in a version-named file no one thought to check.

**Priority: Low.** A maintainability nicety, not a coverage gap — every test still
runs and still catches what it's meant to.

---

## 16. No concurrency/load testing

**Description.** Reviews have reasoned carefully about concurrency risk (e.g. the
petty-cash-float TOCTOU gap at #3, and the two atomicity fixes made in the database
review) but none could exercise *genuine* concurrent access — Django's default test
runner and SQLite don't straightforwardly support multiple real threads/processes
hitting the same test database at once the way production traffic would.

**Reason not fixed.** Proper concurrency testing needs either a
Postgres/MySQL-backed test environment with real multi-connection support and a
tool like `pytest-django` with `django_db(transaction=True)`, or a dedicated
load-testing tool (Locust, k6) run against a staging deployment — both are
infrastructure additions, not safe in-repo test changes.

**Expected benefit.** Direct evidence (not just reasoning from code review) about
how the application behaves under concurrent writes to the same fund, and how it
performs under realistic multi-user load — valuable if the application is adopted
by a larger congregation with more simultaneous users.

**Priority: Low**, given this church's actual current usage pattern — the same
reasoning already applied to the petty-cash TOCTOU finding.

---

## 19. Per-session detail (not just count + bulk terminate)

**Description.** A user's Security tab shows an active-session **count** and offers a
**"force logout everywhere"** bulk action, by decoding
`django.contrib.sessions.models.Session` rows. There is no per-session list
(device/browser, IP, last-active time, an individual "end this one session" control).

**Reason not built.** Django's session data doesn't store device/user-agent
information by default — only what a session's own middleware chain records. Building
a genuinely useful per-session table (recognisable device names, accurate "last seen"
times) needs a small amount of additional tracking (e.g. stamping user-agent and
last-seen-at into the session data or a companion model).

**Expected benefit.** Lets an administrator (or, later, a user managing their own
sessions) end one suspicious session without logging out every device.

**Priority: Low-Medium.**

---

## 20. Password expiry is tracked but not yet enforced

**Description.** `UserProfile.password_changed_at` is stamped automatically
whenever a password changes (self-service or admin-reset), and shown on the Security
tab — but there is no `SiteConfig`-level "maximum password age" setting, and no
enforcement (a middleware forcing a change once a password is older than N days), the
way `require_2fa_for_treasurers` is enforced today.

**Reason not built.** Whether to enforce password expiry at all is a policy
decision (current security guidance is mixed on forced periodic rotation — NIST's more
recent guidance argues *against* mandatory rotation in favour of length/breach-checking),
so this was left as a recorded decision point rather than assumed.

**Expected benefit, if wanted.** A `SiteConfig.password_max_age_days` (blank/0 =
disabled) plus a small middleware extension of the same shape as
`ForcePasswordChangeMiddleware`, checking `password_changed_at` against it.

**Priority: Low**, pending a policy decision on whether periodic rotation is wanted.

---

## 21. "Copy permissions between two existing users" not implemented

**Description.** Copying a user's permissions to another *existing* account is not
supported (distinct from **cloning**, which is implemented — creating a *new* account
with the same role/profiles/led-departments as an existing one).

**Reason not built.** Overwriting an existing, possibly-customised user's
rights from another user's is a more destructive operation than cloning into a new
account (there's no "undo" beyond checking the audit log and manually reverting), and
needs its own careful confirmation UX. Cloning covers the far more common real
scenario ("set up another Assistant like Jane") safely.

**Expected benefit.** A rarely-needed convenience; low priority relative to the risk
of a rushed implementation encouraging accidental overwrites.

**Priority: Low.**

---

## 24. Executive and leader dashboards don't assemble figures via ReportContext

**Where this stands.** The main `DashboardView` now obtains its headline
figures (fund summary, trust summary, trust-to-remit, giving by group, income by
channel, tithe) through a single `ReportContext`, so they equal the reports'
metrics by construction (verified by reconciliation test).

**What remains.** The executive dashboard's blended live+historical trend and the
leader dashboards remain on their bespoke paths — a larger, separate migration. See
also #31, which is the same work described from the Phase 7 angle.

**Priority: Medium.**

---

## 29. `html` section kind not yet used

**Description.** `SectionData` supports `kind="html"` for arbitrary safe HTML
fragments, but no component emits it yet (the library covers its needs with
table/keyvalue/kpi/chart/commentary/info/signature).

**Recommendation.** Add a `RawHtmlComponent` if/when a report needs bespoke
markup (e.g. a formatted legal notice); render it in the template with
appropriate escaping/sanitisation.

**Priority: Low.**

---

## 30. Remaining reports to migrate onto the report engine

**Already migrated:** Cash Flow Statement (`cash_flow_v2`), Statement of Fund Balances
(`fund_balances_v2`), Budget vs Actual (`budget_vs_actual_v2`), Income & Expenditure,
Trial Balance, Financial Position summary, Board Report.

**Still on legacy:** the *detailed* Financial Position (NBV/prepayments/advances —
confirmed still a plain `TemplateView` building its own context by hand; the engine's
equivalent section exists only embedded inside the Board Report, whose own docstring
notes the standalone detailed statement still reads separately), the Bank
Reconciliation register (confirmed still a plain `TemplateView`), comparative/
multi-period/prior-year statement wrappers, and the operational + member/ministry
registers (Cash Book, Payment/Receipt/Instrument/Cheque registers, Staff Advances,
Petty Cash, Expense/Journal/Ledger, Asset/Depreciation, Liability, Loan, Trust,
Envelope, Giving, Pledge, Department, Development Project, Audit; Member/
Contribution statements, Giving history, Donor, Ministry, Leader reports).

**Recommendation.** Migrate each by composing existing components + narratives,
proving figure-equivalence against the legacy view first. The machinery exists;
these are compositions, not new infrastructure.

**Priority: Medium.**

---

## 31. Executive dashboard & leader dashboards still compute figures inline

**Where this stands.** The executive dashboard already draws income through
`core.metrics.income_credits` (the definition `total_income` wraps), so its headline
figures **reconcile with the reports by construction** (verified by test). The
remaining work is structural, not correctness: routing the executive dashboard's
blended live+historical trend and the leader dashboards through `ReportContext` for
provenance/memoization consistency. Confirmed still true: `core/services/dashboard.py`'s
`charts()` and `core/services/forecast.py`'s `horizons()` still build trend series from
raw querysets/aggregation with no `ReportContext` involved, and `leaders/views.py` has
zero `ReportContext` usage anywhere in the file.

**Recommendation.** Introduce period/scope-aware metrics for the historical-trend
figures, then route these dashboards through `ReportContext`. Larger than the
main-dashboard migration because of the historical-data blending and leader
scoping.

**Priority: Medium** (correctness already holds; this is consolidation).

---

## 32. Legacy statement/report views can be retired once engine versions are adopted

**Description.** The migrated reports run in parallel with the legacy views
(`IncomeStatementView`, `FinancialPositionView`, `TrialBalanceView`, the Monthly/
Board report) to preserve URLs and allow verification. Once the engine versions
are adopted as the primary reports, the legacy views and templates can be retired
(or reduced to thin redirects) to remove duplicated presentation code. The engine's
own `treasurer_report.py` module states outright: "Runs alongside the legacy
board/monthly reports; nothing existing changed."

**Recommendation.** After a review period, point the existing report URLs at the
engine reports (via a small adapter) and delete the superseded view/template
code. Keep the export byte-compatibility in mind for anyone scripting downloads.

**Priority: Low-Medium.**

---

## 33. Narrative localisation / templating

**Description.** Narrative text is composed in English in code (392 lines of
Python f-strings in `reports/services/narratives.py`, confirmed unchanged). A future
need for other languages, or for churches to customise wording, would benefit from
externalising the sentence templates.

**Recommendation.** If localisation is required, move narrative sentence fragments
into templates/catalogues keyed by narrative + style, keeping the metric-sourced
values as substitutions. Determinism must be preserved.

**Priority: Low.**

---

## 35. Snapshot integrity for non-deterministic export formats

**Description.** Only the payload checksum and CSV export are byte-deterministic;
xlsx/docx embed timestamps and pdf embeds metadata, so their bytes vary between
identical renders. The snapshot service therefore checksums the payload (canonical
anchor) and CSV, and treats other formats as point-in-time copies.

**Recommendation.** If byte-stable archival of xlsx/pdf is required, normalise
their embedded timestamps/metadata at render time (e.g. fixed creation date) so
their checksums become deterministic and can be used for drift detection.

**Priority: Low.**

---

## 37. Report Designer canvas — palette builder shipped; not yet a free 2-D canvas

**Where this stands.** The designer is already a structured builder over the same
persisted JSON, not a raw-JSON editor: clicking a palette entry adds a section with
its own title/width/params fields and layout toggles (collapsible, print/export
visibility, page-break, grouping), each section's grip drags to reorder, and
hand-editing JSON is now a collapsed "Advanced" panel most users never open.
"Preview without saving" already renders the current unsaved state through the
existing render endpoint. This is a substantial part of the original recommendation
already delivered, in a commit well predating either recent fix round — the doc had
not caught up.

**What is still missing from a genuine canvas.** Reordering only changes position
within one linear list — there is no free 2-D placement beyond the width-column
choice — and the preview opens in a new tab rather than updating inline as sections
are edited.

**Recommendation.** Add an inline (same-page) live preview pane wired to the existing
render endpoint, and, if freeform layout is wanted beyond width columns, let a
palette entry be dragged directly onto the section list rather than only clicked. No
backend change needed — the same `sections` JSON (component + params + LayoutMeta) is
already the contract.

**Priority: Low (UX polish).**

---

## 38. Actual report distribution (email/notification sending)

**Description.** Schedules carry recipients and every generated snapshot is linked
to its run, but the actual sending of the snapshot (email attachment / internal
notification) is not wired. Approval-before-send is modelled (`require_approval`)
but not enforced in a send pipeline.

**Recommendation.** Add a distribution step after `execute_schedule` that, for
schedules with recipients, emails the snapshot's export (or a link) via the
existing email backend, honouring `require_approval`. Record a delivery history.
Note: a real SMTP `EMAIL_BACKEND` is now conditionally configured when
`DJANGO_EMAIL_HOST` is set (see #17, now closed), so the dependency this item used
to note is gone — a church with SMTP configured could have this wired without first
solving an email-transport problem.

**Priority: Low-Medium.**

---

## 39. Snapshot retention policy & background scheduler

**Description.** Scheduling execution and manual/'due' running exist, but there is
no background worker invoking `run_due_schedules` on a timer, and no retention
policy pruning old snapshots.

**Recommendation.** Add a management command (or Celery beat task) calling
`run_due_schedules` periodically, and a retention setting (keep N per report, or
age-based) applied after each run.

**Priority: Low (operational).**

---

## 41. Advanced forecasting (seasonality-aware)

**Description.** The Trend & Forecast Engine uses a transparent linear projection.
For income with strong seasonality (e.g. camp-meeting months), a seasonality-aware
model would project more accurately while remaining explainable.

**Recommendation.** Add an optional seasonal-decomposition forecast (still
deterministic and labelled a projection), keeping the linear model as the default
transparent baseline.

**Priority: Low.**

---

## 42. Persisted insight snapshots for trend-of-insights

**Description.** Insights are computed live each request (correct, always current).
Persisting a periodic snapshot of the insight set would enable "insight trends"
(e.g. how many criticals over time) and alerting on new criticals.

**Recommendation.** On a schedule (reusing the Phase 8 scheduler), persist the
insight set as a snapshot and add a small trend-of-insights view. Keep live
computation as the source of truth; the snapshot is for history/alerting only.

**Priority: Low.**

---

## 44b. Report-Designer editing of a report's presentation template

**Description.** `Report.html_template` lets a registered report opt into a
purpose-built presentation template (the Treasurer's board pack) while keeping
identical section data and the generic engine template as the default. The
Report Designer's persisted model (`ReportDefinition`) has no `html_template` field
at all, and the designer's compile step never sets one on the compiled report, so an
administrator cannot choose a presentation style per designed report.

**Recommendation.** Expose a small "presentation style" choice on the designer
(generic grid vs. board pack) that maps to `html_template`, so designed reports
can also use the richer presentation without code.

**Priority: Low.**

---

## 45. Deep drill-down from a figure to its supporting transactions in-chat

**Description.** The assistant answers "which transactions make up this amount?"
from the knowledge context at a summary level. A deeper drill could return the
actual supporting journal/transaction rows for a specific metric and period via
the dependency graph. `knowledge_for()`'s docstring aspirationally mentions "the
supporting reports and transactions", but no `transactions` key is actually
populated anywhere in what it returns.

**Recommendation.** Extend the Knowledge Service with an optional transaction-level
drill for a given metric+period, surfaced by the assistant when asked, still
read-only and registry-sourced.

**Priority: Low-Medium.**

---

## 50. Follow-ups noted during the maker-checker redesign

* **Live-browser verification.** The DOM-harness testing is strong but
  not a substitute for clicking through the grid (especially drag-reorder,
  resize, and pin) in a real browser.
* **Three-way segregation of duties.** `require_different_approver` still only
  requires Post's actor to differ from the batch's *creator*
  (`envelopes/services/batches.py`'s `post_batch` checks only
  `batch.created_by_id == user.id`), not also from the *approver* — a treasurer who
  approves a batch (setting `reviewed_by`) can still post that same batch
  themselves; nothing checks `reviewed_by_id` anywhere in the post path. A
  stricter three-actor mode (maker ≠ checker ≠ poster) would be a small,
  config-gated addition on top of the same pattern already used elsewhere.
  **Priority: Low-Medium** — worth weighing against the two recent audit rounds'
  focus on exactly this class of control gap (an actor doing two roles a process
  intends to keep separate); this is a live instance of the same shape, just not
  yet reported as a defect.
* **Column resize persistence granularity.** Widths save per user per grid;
  no per-device variant, so a very different screen size on a second device
  will reuse the same saved widths (usually fine, occasionally cramped).
  **Priority: Low.**
* **Bulk actions on the Review Queue.** Approve/return/reject/post are
  still strictly per-batch; a treasurer processing many small batches at once has
  no bulk action. **Priority: Low-Medium**, revisit once real usage volume is
  known.

---

## 54. Follow-ups noted during the export-quality review

* **Client-side canvas export duplication.** The dashboard's
  `downloadLocalFundsPng()` (templates/dashboard.html) is still a complete,
  standalone canvas-drawing implementation — not a thin wrapper over
  `static/js/table_png.js`'s `tableToPng()` — a ~60-line near-duplicate of it.
  Consolidating would mean generalising `tableToPng` to read its title/subtitle
  from `data-*` attributes as a fallback when `opts` doesn't supply them (the
  dashboard's only real difference). **Priority: Low-Medium** — cosmetic/
  maintainability, not a correctness issue.
* **`toDataURL('image/png')` can't embed DPI metadata.** A permanent Canvas API
  limitation (no way to write a PNG `pHYs` chunk client-side) — the
  client-side exporters rely on pixel count alone for perceived quality,
  unlike the two Pillow-generated file types which also carry explicit
  300 DPI tags. Not practically limiting at 4x scale, but worth knowing if a
  future export ever needs an exact physical print size. **Priority: Low.**

*(A third follow-up formerly listed here — three-way segregation of duties for loan
retirement — is closed: `loans/services/loans.py`'s `_retire()`, the shared
implementation behind both `convert_to_donation` and `write_off`, now calls
`_require_different_approver()` against `Loan.created_by_id`, the same pattern used
for expense approval and envelope-batch posting.)*

---

## 56. Benevolent Scheme Engine — Phase 1 follow-ups

Phase 1 delivered the data model, the policy engine, the case workflow, services,
rights, navigation, admin, a read-only JSON API, seed data and 43 tests (see
`docs/BENEVOLENT_MODULE.md`). Two items formerly listed here as deferred — a
per-case levy collection screen, and bank-narration intake for scheme dues — have
since been fully built and are closed; the entries below are what genuinely remains.

**56b. Welfare figures on the Board Pack itself.** `benevolent/report_components.py`
registers 14 `ComponentSection`s and 11 standalone reports, including
`benevolent_overview` ("Benevolent: Overview & KPIs" — `BenevolentKpiComponent` +
`BenevolentSchemeSummaryComponent`), which already shows combined fund balance,
contributions, benefits paid, open cases and committed (approved-unpaid) amounts, all
read from the Financial Metrics Registry — so the report-engine work this item used to
ask for is done. What remains true: `reports/treasurer_report.py` — the actual Board
Pack — does not list any benevolent component in its `sections=[...]`, so a board
still has to open the separate welfare report rather than see it inside the pack. The
remaining work is wiring the existing components into `treasurer_report.py` (e.g. a
new group alongside `G_TRUST`), not writing new ones. *Priority: Low* (was Medium —
it is now a one-file composition change, not new report-engine work).

**56d. Arrears reminders — reminder wiring shipped; scheduling is the remaining gap.**
`refresh_arrears_status()` no longer exists — it was a Phase-3-era compatibility shim
with no callers, removed as dead code. Its old "marks LAPSED/reinstates" framing is
also outdated: Phase 3 split `status` (human-decided) from `standing` (computed), so
there is no LAPSED status to mark any more — only the derived `Standing.ARREARS`/
`INACTIVE` value, recomputed by `schemes.run_automation()` → `services/standing.
refresh_scheme()`. That same nightly run now also calls `notify.send_due_reminders()`,
which sends arrears and renewal reminders over the existing SMS/email channels
(throttled by `reminder_min_gap_days`) — exactly the `pledges/services/reminders.py`
pattern this item asked for. What is still true: nothing *in this repo* schedules
`run_automation`/`manage.py benevolent_automation` to run nightly — no cron/
celery-beat entry, no Procfile `clock` process — matching the same externally
-scheduled gap already noted for report schedules (#39). *Priority: Low* — this is
now a deploy/ops task (add a cron entry), not application code.

**56e. Dependant-aware benefit rules.** A different benefit for a spouse vs a
child is still expressed by creating separate event types
("Bereavement — spouse", "Bereavement — parent") with their own
`SchemeBenefitRule.amount`, which works but is blunt: `SchemeDependant.relationship`
is already read elsewhere (eligibility and levy-exemption decisions) but never to
select a benefit amount. *Priority: Low* — the current approach is workable and no
data would be lost by changing later.

---

## 119. Enterprise Asset Management (EAM) — remaining phases

Phases 0 through 2c are delivered (foundation, ledger backbone, disposal, acquisition
intake and the capital bridge, lifecycle), along with acquisition-date temporal
costing, asset figures in the financial statements, the four asset reports and the
spreadsheet import.

**Phases 3–7 remain unbuilt, confirmed by direct search, not just absence of a
changelog entry:** no work-order/maintenance-plan model exists anywhere in the repo;
no warranty/insurance model exists; no QR-audit/verification model exists; the model
carries only placeholder scaffolding for componentisation/revaluation/heritage
(`componentised`, `revalued_amount`, `is_heritage` fields with zero business logic
anywhere); no DRF dependency is installed; the only multi-church-related code is the
pre-existing `Organization` scaffold. Maintenance (plans, work orders, vendors),
warranties, insurance; verification/QR audits + mobile PWA; revaluation / impairment /
componentisation / heritage; report catalogue + DRF API; multi-church activation are
all still to build.

**Not representable yet.** An improvement or addition to an existing asset cannot
be dated separately, because the register holds one cost and one acquisition date
per asset. Such a payment shows up in the `acquisition_coverage` check as a
double-count. Componentisation is what resolves this properly, and is unstarted —
neither recent fix round touched `assets/services/preflight.py`'s
`acquisition_coverage()`, only asset-disposal-status and depreciation-control bugs
elsewhere in the app.

**Documentation gap, worth separate attention:** the design document this item and
#124a/#124b cite (`ASSET_EAM_DESIGN.md`) does not exist anywhere in the working tree
or in git history. Either it was never committed, or every cross-reference to it
across this file is to a document that needs to be written or located.

---

## 120. Member Self-Service Portal — follow-ups

The portal shipped at `/portal/` (`benevolent/models_portal.py`, `services/portal.py`,
`views_portal.py`, `views_portal_admin.py`, `urls_portal.py`,
`templates/benevolent/portal/`). It is a *surface*, not a second system: it adds no
accounting, no eligibility and no workflow.

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
for a pilot and tedious for a rollout. An "invite everyone enrolled and active"
action with a dry run is the obvious next step. *Priority: Medium.*

**120f. Two-factor for member logins.** `TwoFactorMiddleware` supports members
already (it is role-agnostic), but `require_2fa_for_treasurers` has no member
equivalent, so a church cannot require it for the portal. *Priority: Low.*

---

## 124. Supplier register — what is not delivered

The `vendors` app ships `Vendor` plus categories, tags, contacts, addresses, bank
accounts, documents and notes, with `simple_history` throughout; `Payable.supplier`,
`Expense.vendor` and `FixedAsset.supplier` link spending and assets to it; the pickers
and the split `manage_vendors` / `manage_vendor_bank_details` rights are done. The
following are not, and neither recent fix round touched any of them (the only vendor
change either made was a duplicate-supplier-registration crash fix, unrelated).

**124a. Contracts remain unbuilt.** There is no contracts module. `Document.Kind.
CONTRACT` exists only as a document-type tag ("For a contract or a tax certificate")
— no term, no renewal date, no value, no notice period, no link to the spend it
authorises, no contract model at all. It should be built with the EAM work rather
than bolted onto the supplier register. *Priority: Medium.*

**124b. No REST API.** This application has no DRF dependency and adding one is a
decision worth taking deliberately rather than in passing. A single JSON lookup
endpoint (`vendor_lookup`) exists, in the shape of the existing member lookup.
A real API belongs with the one proposed for assets, as one decision. *Priority: Medium.*

**124c. No supplier dashboard.** The register carries per-supplier figures and
the profile carries ageing, but there is no cross-supplier spend analysis,
top-suppliers chart or ageing summary across the whole register — and when there
is, it should be a Report Engine report, not a bespoke page. *Priority: Medium.*

---

## 125b. Detail-page coverage is a hand-maintained list

`DETAIL_PAGES` in `core/test_seeded_smoke.py` names nine URL/model pairs. A new
detail page is not covered until someone adds it, and nothing complains. Deriving
the list from the URL resolver (any route taking a single `<int:pk>`) would close
that, at the cost of needing a model hint per route. *Priority: Low.*

---

## 126a. The "every built screen is reachable" guard is narrow

`EveryBuiltScreenIsReachableTests` in `core/test_nav_audit.py` still hard-codes the
two portal office screens it was written for (`portal_admin_queue`,
`portal_admin_accounts`), rather than deriving "every built page is linked from
somewhere", which would need a map of pages legitimately reached from a parent
screen. Worth generalising, and not trivially. *Priority: Medium.*

This matters because unreachable-but-working code has been the single most repeated
defect shape in this project (a public form that redirected to login, an invitation
that dead-ended, a page that rendered only on an empty database, a screen with no
door). In every case the tests passed, because a test reverses a URL and requests it
directly — precisely the step a real user cannot take.

---

## 127. Expense and campaign screens — remaining items

**127a-i. The field-group allowlists use substring matching.** `{% if f.name in
'amount date charge' %}` is Django's `in` against a *string*, so a field named
`at` or `e c` would match by accident. It has not bitten yet and the "Other details"
fallback plus the union guard would catch the fallout, but it is a trap. Splitting on
whitespace via a filter would make it exact. *Priority: Low.*

**127a-ii. The recurring-expense form still uses plain Django rendering.** It
carries the same fields as the expense form but not the same sectioning, so the
two screens no longer look alike. Worth applying the same grouping.
*Priority: Medium.*

**127c-ii. A genuinely large SMS group will still time out.** The integrity problem
is fixed — `CampaignMessage` is opened *before* the first message and checkpointed
every 25, with a `state` of RUNNING / DONE / INTERRUPTED and an `intended_count`, so
an interrupted send is visible rather than erased. The scale problem is not: sending
is still a synchronous loop inside the request, called directly from a view's
`post()`. The remaining fix is to move sending out of the request entirely, following
the pattern the benevolent module already uses — a queued row processed by a
management command under cron — rather than adding a task-queue dependency.
`CampaignMessage` is already shaped for it (it carries state and counts, so a worker
would resume rather than restart). Also unaddressed: no rate limit against the
provider. *Priority: Medium.*

---

## 128. Batch expense entry — follow-ups

**128a. `ExpenseUpdate` still composes its own charge description** and overwrites
the one `cashbook/services/expenses.py` produced, so the edit path retains a small
piece of charge logic — `_record_charge()` still takes no `description=` parameter,
so the view cannot delegate. The two formulas currently happen to produce identical
strings, so today's overwrite is a no-op, but the duplication (two places that must
be kept in sync by hand) remains. *Priority: Low.*

**128b. Batch entry has no draft or resume.** A long stack typed into the browser
is lost if the page is closed before saving. *Priority: Low.*

---

## 129a. A batch charge cannot be edited as a batch

`record_batch(shared_charge=...)` records a single shared fee once as a bank-charge
expense on the batch's fund, with `charge_for` deliberately null. Afterwards it is an
ordinary expense, so correcting it means editing that row directly; no screen
understands that it covers a group. *Priority: Low.*

---

## 132. Eight shared CSS classes are still used but never defined — OPEN

`core.test_css_contract` was added in v3.20.0 after `.panel` and `.table` were
found to be used across 38 templates with no definition anywhere. The same audit
found nine more classes used in three or more templates that nothing defines
(`callout` since fixed), so every screen using the remaining eight renders that
element unstyled and nothing complains. Re-checked against the current test run: the
same eight are still exactly the ones in `KNOWN_UNDEFINED` — the ratchet has neither
grown nor shrunk — but two rows' template counts have drifted since this was written,
both because `templates/intelligence/workspace.html` was reworked in an unrelated UI
pass and stopped using either class (not because either was fixed):

| class | templates | examples |
|---|---|---|
| `ph-sub` | 8 | `cashbook/petty_cash.html`, `reports/board_report.html`, `reports/cash_flows.html` |
| `u-sm` | 4 (was 5) | `cashbook/payment_register.html`, `reports/component_catalogue.html`, `reports/metrics_catalogue.html` |
| `btn-link` | 3 (was 4) | `reports/designer_edit.html`, `reports/designer_list.html`, `reports/schedule_list.html` |
| `btn-primary` | 4 | `cashbook/fund_budget.html`, `elder_dashboard.html` |
| `form-check` | 4 | `cashbook/advance_form.html`, `cashbook/expense_detail.html` |
| `field-label` | 3 | `accounts/profile_form.html`, `giving/campaign_list.html` |
| `head-actions` | 3 | `assets/board.html`, `reports/collections_detail.html` |
| `report-table` | 3 | `reports/changes_in_net_assets.html`, `reports/collections_summary.html` |

They are held in `KNOWN_UNDEFINED` in that test as a ratchet: the list may shrink
and must never grow. Each needs deciding individually — some are near-misses for
a class that already exists and does the job (`btn-primary` for `.btn`,
`field-label` for the `.form-row label` rule, `report-table` for `.ledger`),
which should be corrected in the template rather than given a second definition.
Others (`ph-sub`, `head-actions`) look like genuine components that were never
written and should be defined in `app.css`.

**The general lesson.** A missing CSS class is silent by construction: the page
loads, the markup is valid, and the only symptom is that a screen quietly looks
unfinished. No render test, no status-code check and no review of the diff will
show it, because nothing is wrong with the template — the definition is missing
somewhere else entirely.

---

## 133. The demo seed creates payables but no suppliers — OPEN

`seed_demo` creates open payables (e.g. "Mwangi Hardware") as free text with no
`Vendor` rows behind them, so `Vendor.objects.count()` is zero on a fresh demo
database. A data migration exists that backfills `Vendor` rows from free-text
`Payable.vendor` values, but migrations run once at `migrate` time, before
`seed_demo` creates any payables — so it runs against an empty table and backfills
nothing for the demo data specifically. Two consequences:

* The supplier selector on `/payables/` renders with nothing to choose from, so the
  feature cannot be seen or demonstrated on a fresh install.
* The "N open bills are not linked to a supplier" warning fires on the seeded
  data by construction, which reads as a fault in the demo rather than a
  deliberate illustration.

Seeding three or four suppliers with different payment terms, linking most of the
seeded payables to them and leaving exactly one unlinked, would demonstrate the
register, the terms-driven due date and the unlinked warning all at once.

---

## 134. The board report and executive overview bypass the Semantic Reporting Layer — OPEN

`/reports/board/` (`MonthlyTreasurerReportView`, now in `reports/views/treasurer_report.py`)
and `/executive/` (`ExecutiveDashboardView`) still create **zero** `ReportContext`
instances — confirmed directly against current source, and neither view was touched
by either recent fix round (the files that WERE modified, `reports/views/remittance.py`
and `reports/services/balances.py`, are unrelated to this pair). They call
`reports.services.*` and `core.services.*` directly instead of going through the
layer, which is contrary to the project's own rule that every financial figure
reaches a report through the Semantic Reporting Layer and the Financial Metrics
Registry.

The practical cost is that nothing memoises. `ReportContext.metric()` caches per
(name, args) for the life of a render, and `core.perfcache.cached()` adds a
request-scoped memo on top; a view that never builds a context gets neither.
Re-measured directly against seeded data: `/executive/` fires exactly 6 identical
`RecurringExpense` queries (two per forecast horizon, three horizons) and 4 identical
`Pledge` queries; `/reports/board/` shows the equivalent pattern. Both pages are flat
with respect to funds, transactions and users — this is redundancy, not an N+1 — so
it is a correctness-of-architecture issue with a performance symptom, not urgent.

Migrating them is roadmap item 4 (Board Report implementation) and item 5
(migration of remaining reports, #30). It should be a deliberate change with accuracy
tests comparing every figure before and after, not folded into a performance pass.

**A caution for whoever does it.** An earlier audit nearly "fixed" repetition on the
board report that was not repetition at all: normalising digits out of the SQL made
three *different months* of a trend look like one query run three times.
Duplicate-query analysis has to compare fully-bound SQL, including parameters, or it
will invent work that does not exist.

---

## 135. Remaining lazy foreign keys on two screens — OPEN

The v3.21.0 audit added a probe that flags queries issued from
`related_descriptors` during a render — i.e. a foreign key fetched per row
because the queryset did not select it. Three screens showed them at the time.
`/expenses/` no longer does: `ExpenseListView` already carries
`.select_related("department", "recorded_by").prefetch_related("attachments")`,
and re-probing with real attachment rows present fires no related-descriptor
query at all — this bullet was stale before either recent fix round touched the
file; nobody closed it deliberately, the doc simply never caught up. Two remain:

* `/benevolent/` — `membershipexemption` ×2, `memberadjustment` ×2
* `/budget/` — one

These were re-probed in v3.21.1 and are single bounded fetches rather than per-row
patterns, so they are not worth a change on their own; fold them in if those views
are touched for another reason.

None grows with the fund register (the guards in `core.test_query_growth` cover
that axis), so these are bounded and low priority. The fix in each case is a
`select_related` on the queryset in the view.

Note that a per-row FK lookup does **not** always show up as query growth: if
the added rows all share one parent, the SQL is identical every time and a
naive duplicate count hides it. Reproducing this class of fault needs test data
spread across parents, which is why the probe looks at the *call stack* rather
than at the SQL text.

---

## 136. The CSS contract test has a blind spot: reach is not repetition — OPEN

`core.test_css_contract` fails when a class is used in three or more templates
with nothing defining it. The threshold is there to keep the test quiet: a
single-use class name is often a legitimate JavaScript hook that was never meant
to carry style, and failing on those would make the suite noise.

It missed `row-emph` completely. That class marks **every subtotal row and every
grand-total footer the report engine renders**, and it was defined nowhere — so
on every engine report a total was set exactly like the line items above it. It
went unnoticed for as long as it did because it appears in exactly one file:
`templates/reports/engine_report.html`, the single template every engine report
renders through.

**The lesson: a class's blast radius is how many screens it reaches, not how
many templates mention it.** One line in a shared template can style a hundred
pages; three lines in three leaf templates style three. The current threshold
measures the wrong thing for the case that matters most.

Worth considering, in rough order of cost:

1. Treat templates that are extended or included by many others as amplifiers —
   a class used in one of those counts for as many templates as render through
   it. The include/extends graph is already walked by `_styles_reachable_from`,
   but only to resolve which class DEFINITIONS are visible through it, not to
   inflate a class's usage count — so the data this option needs is already
   there, wired to the wrong side of the problem.
2. Failing that, keep a small explicit list of engine-emitted class names that
   must always be defined. `reports.test_statement_readability` pins
   `row-emph`, `row-heading`, `row-subtotal` and `row-grand` this way, which
   covers today's known cases but not tomorrow's.

Until then the guard should be read as catching *widespread* gaps, not
*high-impact* ones — and those are not the same thing.

---

## 137d. The read-gated-view write sweep should be a standing guard test

Three real permission holes were found by one AST sweep — every class whose base is a
read-only mixin and which defines `post`/`put`/`patch`/`delete` (the reconciliation
worksheet, the bank register's opening balance, and the register-exceptions
resolve/recheck branches, all now fixed). That sweep is cheap and belongs as a guard
test rather than something a reviewer remembers to do. Confirmed: no such test exists
anywhere in `core/` today — this remains a one-off manual sweep, not a standing check.

The sweep found nine such classes in total; six are fine and are named here so the
next sweep is quicker: `TelegramSetPinView` and `ToggleFavouriteView` write only
`request.user`'s own row (self-service, correctly read-gated); `FundThankSmsView`
checks `is_treasurer` inside its own `post`; `RegisterImportView` is
`DataEntryRequiredMixin`; `AssistantAskView` and `ExecutiveDashboardView` post no
durable state. `InsightStatusView` (`ReportAccessMixin`) lets a report-access user
dismiss an insight — fully audit-trailed and an annotation rather than a figure, so
left alone, but noted. *Priority: Low.*

**Why the original hole survived review.** The template had always hidden every one
of those controls behind `{% if can_enter_data %}`, so the screen looked right to an
auditor and to anyone reading the page. **A hidden button is not a permission** — and
a view whose class-level mixin is named for *reading* will attract write actions
precisely because the gate reads as already handled.

---

## 138. Residuals from the full-application audit remediation

The audit confirmed 31 defects; 29 are fixed. These remain, all re-confirmed against
current source:

* **`pending_receipts_total` can still double-count.** A paper-receipt memo whose
  envelope counterpart is back-dated to on-or-before the report date still
  double-counts — `receipted_after()`'s own docstring states the gap verbatim:
  "nothing links a memo row to the envelope entry carrying its income... the book
  holds it and this function adds it back anyway." Nothing links a memo row to the
  envelope carrying its income (`mark_manual_receipt` creates no `Envelope`), so
  closing it needs a new nullable FK and a migration. The alternative — matching on
  member/amount/date — would silently delete genuine suspense whenever two members
  give the same amount, so it was refused.
* **The petty-cash recurring fix does not restate history.** The fix is confined to
  `generate_schedule()` and `pay_early()`, both of which only affect newly-created
  rows. No migration restates existing PENDING rows — code cannot retroactively know
  which historical PENDING rows were actually already-disbursed petty cash — so a
  church already running such a schedule keeps an overstated float until each is
  approved by hand.
* **`benevolent/test_portal_render_contract.py` sweeps only argument-less URLs.**
  It installs exactly the unresolved-variable sentinel that should have caught
  `p.get_method_display` years ago, but covers only the ten argument-less portal URLs
  — `portal_case_detail` takes a pk and is therefore absent, along with the timeline
  and payment loops where all three of that file's defects lived. Adding pk-bearing
  URLs to that sweep is the real fix. *Priority: Medium.*

### The owner's decisions, not the code's

* **The exposed credentials are still live.** `.env.example` is scrubbed and
  `config/settings.py` now refuses to boot on the `change-me` placeholder, but
  `git show HEAD:.env.example` (as of the commit that scrubbed it) still yields the
  real SECRET_KEY and the MySQL password in history. **Rotate both.** Rotating
  SECRET_KEY logs everyone out and, unless `TREASURY_ENCRYPTION_KEY` is set
  first, makes encrypted settings and 2FA secrets unreadable — so set that key
  before rotating.
* **Real financial workbooks remain recoverable from git history** (audit item 27).
  Scrubbing the working tree did not remove them. Rewriting history is destructive
  and shared, so it was left for a deliberate decision.

---

## Summary table

| # | Item | Priority |
|---|---|---|
| 9 | Bank Position report wrong until "Opening bank balance" is configured — operational fix available NOW (Settings → Financial Setup), code fix (onboarding prompt) still open | **High** |
| 7 | `cashbook/views.py` remains a "god file" (`reports/views.py` has since been split into a package) | Medium |
| 10 | Legacy-import-only opening-balance fields are a duplicate, easily-misused source of truth | Medium |
| 24 | Executive + leader dashboards don't assemble figures via `ReportContext` | Medium |
| 30 | Remaining reports to migrate onto the report engine | Medium |
| 31 | Executive/leader dashboards still compute figures inline (consolidation) | Medium |
| 50b | No three-way segregation of duties on envelope batch approve/post (approver can also post) | Low-Medium |
| 56b | No benevolent sections wired into the Board Pack itself (components exist; wiring does not) | Low |
| 119 | EAM Phases 3–7 unbuilt; asset improvements not separately datable (componentisation); design doc (`ASSET_EAM_DESIGN.md`) missing from the repo entirely | Medium |
| 120b | No portal self-registration ("claim my record") | Medium |
| 120c | Portal figures are not Report Engine sections | Medium |
| 120d | No bulk portal invitation | Medium |
| 124a | No contracts module (belongs with the EAM work) | Medium |
| 124b | No REST API (DRF decision, jointly with the assets API) | Medium |
| 124c | No cross-supplier dashboard / ageing summary | Medium |
| 126a | "Every built screen is reachable" guard names two screens instead of deriving | Medium |
| 127a-ii | Recurring-expense form lacks the expense form's sectioning | Medium |
| 127c-ii | Large SMS sends still run in-request and can time out; no provider rate limit | Medium |
| 138 | `test_portal_render_contract` sweeps only argument-less portal URLs | Medium |
| 11 | Data tables lack `scope="col"` on header cells (4 templates now do; 212 don't) | Low-Medium |
| 19 | Per-session detail (device/IP/last-seen, individual termination) not built | Low-Medium |
| 32 | Legacy statement/report views can be retired once engine versions are adopted | Low-Medium |
| 38 | Report distribution (email/notification sending) not wired — SMTP dependency now resolved | Low-Medium |
| 45 | Deep drill-down from a figure to its supporting transactions in-chat | Low-Medium |
| 50a | Maker-checker: live-browser click-through not yet done | Low-Medium |
| 50d | Maker-checker: bulk actions on the Review Queue | Low-Medium |
| 54 | Canvas-export duplication (loan-retirement segregation of duties item is closed) | Low-Medium |
| 3 | No row-level locking on petty-cash-float checks (TOCTOU race) | Low |
| 4 | No systematic N+1/index audit against real production traffic | Low |
| 5 | Large file imports run synchronously, no background task queue | Low |
| 6 | `StaffAdvance.balance` computed per-row on the advance list | Low |
| 8 | Department-dropdown queryset construction repeated (non-identically) across 6 forms | Low |
| 12 | No dedicated mobile layout audit performed | Low |
| 13 | Two unrelated models are both named `BudgetLine` (departments vs cashbook) | Low |
| 15 | Test files organised by when added, not what they cover (cashbook: 55 files) | Low |
| 16 | No concurrency/load testing (SQLite/default test runner limitation) | Low |
| 20 | Password expiry tracked but not enforced — pending a rotation policy decision | Low |
| 21 | Copy permissions between two existing users not implemented | Low |
| 29 | `html` section kind not yet used by any component | Low |
| 33 | Narrative localisation / templating | Low |
| 35 | Snapshot integrity for non-deterministic export formats (xlsx/pdf) | Low |
| 37 | Report Designer canvas: palette/drag-reorder shipped; still not a free 2-D canvas | Low |
| 39 | Snapshot retention policy & background scheduler | Low |
| 41 | Advanced (seasonality-aware) forecasting | Low |
| 42 | Persisted insight snapshots for trend-of-insights | Low |
| 44b | Report Designer cannot choose a presentation template per report | Low |
| 50c | Maker-checker: column-resize per-device granularity | Low |
| 56d | Arrears reminders now wired to SMS/email; only the nightly cron entry itself is missing | Low |
| 56e | Dependant-aware benefit rules | Low |
| 120a | Portal notifications are near-real-time, not push | Low |
| 120f | No member-login 2FA requirement setting | Low |
| 125b | Detail-page smoke coverage is a hand-maintained list | Low |
| 127a-i | Field-group allowlists use substring matching | Low |
| 128a | `ExpenseUpdate` still composes its own charge description | Low |
| 128b | Batch expense entry has no draft or resume | Low |
| 129a | A shared batch charge cannot be edited as a batch | Low |
| 137d | Read-gated-view write sweep should become a standing guard test | Low |
| 132 | Eight shared CSS classes used but never defined | Open (ratchet) |
| 133 | Demo seed creates payables but no suppliers | Open |
| 134 | Board report and executive overview bypass the Semantic Reporting Layer | Open |
| 135 | Remaining lazy foreign keys on two screens (down from three) | Open (bounded) |
| 136 | CSS contract test measures reach as repetition | Open |
| 138 | `pending_receipts_total` double-count; petty-cash fix doesn't restate history | Open |
| 138 | **Owner:** rotate the exposed SECRET_KEY and MySQL password | **Owner's call** |
| 138 | **Owner:** financial workbooks recoverable from git history | **Owner's call** |

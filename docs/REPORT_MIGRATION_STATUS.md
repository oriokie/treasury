# Report Migration Status & Legacy Retirement Plan (Phase 7)

*Status of migrating every report onto the Generic Report Engine, and the plan
for retiring the superseded legacy implementations.*

---

## 1. Migrated reports (on the Generic Report Engine)

Every report below is composed from registry metrics + reusable components +
(where useful) the Financial Narrative Engine, rendered through the common
Rendering Framework, and consumes figures **only** through the Semantic Reporting
Layer. Each has a URL under `/reports/r/<key>/`, all six export formats,
drill-down, filters, permissions and a dependency map.

| Report | Engine key | Equivalence to legacy |
|---|---|---|
| Income & Expenditure | `income_statement_v2` | recurrent/capital/operating/net-surplus proven identical (Phase 6 tests) |
| Trial Balance | `trial_balance_v2` | `trial_balance` metric (ledger); balances by construction |
| Financial Position (summary) | `financial_position_v2` | assets/liabilities/net from registry metrics |
| Board / Treasurer's Report | `board_report_v2` | rebuilt from components + narratives (Phase 6); reduced to the board's own statement set with a print-first template and editable per-section commentary — see [BOARD_PACK.md](BOARD_PACK.md) |
| **Cash Flow Statement** | `cash_flow_v2` | reconciles opening + net change == closing; classification mirrors legacy `StatementOfCashFlowsView` (Phase 7) |
| **Statement of Fund Balances** | `fund_balances_v2` | total == total closing cash; from `fund_summary` (Phase 7) |
| **Budget vs Actual** | `budget_vs_actual_v2` | totals equal the canonical `budget_vs_actual` service the legacy view uses (Phase 7) |
| **Reporting Consistency Audit** | `consistency_audit` | new cross-report reconciliation (Phase 7) |

Demonstrations: `fund_overview`, `board_pack_demo` (component-library demos).

New metrics this phase (registry now 26): `financing_activity`,
`loan_retirement_income`, `remittances_total` — canonical implementations in
`loans.services.reporting` / `reports.services.balances`, so the Cash Flow
statement reads every figure from the registry.

---

## 2. Reporting consistency audit

`reports/consistency_reports.py` + report `consistency_audit`. For a period it
checks the identities that must hold across every statement, all from one
`ReportContext`:

* Trial balance balances (debits == credits).
* Accounting equation holds (A == L + NA).
* I&E surplus == income − operating − capital.
* Cash flow reconciles (opening + net change == closing fund cash).
* Fund balances total == total closing cash.
* Tithe / total income consistent across reports & dashboard.

On the seeded data all checks pass. Because every figure is a registry metric
read through one context, a failure would indicate a genuine accounting
inconsistency, not a definitional drift.

---

## 3. Legacy retirement plan

**Principle (unchanged from the phase brief):** retire a legacy implementation
only after functional equivalence is proven, and preserve existing URLs,
permissions and export behaviour throughout.

**Current stance — parallel run, nothing deleted yet.** The engine reports run
alongside the legacy views. The legacy URLs (`/reports/income-statement/`,
`/reports/cash-flows/`, `/ledger/trial-balance/`, the Monthly/Board report, etc.)
are **untouched**, so no user-facing behaviour or scripted export has changed.
This is deliberate: the migrated reports have proven *figure* equivalence, but
their on-screen HTML and their CSV/Excel byte-layout differ from the legacy
templates, and a church may rely on the exact legacy layout. Swapping the URLs
before the treasurer has validated the new presentation would be premature.

**Retirement sequence (for a later, deliberate step):**

1. **Adopt** each engine report as the primary link in the reports index (done
   for the migrated statements — the index now links them).
2. **Observe** a review period where treasurers use the engine reports and
   confirm the presentation and exports meet their needs.
3. **Redirect** the legacy URL to the engine report via a thin adapter (keeping
   the URL name so templates/bookmarks keep working), once the church signs off.
4. **Delete** the superseded view + template, recording it in the "retired"
   table below.

No legacy report has been deleted in Phase 7 — the retirement is staged behind
human validation by design.

### Retired reports

| Report | Legacy view | Retired in | Replaced by |
|---|---|---|---|
| _(none yet)_ | | | |

---

## 4. Remaining legacy reports (not yet migrated)

The operational and member/ministry reports remain on their legacy
implementations; the machinery now exists to migrate each by composing existing
components + narratives, proving equivalence first:

* **Operational:** Cash Book, Bank Reconciliation reports, Payment/Receipt/
  Instrument/Cheque registers, Staff Advances, Petty Cash, Expense register,
  Journal register, Ledger reports, Asset register, Depreciation, Liability
  reports, Loan reports, Trust Fund reports, Envelope reports, Giving reports,
  Pledge reports, Department reports, Development Project reports, Audit reports.
* **Member & ministry:** Member statements, Contribution statements, Giving
  history, Donor reports, Department contribution reports, Ministry reports,
  Leader reports.
* **Financial statements:** the *detailed* Statement of Financial Position (with
  NBV, prepayments, advances, accrual adjustments) — the summary is migrated;
  comparative / multi-period / prior-year statement wrappers.

These are recorded as recommendation #30 (updated) and remain deliberately out of
scope for a single phase to avoid shallow, unverified migrations.

---

## 5. Snapshot foundation

`reports/models.py::ReportSnapshot` + `reports/services/snapshots.py`. Captures a
rendered engine report as an immutable, versioned record: period, generation
timestamp + user, app/template/schema versions, structured payload, a
deterministic **payload checksum** (the integrity anchor) plus optional
per-format checksums, and provenance (filters, metrics used, component keys,
services). Finalised snapshots are immutable (save raises). `verify_snapshot`
detects drift by re-rendering and comparing checksums. No scheduling; no change
to current report behaviour. See `docs/SNAPSHOT_FOUNDATION.md`.

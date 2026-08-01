# The Board / Treasurer's Report (`board_report_v2`)

*The pack a church board is handed. Deliberately short, print-first, and
explained section by section in words the treasurer can change.*

`/reports/r/board_report_v2/` · template `reports/board_pack_min.html`

---

## 1. What is in it, and why nothing else is

A board meets for an hour. The pack is therefore the shortest set of statements
that still answers the questions a board is accountable for:

| # | Section | Component | Answers |
|---|---|---|---|
| 1 | Key figures + executive summary | `KpiCardsComponent`, `NarrativeComponent` | Where do we stand? |
| 2 | Collections summary | `CollectionsSummaryComponent` | What came in and what went out? |
| 2 | Trust fund summary | `TrustFundSummaryComponent` | How much of it was never ours? |
| 3 | Statement of fund balances | `FundBalancesStatementSection` | What is each fund holding? |
| 3 | Statement of financial position | `FinancialPositionSummarySection` | What do we own and owe? |
| 3 | Statement of cash flows | `CashFlowStatementSection` | Where did the cash actually move? |
| 4 | Trial balance | `TrialBalanceSection` | Do the books balance? |
| 4 | Bank reconciliation | `BankReconciliationComponent` | Does the bank agree? |
| 4 | Matters for the board | `NarrativeComponent("recommendations")` | What must we decide? |
| 5 | Signatures | `SignatureBlockComponent` | Adoption |

Charts, budget variance, department analysis and the wider commentary set live
on the fuller Treasurer's board pack (`treasurer_report_v3`). Putting them here
would cost the board the plot.

## 2. The two monthly tables

`CollectionsSummaryComponent` and `TrustFundSummaryComponent` read the
period-aware metrics `collections_summary_monthly` and
`trust_collections_monthly` (`reports.services.monthly`). Both **collapse to a
single figure column when the reporting period sits inside one calendar month**
and break out per month when it does not, so "for the month of July" and "for
January to December" are the same component.

The credit and expense basis is identical to the standalone Collections Summary
report — confirmed credits that are not `excluded_from_income`, and effective
expenses other than remittances — and a test asserts the two agree for the same
dates. The trust table's grand total ties to the collections table's trust
column by construction.

Trust funds with nothing collected in the period are omitted, and the rest are
ordered by what they raised.

## 3. What the statements drop

Board packs are read, not audited, so each statement omits what it has nothing
to say about. Totals and structure are never affected.

* **Fund balances** — dormant funds (no balance, no movement) are dropped;
  funds are ordered by closing balance, largest first, within the local and
  trust blocks; the **Transfers column disappears entirely** when no money moved
  between funds; and a fund that neither received nor paid shows a blank rather
  than `0.00`.
* **Financial position** — detail lines with no balance at the date are not
  printed. The two subtotals and Net assets always stand, because "total
  liabilities: nil" is itself the point.
* **Cash flows** — a nil detail line is dropped, and an activity with neither
  movement nor a subtotal drops out whole. Any activity that did move keeps its
  heading and its subtotal.

The last two are **opt-in** (`hide_nil_lines=True`, set only by this report).
The standalone `financial_position_v2` / `cash_flow_v2` and the full statements
pack are read as accounting documents, where a nil line is a positive statement
that the church holds none of that thing, so they are unchanged. The fund
balances rules above apply to that statement everywhere it is used.
* **Trial balance** — an account prints in one column; the other is blank.

## 4. Loans converted to donations, in the cash flow

A conversion (or write-off) moves no money: the lender's claim is extinguished
and income is recognised in its place, recorded as a contra pair of ordinary
documents (`loans.services.loans._retire`). It therefore belongs in **no cash
line** of the statement, and it **does not reduce loan receipts** — the
borrowing was a real cash inflow, often in an earlier period, and netting the
gift against it would misstate financing, potentially to a negative.

Both legs are excluded from operating cash:

* the **income leg** explicitly, via the `loan_retirement_income` metric;
* the **settlement leg** automatically, because it is a `LOAN_REPAYMENT`
  document and `_effective_expense_qs` excludes liability documents.

The statement therefore still reconciles to the movement in fund cash (tested).
What the conversion does require is disclosure, so it appears as a **non-cash
memo** below the financing section rather than being left invisible.

## 5. Bank reconciliation

`BankReconciliationComponent` reconciles as at the **period end**, using the
bank's own balance for that date (imported register or live feed, whichever is
nearer — see `reports.services.balances.bank_position`):

```
Balance per bank statement at <date>
Add:  deposits in transit          (cash_in_transit)
Less: unpresented payments         (unpresented_payments_total)
= Adjusted bank balance
  Balance per cash book
= Unreconciled difference
```

When no bank balance exists for that date the section prints **no figure** and
says it is not reconciled. A lone cash-book balance dressed up as a
reconciliation would tell the board the account had been checked when it has
not.

## 6. Editable commentary

Every section carries an explanation, in three layers (last one wins):

1. **Generated** — `reports.services.narratives.BUILDERS`, one builder per
   section key, writing from the figures the section itself shows. Always
   available, no configuration, no network, byte-identical on a re-run.
2. **AI** — `POST /reports/narrative/ai/` hands the model *that section's rows
   and totals and nothing else*, with a system prompt forbidding invented
   figures. Requires the assistant to be switched on (Settings → Assistant);
   when it is off the endpoint says so and the generated text stands.
3. **Edited** — `POST /reports/narrative/save/` stores the treasurer's own
   words in `ReportNarrative`, keyed by report + section + period, so editing
   July never rewrites June. Saving an empty box clears the override and the
   generated text returns.

`EngineReportView._annotate_narrative` applies the layering to **every** engine
report, so a new report gains editable commentary without changing a section.
The chosen text travels with the PDF and Word exports as well as the screen.

## 7. Print

The pack is print-first: A4 portrait, 20/14/16 mm margins, a running head and
foot that repeat on every page, `thead` repeated across page breaks, no row or
section split across a page, and page breaks before the fund balances, trial
balance and signature blocks. Fills are dropped in favour of rules so the pack
survives a monochrome photocopier. **Print / Save as PDF** in the browser is the
intended route to a presentation-grade PDF; `?export=pdf` remains the
ReportLab export.

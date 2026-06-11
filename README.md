# Church Treasury

An institutional-grade treasury system for a local SDA church. It replaces the
spreadsheet workflow with a single ledger that ingests bank/M-Pesa statements,
matches givers to members automatically, tracks expenses through an approval
workflow, and produces the full set of reports the church committee, the
conference, and an auditor will ask for.

Built with Django 5.2 (LTS), server-rendered templates with a little HTMX, and
an audit trail on every record via `django-simple-history`.

---

## What it does

- **Statement import.** Upload a `.csv`, `.xlsx`, or `.xls` bank/M-Pesa
  statement. Columns are auto-detected; each row's M-Pesa receipt is the unique
  key, so re-importing the same file safely skips rows already captured.
- **Three narration shapes** are parsed automatically — standard paybill
  (`UER…~441211#tithe~2547…~…~NAME`), "Other" payments, and bank transfers
  (`AC0C… EDWIN ORIOKI Grp12dev`). Development-group references like `Grp12dev`
  are detected by number.
- **Member matching.** Payers are matched to members by phone first, then by an
  order-insensitive name key (and known aliases). Ambiguous or unknown rows go
  to a shared **review queue**; nothing is ever auto-merged, and no gift is ever
  orphaned. Confirmed duplicates are resolved with a **merge** that repoints
  gifts and records the absorbed spelling as an alias.
- **Cash & envelopes.** Manual entry for loose offerings and envelope counts.
- **Expenses.** A full expense register with categories and a
  `Pending → Approved → Paid` (or `Rejected`) workflow. Only approved/paid
  expenses affect fund balances. The expense form **defaults to today's date**,
  shows the **selected fund's available balance** as you choose it, and the
  **claimant autocompletes from the member list** (with member type). Banking
  entries and expenses can be **edited** after entry; every change is on the
  audit log.
- **Envelopes.** An Excel-like ledger-entry grid modelled on the Sabbath cash
  sheets — sticky contributor column and header, one row per contributor,
  contributor-name autocomplete (~5 suggestions), an auto-incrementing receipt
  number, and Enter/Tab to move down a column. A **column chooser** lets the
  treasurer pick just the funds collected that Sabbath (defaults: Tithe,
  Combined Offering, Camp Meeting, Development, Sabbath School, Loose Offering,
  LCB, Thanksgiving), so the sheet stays small. Prefer to type in Excel? Download
  a **template** for the chosen columns, fill it offline, and **import** it. The
  envelopes page is organised **by month, grouped per Sabbath** (Sunday–Friday
  entries roll into the coming Sabbath); each Sabbath can be **downloaded as an
  Excel sheet with a trust/local summary at the bottom**, and any envelope can be
  **reassigned to another Sabbath**. Envelopes can be **cash** (these post into
  the central ledger) or **bank** (linked to the imported statement row, marked
  *processed via envelope* so the same money is never counted twice). One click —
  **“Receipt bank giving”** — turns a month's auto-allocated bank gifts (tithe,
  combined offering, etc.) into envelopes, **accounted only once**. A receipt — or
  an SMS, if integrated — is issued on entry.
- **Envelope reports.** A per-Sabbath **Treasurer's Cash Statement** (each
  contributor's split across funds, with trust funds itemised and the grand
  total) and a monthly **Offering Summary** (funds across the Sabbaths of the
  month, trust subtotal then local). Both are CSV-exportable and **print to a
  single landscape A4 page in width** (the table always fits the page width and
  simply runs onto further pages lengthwise), with a centred church header and a
  signature block.
- **~22 reports**, all date-filterable and printable: monthly treasurer's
  statement (with CSV export), offering summary by Sabbath, the two envelope
  reports above, **accounts by month** (every account's collections and expenses
  across the months of a year), **trust funds by month** (the Trust Fund and its
  sub-accounts per month), **collections summary** (collections, trust funds and
  expenditure per month in one table), tithe, giving by demographic group,
  development-group progress, expenditure (by fund/category/claimant), income vs
  expenditure, fund ledgers, trust funds, conference remittance advice, member
  annual statements, cash book, bank reconciliation, annual summary, and an
  audit log.
- **Remit trust funds.** From the remittance advice, one button raises a paid
  remittance expense against each trust fund for the amount still outstanding in
  the period — the monthly lump sum sent to the field — and the advice then nets
  off what's already been remitted. Batch remittances (draft → approve → mark
  remitted) **post to the general ledger and clear the outstanding liability
  consistently across the dashboard, the financial statements and the ledger**, so
  a paid trust fund never lingers as outstanding anywhere.
- **Bank-statement debits.** Money leaving the bank (review queue → *Bank
  debits*) is classified per line as a bank charge, a new expense to a fund, a
  match to an expense already entered (no double count), or a float withdrawal
  (cash drawn to pay expenses later, parked in a Float / Cash-on-hand holding
  fund).
- **Per-fund expense visibility.** Each fund has a "show in expenses" switch, and
  parent funds have a "children appear in expenses" switch — so collection-only
  accounts can be hidden from the expense picker entirely.
- **Flexible allocation rules.** A rule can match a reference exactly, or by
  starts-with / ends-with / contains, with exact matches taking priority.
- **Delete with caution.** Treasurers can delete a ledger entry or an expense from
  the list (with a confirmation prompt); the deletion is preserved in the audit log.
- **Cleaner envelope import.** The import template and column chooser list only the
  standalone funds and split offerings, not the Trust/LCB sub-accounts.
- **Refined, modern UI.** A polished design system across the whole app: layered
  soft shadows, refined forest-and-brass palette, a gradient sidebar with clear
  active states, a translucent sticky topbar with a user avatar, modern buttons
  with hover/press states and focus rings, cleaner tables (sticky headers, subtle
  zebra striping), custom-styled selects, a reworked sign-in screen, and a
  mobile slide-in navigation with an overlay. Respects reduced-motion preferences.
- **Fixed-asset register with depreciation.** A register of land, buildings,
  furniture, equipment, vehicles, IT, musical instruments and other assets, with
  per-category depreciation rules (straight-line or reducing-balance) configurable
  in Settings and overridable per asset. The register shows cost, accumulated
  depreciation and net book value as at any date, supports disposals, and prints.
- **Plain-language assistant (revamped).** A chat screen that answers questions straight from live data — collections, fund balances, tithe, trust still to remit, budget vs actual, operating surplus/deficit, cash position, whether the books balance, asset net book value, recent activity and more — each with a link to the full screen. It is read-only and works offline (rule-based); when an AI key is configured it can also answer free-form questions grounded in a live data snapshot. The interface has grouped starter prompts, typing feedback, and keeps the conversation within your session.
- **Chart of accounts (real accounting structure, now editable).** A five-element
  chart — Assets, Liabilities, Equity/net assets, Income and Expenses — sitting
  behind the funds/departments, browsable on its own page. Treasurers can **add,
  rename, reparent, deactivate and delete** accounts from the app. The built-in
  accounts the posting engine relies on are marked *core* and protected: they can
  be renamed or deactivated but not deleted, and their code/type stay fixed. An
  account that already has postings is deactivated rather than deleted, so the
  ledger and trial balance always stay intact and balanced.
- **Operational, reconciled general ledger.** Every journal posting is tagged
  with its fund, inter-fund transfers post to the ledger as equity reclassifications,
  and a **Ledger reconciliation** report proves — fund by fund — that the general
  ledger ties exactly to the fund reports, alongside the entity check that Assets =
  Liabilities + Fund balances. The chart of accounts is a complete, authoritative
  system of record that always agrees with the operational reports.
- **Double-entry general ledger.** Behind the simple forms, every receipt, payment
  and remittance posts balanced debit/credit entries (local receipts credit income,
  trust receipts credit a liability, expenses debit expense or fixed assets, etc.).
  This produces a **trial balance** (which proves the books balance), a **general
  ledger** with a running balance per account, and a **journal** of every posting —
  all exportable. The ledger updates live and can be rebuilt from source documents.
- **Year-end close & carry-forward.** Fund balances carry forward automatically
  (a year's opening equals the prior year's closing, computed from the ledger), and
  a formal year-end close on the Controls page records an immutable snapshot of each
  fund's carried-forward balance and locks the year's months. Closed years can be
  re-opened by an administrator.
- **Inter-fund transfers.** Move money between the church's own funds with a
  dedicated transfer (not income or expenditure): the source fund decreases and
  the destination increases by the same amount, leaving total funds unchanged.
  Trust funds are excluded (restricted for remittance), transfers appear on each
  fund's ledger and never inflate giving or expenditure reports, and they can be
  reversed (a mirror entry is posted, nothing is deleted).
- **Petty cash.** A dedicated petty cash float with its own register: **top up**
  the float by drawing cash from any fund, **record disbursements** (small cash
  payments, charged by category and marked paid on the spot), and see a running
  balance with the imprest/float position (how much to top up to get back to the
  set float). The register ties to the general ledger like any fund, and an
  optional imprest amount is configured in Settings.
- **Recurring (scheduled) expenses.** Predetermined payments that fall due every
  Sabbath or every month — allowances, stipends, standing charges. Each schedule
  generates real expense entries on their due dates (with one click), which then
  flow through approval and post to the ledger like any expense; locked months are
  skipped and an entry is never created twice. (This is distinct from the
  recurrent-vs-capital accounting classification below.)
- **Recurrent vs capital expenditure.** Every expense is classified as recurrent
  (day-to-day running cost) or capital (creates/improves a fixed asset), and a
  capital expense can be linked to the fixed-asset register entry it funded. The
  Income & Expenditure statement separates the two — showing an operating
  surplus/(deficit) before capital, then capital expenditure, then the net result
  — and the expenses list can be filtered by type.
- **Full SDA financial-statement suite.** Following the General Conference
  *Financial Management in SDA Church Organizations* framework, the app produces the
  complete set: (1) **Statement of Financial Position** in the classified **Net
  Assets** format — current/non-current assets, liabilities (trust funds payable),
  and net assets classified as Unallocated, Allocated and Invested in property;
  (2) **Statement of Financial Activity** — revenue, operating and capital
  expenditure, surplus/(deficit), and a change-in-net-assets reconciliation;
  (3) **Statement of Changes in Net Assets** — how each class of net assets moved
  over the period (opening + surplus ± capital reclassification ± transfers =
  closing); and (4) a **Statement of Cash Flows** on the operating/investing/
  financing basis that reconciles the movement in total cash. Net assets is used
  throughout instead of “fund balance”, tithe is treated as a **pass-through
  liability** (held on behalf of the field, not the church's revenue), and the
  statements tie to one another (changes-in-net-assets and cash-flow closings agree
  with the financial position). All export to CSV/Excel and print to PDF.
- **Tabbed reporting suite.** Reports are organised into Treasurer, Trust funds,
  Financial, Executive and Oversight tabs, including new Daily, Weekly, Cash-flow,
  Board, Pastor's and Conference-submission reports. Every report exports to CSV
  and styled Excel (.xlsx), and prints to PDF with a church/period header.
- **Automatic bank reconciliation.** An engine scores each bank withdrawal
  against recorded expenses on amount, date, cheque/voucher and payee/narration,
  producing a confidence score with a plain-language reason. Matches ≥ 90% are
  linked automatically; 55–89% are queued for one-click confirmation (or reject).
  Every suggestion is stored as a ReconciliationMatch for traceability.
- **Executive dashboard (Chart.js).** A live overview with KPI cards (today's
  collections, bank & cash position, outstanding trust, pending expenses, items in
  review) and charts for giving trend, department spending, monthly income/expense
  and trust balances. Chart.js is vendored locally, so it works on an offline
  church server.
- **Financial-health KPIs + anomaly alerts.** Traceable, rule-based metrics
  (cash-flow trend, expense growth, giving trend, remittance compliance, budget
  compliance) and proactive alerts — department overspending, over-budget funds,
  overdue/missing remittances, sudden giving drops, unusually large expenses and
  possible duplicates — surfaced at the top of the dashboard.
- **Budget management with variance.** Year-scoped budgets (Budget / BudgetLine)
  per fund, set on the Budgeting page with an optional category breakdown. A new
  **Budget vs Actual** report shows Budget, Actual, Variance and Variance % per
  fund — annual, quarterly or monthly (the annual budget is pro-rated to the
  period) — with overspends flagged and CSV export.
- **Reversals, not deletions.** Ledger entries are never destroyed — a treasurer
  reverses an entry, which posts a contra line that nets it to zero while both
  remain visible, with a TransactionReversal audit record.
- **Period locking.** A Controls page lets a treasurer lock a month (closing a
  quarter/year = locking its months); locked periods block create/edit/reverse
  for everyone except an administrator, who can override and unlock.
- **Duplicate detection.** The Controls page flags likely duplicate expenses and
  offerings; bank imports are de-duplicated automatically by bank reference.
- **Multi-fund debit matching.** One bank withdrawal can be matched to several
  expenses across different funds (their total must equal the debit); each expense
  keeps its own fund, and the debit is flagged as a split payment.
- **Trust funds as a restricted liability.** Trust money is now tracked as an
  outstanding liability to the field and cannot be charged operating expenses —
  it is only remitted. A dedicated **Trust Fund Remittance** dashboard shows each
  fund's collected / remitted / outstanding / days-outstanding, and a batch
  workflow (generate → approve → mark remitted, with a batch number and cheque
  details) clears the liability only when the funds are actually sent.
- **One canonical Sabbath calendar.** Envelopes, offerings, cash, expenses and all
  reports now derive the Sabbath an entry belongs to from a single rule, so the
  stored week ordinal always agrees with the Saturday-bucketed reports.
- **Safer bank-debit matching.** Matching one debit to several expenses now
  requires they all belong to the same fund, protecting fund accuracy.
- **Visible validation + fuller audit trail.** Forms show non-field errors instead
  of redirecting away, and allocation-rule create/delete is now on the audit log
  alongside transactions, expenses and envelopes.
- **Real chart of accounts seeded.** The church's full account list (42 funds)
  is seeded with opening balances — e.g. Development 3,047,812.72 and the LCB
  budget line −6,672.01 — alongside the maintained Trust Fund and Local Church
  Budget account trees. Allocation rules are built automatically from the
  labelled bank-narration dataset (references like AWMREG, PFREG, CHOIRLAUNCH,
  KYETHANI, APM map to the right fund), so most M-Pesa giving auto-allocates.
- **Settings tabs & optional LLM.** Settings is organised into tabs (Branding,
  Features, SMS, Assistant, Signatories). The assistant is offline by default; if
  you purchase API credentials you can enable a provider (Anthropic, OpenAI,
  Gemini, Groq, OpenRouter, or a custom OpenAI-compatible endpoint), and it falls
  back to the offline engine if a call fails.
- **Annual budgeting page.** Budgets and brought-forward opening balances are set
  on a dedicated Budgeting screen (per year), keeping the Funds page purely for
  structure. Opening balance is documented as the start-of-year carry-forward.
- **Bank-debit handling, multi-match.** A single withdrawal can be matched to
  several expenses at once; the system requires the selected expenses to sum to
  the debit amount before linking them.
- **Review-queue offline workflow.** Export the queue to CSV, allocate funds in
  a spreadsheet, and re-import to apply — optionally remembering rules.
- **Cheque remittance.** Remitting trust funds records a paid remittance expense
  (method Cheque, with the cheque number/date), so the system knows what has been
  sent and nets it off the amount still to remit.
- **Treasury assistant.** A chat page (`/assistant/`) where you ask in plain
  language — "total collections last month", "balance of Tithe", "trust funds to
  remit", "outstanding expenses", "top givers this year", "how much did Jane give
  this year", "what's in the review queue" — and it answers from live data with a
  small table and a link to the full screen. It runs entirely offline (no API
  key) and is read-only: it retrieves and explains, never changes anything.
- **SMS receipts, configurable.** When a member has a phone number, the system
  can text them a receipt. The scope is a setting: off, all envelope entries, or
  bank receipts only — so you can, for example, auto-text everyone whose bank
  gift you receipt, or only those, without code changes.
- **Bank reconciliation sheet.** A working reconciliation you can save: start
  from the bank statement closing balance, add reconciling items you type in
  (unpresented cheques, deposits in transit, cash at hand, bank charges,
  interest, errors…), each adding to or subtracting from the bank balance, and
  the sheet shows the adjusted balance against the cash-book balance and the
  remaining difference. The cash-book balance is suggested from the ledger and
  can be overridden.
- **System configuration page** (treasurer-only **Settings**): turn features on
  or off — expense approval, M-Pesa-ref display, development groups, automatic
  member creation, automatic envelope receipts — and configure **Advanta bulk
  SMS** (see below).
- **Two fund types only:** **Trust funds** (remitted to the field, e.g. East
  Nairobi Field) and **Local funds** (kept by the church for its departments).
  **Development groups** have their own CRUD; giving to a group is tracked
  against it and flows into the Development local fund.
- **Sub-accounts (subgroups).** Any fund can have child accounts that roll up to
  it — e.g. *Youth Ministry* → Potluck / Choir / Mission, or *Church Building
  Fund* → named phases. A sub-account inherits its parent's fund type, can be
  given to and spent from in its own right, and the parent's fund ledger shows
  each sub-account plus a combined total. (Development groups are the numbered
  sub-accounts of the Development fund.)
- **Split offerings.** Some collections are given as one lump sum but divide by
  percentage across funds — e.g. **Combined Offering** and **Thanksgiving** are
  50% trust (remitted) / 50% local. Define a *split fund* with its component
  percentages once; thereafter a member's single figure — whether keyed in the
  envelope ledger, entered as cash, or arriving on the bank statement under that
  reference — is automatically posted as separate ledger entries to each side,
  with rounding handled so the parts always sum to the original. The seed sets
  up a **Trust Fund** account (Tithe, Combined Offering 50%, Camp Meeting,
  Evangelism–Field, Station Development, Thanksgiving 50%) and a **Local Church
  Budget** account (Sabbath School, Loose Offering, Combined Offering 50%,
  Thanksgiving 50%, LCB budget line, Envelopes/SUS, LCB departments) to match a
  typical SDA local-church chart.
- **Members** can optionally be flagged as a **church member** or **Sabbath
  School member**.
- **Roles.** Treasurer (full access incl. approvals, settings and user
  management), Assistant (data entry, imports, queue, envelopes — no approvals),
  Auditor (read-only).

### SMS (Advanta)

SMS is off by default. On the **Settings** page, switch it on and enter your
Advanta credentials (API key, partner ID, sender ID/shortcode) — see
`advantasms.com/bulksms-api`. The integration posts to Advanta's QuickSMS
`sendsms` endpoint; the **base URL and field names are configurable**, so adjust
them to match Advanta's current documentation if needed. Every send is recorded
in an SMS log, and there's a *Send test SMS* button. Sending never raises — a
failed/disabled attempt is just logged.

### Petty cash (tied to ministries)

Petty cash is a **cash location** (a physical float), not a fund. You **top up**
the float (cash placed in the box) and **record disbursements** that are charged
to the **fund/ministry they were spent on** — so the ministry carries the cost and
**fund balances stay correct**. The float balance is a control total (top-ups
less petty disbursements) you reconcile against the cash in the box, and the page
shows the imprest target and how much to top up to reach it.

### Receipts & supporting documents

- **Expense receipts.** Every expense has a detail page where you can **attach the
  receipt** a claimant brings back (and any number of supporting files). The
  expense list flags whether a receipt is on file.
- **Asset documents.** Each fixed asset has a detail page for attaching
  **purchase receipts, warranties, photos** and other documentation.

### Asset disposal

Disposing of an asset records the **method** (sold / scrapped / donated / lost),
the **proceeds** and the **fund** that receives them. The app computes the
**gain or (loss)** as proceeds less net book value, records the proceeds as a
receipt into the chosen fund, and removes the asset from the register — so net
assets move by exactly the gain or loss.

### Contribution receipts (standard & ETR)

From the **Envelopes** page (and each envelope's detail) you can print a
**contribution receipt** showing the church name, date, receipt number,
contributor, **itemised contributions** and total, with a footer message. The
footer comes from **Settings → Branding → receipt message** (a sensible default
is used if it's blank). A compact **ETR / thermal** layout (≈72 mm, monospace) is
available for supermarket-style receipt printers — use the *ETR* link to print it.

### Payables, accruals & prepayments

Beyond pure cash, the **Payables & accruals** page tracks:

- **Accounts payable** — things **purchased on credit** (owed but not yet paid).
- **Accrued expenses** — costs **incurred but not yet invoiced/paid**.
- **Prepayments** — amounts **paid in advance** spanning future periods; the
  **unexpired** portion is shown as a prepaid asset (straight-line by month).

These are an **accrual overlay** on the cash-basis funds: they appear as
**memoranda on the Statement of Financial Position** (payables and accruals as
current liabilities, unexpired prepayments as a current asset, with a matching
*accrual adjustments* line within net assets so the statement still balances).
**Settling** a payable or accrual records the actual payment as an expense in its
fund, so the cash books recognise it at payment date.

### Transactions date filter

The **Transactions** list filters by a **date range** (From / To) alongside
search, channel, status and fund.

### Accounting basis & notes

The system keeps **spendable fund balances on a cash basis**, which suits most
local churches, while presenting **classified, accrual-aware statements**:

- **Fixed assets** are held at **cost** in the general ledger; **depreciation and
  net book value** are tracked in the asset register and reflected on the
  Statement of Financial Position as *Invested in property*. The Financial
  Position statement is the authoritative view of property values.
- **Capital purchases** reduce the buying fund's spendable balance and increase
  *Invested in property* by the same amount — total net assets are unchanged, which
  is the correct fund-accounting treatment.
- **Tithe** is a **pass-through liability** (held on behalf of the field), excluded
  from the church's revenue.
- **Payables, accruals and prepayments** adjust the cash position to an accrual
  view via memoranda, as described above.

The **general ledger stays balanced** (assets = liabilities + net assets) across
all of these, verified by the automated test suite.

---

## Security & approval controls

The system is built for shared use by multiple treasurers and assistants, so it
enforces separation of duties and protects sensitive data.

**Configurable approvals** (Settings → Approvals):

- **Require expense approval** — when on, recorded expenses stay *pending* until a
  treasurer approves them, and only then affect fund balances.
- **Dual approval threshold** — expenses at or above this amount need a **second,
  different treasurer** to co-approve before they can be marked paid (set to 0 to
  disable). Recurring schedules respect this too: a high-value scheduled item is
  never auto-approved, regardless of the approval setting or who generated it.
- **Enforce fund balance** — blocks an expense that would overdraw its fund.
  Treasurers can override with an explicit, logged confirmation; assistants are
  always blocked.
- **Enforce petty cash float** — blocks a petty cash disbursement larger than the
  float on hand.
- **Require dual year-end close** — a year-end close is *initiated* by one
  treasurer and only takes effect (balances carried forward, months locked) once a
  **second treasurer confirms** it.

**Data protection & integrity:**

- **Credentials encrypted at rest.** SMS and LLM API keys are encrypted with
  Fernet before being written to the database, so they never appear in the table
  or in backups. The key derives from `TREASURY_ENCRYPTION_KEY` (falling back to
  `SECRET_KEY`); set a dedicated `TREASURY_ENCRYPTION_KEY` in production.
- **Login brute-force protection** via *django-axes* — repeated failed logins
  lock the account/IP (default 5 attempts, 15-minute cool-off; configurable with
  `AXES_FAILURE_LIMIT`).
- **Password policy** — a minimum length of 10 plus similarity, common-password
  and numeric checks. (The demo seed users keep simple passwords for convenience;
  change them in any real deployment.)
- **Settings are audited.** Changes to configuration (including disabling approval
  or changing keys) are tracked in history with the user who made them.
- **Duplicate guards.** Manual cash entries warn and require confirmation when a
  matching fund/date/amount entry already exists, mirroring the bank-import
  dedupe.
- **Atomic financial writes.** Ledger postings, payable/accrual settlement and
  reconciliation links run inside database transactions, so a mid-operation
  failure can't leave the books half-written.
- **Positive amounts enforced** on transactions at the model level; reversals use
  an explicit contra entry rather than negative input.

> **Set `TREASURY_ENCRYPTION_KEY` and a strong `SECRET_KEY` in production**, and
> change the demo passwords. See *Configuration* below.

---

## External connections & bulk receipts

- **TLS / certificates.** Outbound calls (SMS and the optional assistant LLM)
  verify TLS using the bundled **certifi** roots, with a fallback to the system
  trust store, and honour a `TREASURY_CA_BUNDLE` (or `SSL_CERT_FILE`) for networks
  behind an inspecting proxy. This resolves the common
  `CERTIFICATE_VERIFY_FAILED: unable to get local issuer certificate` error.
- **Test the assistant.** On **Settings → Assistant**, after saving your provider,
  key and (optional) model, click **Test assistant connection**. It makes one live
  call and shows either the model's reply or the exact provider error (bad key,
  unknown model, etc.). Leaving the model blank uses a sensible default per
  provider (e.g. `llama-3.3-70b-versatile` for Groq, not an OpenAI model name).
- **Bulk contribution receipts.** On the **Envelopes** page, each Sabbath has
  **Receipts (A4)** — several compact receipts tiled per page with cut lines, to
  save paper on a plain printer — and **ETR**, a continuous run of 72 mm thermal
  slips for a receipt printer.

## Allocation, review & data-entry refinements

- **Period vs permanent allocation rules.** Each rule can be left permanent (any
  period) or given a `valid from`/`valid to` range. A dated rule that covers the
  transaction date **overrides** a permanent rule for the same reference — useful
  for campaign or camp-meeting references that mean different funds at different
  times of year.
- **Searchable fund picker in the review queue** — start typing a fund name to
  filter the list for fast allocation.
- **Statement-import confirmation (optional).** Turn on *Require import
  confirmation* (Settings → Approvals) and auto-allocated rows are **held** — they
  don't affect balances until reviewed. The import page then offers **Review
  auto-allocated**, where you can edit the fund per row, **download an Excel** to
  check offline, and confirm all or selected rows.
- **Text & link receipts.** Besides file uploads, an expense can carry a pasted
  **text receipt** (e.g. an M-Pesa SMS) or a **link** to an online/e-receipt.
- **Budget from breakdown.** A fund's annual budget is the **sum of its breakdown
  lines** (edited on the breakdown page) and shown read-only on the budget screen;
  the year selector now correctly remembers the chosen year.
- **Smarter duplicate detection.** A possible duplicate offering now requires the
  **amount and a similar payer name (>50% match)**, so two different givers of the
  same amount on the same day are no longer wrongly flagged.
- **Compact, economical receipts & ledger.** Bulk A4 receipts tile three-up with
  guaranteed no page-splitting; the envelope entry grid is tighter so more fund
  columns are visible; the Excel import page is a full-width, two-step layout with
  drag-and-drop.

## Budget source-of-funds & board report

- **Source of funds per budget line.** Every itemised budget line names where its
  money comes from: the **department's own funds** (the default when left blank),
  the **Local Church Budget (LCB)**, or **another fund**. Set it on each fund's
  budget breakdown page.
- **Board Budget Summary** (Reports → Board Budget Summary). A board-facing report
  showing each department's budget split by source (own / LCB / other), the
  church-wide totals, and — most importantly — **how much the Local Church Budget
  is expected to incur**, broken down as per-department allocations. Exportable to
  Excel/CSV and printable.
- **Prior-year pegging.** Trust-fund and LCB-item budgets can be started from the
  previous year: where a prior-year breakdown exists, a **“Copy {last year}
  breakdown”** button clones those lines (amounts and sources) into the new year,
  fully editable. The breakdown and board report both show the prior-year total
  for reference; where no prior data exists you simply enter the lines.

## Controls, corrections & extra channels

Accounting fixes in this revision: the dashboard's trust **"To remit"** is now the
true outstanding liability (receipts − remittances); the expense-form balance check
includes **inter-fund transfers**; the **Income & Expenditure** statement excludes
trust funds and remittances (so the surplus/deficit reflects local operations only);
a **second approval** can only be given after the first; the **offering summary**
shows every Sabbath across multi-month ranges; and **recurring expenses** in a
locked month regenerate correctly once it is unlocked (no lost stipends).

New capabilities:

- **Manual journal entries** (General ledger → Journal → *Manual entry*) for audit
  adjustments, opening-balance corrections and reclassifications. Entries must
  balance and are preserved when the ledger is rebuilt from source.
- **Sabbath cash-count sheet** (Envelopes → Cash counts): denomination breakdown,
  two or three counters with signatures, and an automatic discrepancy flag against
  the system's expected receipts.
- **Multiple bank accounts** (Settings → Channels → Manage bank accounts): tag each
  statement/transaction to an account for per-account reconciliation. One account is
  the default, so existing single-account imports are unaffected.
- **Staff advances / imprest** (Expenses → Staff advances): issue an advance, record
  settling receipts against it, and see the surplus to recover or shortfall to
  reimburse, then close it.
  Accounting treatment: issuing an advance is **not** expenditure — it does not hit
  the Income & Expenditure statement; only the settling receipts (recorded against
  the advance) reduce fund balances and appear as expenditure. On the **Statement of
  Financial Position**, an unspent advance is shown as a **receivable** (reclassified
  out of cash), so the balance sheet is correctly classified; once the advance is
  closed it drops off receivables.
- **Notifications**: an in-app bell alerts treasurers when an expense needs approval
  (and for remittance/budget notices); optional email can be switched on in
  Settings → Channels.
- **WhatsApp receipts** and **M-Pesa Daraja (Paybill) pull**: optional channels with
  settings and service scaffolds under Settings → Channels, ready to enable once the
  church's provider credentials are in place.

## Email, configurability, group reports & AI insights

- **System email (SMTP).** Settings → Email lets the treasurer configure outgoing
  mail (host/port/TLS/credentials/from-address) and send a test. It powers
  development-group report emails and the optional approval/remittance emails.
- **Development-group reconciliation.** Each group can carry a **leader name and
  email**. Reports → Development groups → *Members →* shows every member's
  contributions for the period (exportable), and the report can be **emailed to the
  group leader** with one click (only if an email is set and SMTP is configured).
- **Configurable items.** Expense **categories** can be extended with your own
  (Settings → Channels → Manage expense categories) — the built-ins remain. Extra
  **development-group reference words** (e.g. "project", "phase") can be added in
  Settings so the importer recognises them, on top of the usual grp/devgroup forms.
- **Cash-count expected total** now reflects true cash on hand: cash entries **plus
  cash envelopes**, excluding bank-receipted envelopes, **less** cash disbursed that
  Sabbath.
- **Executive dashboard** carries an optional **AI insights** panel (when the
  assistant is enabled): a one-click board briefing of highlights, risks and
  recommendations drawn from the live figures.
- Allocation-rule deletion is fixed.

## Receipt delivery, envelope corrections & group emails

- **Send receipts by SMS or WhatsApp** (Envelopes page) instead of printing — per
  envelope (✉ / 🟢 icons) or for a whole Sabbath ("SMS all" / "WhatsApp all").
  Requires a linked member with a phone number and SMS/WhatsApp enabled in Settings.
- **Fixing envelope mistakes.** Each envelope row now has a delete (✕) action that
  removes it and the ledger entries it created. If you instead **reverse** an
  envelope's entry from the ledger, the envelope is shown **struck through** and
  marked "reversed" in the list.
- **Transparent cash count.** The count screen now shows the expected total broken
  down (cash entries + cash envelopes − cash disbursed, bank deposits excluded), so
  you can see exactly what is included; reversed entries net to zero automatically.
- **Development-group leader emails.** Reports → Development groups has **Email all
  leaders** (each group's member report is sent to its leader, spaced ~30s apart to
  avoid spam filters) and **Detailed Excel (all groups)** — a workbook with a
  summary sheet plus a per-group member breakdown.
- **Friendlier AI insights.** When the assistant can't run (key/model/disabled), the
  executive dashboard now shows a witty, plain-language nudge instead of a raw error.

## Correctness fixes (balances, reversals, performance)

- **Consistent balance rules.** Every balance and income figure — dashboard KPIs,
  fund balances, the cash-count expected total, development-group totals and the
  ledger — now applies one rule to receipts: **confirmed, not reversed, not a
  reversal contra**. Previously the dashboard's bank/cash cards filtered reversals
  on one side only and ignored the `confirmed` flag, so unconfirmed imports or a
  reversed entry could overstate the displayed balance. The cards now tie out to the
  ledger and the fund reports.
- **Reversals are validator-clean.** A reversal now records a contra entry with a
  **positive** amount (flagged as a reversal) instead of a negative one, so it can
  never violate the "amount ≥ 0.01" rule. Both the original and the contra are
  excluded from all totals and from the general ledger, so a reversed receipt
  disappears cleanly from every report and the books still balance.
- **Faster fund checks.** The fund-overspend guard now reads a single fund's balance
  with targeted aggregations instead of looping the whole portfolio, so it stays
  fast as data grows. It ties out exactly to the fund report's closing balance.
- **PostgreSQL.** All reports use database-agnostic date functions (no SQLite
  `strftime` SQL), so the Annual Summary and everything else run on PostgreSQL
  unchanged.

## Edit controls & concurrency

- **Period locks apply to edits too.** Editing an expense now respects period locks
  exactly as creation does — you cannot change the date, amount or fund of an entry
  in (or move one out of) a month the treasurer has locked.
- **Editing can't bypass approval.** An approved or paid expense can only be edited
  by a treasurer, and if its **amount or fund changes** it is automatically returned
  to *pending* (approvals cleared), so it must be re-approved — re-applying the
  dual-approval threshold and fund checks. Cosmetic edits (e.g. wording) keep their
  status.
- **Safe remittance numbering.** Batch numbers are now allocated inside a locked,
  retrying transaction, so two treasurers remitting at the same moment can't collide
  on a number or hit an IntegrityError.

## Review hardening (attachments, performance, commitments, remittance)

- **Receipt uploads are validated.** Expense attachments must be a PDF or image
  (.pdf/.jpg/.png/.heic/.webp/.gif) and ≤ 10 MB — arbitrary files (e.g. executables)
  are rejected, matching the statement-upload guard.
- **Cash-count total computed in the database.** The expected-cash breakdown (cash +
  cash envelopes − cash disbursed) is now a single conditional aggregation instead of
  a Python loop over the week's rows.
- **Asset NBV without N+1.** A new `nbv_total(as_of)` loads the depreciation rules and
  site config once and reuses them across all assets, replacing the per-asset rule
  lookups; the three report call sites use it.
- **Payables & accruals are visible.** They already feed the Statement of Financial
  Position; the executive dashboard now also shows a clickable "Payables & accruals"
  commitments card. (By design these stay an overlay — the cash-basis fund balances
  are not reduced by an unpaid invoice, which keeps the fund engine a true cash record.)
- **Remittance due date enforced.** The configured trust-remittance due day now drives
  a *danger* alert (with a link to prepare the remittance) once the deadline passes
  while trust funds remain unremitted.
- **Remittance history on the Trust report.** The Trust Funds report now lists each
  remittance batch inline (number, date, cheque, status, amount) for conference prep.

## Sabbath close (instead of a fixed cutoff time)

Which Sabbath a gift is *filed under* is governed by closing a Sabbath, not a clock:

- **Close when you finish, whenever that is.** On a Sabbath's cash-count page, click
  **Close this Sabbath** once it is counted and receipted. There is no fixed 4pm —
  you close when you actually pool the statement, be that early or late afternoon.
- **Late gifts roll forward.** A gift whose natural Sabbath is already closed is
  automatically credited to the **next open Sabbath** (rolling past several closed
  ones if needed). Its real transaction date never changes, so a closed count is
  never reopened. A treasurer can reopen a Sabbath if a correction is needed, and
  any entry can be nudged a Sabbath forward/back manually.
- **Books vs. attribution are separate.** A gift's effect on fund balances, the
  ledger, financial statements and bank reconciliation is driven by its **transaction
  date** (cash basis) — *not* by its Sabbath. So a **mid-week or month-end import**
  (e.g. a month ending on a Wednesday) captures every gift in the correct month
  regardless of Sabbath: items "pending to be receipted next Sabbath" are already in
  the accounts by date, and simply appear under the next Sabbath's offering column
  when that Sabbath is counted. Nothing is left unaccounted.

The behaviour can be switched off in Settings → Channels (then every gift stays on
its natural Sabbath).

## Donations terminology & un-receipted bank items

The term "gifts" now reads **donations** throughout the interface.

**How an un-receipted bank donation for a trust fund is treated.** A bank donation
is only credited to a specific trust fund (and so increases that fund's "to remit")
**once it is receipted** — i.e. confirmed and allocated to the fund. Until then:

- it does **not** touch the trust fund balance or the amount to remit; and
- it is **not invisible** — because the money is genuinely in the bank, it is shown
  on the Statement of Financial Position as **"Bank receipts pending allocation"**
  (a cash asset) matched by a **"Receipts pending allocation (unidentified)"**
  liability. The statement therefore still ties to the bank, and the money is
  carried in suspense rather than dropped.

This covers both held-for-review imports (still unconfirmed) and confirmed-but-
unallocated receipts (no fund yet). When the donation is receipted to its trust
fund, it moves out of suspense and into that fund's collected/▸to-remit, and the
general ledger and reports all agree. (Financial recognition is by transaction date
on a cash basis; receipting resolves the *allocation*, not whether the money exists.)

## Accounting review — second (deeper) pass

A second review went beyond the report totals into the ledger and asset accounting:

- **Asset disposals no longer overstate income.** Previously the full disposal
  proceeds were recorded as a receipt and so counted as income. Now the proceeds
  are flagged as a **capital receipt** — they still increase the fund's cash (the
  money is real), but they are excluded from Income & Expenditure, where only the
  **gain/(loss) on disposal** (proceeds − net book value) is recognised. The
  balance sheet still balances after a disposal.
- **The general ledger is a genuine, complete double-entry** and is not merely
  "balanced by construction": receipts post DR Cash / CR Income (CR Trust Payable
  for trust funds); expenses post DR Expense / CR Cash (remittances DR Trust
  Payable; capital purchases DR Fixed Assets); inter-fund transfers net to zero;
  opening balances are established once. The per-fund balance computed purely from
  the ledger **ties exactly** to the fund reports (including after a capital
  purchase), and the entity-level accounting equation (assets = liabilities +
  funds) holds.
- **Depreciation arithmetic verified** for both straight-line and reducing-balance,
  including the salvage-value floor (an asset is never depreciated below salvage).
- **No double counting confirmed** across the accrual overlay and petty cash:
  credit purchases (payables) and petty-cash top-ups do not move the cash-basis
  fund balances, and a petty-cash disbursement charges its fund exactly once.

## Accounting review (correctness pass before go-live)

A full accountant's review verified the core calculations against standard
invariants and fixed the issues found:

- **Reversed donations no longer double-count in reports.** When a reversal records
  a positive contra entry, several report totals (Income & Expenditure, the Monthly
  Treasurer's report, fund ledgers, member statements, the Annual summary, cash-flow
  and top-giver figures) had to exclude both the reversed original and its contra.
  All recognised-income figures now flow through one canonical rule —
  **confirmed, not reversed, not a contra** — so every report agrees.
- **Capital purchases are excluded from operating expenditure.** Buying an asset is
  not a running cost, so the Income & Expenditure statement excludes capital
  expenditure (it is shown as a separate "capital additions" memo). This stops the
  purchase year's deficit being overstated; the asset instead appears at net book
  value on the Statement of Financial Position.
- **Unconfirmed (held-for-review) donations are excluded** from income everywhere,
  consistent with the fund balances and ledger.
- **Verified invariants** (on the demo data and in automated tests): the general
  ledger balances (debits = credits); the Statement of Financial Position balances
  (assets = liabilities + net assets), including with capital, reversals, advances,
  prepayments, accruals and pending receipts present; each fund's balance equals
  opening + receipts − approved/paid expenses + transfers; trust "to remit" equals
  collected − remitted and ties to the trust payable on the balance sheet;
  inter-fund transfers net to zero; and total recognised income reconciles to fund
  receipts plus the pending-allocation suspense (nothing leaks).
- **No double counting** between expenses and bank statement debits: only `Expense`
  records reduce a fund; statement debits are reconciled to them via their link.

Note on depreciation: on this cash basis, an asset's full cost leaves the fund when
purchased and the asset then sits at net book value on the balance sheet; periodic
depreciation is a balance-sheet memo and is not posted as an income-statement
expense. This is the standard treatment for a cash-basis church and keeps the
balance sheet tying out.

## Testing

The suite has **300+ automated tests** covering the accounting engine, the report
surface (every report page renders and key figures are checked), the expense and
approval workflow, fund transfers, envelopes and counting, the Sabbath-close model,
member matching/merge, the general ledger and its reconciliation to the fund
reports, statement import and dedup, asset depreciation and disposal, and the
email/SMS services (mocked — no network).

Run them with:

```bash
python manage.py test
```

To measure coverage (the `coverage` package is in `requirements.txt`):

```bash
coverage run --source=. --omit="*/migrations/*,*/tests.py,*/test_*.py,manage.py" manage.py test
coverage report          # or: coverage html
```

Business-logic coverage is ~79%; the remaining gaps are the optional, network-gated
integrations (the AI assistant, live SMS/WhatsApp/M-Pesa gateways), which require
live credentials to exercise and are not part of the core accounting.

## Real-time bank feed (Co-operative Bank CBS events)

The system can receive a notification for **every transaction in real time** via the
Co-operative Bank *CBS Event Notification* service. The bank's Core Banking System
**pushes** each debit/credit to a webhook this app exposes — you do not poll. Each
event becomes a `Transaction` immediately, allocated to a fund (or queued for
review) and matched to a member, exactly as a statement import would, so giving
appears without waiting for the weekly CSV.

**Endpoint:** `POST /api/bank/cbs-events/` (JSON in, `{"MessageCode":"200", ...}`
out). It is idempotent — the bank re-delivers on any non-2XX reply, and a repeated
`TransactionId` is acknowledged without creating a duplicate.

**Configure it** in Settings → Channels → *Real-time bank feed*: enable it, pick the
authentication mode, and set the credentials. The page shows the exact webhook URL
to give the bank. A read-only **feed log** (`/statements/feed-log/`) shows every
event received, its status, and the transaction it created.

### What the bank needs from you (per the CBS spec)
- The **full webhook URL** events should be posted to (shown on the settings page).
- In **production** that URL must run on **HTTPS (SSL)**; the bank's test
  environment may call an unsecured endpoint.
- An **authentication method** — the spec supports **Basic** (username & password)
  or **token/bearer**. Configure the same values here and share them with the bank.

### What you need from the bank
- Onboarding under the written agreement referenced in the CBS specification.
- The **institution account number(s)** to be monitored (matched to your bank
  accounts by `AcctNo`).
- Agreement on the auth method and the credentials the bank will present.

Credentials are encrypted at rest (Fernet), and every webhook call is authenticated
against them before any data is written — the endpoint is the only public,
machine-to-machine entry point, so it is locked down accordingly.

## Quick start (development, SQLite)

Requires Python 3.12.

```bash
cd treasury
python -m venv .venv
. .venv/bin/activate            # Windows: .venv\Scripts\activate
pip install -r requirements.txt

python manage.py migrate
python manage.py seed_demo      # optional: demo funds, members, giving, users
python manage.py runserver
```

Open http://127.0.0.1:8000/ and sign in.

If you ran `seed_demo`, three accounts are created:

| Username    | Password       | Role                       |
|-------------|----------------|----------------------------|
| `treasurer` | `treasurer123` | Treasurer (full access)    |
| `assistant` | `assistant123` | Assistant (data entry)     |
| `auditor`   | `auditor123`   | Auditor (read-only)        |

If you did **not** seed, create your own admin first:

```bash
python manage.py createsuperuser
```

A superuser has full (Treasurer-level) access automatically. To create ordinary
staff users and assign roles, sign in as a treasurer and use **Users & roles**
in the sidebar (or Django admin at `/admin/`).

---

## Trying the statement import

Two sample statements live in `sample_data/`:

- `sample_statement.csv` — exercises the three narration shapes plus a
  bank-charge debit.
- `sample_bank_statement.csv` — mirrors the **real bank export layout**: a few
  preamble rows, then a header row (`Posting Date, Value Date, Core Ref,
  Channel REF, Narration, Debit Amount, Credit Amount, Running Balance`), comma
  grouped amounts, the **Core Ref** as the dedup key and the **Channel REF** as
  the visible M-Pesa reference. Development-group references are recognised
  across the many spellings seen in practice (`DEVGR7`, `devg14`,
  `DEVLOP GP14`, `dev grp5`, `DEv Gp39`, `Devgrp11`, …) and unknown groups are
  created automatically.

Sign in as the treasurer or assistant, go to **Import statement**, upload a
file, and watch rows auto-allocate while unclear ones go to the **Review
queue** — tick "Remember this reference" to teach the system a rule for next
time. Re-importing the same file is safe: rows already captured (by Core Ref)
are skipped.

---

## Configuration

Most day-to-day settings live on the **Settings** page in the app (branding,
feature toggles, SMS) and are stored in the database, so a treasurer can change
them without touching the server. Expense-approval, for example, is driven by
the *Require expense approval* toggle there.

Deployment settings are read from environment variables; see `.env.example`.
There is no automatic `.env` loading, so export the variables (or use a tool
like `direnv`) before running. Common ones:

- `DJANGO_SECRET_KEY`, `DJANGO_DEBUG`, `DJANGO_ALLOWED_HOSTS`
- `SITE_NAME` — fallback branding before the Settings page is configured

---

## Production deployment & HTTPS

The app is designed to run behind a TLS-terminating reverse proxy (nginx, Caddy
or Traefik). Set `DJANGO_DEBUG=False` and the security middleware switches on
automatically: HTTPS redirect, HSTS (1 year, incl. subdomains, preload), secure +
HTTP-only session/CSRF cookies, `X-Frame-Options: DENY`, content-type nosniff and
a same-origin referrer policy. `SECURE_PROXY_SSL_HEADER` is set so Django trusts
the proxy's `X-Forwarded-Proto`.

Set these environment variables in production:

```
DJANGO_DEBUG=False
DJANGO_SECRET_KEY=<a long random string>
DJANGO_ALLOWED_HOSTS=treasury.yourchurch.org
DJANGO_CSRF_TRUSTED_ORIGINS=https://treasury.yourchurch.org
TREASURY_ENCRYPTION_KEY=<a second long random string, used to encrypt stored API keys>
# optional: DJANGO_HSTS_SECONDS, DJANGO_SECURE_SSL_REDIRECT, AXES_FAILURE_LIMIT
```

Terminate TLS at the proxy and forward to the app (gunicorn/uvicorn) with
`X-Forwarded-Proto: https`. Run `python manage.py check --deploy` to confirm the
security settings are active.

## Running on PostgreSQL (production)

The app defaults to SQLite for zero-config evaluation. To use PostgreSQL,
install the driver and set the database variables:

```bash
pip install "psycopg[binary]"
export POSTGRES_DB=treasury POSTGRES_USER=treasury \
       POSTGRES_PASSWORD=secret POSTGRES_HOST=localhost POSTGRES_PORT=5432
python manage.py migrate
```

When `DJANGO_DEBUG=False`, secure-cookie and SSL-redirect settings switch on;
set `DJANGO_ALLOWED_HOSTS` and `DJANGO_CSRF_TRUSTED_ORIGINS` accordingly, and
run `python manage.py collectstatic`.

All reports — including the Annual Summary — group dates with database-agnostic
ORM functions (e.g. `ExtractYear`), so no SQLite-specific SQL is used; the app runs
on PostgreSQL without code changes.

---

## Tests

```bash
python manage.py test
```

Covers phone normalization and the order-insensitive name key, the
member-matching and merge logic, the allocation engine (seeded rules,
dev-group detection across messy spellings, review fallback), the three
narration shapes, the importer (row classification, duplicate skipping,
member/fund linking), the **real bank `.xls` layout** (header detection,
M-Pesa-ref capture, reading from bytes), the **envelope ledger** (cash
envelopes create ledger entries, duplicate-receipt protection, next-receipt
increment), and the **SMS** disabled/missing-credential paths.

---

## Project layout

```
config/        project settings, root urls
core/          roles, permissions, dashboard, SiteConfig + SMS, settings page,
               member-search & next-receipt endpoints, seed_demo command
accounts/      user creation & role assignment
departments/   funds (Trust / Local) and development-group CRUD
members/        members, aliases, phone/name matching, merge, duplicates
giving/         the central Transaction ledger, allocation rules & engine
statements/    statement upload, parser (csv/xls/xlsx), synchronous importer
cashbook/      expenses + approval workflow + edit
envelopes/     envelope giving: ledger entry, cash/bank handling, lines
reports/       the report catalog + CSV export + balance aggregates
templates/     base shell, per-app templates, report templates
static/css/    the design system
sample_data/   sample statements to import
```

The statement importer runs synchronously, which is fine for a typical weekly or
monthly statement. For very large files, `statements/services/importer.run_import`
is the single function a Celery task would wrap.

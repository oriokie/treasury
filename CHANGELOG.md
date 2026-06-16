# Changelog

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

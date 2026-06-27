"""Single source of truth for the application version.

The version lives in the VERSION file at the project root (one line, semver).
It is shown in the app footer and used by the update checker so a hosted
instance can tell the user when a newer release is available on GitHub.
"""
import subprocess
from functools import lru_cache
from pathlib import Path

VERSION_FILE = Path(__file__).resolve().parent.parent / "VERSION"


@lru_cache(maxsize=1)
def get_version():
    try:
        return VERSION_FILE.read_text(encoding="utf-8").strip() or "0.0.0"
    except OSError:
        return "0.0.0"


@lru_cache(maxsize=1)
def get_git_revision():
    """Short commit hash if running from a git checkout, else ''."""
    try:
        out = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=VERSION_FILE.parent, capture_output=True, text=True, timeout=3)
        return out.stdout.strip() if out.returncode == 0 else ""
    except (OSError, subprocess.SubprocessError):
        return ""


def version_string():
    v = get_version()
    rev = get_git_revision()
    return f"v{v}" + (f" ({rev})" if rev else "")


WHATS_NEW = {
    "1.75.0": "New Appearance & Preferences page (in the account menu, and linked from Settings) lets "
              "every user personalise their own workspace: light/dark/system theme, an accent colour "
              "(presets or a custom pick), sidebar style (expanded, compact or icon-only), font size, "
              "boxed or full width, rounded or square cards, dashboard widgets you can show/hide and "
              "drag to reorder, your default landing page after login, rows-per-page and table density, "
              "accessibility options (high contrast, reduced motion, larger targets, focus outlines), "
              "and notification toasts (on/off, duration, optional desktop alerts). Changes apply "
              "instantly and save to your account, so they follow you on any device — with a Reset to "
              "Defaults button if you want to start over.",
    "1.74.0": "A round of UX and accessibility polish across the whole app: a skip-to-content link and "
              "clearer keyboard focus, screen-reader labels on the menu, search and notification icons, "
              "a slim loading bar during background actions, dismissible (and auto-clearing) status "
              "messages, data tables that now scroll cleanly on phones, stronger text contrast, and a "
              "guard that prevents a form being submitted twice by accident — all without changing how "
              "anything works.",
    "1.73.0": "You can now choose exactly which funds make up the Local Church Budget under Settings → "
              "Channels → Allocation & categories. Reports (the Monthly Treasurer's Report, the LCB trend "
              "and LCB expenditure) use those funds and their sub-accounts, so newly added accounts are "
              "always included — no more guessing from the fund name (it still falls back to name "
              "matching if you leave the selection empty). Also fixed dashboard stat tiles where long "
              "figures such as Total receipts could overflow.",
    "1.72.0": "Expense receipts are now filed by the month each expense was incurred, and a new "
              "Receipts page (Expenses → Receipts) shows them grouped by month for printing, with a "
              "one-click ZIP download of a whole period for audit. The Monthly Treasurer's Report is "
              "substantially upgraded: the trust and LCB trends now compare the current month with the "
              "previous two, every LCB account is listed (new ones appear automatically), the five-year "
              "trend is a chart, LCB expenditure is now shown correctly, a full local-funds statement "
              "(opening, receipts, expenses, closing) replaces the old activity list, and the financial "
              "position, cash-flow statement and bank reconciliation now mirror the detailed main "
              "reports.",
    "1.71.0": "The bank reconciliation page is clearer — a summary strip at the top, reconciling items "
              "grouped into additions and subtractions, and the petty-cash float can now be added as a "
              "reconciling item automatically (the cash book already includes that cash, so it's added "
              "back as cash-at-hand to reconcile to the bank — not double-counted). The petty cash "
              "register gained a period selector and Excel/CSV download. And the main dashboard now shows "
              "a live bank-balance tile from the real-time feed when available.",
    "1.70.0": "A batch of report fixes and polish. The ledger reconciliation page layout is fixed, and "
              "both it and the general journal now export to Excel. The Monthly Treasurer's Report is now "
              "a formal, detailed report — with a masthead, a per-fund collections breakdown, an itemised "
              "income statement, and a sign-off block. The Statement of Financial Position now splits "
              "trust funds payable correctly into receipted and not-yet-receipted (with unallocated bank "
              "receipts shown separately as suspense). The income statement no longer shows headings in "
              "block capitals, and it and the changes-in-net-assets statement both gained a period "
              "selector. And historical data is easier to reach (Reports → Historical data, and a button "
              "on the Annual summary), where you can now expand each year to see and delete individual "
              "months.",
    "1.69.0": "A redesigned, board-ready Monthly Treasurer's Report (Reports → Monthly Treasurer's "
              "Report) pulls everything onto one compact page: collections summary, a four-month trust "
              "and LCB sub-account trend, a five-year year-to-date trend, LCB expense and local-fund "
              "breakdowns, the income statement, financial position, cash-flow statement and the latest "
              "bank reconciliation — each with a short plain-language note and an AI-written headline. "
              "Historical comparison data can now be kept per month (and imported from Excel, with a "
              "sample provided), with yearly totals computed automatically. And the financial position "
              "now splits trust funds payable into receipted and not-yet-receipted, and explains the "
              "general (unallocated) and Board-designated (allocated) fund classes.",
    "1.68.0": "Reporting accuracy fixes. The bank position now subtracts bank-paid expenses entered "
              "directly (not just debit rows) so it no longer overstates the balance, and it shows the "
              "live cleared balance from the real-time bank feed alongside the imported statement. The "
              "Statement of Cash Flows always reconciles now, even if a payment has no capital/recurrent "
              "tag. And duplicate-offering detection is smarter: the two halves of a split offering are "
              "no longer mistaken for duplicates, and bank+envelope matches must now be close in time — "
              "removing the false positives from two genuine same-amount gifts weeks apart while still "
              "catching real double counts.",
    "1.67.0": "The envelopes page now shows each Sabbath collapsed by default — tap a Sabbath to "
              "expand its list. Pledge campaigns can now be deleted (only when they have no pledges); "
              "inter-fund transfers already support a reversing entry. You can now send an SMS to "
              "development-group members — all groups or one group — with a customizable message. And "
              "allocation rules are now editable and surfaced under Settings → Channels → “Allocation "
              "& categories”.",
    "1.66.0": "Several safety and audit hardening fixes: the bank debit queue now respects locked "
              "accounting periods (you can no longer post an expense into a closed month through it); "
              "rejecting an expense records a separate ‘rejected by’ (instead of mislabelling it as "
              "approved) and notifies the person who submitted the claim; unexpected errors are now "
              "logged on the server with a full trace rather than only shown as a generic message; and "
              "the HTMX library is now served from the app itself instead of a public CDN, removing a "
              "supply-chain risk.",
    "1.65.0": "Collection accounts (e.g. Camp Group 1–22) now receive contributions but are never "
              "selectable for expenses, and a one-click consolidation rolls every sub-account balance "
              "into the parent (with full transfer records; the children zero out but keep their "
              "history). Accounts now have a lifecycle — Active, Closed, Archived — so finished camps "
              "and fundraisers can be closed (only at a zero balance) and stay in historical reports "
              "without accepting new transactions; every status change is logged. New Cheque register "
              "tracks issued cheques and whether they've cleared, and feeds the bank reconciliation's "
              "unpresented-cheques figure automatically. Finally, envelopes receipted against a bank "
              "credit no longer show up as “Receipts Pending Allocation” on the financial position.",
    "1.64.0": "The Statement of Financial Position is now period-correct: a payable or accrued expense "
              "settled after the statement date still shows as a liability on that date (e.g. a 14th "
              "statement shows an accrual that was paid on the 15th). And a fund report now has a "
              "“Thank contributors (SMS)” button — it lumps each member's giving to the fund and its "
              "sub-accounts for the selected period and sends a customizable thank-you message.",
    "1.63.0": "On a fund report, the summary cards at the top now include the fund's sub-accounts "
              "(opening, receipts and closing balances roll the sub-accounts in, since they belong to "
              "the parent). A bank debit can now be allocated straight to petty cash, topping up the "
              "float. And recurring expense schedules can now be deleted (any expenses they already "
              "created stay in the ledger).",
    "1.62.0": "Backups can now be sent to off-site storage automatically: under Settings → Backup, "
              "enable off-site upload and give an HTTPS destination (Nextcloud/WebDAV or any endpoint "
              "that accepts an authenticated upload). The nightly backup (and a “Send a backup off-site "
              "now” button) uploads an encrypted copy, so a server failure never loses the books. "
              "Backup emails now also use your configured SMTP settings.",
    "1.61.0": "New Cash flow forecast report (Reports → Cash flow forecast) projects your cash "
              "position 30 days, a quarter and a year ahead — built from your giving run-rate, the "
              "actual schedule of recurring expenses and outstanding pledges, with a chart and a "
              "full breakdown. The executive overview now also shows the forecast at a glance and "
              "total outstanding pledges, alongside the existing giving, budget, department and trust "
              "KPIs and charts.",
    "1.60.0": "The payables page now lets you edit and delete payables, accruals and prepayments "
              "(settled items stay locked for safety), and a payable/accrual can be settled by linking "
              "an expense you already entered by mistake — instead of creating a duplicate. Pledges can "
              "now also be deleted by a treasurer (matched gifts stay in the ledger, just unlinked).",
    "1.59.0": "Recurring expenses can now repeat monthly, quarterly or yearly (in addition to every "
              "Sabbath). And the in-app update check now also recognises version tags on GitHub, not "
              "only published Releases — so the “check for updates” page works for a tag-based "
              "workflow instead of always showing “none”.",
    "1.58.0": "Camp/fund budgets are now itemised: set named budget items (Accommodation, Catering, "
              "Pulpit, …) on a fund's Budget & goals page, and when you record an expense on that fund "
              "you can tag it to the specific item — so the page shows actual spend per item. The "
              "expense's own category is unchanged and still used for the overall categorisation. Also "
              "fixed outgoing email on port 465: it now uses implicit SSL (the cause of the connection "
              "time-out), with STARTTLS still used on port 587.",
    "1.57.0": "Settling a payable or accrual now opens the normal expense form pre-filled, so you can "
              "record how it was actually paid — payment method, claimant and any M-Pesa/bank charge — "
              "before it's posted and the obligation closed. New camp/fund budgets: open a fund's "
              "“Budget & goals” page to set a per-category budget (accommodation, catering, pulpit, …) "
              "and see budget-vs-actual for the year, plus a contribution goal that groups give towards "
              "and a yearly camp-meeting goal, each tracked against what's been collected.",
    "1.56.0": "The real-time bank feed page now shows the current cleared bank balance as a card and "
              "lets you expand each event's raw JSON. The audit log gains search, filters (record "
              "type, change, user, date range), pagination and a CSV download. And the executive "
              "overview drops the slow financial-health alert scan in favour of fast at-a-glance "
              "facts (top fund and spend category this month, givers, largest single gift).",
    "1.55.0": "Profile rights now work on the leader pages: a profile that grants “see full phone "
              "numbers” is honoured (previously leaders always saw masked numbers), and giver "
              "identities can be granted or withheld per profile. The Treasury Controls page loads "
              "much faster — the possible-duplicate scans now run only when you click “Run check”, "
              "and the duplicate-offering logic is smarter: distinct bank gifts that merely share a "
              "paybill reference are no longer flagged, while the same gift counted on both the bank "
              "and an envelope in one month (or an envelope re-typed in a Sabbath) is.",
    "1.54.0": "Telegram report answers can now include a clickable link straight to the report. Set "
              "your site address (e.g. https://kws.oriokie.com) under Settings → Telegram, and the "
              "bot's replies will link to the matching report; leave it blank and replies stay "
              "text-only as before.",
    "1.53.0": "The expense recategorise round-trip can now also switch an expense between capital "
              "and recurrent (new column in the download). The leader department view is simpler and "
              "shareable: the charts are gone, collection-only subgroups show just their name and "
              "total contribution with no expenses, and there's a JPEG download for the subgroups "
              "stamped with the date and time so you can see how current the figures are. The fund "
              "report lists sub-accounts busiest-first (by receipts) and offers the same JPEG download.",
    "1.52.0": "Split offerings are safer and the Telegram bot is smarter. Confirming auto-allocated "
              "imports no longer re-points a split offering's halves to the wrong account when "
              "“require confirmation” is on — the component funds are locked. The review queue's "
              "manual Split can now target a split fund, which sub-divides that part across its "
              "components. On Telegram, /balance with no fund lists every fund's balance with a total, "
              "and when the assistant LLM is enabled the bot uses it to work out which report you're "
              "asking for from plain language.",
    "1.51.0": "The cash entry form now asks for the development group whenever a development fund "
              "is chosen, and won't save without it. Petty cash disbursements now mirror the expense "
              "form: choose how it was paid (cash, bank, M-Pesa or cheque) and add any M-Pesa/bank "
              "transaction charge — useful when the float is held on M-Pesa or a bank account — which "
              "is recorded as a linked charge and also reduces the float. Expenses can be flagged "
              "“paid from petty cash” on both the expense form and the import (new column), so the "
              "float stays accurate.",
    "1.50.0": "Fixes: the development-groups “unassigned” page no longer errors when a contribution "
              "has no linked member; bank-statement import now reads the real M-Pesa receipt code "
              "from mobile/bank-channel narrations, so two genuinely different payments are never "
              "dropped as duplicates; notifications now disappear from the list once read (with a "
              "per-item Dismiss); and the transactions Excel/CSV export gains a “Receipt status” "
              "column (receipted by envelope, manual receipt, memo, or not receipted) to aid "
              "reconciliation.",
    "1.49.0": "Fixed a split-fund allocation bug: when a reference (e.g. Combined Offering) had both "
              "a split-fund rule and an older single-account rule, the gift could be sent wholly to "
              "the wrong account (e.g. 13th Sabbath Offering). A deliberately-configured split fund "
              "now always wins. Expenses can also carry an M-Pesa/bank charge on import (new column "
              "on the template), just like the manual form — the charge is recorded as its own "
              "bank-charge expense and linked back to the expense that incurred it.",
    "1.48.0": "New “Run rules on pending” button on the review queue: after you add allocation "
              "rules following an import, click it to apply the current rules to the items still in "
              "the queue — anything that now matches is allocated automatically, the rest stay for "
              "review. Locked periods are skipped and split-fund matches are left for manual handling.",
    "1.47.0": "Assistants can now record offering envelopes from Telegram: send /envelope and the "
              "bot walks through the Sabbath, the member, and the amount for each fund, then saves "
              "it straight into the books (it appears in reports and reconciliation like any other "
              "envelope). Everything is configurable on Settings → Telegram: turn envelope entry on/"
              "off, choose which funds are offered, set cash vs bank, require a confirmation step, "
              "and decide whether new members may be created from Telegram. Locked periods are "
              "respected and every entry is attributed to the signed-in user.",
    "1.46.0": "Major speed-ups on the Executive and Controls pages — both were doing thousands of "
              "tiny database lookups on large datasets and are now 15–60x faster (Controls from "
              "~4.8s to under 0.1s, Executive from ~5.1s to ~0.3s). Added optional short-lived "
              "caching of the heavy dashboard figures (set DJANGO_DASH_CACHE_TTL in production; any "
              "change to giving or expenses refreshes it instantly) and a set of automatic checks "
              "that fail the build if a page ever starts over-querying the database again.",
    "1.45.0": "Performance at scale: the expenses list no longer runs a separate query per row to "
              "check for receipts (it now resolves in one query — about 4x fewer database hits on a "
              "full page), and the member list gained an index on the name so it stays fast when "
              "sorted across tens of thousands of members. Verified the other high-traffic pages "
              "(transactions, dashboard, reports) stay query-light on an 18,000-transaction dataset.",
    "1.44.2": "Error monitoring & alerts: server errors are now written to a rotating log file and "
              "(when an admin email and SMTP are configured) emailed to administrators, with "
              "optional Sentry integration. Email settings are configurable via environment "
              "variables, which also enables the off-site backup emailer.",
    "1.44.1": "Maintenance & hardening from a full audit: dashboard chart data is now escaped so a "
              "fund or member name can never inject markup; the background poller no longer touches "
              "the database during management checks; a redundant query in the trust report was "
              "removed; and a couple of date-sensitive tests were made deterministic.",
    "1.44.0": "New Profiles & rights system: create fully-configurable profiles (any combination of "
              "rights — recording, approvals, remittance, setup, reports, and sensitive data such as "
              "seeing member phone numbers in full vs masked) and assign them to users. It layers on "
              "top of the existing Treasurer/Assistant/Auditor/Leader roles — users without a profile "
              "keep their current access unchanged, so nothing breaks. Member phone numbers are now "
              "masked for anyone whose profile doesn't grant the right to see them in full.",
    "1.43.0": "Building an asset's cost from capital expenses is now safe to repeat — each expense "
              "is linked to the asset and skipped on the next run, and the asset page lists exactly "
              "which expenses make up the cost. Reclassifying a linked expense to recurrent (or "
              "deleting it) reduces the asset's cost automatically. The legacy importer now creates "
              "a “Church building” asset and capitalises all development/construction expenses onto it.",
    "1.42.0": "Trust funds now separate receipted from unreceipted money: only RECEIPTED trust "
              "money (a receipt issued — envelope or manual) is shown as outstanding to remit, "
              "and trust money received but not yet receipted appears on its own “unreceipted "
              "(pending receipting)” liability line across the Trust report, Remittance advice, "
              "Conference submission and the remittance dashboard. New “Construction in progress” "
              "asset category that doesn't depreciate, with a tool to accumulate its cost from "
              "capital expenses (any fund, any date range incl. prior years) or set it manually. "
              "Fixed the envelope-ledger name autocomplete (the scroll container was hiding it). "
              "Budget breakdown lines can record the quarter a fund expects to spend them.",
    "1.41.0": "Budget breakdown lines can now carry the quarter a fund expects to spend them "
              "(Q1–Q4, or blank for spread across the year), shown on the budget page for "
              "planning insight. Statement import already updates a known member's phone when "
              "they had none, and the envelope ledger already autocompletes names from the "
              "members list.",
    "1.40.0": "Fund ledger sub-accounts can now be downloaded to Excel/CSV. The fund structure "
              "import gained a \"Show in expenses\" column. Transaction-charge expenses now name "
              "the parent expense they belong to. The Offering/Collection summary scales to fit a "
              "single A4 landscape page when printed. And the backup workbook now records, for "
              "audit only, who created each row (Transactions, Expenses, Members, Departments, "
              "Reconciliations) — never shown in the app or on-screen reports.",
    "1.39.0": "New Collections Detail report: pick any period and see collections broken down by "
              "fund, with trust/local subtotals and a grand total that reconciles exactly to the "
              "Collections Summary for the same dates. Downloads to Excel and CSV. Linked from the "
              "reports menu and the summary page.",
    "1.38.1": "Fixed campaign fallback allocation: a matched giver's contribution now splits to "
              "their own subgroup fund (e.g. CAMP_1) instead of landing on the campaign's parent "
              "fund. The subgroup fund is created on demand under the parent and inherits its "
              "trust/local type so it still rolls up correctly. Givers with no group fall back to "
              "the parent fund as before.",
    "1.38.0": "Campaigns page redesigned with a cleaner form and a downloadable sample upload "
              "file; member uploads now skip unreadable rows (and never crash on an over-long "
              "or malformed phone) and report how many loaded. Bulk action buttons on the "
              "Expenses and Transactions lists now sit alongside Apply filters and light up only "
              "for actions the ticked rows actually qualify for.",
    "1.37.0": "The transactions list now has the same row checkboxes and select-all as expenses, "
              "with a single “Reverse selected” bar — tick several entries and reverse them at "
              "once (each gets a contra posting and any linked envelope receipt is removed). "
              "Already-reversed rows and locked periods are skipped. Edit, Split and Receipt "
              "stay per row.",
    "1.36.0": "Expenses now have row checkboxes and a single action bar — tick several and "
              "Approve, Reject, Mark paid or Delete them at once, with Edit still per row. The "
              "fund-ledger export gained ID and Type columns identifying each item, and the "
              "backup workbook now carries the database ID on the Transactions, Expenses and "
              "Reconciliations sheets.",
    "1.35.0": "New Campaigns table for appeals like Camp Meeting. After the normal allocation "
              "rules miss, a contribution whose reference contains one of the campaign's trigger "
              "words (e.g. expense, campexpense) is matched to a member by phone or a unique "
              "name and allocated to the campaign's fund, tagged with that member's group. Upload "
              "the Name/Mobile/Group sheet per campaign, and delete the whole campaign when the "
              "appeal ends (past allocations keep their group tag). Works for any fund's "
              "subgroups, on both the file import and the live bank feed.",
    "1.34.3": "The bank real-time feed (CBS webhook) now accepts the bank's token however it is "
              "sent — a bare Authorization header, a Bearer/Token scheme, or X-Auth-Token / "
              "X-Api-Key — and compares it securely. Incoming credits are allocated to funds "
              "with the same allocation rules as the statement import, so the live feed and file "
              "import never diverge.",
    "1.34.2": "Fixed inflated collections under the new model. Marking a bank credit as "
              "receipted — whether from the bulk mark-processed tool, the per-credit action, or "
              "a paper receipt — now turns it into a memo (excluded from income and detached "
              "from its fund), so it no longer double-counts against the envelope that already "
              "recorded the gift. Re-run the bulk mark-processed file once after updating to "
              "settle credits that were marked before this fix.",
    "1.34.1": "Aligned the cash-count sheet and the rest of the reports with the new model. "
              "The cash count no longer treats a bank envelope as physical cash, and the income "
              "reports (by channel, by group, tithe, offering, development) all leave out the "
              "receipted bank-credit memos, so every figure — dashboard included — counts each "
              "gift exactly once.",
    "1.34.0": "Adopted the legacy accounting model. A bank envelope now posts income just like "
              "a cash envelope (the envelope is the record of the gift), and when its matching "
              "bank-statement credit is receipted during Sabbath reconciliation that credit is "
              "turned into a memo — excluded from income and detached from its fund — so the gift "
              "is counted exactly once, on the envelope side. The Sabbath reconciliation now "
              "exists to find bank credits still counted as income that have actually been "
              "receipted, so you can clear the double-count.",
    "1.33.0": "From the Sabbath reconciliation page you can now change a record's status "
              "directly: mark an unreceipted bank credit as receipted (a confirmation only — one "
              "credit can cover several envelopes, so no link is required), and move an envelope "
              "that was entered as cash over to bank (removing its duplicate cash entry so the "
              "gift is not counted twice). Matches where a bank credit is still unreceipted but "
              "the gift was typed as cash are flagged as the overstating case.",
    "1.32.1": "trust_reconcile is more accurate: an envelope line that is excluded from income "
              "but whose envelope is linked to a bank credit is no longer counted as missing from "
              "collections (the bank credit is already there).",
    "1.32.0": "Sabbath reconciliation now also pairs gifts that share a first name and amount "
              "when there is only one such person that Sabbath (e.g. 'ADAM KEN' and 'ADAM NYAN'), "
              "and confirming a match marks the bank credit as receipted without changing the "
              "ledger (the credit stays as income; hand-typed bank envelopes remain the offering "
              "record).",
    "1.31.0": "Smarter Sabbath reconciliation. It now auto-matches a bank credit to an "
              "envelope only when the name and amount agree and there is no duplicate (so it "
              "never mis-pairs two givers of the same amount), and it suggests any gift that is "
              "the only one of its amount that Sabbath even when the names don't line up. It "
              "stays a detector — surfacing bank credits and envelopes that haven't been "
              "captured — and never creates a second ledger entry.",
    "1.30.1": "trust_reconcile now recognises bank envelopes that are linked to their "
              "imported bank credit (env.bank_transaction), so it no longer reports them as "
              "orphan lines — giving an accurate view of what genuinely still needs "
              "reconciling.",
    "1.30.0": "The Purge button for a statement import now stays available for a week "
              "after upload (it used to vanish the next day), so a recent import can still "
              "be undone — useful when an import duplicated giving that was already "
              "recorded.",
    "1.29.0": "New: Undo envelope entries. From the envelope list, treasurers can pick a "
              "Sabbath (and optionally cash/bank), preview the envelopes, and reverse the whole "
              "batch at once — handy for entries typed before a bank statement was imported. "
              "Cash ledger entries are removed; real bank deposits are kept and returned to the "
              "receipt queue so they can be receipted correctly. Locked periods are protected.",
    "1.28.1": "Reverted the change that made bank envelopes create their own ledger "
              "entry — the bank statement import remains the single source of that money. "
              "The trust_reconcile diagnostic stays so you can see exactly how the offering "
              "summary and collections summary line up for any month.",
    "1.27.1": "New diagnostic: run 'python manage.py trust_reconcile YEAR MONTH' to see "
              "exactly why the Offering Summary trust total and the Collections Summary trust "
              "total differ for a month — orphan envelope lines, excluded transactions, or a "
              "Sabbath/month-boundary timing difference — each with an amount.",
    "1.27.0": "Reconciliation & allocation improvements: delete a bank reconciliation "
              "within a week of creating it; a reconciliation can now recompute its "
              "cash-book balance from the ledger in one click (and confirms when saved); "
              "and allocation rules can no longer be pointed at the internal half of a "
              "split offering — pick the split fund itself so split giving divides "
              "correctly.",
    "1.26.0": "Trust fund fix: the whole app now classifies a fund as trust from "
              "one authoritative setting (its Fund Type), so the reports, the general "
              "ledger and the reconciliation always agree. A new audit_funds check finds "
              "any fund whose Fund Type disagrees with the envelope summary and fixes it "
              "in the direction you confirm. Rebuild the ledger after repairing.",
    "1.25.2": "Two fixes: the database backup now connects the same reliable way "
              "the app does (fixing the mysqldump access-denied error) and uses the "
              "current MariaDB tool name; and the Ledger date filter now reads dates "
              "more robustly so From/To always apply and a stray value can never break "
              "the page.",
    "1.25.1": "The Ledger check page now offers a one-click Rebuild when any fund "
              "does not tie to the general ledger — for example a contribution that "
              "was counted by the fund but not yet posted. Treasurers see the fix "
              "button; others see a note to ask a treasurer.",
    "1.25.0": "Three fixes: the envelope/offering summary now lists funds that were "
              "given to directly even when they have sub-accounts (e.g. VBS), so the "
              "totals match the envelopes counted; the Ledger search box now also finds "
              "entries by amount and by M-Pesa code; and the assistant chatbot now "
              "reports recognised income only (no double-counted rows) and can answer "
              "What is new.",
    "1.24.0": "Wording: everywhere the app used to say gift or gifts it now says "
              "contribution or contributions — on the dashboard, the review queue, "
              "receipts, leader views, reports and spreadsheet exports — for language "
              "that fits the church better. No figures or behaviour changed.",
    "1.23.0": "The dashboard now opens with a Latest Sabbath snapshot — what came in "
              "on the most recent Sabbath, how it compares with the week before, the "
              "number of contributions and envelopes, and the top funds — so you can see the "
              "week at a glance before drilling in.",
    "1.22.0": "Faster data entry. In the weekly envelope grid you can now move between "
              "rows with the Up/Down arrow keys (as well as Enter), and the grid is "
              "easier to use on a phone or tablet — bigger tap targets, smooth scrolling "
              "and full-width buttons. On the cash and expense forms the member, fund "
              "and claimant lookups are now fully keyboard-driven: arrow to highlight, "
              "Enter to pick, Escape to close.",
    "1.21.0": "Reports now print properly. Use the Print button (or Ctrl/Cmd-P) on any "
              "report and you get a clean document — a church letterhead with the report "
              "title, period and date, no on-screen menus or buttons, table headers "
              "repeated on every page, and signature lines on the monthly, remittance "
              "and board reports. Long fund ledgers now print in full.",
    "1.20.0": "Final consistency polish: the design-system utility classes now cover "
              "the import wizards, executive summary, controls and remaining tools, so "
              "the whole interface shares one spacing, type and layout language. The "
              "only styling left inline is dynamic (e.g. progress widths) or stateful.",
    "1.19.0": "Consistency rollout: the shared design-system classes now extend "
              "across the secondary screens too (settings, fund and member pages, "
              "imports, remittance and ledger tools), so spacing, type and layout are "
              "uniform throughout. Behaviour is unchanged.",
    "1.18.0": "UI modernization sweep complete: the Fund Ledger, Journal, Bank "
              "Reconciliation and Contributions/Receipts screens now use the same "
              "component library as the rest of the app — consistent headers, stat "
              "tiles, sticky table headers and spacing. All ten priority screens are "
              "now free of ad-hoc styling, with accounting logic unchanged.",
    "1.17.0": "UI modernization sweep (part 1): the Dashboard, Transactions, "
              "Expenses list, Expense detail (now with inline approve/pay actions), "
              "Pledges and Reports screens were rebuilt on a shared component library "
              "— responsive KPI tiles, consistent alerts, filters and toolbars, and "
              "sticky table headers on long ledgers. Accounting logic is unchanged.",
    "1.16.0": "Polish + hardening pass: the app now refuses to start in production "
              "on an insecure secret key, and warns about other risky settings. The "
              "error pages (403/404/500) and sign-in share one cleaner, branded look. "
              "Added reusable page-header, stat-card and empty-state building blocks "
              "and shared spacing/layout utilities, plus a safeguard so wide tables "
              "scroll instead of overflowing on phones.",
    "1.15.0": "Two-factor sign-in can now send your code by SMS or email, not just "
              "an authenticator app. Choose your method when setting up two-factor; "
              "at sign-in we send a 6-digit code (valid 5 minutes) and you can resend "
              "it. SMS uses the church's Advanta account; email uses the mail server. "
              "Recovery codes still work for every method.",
    "1.14.0": "Group leaders: assigning the parent fund (e.g. CAMP MEETING) now "
              "gives the leader its whole tree — every subgroup (CAMP_1..CAMP_30) "
              "with drill-down to each. A leader can also be assigned a single "
              "subgroup. Performance: the dashboard, leader pages, and fund ledger "
              "no longer slow down as the number of groups/sub-accounts grows.",
    "1.13.1": "Fix: the rule that stops the same contribution being matched to a "
              "pledge twice is now enforced on the production database too (the "
              "previous form was silently ignored by MariaDB). Adding numbered fund "
              "families for any department remains a Settings change, no code needed.",
    "1.13.0": "New: numbered fund families. Set one line in Settings — for "
              "example 'expense, exp, expe = CAMP_{n}' — and giving narrated "
              "EXPENSE1, exp1 or expe1 is sent to the fund CAMP_1, EXPENSE30 to "
              "CAMP_30, and so on for every group at once. No more a rule per group, "
              "and EXPENSE1 is correctly kept apart from EXPENSE10.",
    "1.12.0": "Leader dashboard: development-group figures now follow the period "
              "you pick, and you can download a group summary for that period. The "
              "multi-year trend now compares January-to-the-current-month of each "
              "year, so a year in progress is judged fairly. The cash page can now "
              "delete an entry (it is the same record as its ledger row); edits "
              "still happen at the ledger.",
    "1.11.0": "Department leaders get a redesigned dashboard for the area they "
              "lead: headline figures (balance, collections, expenses, net), a "
              "collections-vs-expenses chart, how giving arrives, top contributors, "
              "budget and pledge progress, and development-group standings. Each "
              "area — collections, expenses, pledges — now has its own focused, "
              "downloadable page. Everything stays read-only and scoped to the "
              "leader's own departments.",
    "1.10.2": "The two-factor code page is now a fully self-contained page, so it "
              "always renders — including when an existing session needs "
              "re-verifying, which previously showed a blank screen.",
    "1.10.1": "Fixes two-factor sign-in: the code-entry page rendered blank "
              "(it runs before you are logged in, so it now uses the sign-in "
              "layout), and the enrolment QR code now displays without needing "
              "an image library on the server. A recovery code still works in the "
              "same box.",
    "1.10.0": "Two new Excel importers — allocation rules and expenses — each "
              "with a template and review step. Allocation rules gain a regex match "
              "type so one rule covers narration variations (EXPENSE_1 / exp1 / "
              "expe1). Split funds are now selectable when bulk-allocating, and the "
              "Sabbath reconciliation regroups split parts and shows each envelope's "
              "fund allocation, with one-click apply. The envelope import asks what "
              "to do with unrecognised fund columns instead of dropping them. Trust "
              "outstanding remittance is now a true running balance, loose cash keeps "
              "the Sabbath you date it to, and a reset_2fa command recovers locked-out "
              "sign-ins.",
    "1.9.0": "The Sabbath reconciliation can now apply a match in one click — "
             "selected pairs are marked as bank giving and the duplicate cash "
             "entry is removed, so the contribution is counted once. Statement import can "
             "force the Sabbath every entry counts under, for imports done after "
             "the day. The dashboard swaps 'Giving by group' for a 'How giving "
             "arrives' card (bank / cash / envelope mix) and the local-funds table "
             "gets a one-click JPEG download. Pledges can be imported straight into "
             "a campaign from its page.",
    "1.8.0": "Per-Sabbath reconciliation matches bank giving (receipted and "
             "manually receipted) against the envelopes counted for that Sabbath, "
             "with fuzzy name matching for misspelt names and a balance check. "
             "Leaders get detailed, downloadable collections and expense pages plus "
             "a development-group drill-down. Two-factor sign-in no longer errors "
             "when a saved authenticator secret can't be read, and a recovery code "
             "now works in the same box. 'Receipt bank giving' can be limited to a "
             "single Sabbath.",
    "1.7.0": "A batch of fixes and queue improvements: bulk-allocate contributions in the "
             "review queue and fetch unallocated contributions from the ledger; trust "
             "'to remit' now counts trust funds only; the expense form no longer "
             "silently drops an entry that exceeds a fund's balance; M-Pesa charges "
             "are kept out of duplicate detection; possible duplicates are sorted by "
             "payer with fuzzy near-match for misspelt names; the rules list is "
             "paginated; friendly error pages with admin alerts; and the Sabbath cash "
             "count now reflects physical cash only, excluding bank giving keyed on "
             "the cash sheet.",
    "1.6.0": "Manual receipts are now a distinct state from system receipts. "
             "Marking bank entries as a manual receipt (for contributions already "
             "receipted on paper) keeps them out of both the review queue and the "
             "receipt-bank-giving pull, and never creates a system envelope — so "
             "they can't be receipted twice. It is reversible (untick to issue a "
             "system receipt later), shown with its own label on the ledger, and "
             "applies across every part of a split contribution.",
    "1.5.1": "Receipt bank giving no longer re-receipts a contribution that is already "
             "accounted for. Items that already carry an envelope record are now "
             "excluded from the bank-giving pull even if their processed flag had "
             "drifted, and the same guard applies when receipting one part of a "
             "split contribution — so nothing is receipted twice.",
    "1.5.0": "A dedicated fund and sub-account importer: download a template "
             "(which lists your existing funds), add funds and their sub-accounts, "
             "and import the whole chart of accounts at once. The budget template "
             "now comes pre-filled with a row for every existing fund. And marking "
             "a bank entry processed via envelope now also removes it (and every "
             "part of a split contribution) from the review queue.",
    "1.4.2": "The bulk mark-processed tool now handles split offerings. A split "
             "contribution (e.g. Combined Offering) appears in the ledger as several rows "
             "with the amount divided; enter the total the member gave and every "
             "part of that contribution is marked processed together.",
    "1.4.1": "Three fixes: SMS settings no longer repeat on every settings tab; "
             "the bulk fund/department import is now linked from the Funds & "
             "departments page; and a new bulk tool marks bank entries as processed "
             "via a hand-written envelope (upload reference + amount) so they are "
             "kept out of receipting without being entered twice.",
    "1.4.0": "Department leader logins: a new read-only role that sees only its "
             "own department(s) — collections, expenses, sub-accounts, development "
             "groups and pledges — with contact numbers masked for privacy. Plus "
             "configurable encryption: choose the key source, turn it on or off, and "
             "rotate the key with a built-in re-encrypt command.",
    "1.3.0": "Security and oversight release: automated, encrypted, off-site "
             "nightly database backups (a cron-run command with rotation and "
             "email); two-factor authentication for logins (authenticator app + "
             "recovery codes, optionally required for treasurers); and a revamped "
             "dashboard with a single Needs Attention panel surfacing allocations, "
             "pending approvals, pledge drafts, and overdue remittances at a glance.",
    "1.2.1": "Treasurer-only bulk pledge importer: download a template, upload "
             "completed pledge cards, and the wizard matches members and campaigns "
             "(prompting you to map or create any that do not match). Imported "
             "pledges land as drafts for approval and never affect fund balances.",
    "1.2.0": "Pledge matching now works inline: when a contribution arrives from a "
             "member with an active pledge, the system can flag it for review or "
             "apply it automatically (configurable in Settings to Pledges). New match "
             "suggestions review queue. Plus an optional public link members can use "
             "to submit a pledge themselves — submissions are held as unverified "
             "drafts for treasurer approval and never touch fund balances.",
    "1.1.0": "New Pledge Management module: pledge campaigns, member pledges with "
             "installment schedules, approval workflow, automatic and manual matching "
             "of real contributions to pledges, SMS/WhatsApp reminders, progress "
             "reports and year-end member statements. Pledges are informational only "
             "and never affect fund balances — money is recognised solely through "
             "normal contributions, which are then matched to pledges.",
    "1.0.19": "Downloadable budget template with per-department line items and a "
              "funding source; tighter duplicate detection (same-Sabbath expenses, "
              "same-channel offerings, envelope duplicates); remittance deadlines "
              "default to the 1st of the next month and auto-mark remitted from "
              "completed batches.",
    "1.0.18": "Names are kept in uppercase everywhere; expenses gained a search box "
              "and a category-only bulk re-import; the trust remittance dashboard now "
              "counts down to the reporting Sabbath using freely-set monthly deadlines; "
              "and a new bulk fund and budget import reads a budget workbook, matching "
              "each fund to a department and prompting you to map or create the rest.",
    "1.0.17": "Compacted the Ledger so all columns and action buttons stay visible "
              "(wide tables now scroll instead of clipping); added the Remittance "
              "calendar to the Reports nav; fixed the settings Restore card showing on "
              "every tab and tidied the settings tabs into one row.",
    "1.0.16": "Redesigned the Ledger, Envelopes and Expenses screens around a "
              "unified, enterprise-grade workspace layout — a calm summary strip, a "
              "contained command toolbar, refined tables and clearer empty states — "
              "while keeping the church's warm forest-and-brass identity.",
    "1.0.15": "Extended the financial-accuracy suite with 15 edge-case tests "
              "(date boundaries, unconfirmed/pending entries, split offerings, "
              "rounding, mis-keyed dates) — 44 accuracy tests in all.",
    "1.0.14": "Added a financial-accuracy test suite (29 tests) asserting the core "
              "accounting invariants: departmental balances, reconciliation, the "
              "balance sheet, and the statement of cash flows all tie out.",
    "1.0.13": "Added an interactive deployment installer (deploy/install.sh) that "
              "collects settings with validation and sets up the database, Python "
              "environment, gunicorn, Apache, nginx and SSL.",
    "1.0.12": "Redesigned transactions page with summary cards; fixed trust "
              "remittances being counted as expenses in prior-year totals; new "
              "remittance calendar with reporting-Sabbath logic and dashboard alerts; "
              "mark-as-receipted without raising an envelope; optional starting "
              "receipt number for bulk receipting; reorganised settings page.",
    "1.0.11": "Redesigned the envelope entry screen with live running totals, a "
              "per-fund summary bar, column totals, and duplicate-name flags for "
              "faster, clearer Sabbath entry.",
    "1.0.10": "Per-Sabbath Excel: receipt numbers show without the month/sabbath "
              "prefix; Combined Offering & Thanksgiving appear as one block per "
              "contributor but split into trust/local in the summary, which now has "
              "cell borders.",
    "1.0.9": "New bank-position report: compares the system bank balance to the "
             "statement closing balance to catch entries that never made it into "
             "the app.",
    "1.0.8": "Receipt a single bank/M-Pesa contribution as an envelope on demand, with an "
             "optional hand-written receipt number — without double-counting.",
    "1.0.7": "Reconciliation variance finder now detects re-allocated, edited, "
             "excluded and reversed entries (not just unposted ones) and offers a "
             "one-click ledger rebuild to fix them.",
    "1.0.6": "Richer transaction export (M-Pesa ref etc); SMS/WhatsApp icons gated "
             "on settings; bordered printable Sabbath sheet; reconciliation variance "
             "finder; mobile layout improvements.",
    "1.0.5": "Fixed a 500 error on the budget breakdown page when the Local "
             "Church Budget fund was matched by name.",
    "1.0.4": "Update checker supports private repos via GITHUB_TOKEN, and now "
             "re-checks every 10 min instead of caching once per process.",
    "1.0.3": "Dedup also catches repeated M-Pesa receipts; cleanup command for "
             "existing duplicates; statement purge can unlink linked expenses.",
    "1.0.2": "Dedup keys normalised to uppercase so duplicate detection is exact "
             "regardless of database collation (fixes latin1 case-folding).",
    "1.0.1": "In-app updates verified; engine-aware backups; auto .env loading; "
             "production hardening (WhiteNoise, health check, gunicorn).",
    "1.0.0": "Initial release.",
}


def whats_new():
    return WHATS_NEW.get(get_version(), "")

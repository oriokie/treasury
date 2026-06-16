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

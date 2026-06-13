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
    "1.0.8": "Receipt a single bank/M-Pesa gift as an envelope on demand, with an "
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

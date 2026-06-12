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

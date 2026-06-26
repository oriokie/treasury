"""Shared helpers: money formatting, Sabbath-week computation, period parsing."""
import datetime as dt
from decimal import Decimal


def sabbath_of(date: dt.date) -> dt.date:
    """The Sabbath (Saturday) an entry belongs to: the Saturday of its week.
    Sunday–Friday roll forward to the coming Saturday; a Saturday is itself.
    This is the single canonical rule used for envelopes, offerings and reports."""
    return date + dt.timedelta(days=(5 - date.weekday()) % 7)


def saturdays_of_month(year: int, month: int):
    """All Saturdays falling within a given month, in order."""
    d = dt.date(year, month, 1)
    d += dt.timedelta(days=(5 - d.weekday()) % 7)
    out = []
    while d.month == month:
        out.append(d)
        d += dt.timedelta(days=7)
    return out


def last_saturday(today=None) -> dt.date:
    """The most recent Saturday on or before today."""
    today = today or dt.date.today()
    return today - dt.timedelta(days=(today.weekday() - 5) % 7)


def sabbath_week_of(date: dt.date) -> int:
    """Ordinal (1..5) of the entry's Sabbath within that Sabbath's month.

    Derived from the canonical Sabbath (sabbath_of) so the stored sabbath_week
    always agrees with the Saturday-bucketed envelope and offering reports."""
    sab = sabbath_of(date)
    sats = saturdays_of_month(sab.year, sab.month)
    return (sats.index(sab) + 1) if sab in sats else (((sab.day - 1) // 7) + 1)


# Backwards-compatible alias used around the codebase.
sabbath_bucket = sabbath_of


def parse_period(request):
    """Read ?start=&end= (or ?year=&month=) query params into a (start, end) date pair.

    Defaults to the current month if nothing is supplied.
    """
    today = dt.date.today()
    preset = request.GET.get("period")
    if preset:
        if preset == "month":
            return today.replace(day=1), today
        if preset == "quarter":
            q_start_month = 3 * ((today.month - 1) // 3) + 1
            return dt.date(today.year, q_start_month, 1), today
        if preset == "year":
            return dt.date(today.year, 1, 1), today
        if preset == "all":
            return dt.date(2000, 1, 1), today
    start_raw = request.GET.get("start")
    end_raw = request.GET.get("end")
    year = request.GET.get("year")
    month = request.GET.get("month")

    def _d(s):
        try:
            return dt.date.fromisoformat(s)
        except (TypeError, ValueError):
            return None

    if start_raw or end_raw:
        start = _d(start_raw) or today.replace(day=1)
        end = _d(end_raw) or today
        return start, end

    if year:
        y = int(year)
        if month:
            m = int(month)
            start = dt.date(y, m, 1)
            end = (dt.date(y + (m == 12), (m % 12) + 1, 1) - dt.timedelta(days=1))
        else:
            start, end = dt.date(y, 1, 1), dt.date(y, 12, 31)
        return start, end

    return today.replace(day=1), today


def money(value) -> str:
    value = Decimal(value or 0)
    return f"{value:,.2f}"


def safe_json(obj):
    """json.dumps hardened for embedding inside a <script> block via |safe.

    Escapes the characters that could otherwise break out of the script element
    or start an HTML comment, so user-controlled strings (fund or member names)
    embedded in chart data can't inject markup. Pair with |safe in the template.
    """
    import json
    return (json.dumps(obj)
            .replace("<", "\\u003c")
            .replace(">", "\\u003e")
            .replace("&", "\\u0026")
            .replace("\u2028", "\\u2028")
            .replace("\u2029", "\\u2029"))


def block_if_locked(request, d):
    """Return True (and flash an error) if date d is in a locked accounting period.

    No one — including superusers — may post into a locked period; it must be
    unlocked first via Controls. Single source of truth for the period-lock guard
    used across the giving and cashbook views."""
    from core.models import period_locked
    from django.contrib import messages as _m
    lock = period_locked(d)
    if lock:
        _m.error(request, f"{lock} is locked. Unlock the period (Controls) before "
                          f"posting or editing entries in it.")
        return True
    return False


import logging as _logging
_tlog = _logging.getLogger("treasury")


def log_exception(context=""):
    """Log the exception currently being handled, with full traceback, to the
    'treasury' logger. Use inside a broad `except` that shows the user a generic
    message, so the server still records what actually went wrong."""
    _tlog.exception("Handled exception%s", f" in {context}" if context else "")

from django import template

register = template.Library()


# --- central money formatting -------------------------------------------------
# The single place money is turned into a display string. Replaces the
# `{{ x|floatformat:N|intcomma }}` pattern that was scattered across ~167
# templates. `money` is a byte-identical drop-in for that pattern (verified
# against Django's own floatformat+intcomma over a battery of values), so
# migrating a template changes nothing a user sees — it just centralises the
# rule so decimals, rounding, grouping and blank-handling live in one place.
#
# This is display only: it never computes a figure, so it does not touch the
# Financial Metrics Registry. Output is plain text (a real minus sign, real
# parentheses) so it survives the Word-HTML and PNG-table export paths, which
# read rendered text rather than re-running CSS.
import decimal as _decimal


def _to_decimal(value):
    """Coerce a template value to Decimal, or None if it isn't a number.
    Mirrors what floatformat accepts (Decimal, int, float, numeric str)."""
    if value is None or value == "":
        return None
    if isinstance(value, _decimal.Decimal):
        return value
    try:
        return _decimal.Decimal(str(value))
    except (_decimal.InvalidOperation, ValueError, TypeError):
        return None


def _grouped(dec, places):
    """Format a non-negative Decimal with thousands separators and exactly
    `places` decimals, rounding half-up — matching floatformat:N|intcomma."""
    q = _decimal.Decimal(1).scaleb(-places) if places else _decimal.Decimal(1)
    dec = dec.quantize(q, rounding=_decimal.ROUND_HALF_UP)
    sign, digits, exp = dec.as_tuple()
    s = f"{dec:.{places}f}"
    intpart, _, frac = s.partition(".")
    intpart = f"{int(intpart):,}"
    return f"{intpart}.{frac}" if places else intpart


@register.filter
def money(value, places=2):
    """Format money for display: thousands separators, fixed decimals, a real
    leading minus for negatives, and an em dash for blank/non-numeric values.

    `{{ x|money }}`  -> "1,234.50"   (2 dp, the default)
    `{{ x|money:0 }}` -> "1,235"      (0 dp)
    `{{ None|money }}` -> "—"

    Drop-in for `{{ x|floatformat:2|intcomma }}` and
    `{{ x|floatformat:0|intcomma }}`."""
    return _money(value, places, accounting=False)


@register.filter
def money_acct(value, places=2):
    """Accounting presentation: negatives in parentheses instead of a minus,
    e.g. -1234.5 -> "(1,234.50)". The parentheses are real characters (not a
    CSS effect), so they survive Word and PNG exports. Pair with the `.num`
    cell class — reports tint parenthesised figures via the existing
    `is_negative` styling. Blank/non-numeric -> em dash."""
    return _money(value, places, accounting=True)


def _money(value, places, accounting):
    try:
        places = int(places)
    except (TypeError, ValueError):
        places = 2
    dec = _to_decimal(value)
    if dec is None:
        return "—"
    body = _grouped(abs(dec), places)
    # a value that rounds to zero is neither negative nor parenthesised
    zero = _grouped(_decimal.Decimal(0), places)
    if dec < 0 and body != zero:
        return f"({body})" if accounting else f"-{body}"
    return body


@register.filter
def get_item(d, key):
    """Look up a dict value by variable key in templates."""
    try:
        return d.get(key)
    except AttributeError:
        return None


@register.filter
def pct_width(value):
    """Clamp a percentage to 0..100 for progress bars."""
    try:
        v = float(value)
    except (TypeError, ValueError):
        return 0
    return max(0, min(100, v))


@register.filter
def pct_of_12(value):
    """Turn a 12-column grid width into a flex-basis percentage (6 -> 50)."""
    try:
        w = int(value)
    except (TypeError, ValueError):
        return 100
    w = max(1, min(12, w))
    return round(w / 12 * 100)


@register.filter
def sentence_fund(name):
    """Display a fund/department/member name in a readable case instead of the
    ALL CAPS many were originally entered in. Short all-caps tokens (<=4
    letters — AMM, AWM, LCB, PF, SDA...) are kept as acronyms; longer words
    are turned into ordinary Sentence Case. Leaves already-mixed-case names
    untouched. Underscores become spaces (e.g. "AMM_CHOIR" -> "AMM Choir")."""
    if not name:
        return name
    s = str(name)
    if s != s.upper():
        return s   # already has lowercase somewhere -> leave as authored
    words = s.replace("_", " ").split()
    out = []
    for w in words:
        core = w.strip("()/.-")
        if core.isalpha() and len(core) <= 4:
            out.append(w)               # short acronym -> keep upper
        else:
            out.append(w.capitalize())
    return " ".join(out)


@register.filter
def abs_val(value):
    """Absolute value of a number, for rendering signed movement deltas where
    the sign/arrow is shown separately (e.g. '▲ 1,234' vs '▼ 1,234')."""
    try:
        return abs(float(value))
    except (TypeError, ValueError):
        return value


@register.filter
def is_negative(value):
    """True where a cell's value is a negative number; safely False for
    blanks, text and None — used by the report engine to tint negative
    figures without erroring on mixed-type cells."""
    try:
        return float(value) < 0
    except (TypeError, ValueError):
        return False

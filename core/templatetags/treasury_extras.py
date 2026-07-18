from django import template

register = template.Library()


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

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

from django import template
from core.rights import display_phone, has_right

register = template.Library()


@register.filter(name="phone_for")
def phone_for(value, user):
    """Render a phone number full or masked, per the viewer's rights."""
    return display_phone(user, value)


@register.simple_tag(name="user_can")
def user_can(user, key):
    return has_right(user, key)

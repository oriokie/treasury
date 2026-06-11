"""Role definitions and permission helpers.

Three roles, modelled as Django Groups so they map onto the admin and the
standard permission framework:

  TREASURER  - full access, including expense approval and remittance.
  ASSISTANT  - data entry, allocation, queue work, expense entry. No approval/remit.
  AUDITOR    - read-only: reports and the audit log.
"""

TREASURER = "Treasurer"
ASSISTANT = "Assistant"
AUDITOR = "Auditor"

ALL_ROLES = [TREASURER, ASSISTANT, AUDITOR]


def user_roles(user):
    if not user.is_authenticated:
        return set()
    if user.is_superuser:
        return set(ALL_ROLES)
    return set(user.groups.values_list("name", flat=True))


def is_treasurer(user):
    return user.is_superuser or TREASURER in user_roles(user)


def is_assistant(user):
    return user.is_superuser or ASSISTANT in user_roles(user)


def is_auditor(user):
    return AUDITOR in user_roles(user)


def can_enter_data(user):
    """Treasurers and assistants can create/allocate entries."""
    return is_treasurer(user) or is_assistant(user)


def can_approve(user):
    """Only treasurers approve expenses and post remittances."""
    return is_treasurer(user)


def role_label(user):
    """Human label for a user's primary role."""
    if user.is_superuser:
        return "Administrator"
    roles = user_roles(user)
    for r in (TREASURER, ASSISTANT, AUDITOR):
        if r in roles:
            return r
    return "—"

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
LEADER = "Leader"   # departmental leader: read-only, scoped to own department(s)

ALL_ROLES = [TREASURER, ASSISTANT, AUDITOR, LEADER]


def user_roles(user):
    if not user.is_authenticated:
        return set()
    if user.is_superuser:
        return set([TREASURER, ASSISTANT, AUDITOR])  # admin is staff, not a leader
    return set(user.groups.values_list("name", flat=True))


def is_treasurer(user):
    return user.is_superuser or TREASURER in user_roles(user)


def is_assistant(user):
    return user.is_superuser or ASSISTANT in user_roles(user)


def is_auditor(user):
    return AUDITOR in user_roles(user)


def is_leader(user):
    """A departmental leader (read-only, scoped to their own departments)."""
    return user.is_authenticated and not user.is_superuser \
        and LEADER in user_roles(user)


def is_staff_role(user):
    """Treasurer/Assistant/Auditor/admin — the church office roles that may use
    the full application. Explicitly excludes a pure departmental Leader."""
    if user.is_superuser:
        return True
    r = user_roles(user)
    return bool(r & {TREASURER, ASSISTANT, AUDITOR})


def can_enter_data(user):
    """Treasurers and assistants can create/allocate entries."""
    return is_treasurer(user) or is_assistant(user)


def can_approve(user):
    """Only treasurers approve expenses and post remittances."""
    return is_treasurer(user)


def can_allocate_dev_offering(user):
    """Treasurers, and anyone granted the development-offering right, may view and
    allocate unassigned development offering."""
    return is_treasurer(user) or has_right(user, RIGHT_DEV_OFFERING)


def can_manage_advances(user):
    """Treasurers and assistants manage advances by default; the right can also be
    granted to any other user (e.g. a leader who runs their own advances)."""
    return is_treasurer(user) or is_assistant(user) or has_right(user, RIGHT_ADVANCES)


def can_build_dev_groups(user):
    """Treasurers, and anyone granted the dev-group-builder right, may generate
    balanced development groups."""
    return is_treasurer(user) or has_right(user, RIGHT_DEV_GROUP_BUILDER)


def can_allocate_dev_offering(user):
    """Treasurers, and anyone granted the development-offering right, may view and
    allocate unassigned development offering."""
    from .rights import has_right
    return is_treasurer(user) or has_right(user, "allocate_dev_offering")


def can_manage_advances(user):
    """Treasurers and assistants manage advances by default; the right can also be
    granted to any other user (e.g. a leader who runs their own advances)."""
    from .rights import has_right
    return (is_treasurer(user) or is_assistant(user)
            or has_right(user, "manage_advances"))


def can_build_dev_groups(user):
    """Treasurers, and anyone granted the dev-group-builder right, may generate
    balanced development groups."""
    from .rights import has_right
    return is_treasurer(user) or has_right(user, "build_dev_groups")


def role_label(user):
    """Human label for a user's primary role."""
    if user.is_superuser:
        return "Administrator"
    roles = user_roles(user)
    for r in (TREASURER, ASSISTANT, AUDITOR, LEADER):
        if r in roles:
            return "Department leader" if r == LEADER else r
    return "—"
